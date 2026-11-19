"""Dedicated executor for blocking Composio calls (single owner; tasks 1.5 / 1.9).

The Composio SDK is synchronous. Called on the event loop it freezes every
other agent, LLM call and HTTP request for the duration (mechanism M5,
reproduced as E1: a 1.5 s freeze). ``run_gmail`` runs such a call on a
dedicated thread pool instead.

- ``GMAIL_WORKERS`` defaults to 1: Composio client thread safety is unverified
  (U5), so calls stay serialized exactly as they effectively are today; only the
  event loop is freed.
- ContextVars are copied into the worker (``loop.run_in_executor`` does not do
  this), so telemetry and journaling inside the thread see the run and turn.
- Cancelling the awaiting coroutine does NOT stop the thread. For reads the
  late result is simply discarded. Sends must therefore be shielded and have
  their outcome recorded by a done-callback (services/safety/send_guard.py).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

from ...overload_config import GMAIL_WORKERS

T = TypeVar("T")

_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None


def get_gmail_executor() -> ThreadPoolExecutor:
    """Lazily create the pool so importing this module starts no threads."""
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=int(GMAIL_WORKERS.get()), thread_name_prefix="gmail")
        return _executor


def submit_gmail(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> "asyncio.Future[T]":
    """Schedule ``fn`` on the Gmail pool with the caller's context; return an asyncio future."""
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, fn, *args, **kwargs)
    return asyncio.get_running_loop().run_in_executor(get_gmail_executor(), call)


async def run_gmail(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Await a blocking Gmail call without blocking the event loop."""
    return await submit_gmail(fn, *args, **kwargs)


def shutdown_gmail_executor(wait: bool) -> None:
    """Stop the pool (app shutdown). A later call to run_gmail creates a new one."""
    global _executor
    with _lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=wait)


def reset_for_tests() -> None:
    shutdown_gmail_executor(wait=True)


__all__ = ["get_gmail_executor", "run_gmail", "shutdown_gmail_executor", "submit_gmail"]
