"""Tracked background tasks (invariant I11).

Every background task created by new code goes through :func:`spawn`, which
(1) keeps a strong reference (asyncio only keeps weak ones), (2) attaches a
done-callback that logs and emits telemetry for exceptions instead of losing
them, and (3) lets shutdown cancel or await everything that is still running.
Legacy fire-and-forget call sites are left untouched when their flag is off.
"""

from __future__ import annotations

import asyncio
from typing import Coroutine, Optional, Set

from ..logging_config import logger

_tasks: Set["asyncio.Task[object]"] = set()


def _on_done(task: "asyncio.Task[object]") -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    logger.error("Background task %s failed: %s", task.get_name(), exc, exc_info=exc)
    try:
        from ..services import telemetry

        telemetry.emit("background_task_failed", task=task.get_name(), error_class=type(exc).__name__)
    except Exception:  # pragma: no cover - telemetry never raises, belt and braces
        pass


def spawn(coro: Coroutine[object, object, object], *, name: str) -> "asyncio.Task[object]":
    """Create a tracked task on the running loop."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def pending() -> Set["asyncio.Task[object]"]:
    return {task for task in _tasks if not task.done()}


async def shutdown(timeout: Optional[float]) -> int:
    """Await tracked tasks up to ``timeout`` seconds, then cancel the rest. Returns how many were cancelled."""
    running = pending()
    if not running:
        return 0
    done, still_running = await asyncio.wait(running, timeout=timeout)
    for task in still_running:
        task.cancel()
    if still_running:
        await asyncio.gather(*still_running, return_exceptions=True)
    return len(still_running)


def reset() -> None:
    """Forget tracked tasks (tests; each test runs its own event loop)."""
    _tasks.clear()


__all__ = ["pending", "reset", "shutdown", "spawn"]
