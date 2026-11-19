from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Dict, List, Optional

import httpx

from .. import flags
from ..config import get_settings
from ..logging_config import logger
from ..overload_config import (
    LLM_MIN_ATTEMPT_TIMEOUT_S,
    LLM_MIN_CALL_BUDGET_S,
    LLM_REQUEST_TIMEOUT_S,
    LLM_RETRY_BASE_S,
    LLM_RETRY_MAX_ATTEMPTS,
    LLM_RETRY_MAX_DELAY_S,
)
from ..services import telemetry
from ..services.context import current_run
from ..utils import clock

OpenRouterBaseURL = "https://openrouter.ai/api/v1"
_REQUEST_TIMEOUT_S = 60.0  # existing value; mirrored as LLM_REQUEST_TIMEOUT_S in overload_config

_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cost")


class OpenRouterError(RuntimeError):
    """Raised when the OpenRouter API returns an error response."""


def _headers(*, api_key: Optional[str] = None) -> Dict[str, str]:
    settings = get_settings()
    key = (api_key or settings.openrouter_api_key or "").strip()
    if not key:
        raise OpenRouterError("Missing OpenRouter API key")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    return headers


def _build_messages(messages: List[Dict[str, str]], system: Optional[str]) -> List[Dict[str, str]]:
    if system:
        return [{"role": "system", "content": system}, *messages]
    return messages


def _handle_response_error(exc: httpx.HTTPStatusError) -> None:
    response = exc.response
    detail: str
    try:
        payload = response.json()
        detail = payload.get("error") or payload.get("message") or json.dumps(payload)
    except Exception:
        detail = response.text
    raise OpenRouterError(f"OpenRouter request failed ({response.status_code}): {detail}") from exc


def _http_client() -> httpx.AsyncClient:
    """HTTP client factory (tests swap in an httpx.MockTransport here)."""
    return httpx.AsyncClient()


def _prompt_chars(messages: List[Dict[str, Any]], system: Optional[str]) -> int:
    total = len(system or "")
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            total += len(content)
    return total


def _usage_fields(result: Any) -> Dict[str, Any]:
    usage = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(usage, dict):
        return {field: None for field in _USAGE_FIELDS}
    return {field: usage.get(field) for field in _USAGE_FIELDS}


def _emit_llm_call(
    *,
    role: str,
    model: str,
    attempt: int,
    retry_reason: Optional[str],
    http_status: Optional[int],
    ok: bool,
    error_class: Optional[str],
    started: float,
    prompt_chars: int,
    tools_count: int,
    result: Any,
    response: Optional[httpx.Response] = None,
) -> None:
    headers = response.headers if response is not None else {}
    telemetry.emit(
        "llm_call",
        role=role,
        model=model,
        attempt=attempt,
        retry_reason=retry_reason,
        http_status=http_status,
        ok=ok,
        error_class=error_class,
        latency_ms=round((time.monotonic() - started) * 1000, 1),
        prompt_chars=prompt_chars,
        tools_count=tools_count,
        ratelimit_limit=headers.get("x-ratelimit-limit"),
        ratelimit_remaining=headers.get("x-ratelimit-remaining"),
        **_usage_fields(result),
    )


class _AttemptFailed(Exception):
    """One HTTP attempt failed. ``error`` is what the caller sees if no retry happens."""

    def __init__(self, error: BaseException, *, retry_reason: Optional[str], retry_after: Optional[float]) -> None:
        super().__init__(str(error))
        self.error = error
        self.retry_reason = retry_reason  # None = not retryable (e.g. a 400)
        self.retry_after = retry_after


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_sleep = asyncio.sleep  # tests replace these two
_rng = random.Random()


def _retry_after_seconds(response: Optional[httpx.Response]) -> Optional[float]:
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None  # HTTP-date form is not used by OpenRouter; ignore it


