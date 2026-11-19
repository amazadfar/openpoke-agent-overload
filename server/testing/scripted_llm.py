"""Scripted stand-in for OpenRouter (tests and the parity harness; never production).

Two installation modes:

- ``install()`` (default, also used on the *baseline* tree by the parity
  harness): monkeypatches ``request_chat_completion`` in the five caller
  modules. Emits an ``llm_call`` telemetry event itself when telemetry exists.
- ``install_transport()``: keeps the real ``request_chat_completion`` (retries,
  usage parsing, telemetry) and serves scripted responses through an
  ``httpx.MockTransport``. Used where the client's own behaviour matters (LLM
  retries under a rate-limit storm).

Role detection: the ``role=`` kwarg when present, otherwise system-prompt
markers (IA: "You are OpenPoke"; EA: "execution engine"; search: "Gmail search
assistant"; classifier: "review incoming Gmail messages"; summarizer: "memory
curator"). Execution calls are additionally keyed by agent name, parsed from
"Agent Name: ..." in the EA system prompt, so scripts can be per agent.

Self-contained (stdlib + httpx; optional server imports) so the parity harness
can load it by file path.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Union

CALLER_MODULES = (
    "server.agents.interaction_agent.runtime",
    "server.agents.execution_agent.runtime",
    "server.agents.execution_agent.tasks.search_email.tool",
    "server.services.gmail.importance_classifier",
    "server.services.conversation.summarization.summarizer",
)

_ROLE_MARKERS = (
    ("interaction", "You are OpenPoke"),
    ("execution", "execution engine"),
    ("search", "Gmail search assistant"),
    ("classifier", "review incoming Gmail messages"),
    ("summarizer", "memory curator"),
)
_AGENT_NAME_RE = re.compile(r"^Agent Name: (.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------

_call_counter = 0


def tool_call(name: str, **arguments: Any) -> Dict[str, Any]:
    global _call_counter
    _call_counter += 1
    return {
        "id": f"call_{_call_counter}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def respond(*tool_calls: Dict[str, Any], content: str = "", usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = list(tool_calls)
    payload: Dict[str, Any] = {"choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}]}
    if usage is not None:
        payload["usage"] = usage
    return payload


def final(text: str) -> Dict[str, Any]:
    return respond(content=text)


@dataclass
class HTTPFailure:
    """Transport-mode only: answer with an HTTP error instead of a completion."""

    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: Dict[str, Any] = field(default_factory=lambda: {"error": {"message": "scripted failure"}})


@dataclass
class Step:
    """A scripted response with its own latency (seconds)."""

    response: Any
    latency: Optional[float] = None


Scripted = Union[Dict[str, Any], Callable[["LLMCall"], Any], Step, HTTPFailure, BaseException]


@dataclass
class LLMCall:
    role: str
    agent: Optional[str]
    model: str
    system: Optional[str]
    messages: List[Dict[str, Any]]
    tools: Optional[List[Dict[str, Any]]]
    started: float = 0.0
    ended: float = 0.0
    role_from_kwarg: bool = False

    @property
    def tool_names(self) -> List[str]:
        return [t.get("function", {}).get("name", "") for t in (self.tools or [])]

    @property
    def last_content(self) -> str:
        for message in reversed(self.messages):
            content = message.get("content")
            if isinstance(content, str) and content:
                return content
        return ""

    @property
    def first_user_content(self) -> str:
        for message in self.messages:
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                return message["content"]
        return ""

    def tool_results(self) -> List[Dict[str, Any]]:
        out = []
        for message in self.messages:
            if message.get("role") == "tool":
                try:
                    out.append(json.loads(message.get("content") or "{}"))
                except json.JSONDecodeError:
                    out.append({"raw": message.get("content")})
        return out


def detect_role(system: Optional[str]) -> str:
    text = system or ""
    for role, marker in _ROLE_MARKERS:
        if marker in text:
            return role
    return "unknown"


def _default_response(call: LLMCall) -> Dict[str, Any]:
    if call.role == "interaction":
        return final("")
    if call.role == "execution":
        return final("Done.")
    if call.role == "search":
        return respond(tool_call("return_search_results", message_ids=[]))
    if call.role == "classifier":
        return respond(tool_call("mark_email_importance", important=False))
    if call.role == "summarizer":
        return final("Summary generated: n/a")
    return final("")


class ScriptedLLM:
    """Plays scripted responses per role (and per execution agent), with latency."""

    def __init__(self, *, latency: float = 0.0, max_concurrent: Optional[int] = None) -> None:
        self._queues: Dict[str, Deque[Scripted]] = {}
        self._handlers: Dict[str, Callable[[LLMCall], Any]] = {}
        self._latency: Dict[str, float] = {"*": latency}
        self.calls: List[LLMCall] = []
        self.max_concurrent = max_concurrent
        self.in_flight = 0
        self.peak_in_flight = 0
        self.rate_limited = 0
        self._patched: List[tuple] = []

    # ------------------------------------------------------------ scripting
    def script(self, key: str, responses: List[Scripted]) -> "ScriptedLLM":
        """Queue responses for ``interaction``, ``execution``, ``execution:<agent>``, ``search``, ..."""
        self._queues.setdefault(key, deque()).extend(responses)
        return self

    def on(self, key: str, handler: Callable[[LLMCall], Any]) -> "ScriptedLLM":
        """Dynamic responder used when the queue for ``key`` is empty."""
        self._handlers[key] = handler
        return self

    def set_latency(self, seconds: float, key: str = "*") -> "ScriptedLLM":
        self._latency[key] = seconds
        return self

    def calls_for(self, role: str, agent: Optional[str] = None) -> List[LLMCall]:
        return [c for c in self.calls if c.role == role and (agent is None or c.agent == agent)]

    def pending(self, key: str) -> int:
        return len(self._queues.get(key, ()))

    # ------------------------------------------------------------ dispatch
    def _keys(self, call: LLMCall) -> List[str]:
        keys = []
        if call.role == "execution" and call.agent:
            keys.append(f"execution:{call.agent}")
        keys.append(call.role)
        return keys

    def _next(self, call: LLMCall) -> Scripted:
        for key in self._keys(call):
            queue = self._queues.get(key)
            if queue:
                return queue.popleft()
        for key in self._keys(call):
            if key in self._handlers:
                return self._handlers[key]
        return _default_response

    def _latency_for(self, call: LLMCall, item: Scripted) -> float:
        if isinstance(item, Step) and item.latency is not None:
            return item.latency
        for key in self._keys(call):
            if key in self._latency:
                return self._latency[key]
        return self._latency.get("*", 0.0)

    async def _produce(self, call: LLMCall) -> Any:
        item = self._next(call)
        latency = self._latency_for(call, item)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if latency > 0:
                await asyncio.sleep(latency)
            if isinstance(item, Step):
                item = item.response
            if callable(item) and not isinstance(item, (dict, HTTPFailure, BaseException)):
                item = item(call)
                if asyncio.iscoroutine(item):
                    item = await item
            return item
        finally:
            self.in_flight -= 1
            call.ended = time.monotonic()

    def _new_call(self, *, model, messages, system, tools, role) -> LLMCall:
        detected = role if role and role != "unknown" else detect_role(system)
        agent = None
        if detected == "execution" and system:
            match = _AGENT_NAME_RE.search(system)
            agent = match.group(1).strip() if match else None
        call = LLMCall(
            role=detected,
            agent=agent,
            model=model,
            system=system,
            messages=json.loads(json.dumps(messages, default=str)),
            tools=tools,
            started=time.monotonic(),
            role_from_kwarg=bool(role and role != "unknown"),
        )
        self.calls.append(call)
        return call

    # ------------------------------------------------------- function mode
    async def __call__(
        self,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        api_key: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        base_url: Optional[str] = None,
        role: Optional[str] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        call = self._new_call(model=model, messages=messages, system=system, tools=tools, role=role)
        ok = False
        error_class = None
        try:
            item = await self._produce(call)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, HTTPFailure):
                raise _openrouter_error(f"OpenRouter request failed ({item.status}): scripted")
            ok = True
            return item
        except BaseException as exc:
            error_class = type(exc).__name__
            raise
        finally:
            _emit_llm_call(call, ok=ok, error_class=error_class)

    def install(self, monkeypatch: Any = None) -> "ScriptedLLM":
        import importlib

        for module_name in CALLER_MODULES:
            module = importlib.import_module(module_name)
            if monkeypatch is not None:
                monkeypatch.setattr(module, "request_chat_completion", self)
            else:
                self._patched.append((module, getattr(module, "request_chat_completion")))
                setattr(module, "request_chat_completion", self)
        return self

    def uninstall(self) -> None:
        for module, original in reversed(self._patched):
            setattr(module, "request_chat_completion", original)
        self._patched.clear()

    # ------------------------------------------------------ transport mode
    def install_transport(self, monkeypatch: Any) -> "ScriptedLLM":
        import httpx
        from server.openrouter_client import client as client_module  # type: ignore

        async def handler(request: "httpx.Request") -> "httpx.Response":
            payload = json.loads(request.content.decode("utf-8"))
            messages = payload.get("messages") or []
            system = None
            if messages and messages[0].get("role") == "system":
                system = messages[0].get("content")
                messages = messages[1:]
            call = self._new_call(
                model=payload.get("model", ""), messages=messages, system=system, tools=payload.get("tools"), role=None
            )
            if self.max_concurrent is not None and self.in_flight >= self.max_concurrent:
                self.rate_limited += 1
                call.ended = time.monotonic()
                return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"message": "rate limited"}})
            item = await self._produce(call)
            if isinstance(item, HTTPFailure):
                return httpx.Response(item.status, headers=item.headers, json=item.body)
            if isinstance(item, BaseException):
                raise item
            return httpx.Response(200, json=item)

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(client_module, "_http_client", lambda: httpx.AsyncClient(transport=transport))
        return self


def _openrouter_error(message: str) -> Exception:
    try:
        from server.openrouter_client import OpenRouterError  # type: ignore

        return OpenRouterError(message)
    except Exception:  # pragma: no cover - baseline tree has it too
        return RuntimeError(message)


def _emit_llm_call(call: LLMCall, *, ok: bool, error_class: Optional[str]) -> None:
    try:
        from server.services import telemetry  # type: ignore
    except Exception:
        return
    prompt_chars = len(call.system or "") + sum(
        len(m.get("content")) for m in call.messages if isinstance(m.get("content"), str)
    )
    telemetry.emit(
        "llm_call",
        role=call.role,
        model=call.model,
        attempt=1,
        retry_reason=None,
        http_status=200 if ok else None,
        ok=ok,
        error_class=error_class,
        latency_ms=round((time.monotonic() - call.started) * 1000, 1),
        prompt_chars=prompt_chars,
        tools_count=len(call.tools or []),
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        cost=None,
        source="scripted",
    )


__all__ = [
    "CALLER_MODULES",
    "HTTPFailure",
    "LLMCall",
    "ScriptedLLM",
    "Step",
    "detect_role",
    "final",
    "respond",
    "tool_call",
]
