"""The single wrapper around the three external-send tools (tasks 1.8, 1.9, 1.10).

Order for one send attempt:

    [SEND_GATE]  -> store an approval, return approval_required (nothing sent)
    ledger.begin -> Blocked / ConfirmationRequired: return a structured result (nothing sent)
    call Composio (sync on the loop, or with GMAIL_SENDS_OFF_LOOP on the Gmail
                   executor, shielded, outcome recorded by a done-callback)
    ledger.finish(sent | failed | unknown)

Retries are never applied here (only LLM calls are retried, 1.4).
"""

from __future__ import annotations

import asyncio
import functools
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Set, Union

from ... import flags
from ...logging_config import logger
from ...overload_config import MIN_SEND_BUDGET_S
from ...utils import clock, tasks
from .. import telemetry
from ..context import RunContext, TurnContext, current_run, current_turn, turn_scope
from . import ledger as ledger_mod
from .db import SafetyStoreUnavailable

ToolFn = Callable[..., Any]
_in_flight: Set["asyncio.Future[Any]"] = set()

CONFIRM_ARG = "confirm_duplicate_of"


def classify(result: Any) -> str:
    """Map a Composio-normalized response to a ledger status."""
    if isinstance(result, dict):
        if result.get("successful") is True:
            return "sent"
        if result.get("successful") is False:
            return "failed"
        if "error" in result and not result.get("data"):
            return "failed"  # tool-level refusal before Composio was called (e.g. Gmail not connected)
    return "unknown"


def _intent(action: str, args: Dict[str, Any]) -> ledger_mod.SendIntent:
    ledger = ledger_mod.get_ledger()
    meta = ledger.draft_meta(args.get("draft_id")) if action == "GMAIL_SEND_DRAFT" else None
    return ledger_mod.intent_for(action, args, meta)


def _refusal(kind: str, previous: ledger_mod.SideEffect, override_rejected: bool) -> Dict[str, Any]:
    if kind == "blocked_duplicate":
        message = ("This exact email was already sent or is in progress (previous record below). It was NOT sent again. "
                   "Report this to Poke; only retry with confirm_duplicate_of=<previous id> if the user explicitly asks to send it again.")
    else:
        message = ("A similar email to the same recipients/thread was sent or attempted in the last 24 hours (previous record below). "
                   "It was NOT sent. Ask Poke to confirm with the user; then retry with confirm_duplicate_of=<previous id>.")
    if override_rejected:
        message += " (confirm_duplicate_of is only accepted for user-originated requests.)"
    return {"status": kind, "previous": previous.public(), "message": message}


def _prepare(tool_name: str, action: str, args: Dict[str, Any], agent_name: str, *,
             approved: bool = False) -> Union[int, Dict[str, Any]]:
    """Gate and ledger checks. Returns a ledger record id to proceed, or a structured result to return.

    ``approved`` is only set by execute_approved(), i.e. after a human approval passed every check.
    """
    confirm = args.pop(CONFIRM_ARG, None)
    try:
        confirm = int(confirm) if confirm is not None else None
    except (TypeError, ValueError):
        confirm = None
    intent = _intent(action, args)

    if flags.enabled("SEND_GATE") and not approved:
        from .approvals import get_approvals, summarize

        approval = get_approvals().create(
            tool_name=tool_name, action=action, args=dict(args, **({CONFIRM_ARG: confirm} if confirm else {})),
            fingerprint=ledger_mod.fingerprint(intent), summary=summarize(action, args, intent.recipients, intent.subject_norm),
        )
        telemetry.emit("approval_created", approval_id=approval.id, action=action)
        return {
            "status": "approval_required",
            "approval_id": approval.id,
            "summary": approval.summary,
            "code": approval.code,
            "message": "Nothing was sent. The user must approve this exact action by replying with the code.",
        }

    try:
        begin = ledger_mod.get_ledger().begin(intent, confirm_duplicate_of=confirm)
    except SafetyStoreUnavailable as exc:
        telemetry.emit("side_effect", action=action, status="not_started", blocked_reason="ledger_unavailable", ledger_id=None)
        return {"status": "not_started", "error": f"Send not attempted: {exc}"}
    if isinstance(begin, ledger_mod.Blocked):
        _emit(action, intent, "blocked", ledger_id=begin.previous.id, blocked_reason="exact_duplicate")
        return _refusal("blocked_duplicate", begin.previous, begin.override_rejected)
    if isinstance(begin, ledger_mod.ConfirmationRequired):
        _emit(action, intent, "blocked", ledger_id=begin.previous.id, blocked_reason="near_duplicate")
        return _refusal("confirmation_required", begin.previous, begin.override_rejected)
    _emit(action, intent, "pending", ledger_id=begin.record_id)
    return begin.record_id


