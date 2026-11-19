"""Run admission: per-agent locks (7A), duplicate-dispatch suppression (D1), concurrency gate (4A).

Single owner of "may this run start now?". All state is PROCESS-GLOBAL
(decision 12): until POLL_BATCH, the trigger scheduler builds a new
ExecutionBatchManager per trigger, so per-manager state would miss trigger runs.

Three separate mechanisms, deliberately not merged (locking != idempotency):
- ``agent_lock``: at most one run per agent (journal slug) at a time (I5);
- ``DispatchDeduper``: an exact repeat of an instruction to the same agent
  is not run twice while in flight or shortly after;
- ``ConcurrencyGate``: at most C runs in total, with R slots only user-rooted
  runs may take.
Lock order is always agent lock -> gate; nothing ever holds two agent locks
(execution agents cannot dispatch), so there is no lock-order deadlock.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Deque, Dict, Optional, Tuple

from ...overload_config import CONCURRENCY_TOTAL, CONCURRENCY_USER_RESERVED, DISPATCH_DEDUPE_WINDOW_S
from ...services.execution.log_store import _slugify
from ...utils import clock

# ---------------------------------------------------------------- 7A agent locks
_agent_locks: Dict[str, asyncio.Lock] = {}


def agent_lock(agent_name: str) -> asyncio.Lock:
    """The lock for an agent, keyed by journal slug (names that share a journal share a lock).

    Created lazily from async code so the lock belongs to the running loop.
    """
    slug = _slugify(agent_name)
    lock = _agent_locks.get(slug)
    if lock is None:
        lock = asyncio.Lock()
        _agent_locks[slug] = lock
    return lock


# ---------------------------------------------------------------- D1 dedupe
def normalize_instructions(text: str) -> str:
    """Strip, lowercase, collapse whitespace. Nothing semantic: D1 only catches EXACT repeats."""
    return " ".join((text or "").strip().lower().split())


@dataclass
class DedupeEntry:
    request_id: str
    state: str  # running | completed
    completed_at: Optional[float] = None


class DispatchDeduper:
    """Suppress exact duplicate dispatches while in flight and for a window after completion.

    In-memory only: after a restart the side-effect ledger (D2) still protects sends.
    """

    def __init__(self) -> None:
        self._entries: Dict[Tuple[str, str], DedupeEntry] = {}

    @staticmethod
    def key(agent_name: str, instructions: str) -> Tuple[str, str]:
        digest = hashlib.sha256(normalize_instructions(instructions).encode("utf-8")).hexdigest()
        return _slugify(agent_name), digest

    def _purge(self) -> None:
        window = float(DISPATCH_DEDUPE_WINDOW_S.get())
        now = clock.monotonic()
        expired = [k for k, e in self._entries.items() if e.state == "completed" and e.completed_at is not None
                   and now - e.completed_at > window]
        for key in expired:
            del self._entries[key]

    def check(self, agent_name: str, instructions: str) -> Optional[DedupeEntry]:
        self._purge()
        return self._entries.get(self.key(agent_name, instructions))

    def register(self, agent_name: str, instructions: str, request_id: str) -> None:
        self._entries[self.key(agent_name, instructions)] = DedupeEntry(request_id=request_id, state="running")

    def complete(self, agent_name: str, instructions: str, request_id: str) -> None:
        entry = self._entries.get(self.key(agent_name, instructions))
        if entry is not None and entry.request_id == request_id:
            entry.state = "completed"
            entry.completed_at = clock.monotonic()


_deduper = DispatchDeduper()


def get_deduper() -> DispatchDeduper:
    return _deduper


# ---------------------------------------------------------------- 4A concurrency gate
class ConcurrencyGate:
    """Bounded run concurrency with a reservation for user-rooted work.

    User-rooted runs may take any free slot; other runs (trigger, email,
    system roots) only while free slots exceed the reservation. FIFO within
    each class; user waiters are woken first.
    """

    def __init__(self, total: int, reserved_for_user: int) -> None:
        if not 0 <= reserved_for_user < total:
            raise ValueError("need 0 <= reserved_for_user < total")
        self.total = total
        self.reserved = reserved_for_user
        self._free = total
        self._waiters: Dict[str, Deque[asyncio.Future]] = {"user": deque(), "other": deque()}

    @staticmethod
    def _class(root_origin: str) -> str:
        return "user" if root_origin == "user" else "other"

    def _can_admit(self, cls: str) -> bool:
        return self._free > 0 if cls == "user" else self._free > self.reserved

    @property
    def in_use(self) -> int:
        return self.total - self._free

    @property
    def waiting(self) -> int:
        return sum(1 for q in self._waiters.values() for f in q if not f.done())

    async def acquire(self, root_origin: str) -> None:
        cls = self._class(root_origin)
        queue = self._waiters[cls]
        if not any(not f.done() for f in queue) and self._can_admit(cls):
            self._free -= 1
            return
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        queue.append(future)
        try:
            await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                self.release()  # granted just before we were cancelled: hand the slot back
            else:
                try:
                    queue.remove(future)
                except ValueError:
                    pass
            raise

    def release(self) -> None:
        self._free += 1
        self._wake()

    def _wake(self) -> None:
        for cls in ("user", "other"):
            queue = self._waiters[cls]
            while queue and self._can_admit(cls):
                future = queue.popleft()
                if future.done():
                    continue
                self._free -= 1
                future.set_result(None)

    @asynccontextmanager
    async def slot(self, root_origin: str) -> AsyncIterator[None]:
        await self.acquire(root_origin)
        try:
            yield
        finally:
            self.release()


_gate: Optional[ConcurrencyGate] = None


def get_gate() -> ConcurrencyGate:
    global _gate
    if _gate is None:
        _gate = ConcurrencyGate(int(CONCURRENCY_TOTAL.get()), int(CONCURRENCY_USER_RESERVED.get()))
    return _gate


def reset_for_tests() -> None:
    global _deduper, _gate
    _agent_locks.clear()
    _deduper = DispatchDeduper()
    _gate = None


__all__ = [
    "ConcurrencyGate",
    "DispatchDeduper",
    "agent_lock",
    "get_deduper",
    "get_gate",
    "normalize_instructions",
]
