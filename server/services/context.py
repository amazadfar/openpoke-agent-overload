"""Correlation context for turns and execution runs (single owner).

A *turn* is one interaction-agent invocation (a user message, a batch of agent
results, a scheduler poll, a watcher notification). A *run* is one execution
agent run. Both live in ContextVars, so ``asyncio.create_task`` children inherit
them automatically. ``loop.run_in_executor`` does NOT copy context; the Gmail
executor (services/gmail/executor.py) copies it explicitly.

Phase 0 rule: this module is passive. Setting a scope changes no behaviour; it
only labels telemetry. Later fixes read it (origin for overrides and approvals,
deadline for retries and send budgets, turn id for batches).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Iterator, Literal, Optional, Tuple

Origin = Literal["user", "agent", "trigger", "email", "system"]
ORIGINS: Tuple[str, ...] = ("user", "agent", "trigger", "email", "system")


@dataclass(frozen=True)
class TurnContext:
    """Identity of one interaction-agent turn and the chain it belongs to."""

    turn_id: str
    parent_turn_id: Optional[str]
    origin: str
    root_origin: str  # origin of the first turn in the chain
    depth: int
    user_message: Optional[str] = None  # raw HTTP-received text, only for origin="user"
    poll_id: Optional[str] = None  # scheduler/watcher poll correlation


@dataclass(frozen=True)
class RunContext:
    """Identity of one execution-agent run."""

    run_id: str
    agent: str
    deadline_monotonic: Optional[float] = None
    trigger_occurrence: Optional[Tuple[int, str]] = None  # (trigger_id, scheduled_for)


SYSTEM_TURN = TurnContext(
    turn_id="system",
    parent_turn_id=None,
    origin="system",
    root_origin="system",
    depth=0,
)

_turn_var: ContextVar[Optional[TurnContext]] = ContextVar("openpoke_turn", default=None)
_run_var: ContextVar[Optional[RunContext]] = ContextVar("openpoke_run", default=None)


def new_id(prefix: str) -> str:
    """Short random identifier, e.g. ``turn_3f2a9c1d0b4e``."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def current_turn() -> TurnContext:
    """The active turn, or the synthetic ``system`` turn on legacy/unscoped paths."""
    return _turn_var.get() or SYSTEM_TURN


def has_turn() -> bool:
    return _turn_var.get() is not None


def current_run() -> Optional[RunContext]:
    """The active execution run, if any."""
    return _run_var.get()


def make_turn(
    origin: str,
    parent: Optional[TurnContext] = None,
    *,
    user_message: Optional[str] = None,
    poll_id: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> TurnContext:
    """Build a TurnContext. A parent makes this a follow-up in the parent's chain."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown turn origin: {origin}")
    if parent is not None and parent.turn_id == SYSTEM_TURN.turn_id:
        parent = None
    return TurnContext(
        turn_id=turn_id or new_id("turn"),
        parent_turn_id=parent.turn_id if parent else None,
        origin=origin,
        root_origin=parent.root_origin if parent else origin,
        depth=parent.depth + 1 if parent else 0,
        user_message=user_message if origin == "user" else None,
        poll_id=poll_id if poll_id is not None else (parent.poll_id if parent else None),
    )


@contextmanager
def use_turn(turn: TurnContext) -> Iterator[TurnContext]:
    """Activate an existing TurnContext. A turn is never inside a run, so the run is cleared."""
    turn_token = _turn_var.set(turn)
    run_token = _run_var.set(None)
    try:
        yield turn
    finally:
        _run_var.reset(run_token)
        _turn_var.reset(turn_token)


@contextmanager
def turn_scope(
    origin: str,
    parent: Optional[TurnContext] = None,
    *,
    user_message: Optional[str] = None,
    poll_id: Optional[str] = None,
) -> Iterator[TurnContext]:
    """Open a new turn (a root turn when ``parent`` is None)."""
    with use_turn(make_turn(origin, parent, user_message=user_message, poll_id=poll_id)) as turn:
        yield turn


@contextmanager
def run_scope(
    agent: str,
    *,
    run_id: Optional[str] = None,
    deadline_monotonic: Optional[float] = None,
    trigger_occurrence: Optional[Tuple[int, str]] = None,
) -> Iterator[RunContext]:
    """Open an execution run inside the current turn."""
    run = RunContext(
        run_id=run_id or new_id("run"),
        agent=agent,
        deadline_monotonic=deadline_monotonic,
        trigger_occurrence=trigger_occurrence,
    )
    token = _run_var.set(run)
    try:
        yield run
    finally:
        _run_var.reset(token)


def with_deadline(run: RunContext, deadline_monotonic: Optional[float]) -> RunContext:
    return replace(run, deadline_monotonic=deadline_monotonic)


def context_fields() -> dict:
    """Correlation fields attached to every telemetry event."""
    turn = current_turn()
    run = current_run()
    return {
        "turn_id": turn.turn_id,
        "parent_turn_id": turn.parent_turn_id,
        "origin": turn.origin,
        "root_origin": turn.root_origin,
        "depth": turn.depth,
        "poll_id": turn.poll_id,
        "run_id": run.run_id if run else None,
        "agent": run.agent if run else None,
    }


__all__ = [
    "ORIGINS",
    "Origin",
    "RunContext",
    "SYSTEM_TURN",
    "TurnContext",
    "context_fields",
    "current_run",
    "current_turn",
    "has_turn",
    "make_turn",
    "new_id",
    "run_scope",
    "turn_scope",
    "use_turn",
    "with_deadline",
]