def _emit(action: str, intent: ledger_mod.SendIntent, status: str, *, ledger_id: Optional[int], blocked_reason: Optional[str] = None) -> None:
    nk = ledger_mod.near_key(intent)
    telemetry.emit(
        "side_effect",
        action=action,
        fingerprint_prefix=ledger_mod.fingerprint(intent)[:12],
        near_key_hash=telemetry.sha(nk) if nk else None,
        status=status,
        ledger_id=ledger_id,
        blocked_reason=blocked_reason,
    )


def _finish(record_id: int, action: str, outcome: Any, error: Optional[str]) -> str:
    status = "unknown" if error is not None else classify(outcome)
    try:
        ledger_mod.get_ledger().finish(record_id, status, error)
    except SafetyStoreUnavailable:  # pragma: no cover - the row stays pending; startup sweep makes it unknown
        logger.error("Could not record the outcome of side effect %s", record_id)
    telemetry.emit("side_effect", action=action, status=status, ledger_id=record_id)
    return status


def _unknown_result(record_id: int, error: str) -> Dict[str, Any]:
    return {
        "status": "outcome_unknown",
        "ledger_id": record_id,
        "error": error,
        "message": "The send may or may not have gone out. Do NOT retry; tell Poke the outcome is unknown.",
    }


# --------------------------------------------------------------------------- sync (1.8)
def _sync_send(tool_name: str, action: str, fn: ToolFn, agent_name: str) -> ToolFn:
    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> Dict[str, Any]:
        prepared = _prepare(tool_name, action, kwargs, agent_name)
        if isinstance(prepared, dict):
            return prepared
        try:
            result = fn(**kwargs)
        except Exception as exc:
            _finish(prepared, action, None, str(exc))
            return _unknown_result(prepared, str(exc))
        _finish(prepared, action, result, None)
        return result

    return wrapper


def _close_sent_draft(tool_name: str, args: Dict[str, Any], result: Any) -> None:
    if tool_name == "gmail_execute_draft" and classify(result) == "sent" and flags.enabled("AGENT_CARD"):
        from ..agents.card import note_draft_sent

        note_draft_sent(args.get("draft_id"))


# --------------------------------------------------------------------------- off loop (1.9)
@dataclass
class _SendState:
    record_id: int
    tool_name: str
    action: str
    args: Dict[str, Any]
    agent: str
    run: Optional[RunContext]
    turn: TurnContext
    cancelled: bool = False
    finalized: bool = False
    journaled: bool = False
    status: Optional[str] = None
    result: Any = None
    extra: Dict[str, Any] = field(default_factory=dict)


def _journal(state: _SendState) -> None:
    """Write the send into the owning agent's journal (the cancelled run could not)."""
    from ...agents.execution_agent.agent import ExecutionAgent

    state.journaled = True
    try:
        payload = json.dumps(state.result, default=str)
        ExecutionAgent(state.agent).record_tool_execution(
            state.tool_name, json.dumps(state.args, default=str), payload, raw_arguments=state.args, raw_result=state.result,
        )
    except Exception:  # pragma: no cover - journaling must not break finalization
        logger.exception("Failed to journal a late send outcome")


def _late_notice(state: _SendState) -> None:
    row = ledger_mod.get_ledger().get(state.record_id)
    if row is None or not row.reported_unknown:
        return
    text = (f"Update: {ledger_mod.describe_side_effect(row)}, which was in progress when {state.agent} timed out, "
            f"completed with status {row.status}.")
    telemetry.emit("late_completion_notice", ledger_id=state.record_id, status=row.status)

    async def notify() -> None:
        from ...agents.interaction_agent.runtime import InteractionAgentRuntime

        with turn_scope("agent", parent=state.turn):
            await InteractionAgentRuntime().handle_agent_message(text)

    tasks.spawn(notify(), name="late-send-notice")


def _finalize(state: _SendState, future: "asyncio.Future[Any]") -> None:
    """Done-callback (runs on the event loop): always record the outcome, even if the run was cancelled."""
    _in_flight.discard(future)
    if future.cancelled():
        state.result, error = None, "cancelled before completion"
    elif future.exception() is not None:
        state.result, error = None, str(future.exception())
    else:
        state.result, error = future.result(), None
    state.status = _finish(state.record_id, state.action, state.result, error)
    if error is not None:
        state.result = _unknown_result(state.record_id, error)
    state.finalized = True
    if state.cancelled:
        if not state.journaled:
            _journal(state)
        _close_sent_draft(state.tool_name, state.args, state.result)  # the cancelled run cannot update its card
        _late_notice(state)


