"""Async helpers shared by the L2 mechanism tests (no pytest-asyncio needed)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
BROKER_FIXTURE = REPO_ROOT / "evals" / "fixtures" / "broker_inbox.json"


# Tasks that belong to the test harness itself (the asyncio.run root task and the
# task running the test body). Before Python 3.12, asyncio.wait_for runs its
# awaitable in a NEW task, so "every task except me" would include the waiting
# root task. This set is the single place that knows which tasks are harness.
_harness_tasks: Set["asyncio.Task[Any]"] = set()


def harness_tasks() -> Set["asyncio.Task[Any]"]:
    return set(_harness_tasks)


def other_tasks(ignore: Iterable["asyncio.Task[Any]"] = ()) -> Set["asyncio.Task[Any]"]:
    """Unfinished tasks that are NOT the harness and not explicitly ignored (the system under test)."""
    excluded = _harness_tasks | set(ignore) | {asyncio.current_task()}
    return {t for t in asyncio.all_tasks() if t not in excluded and not t.done()}


def run_async(fn: Callable[[], Awaitable[Any]], timeout: float = 120.0) -> Any:
    """Run ``fn()`` on a fresh event loop with an overall timeout (portable to Python 3.10+)."""

    async def _body() -> Any:
        _harness_tasks.add(asyncio.current_task())  # the test body's task (differs from root before 3.12)
        return await fn()

    async def _main() -> Any:
        _harness_tasks.add(asyncio.current_task())  # the asyncio.run root task
        try:
            return await asyncio.wait_for(_body(), timeout=timeout)
        finally:
            _harness_tasks.clear()

    return asyncio.run(_main())


async def drain(timeout: float = 30.0, ignore: Iterable[asyncio.Task] = ()) -> None:
    """Wait until no other tasks remain (follow-up turns included).

    Settles only after two consecutive empty checks, so a task that spawns a
    child right before finishing is still awaited.
    """
    ignored = set(ignore)
    deadline = time.monotonic() + timeout
    empty_checks = 0
    while True:
        others = other_tasks(ignored)
        if not others:
            empty_checks += 1
            if empty_checks >= 2:
                return
            await asyncio.sleep(0)
            continue
        empty_checks = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            names = sorted(t.get_name() for t in others)
            raise TimeoutError(f"drain timed out with tasks still running: {names}")
        await asyncio.wait(others, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)


class Ticker:
    """Measures event-loop stalls: a 50 ms ticker records its worst lateness."""

    def __init__(self, interval: float = 0.05) -> None:
        self.interval = interval
        self.max_lateness = 0.0
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def _run(self) -> None:
        while self._running:
            started = time.monotonic()
            await asyncio.sleep(self.interval)
            lateness = time.monotonic() - started - self.interval
            self.max_lateness = max(self.max_lateness, lateness)

    def start(self) -> asyncio.Task:
        self._running = True
        self._task = asyncio.get_running_loop().create_task(self._run(), name="ticker")
        return self._task

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


async def user_turn(text: str):
    """Run one real user turn through the interaction agent (as the HTTP handler does)."""
    from server.agents.interaction_agent.runtime import InteractionAgentRuntime
    from server.services.context import turn_scope

    with turn_scope("user", user_message=text):
        return await InteractionAgentRuntime().execute(text)


def dispatch(agent_name: str, instructions: str, **extra: Any):
    """Call the IA's send_message_to_agent tool exactly as the IA loop does."""
    from server.agents.interaction_agent.tools import handle_tool_call

    return handle_tool_call("send_message_to_agent", {"agent_name": agent_name, "instructions": instructions, **extra})


def events(captured: List[Dict[str, Any]], name: str, **match: Any) -> List[Dict[str, Any]]:
    return [e for e in captured if e.get("event") == name and all(e.get(k) == v for k, v in match.items())]


def conversation_entries() -> List[tuple]:
    from server.services.conversation import get_conversation_log

    return list(get_conversation_log().iter_entries())


def agent_messages() -> List[str]:
    return [payload for tag, _, payload in conversation_entries() if tag == "agent_message"]


def journal(agent_name: str) -> List[tuple]:
    from server.services.execution import get_execution_agent_logs

    return list(get_execution_agent_logs().iter_entries(agent_name))


def load_broker_fixture() -> Dict[str, Any]:
    return json.loads(BROKER_FIXTURE.read_text(encoding="utf-8"))