def _body_error_code(result: Any) -> Optional[int]:
    error = result.get("error") if isinstance(result, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


async def _attempt(
    *, url: str, payload: Dict[str, object], api_key: Optional[str], model: str, role: str,
    attempt: int, retry_reason: Optional[str], timeout: float, prompt_chars: int, tools_count: int,
) -> Dict[str, Any]:
    """Exactly one HTTP request (the baseline behaviour), with one llm_call event."""
    started = time.monotonic()
    http_status: Optional[int] = None
    result: Any = None
    response: Optional[httpx.Response] = None
    try:
        async with _http_client() as client:
            try:
                response = await client.post(
                    url,
                    headers=_headers(api_key=api_key),
                    json=payload,
                    timeout=timeout,  # Set reasonable timeout instead of None
                )
                http_status = response.status_code
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error("OpenRouter request failed for model %s (HTTP %s)", model, exc.response.status_code)
                try:
                    _handle_response_error(exc)
                except OpenRouterError as wrapped:
                    status = exc.response.status_code
                    raise _AttemptFailed(
                        wrapped,
                        retry_reason=f"http_{status}" if status in _RETRYABLE_STATUS else None,
                        retry_after=_retry_after_seconds(exc.response),
                    ) from exc
            except httpx.HTTPError as exc:
                logger.error("OpenRouter request failed for model %s (%s)", model, type(exc).__name__)
                wrapped = OpenRouterError(f"OpenRouter request failed: {exc}")
                wrapped.__cause__ = exc
                transient = isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))
                raise _AttemptFailed(wrapped, retry_reason=type(exc).__name__ if transient else None, retry_after=None) from exc

            result = response.json()
            if isinstance(result, dict) and result.get("error"):
                code = _body_error_code(result)
                logger.error(
                    "OpenRouter returned an error for model %s (HTTP %s, error code %s)",
                    model,
                    response.status_code,
                    code if code is not None else "unknown",
                )
                transient = code is not None and (code == 429 or 500 <= code <= 599)
                raise _AttemptFailed(
                    OpenRouterError("OpenRouter returned an error in the completion response"),
                    retry_reason=f"body_{code}" if transient else None,
                    retry_after=_retry_after_seconds(response),
                )
    except BaseException as exc:
        inner = exc.error if isinstance(exc, _AttemptFailed) else exc
        _emit_llm_call(
            role=role, model=model, attempt=attempt, retry_reason=retry_reason, http_status=http_status, ok=False,
            error_class=type(inner).__name__, started=started, prompt_chars=prompt_chars,
            tools_count=tools_count, result=result, response=response,
        )
        raise
    _emit_llm_call(
        role=role, model=model, attempt=attempt, retry_reason=retry_reason, http_status=http_status, ok=True,
        error_class=None, started=started, prompt_chars=prompt_chars, tools_count=tools_count, result=result,
        response=response,
    )
    return result


def _attempt_timeout(deadline: Optional[float]) -> float:
    """Per-attempt timeout: min(60, remaining - 1), at least 5 s, when the run has a deadline."""
    if deadline is None:
        return float(LLM_REQUEST_TIMEOUT_S.get())
    remaining = deadline - clock.monotonic()
    return max(float(LLM_MIN_ATTEMPT_TIMEOUT_S.get()), min(float(LLM_REQUEST_TIMEOUT_S.get()), remaining - 1.0))


def _backoff(attempt: int, retry_after: Optional[float]) -> float:
    """Full jitter: uniform(0, min(max, base * 2^(attempt-1))); Retry-After wins, capped at max."""
    cap = float(LLM_RETRY_MAX_DELAY_S.get())
    if retry_after is not None:
        return min(cap, retry_after)
    return _rng.uniform(0.0, min(cap, float(LLM_RETRY_BASE_S.get()) * (2 ** (attempt - 1))))


async def request_chat_completion(
    *,
    model: str,
    messages: List[Dict[str, str]],
    system: Optional[str] = None,
    api_key: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    base_url: str = OpenRouterBaseURL,
    role: str = "unknown",
) -> Dict[str, Any]:
    """Request a chat completion and return the raw JSON payload.

    ``role`` (interaction/execution/search/classifier/summarizer) only labels
    telemetry; it is never sent to the API.

    With LLM_RETRY (1.4, M4) transient failures (429, 5xx, timeouts, connection
    errors, a 200 whose body carries a 429/5xx error) are retried with
    exponential backoff and full jitter, honouring Retry-After, and never past
    the current run's deadline. Other 4xx are never retried. LLM calls have no
    side effects, which is the ONLY reason retrying them is safe: this mechanism
    must never be reused for Gmail actions.
    """

    payload: Dict[str, object] = {
        "model": model,
        "messages": _build_messages(messages, system),
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    url = f"{base_url.rstrip('/')}/chat/completions"
    prompt_chars = _prompt_chars(messages, system)
    tools_count = len(tools or [])
    telemetry.capture_prompt(role, 1, {"model": model, "system": system, "messages": messages, "tools_count": tools_count})

    retrying = flags.enabled("LLM_RETRY")
    max_attempts = int(LLM_RETRY_MAX_ATTEMPTS.get()) if retrying else 1
    run = current_run()
    deadline = run.deadline_monotonic if (retrying and run is not None) else None
    retry_reason: Optional[str] = None
    attempt = 1
    while True:
        timeout = _attempt_timeout(deadline) if retrying else _REQUEST_TIMEOUT_S
        try:
            return await _attempt(
                url=url, payload=payload, api_key=api_key, model=model, role=role, attempt=attempt,
                retry_reason=retry_reason, timeout=timeout, prompt_chars=prompt_chars, tools_count=tools_count,
            )
        except _AttemptFailed as failed:
            if failed.retry_reason is None or attempt >= max_attempts:
                raise failed.error from failed.error.__cause__
            delay = _backoff(attempt, failed.retry_after)
            if deadline is not None and clock.monotonic() + delay + float(LLM_MIN_CALL_BUDGET_S.get()) > deadline:
                telemetry.emit("llm_retry_skipped", role=role, attempt=attempt, reason="deadline", delay_s=round(delay, 3))
                raise failed.error from failed.error.__cause__
            await _sleep(delay)
            retry_reason = failed.retry_reason
            attempt += 1


__all__ = ["OpenRouterError", "request_chat_completion", "OpenRouterBaseURL"]