def _async_send(tool_name: str, action: str, fn: ToolFn, agent_name: str) -> ToolFn:
    from ..gmail.executor import submit_gmail

    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> Dict[str, Any]:
        run = current_run()
        if run is not None and run.deadline_monotonic is not None:
            remaining = run.deadline_monotonic - clock.monotonic()
            if remaining < float(MIN_SEND_BUDGET_S.get()):
                telemetry.emit("side_effect", action=action, status="not_started", blocked_reason="time_budget", ledger_id=None)
                return {"status": "not_started", "reason": "insufficient time budget",
                        "message": "Nothing was sent: not enough time left in this run to send safely. Report this to Poke."}
        prepared = _prepare(tool_name, action, kwargs, agent_name)
        if isinstance(prepared, dict):
            return prepared
        state = _SendState(prepared, tool_name, action, dict(kwargs), agent_name, run, current_turn())
        future = submit_gmail(fn, **kwargs)
        _in_flight.add(future)
        future.add_done_callback(functools.partial(_finalize, state))  # registered before shield: runs first
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            state.cancelled = True
            if state.finalized and not state.journaled:
                _journal(state)
            raise
        except Exception as exc:
            return _unknown_result(prepared, str(exc))

    return wrapper


def guarded_send(tool_name: str, action: str, fn: ToolFn, agent_name: str) -> ToolFn:
    """Wrap one external-send tool for the current flag set."""
    if flags.enabled("GMAIL_SENDS_OFF_LOOP"):
        return _async_send(tool_name, action, fn, agent_name)
    return _sync_send(tool_name, action, fn, agent_name)


# --------------------------------------------------------------------------- approved sends (1.10)
def execute_approved(approval: Any) -> Any:
    """Run a stored, human-approved action verbatim through the ledger (gate already passed).

    Returns the tool result, or a coroutine of it when sends run off the loop.
    """
    from ...agents.execution_agent.tools import gmail as gmail_tools

    fn = getattr(gmail_tools, approval.tool_name)
    args = dict(approval.args)
    agent = approval.agent or "approved-send"

    def after(result: Any) -> Any:
        from ...agents.execution_agent.agent import ExecutionAgent

        _close_sent_draft(approval.tool_name, args, result)

        try:  # the owning agent should know its pending send went out
            ExecutionAgent(agent).record_tool_execution(
                approval.tool_name, json.dumps(args, default=str), json.dumps(result, default=str),
                raw_arguments=args, raw_result=result)
        except Exception:  # pragma: no cover - journaling is best effort
            logger.exception("Failed to journal an approved send")
        return result

    if flags.enabled("GMAIL_SENDS_OFF_LOOP"):
        async def run() -> Any:
            prepared = _prepare(approval.tool_name, approval.action, args, agent, approved=True)
            if isinstance(prepared, dict):
                return prepared
            from ..gmail.executor import run_gmail

            try:
                result = await asyncio.shield(run_gmail(fn, **args))
            except Exception as exc:
                _finish(prepared, approval.action, None, str(exc))
                return _unknown_result(prepared, str(exc))
            _finish(prepared, approval.action, result, None)
            return after(result)

        return run()

    prepared = _prepare(approval.tool_name, approval.action, args, agent, approved=True)
    if isinstance(prepared, dict):
        return prepared
    try:
        result = fn(**args)
    except Exception as exc:
        _finish(prepared, approval.action, None, str(exc))
        return _unknown_result(prepared, str(exc))
    _finish(prepared, approval.action, result, None)
    return after(result)


# --------------------------------------------------------------------------- drafts
def recording_drafts(fn: ToolFn, agent_name: str) -> ToolFn:
    """Record draft metadata on GMAIL_CREATE_EMAIL_DRAFT success, so sending it later has a content identity."""

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> Dict[str, Any]:
        result = fn(**kwargs)
        try:
            data = result.get("data") if isinstance(result, dict) else None
            if isinstance(result, dict) and result.get("successful") and isinstance(data, dict) and data.get("id"):
                message = data.get("message") if isinstance(data.get("message"), dict) else {}
                ledger_mod.get_ledger().record_draft(
                    draft_id=str(data["id"]),
                    thread_id=message.get("threadId") or kwargs.get("thread_id"),
                    recipients=ledger_mod.normalize_recipients(kwargs.get("recipient_email"), kwargs.get("cc"),
                                                               kwargs.get("bcc"), kwargs.get("extra_recipients")),
                    subject_norm=ledger_mod.normalize_subject(kwargs.get("subject")),
                    body_sha256=ledger_mod.body_sha(kwargs.get("body")),
                    agent=agent_name,
                )
        except Exception:  # metadata is best effort; without it the draft id alone identifies the send
            logger.exception("Failed to record draft metadata")
        return result

    return wrapper


# --------------------------------------------------------------------------- shutdown / tests
async def wait_for_in_flight(timeout: float) -> int:
    """Give in-flight sends up to ``timeout`` seconds; return how many are still running."""
    pending = {f for f in _in_flight if not f.done()}
    if not pending:
        return 0
    _, still = await asyncio.wait(pending, timeout=timeout)
    return len(still)


def in_flight() -> int:
    return sum(1 for f in _in_flight if not f.done())


def reset_for_tests() -> None:
    _in_flight.clear()


__all__ = ["classify", "execute_approved", "guarded_send", "in_flight", "recording_drafts", "wait_for_in_flight"]
