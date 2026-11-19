"""Agent card: one deterministic summary shared by routing and memory (task 2.2, flag AGENT_CARD).

The 1.2 window bounds the prompt but forgets anything older than K requests;
the registry knows identifiers but the agent cannot see them. The card closes
both gaps with no LLM call: after every run (under the agent's lock) the code
updates the registry, and before every run it renders a compact card that is
placed BEFORE the windowed history.

Fields: purpose (first + latest request), counterparties, open drafts (with
thread and recipients), recent threads, active triggers, last outcome, runs,
last activity. Every field is HTML-escaped like the journals, because fields
derive from email content and are an injection surface. Card failures are
logged and counted but never fail a run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html import escape
from typing import Any, Dict, List, Optional

from ...logging_config import logger
from ...overload_config import CARD_MAX_COUNTERPARTIES, CARD_MAX_THREADS, CARD_OUTCOME_CHARS
from .. import telemetry
from .ids import merge_all
from .registry import AgentRow, get_agent_registry, self_emails


@dataclass
class AgentCard:
    name: str
    purpose: Optional[str]
    latest_request: Optional[str]
    counterparties: List[str] = field(default_factory=list)
    open_drafts: List[Dict[str, Any]] = field(default_factory=list)
    threads: List[Dict[str, str]] = field(default_factory=list)
    triggers: List[Dict[str, Any]] = field(default_factory=list)
    last_outcome: Optional[str] = None
    last_outcome_at: Optional[str] = None
    runs: int = 0
    last_active_at: Optional[str] = None


def _row(agent_name: str) -> AgentRow:
    registry = get_agent_registry()
    row = registry.get(agent_name)
    if row is None:  # e.g. a trigger owner created before the registry existed
        row, _ = registry.create(agent_name)
    return row


def build_card(agent_name: str) -> AgentCard:
    registry = get_agent_registry()
    row = _row(agent_name)
    excluded = self_emails()
    emails = [i.value for i in registry.identifiers(row.id, "email") if i.value not in excluded]
    threads = [{"id": i.value, "last_seen": i.last_seen} for i in registry.identifiers(row.id, "thread")]
    details = registry.draft_details(row.id)
    drafts = []
    for ident in registry.identifiers(row.id, "draft"):
        if ident.state != "open":
            continue
        info = details.get(ident.value, {})
        drafts.append({"id": ident.value, "thread": info.get("thread"), "recipients": info.get("recipients", []),
                       "subject": info.get("subject")})
    triggers = []
    try:
        from ..triggers import get_trigger_service

        for name in {row.name, *[a for a in registry.aliases(row.id)]}:
            for record in get_trigger_service().list_triggers(agent_name=name):
                if record.status == "active":
                    triggers.append({"id": record.id, "next": record.next_trigger, "rule": record.recurrence_rule,
                                     "payload": record.payload})
    except Exception:  # pragma: no cover - the card is best effort
        logger.exception("Could not list triggers for the agent card")
    return AgentCard(
        name=row.name,
        purpose=row.purpose,
        latest_request=row.latest_request,
        counterparties=emails[: int(CARD_MAX_COUNTERPARTIES.get())],
        open_drafts=drafts,
        threads=threads[: int(CARD_MAX_THREADS.get())],
        triggers=triggers,
        last_outcome=row.last_outcome,
        last_outcome_at=row.last_outcome_at,
        runs=row.runs,
        last_active_at=row.last_active_at,
    )


def _e(value: Any) -> str:
    return escape(str(value), quote=False)


def render(card: AgentCard) -> str:
    lines = [f"- runs: {card.runs}; last active: {_e(card.last_active_at or 'never')}"]
    if card.purpose:
        lines.append(f"- purpose (first request): {_e(card.purpose)}")
    if card.latest_request and card.latest_request != card.purpose:
        lines.append(f"- latest request: {_e(card.latest_request)}")
    if card.counterparties:
        lines.append(f"- counterparties: {_e(', '.join(card.counterparties))}")
    for draft in card.open_drafts:
        parts = [f"draft_id {draft['id']}"]
        if draft.get("thread"):
            parts.append(f"thread {draft['thread']}")
        if draft.get("recipients"):
            parts.append("to " + ", ".join(draft["recipients"]))
        if draft.get("subject"):
            parts.append(f"subject \"{draft['subject']}\"")
        lines.append(f"- open draft: {_e('; '.join(parts))}")
    if card.threads:
        lines.append("- recent threads: " + _e(", ".join(f"{t['id']} (seen {t['last_seen']})" for t in card.threads)))
    for trigger in card.triggers:
        lines.append(f"- active trigger {trigger['id']}: next {_e(trigger['next'])}; {_e((trigger['payload'] or '')[:120])}")
    if card.last_outcome:
        lines.append(f"- last outcome ({_e(card.last_outcome_at)}): {_e(card.last_outcome)}")
    return "\n".join(lines)


def render_for_prompt(agent_name: str) -> Optional[str]:
    """The card text for the EA system prompt; None (and telemetry) on any failure."""
    try:
        text = render(build_card(agent_name))
        return text or None
    except Exception as exc:
        logger.exception("Agent card build failed; continuing without it")
        telemetry.emit("card_failed", stage="render", error_class=type(exc).__name__)
        return None


def update_after_run(agent_name: str, instructions: str, runtime: Any, result: Any) -> None:
    """Update identifiers, drafts, purpose, last outcome, runs and last activity. Never raises."""
    try:
        registry = get_agent_registry()
        row = _row(agent_name)
        collected = getattr(runtime, "collected_ids", []) if runtime is not None else []
        registry.record_identifiers(row.id, merge_all(collected), exclude_emails=self_emails())
        for entry in getattr(runtime, "tool_log", []) if runtime is not None else []:
            _apply_draft_lifecycle(registry, row.id, entry)
        registry.set_purpose(row.id, instructions, instructions)
        registry.record_run(row.id, getattr(result, "response", None), int(CARD_OUTCOME_CHARS.get()))
    except Exception as exc:
        logger.exception("Agent card update failed; the run result is unaffected")
        telemetry.emit("card_failed", stage="update", error_class=type(exc).__name__)


def _ok(result: Any) -> bool:
    return isinstance(result, dict) and result.get("successful") is True


def _apply_draft_lifecycle(registry, agent_id: int, entry: Dict[str, Any]) -> None:
    tool, args, result = entry.get("tool"), entry.get("arguments") or {}, entry.get("result")
    if tool == "gmail_create_draft" and _ok(result):
        data = result.get("data") or {}
        draft_id = data.get("id")
        if draft_id:
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            recipients = [r for r in [args.get("recipient_email"), *(args.get("cc") or []), *(args.get("extra_recipients") or [])] if r]
            registry.record_draft(agent_id, str(draft_id), message.get("threadId") or args.get("thread_id"),
                                  recipients, args.get("subject"))
    elif tool in ("gmail_execute_draft", "gmail_delete_draft") and _ok(result) and args.get("draft_id"):
        registry.close_draft(str(args["draft_id"]))  # sent or deleted: no longer open


def note_draft_sent(draft_id: Optional[str]) -> None:
    """A draft sent outside an EA run (approve_action) is closed too."""
    if not draft_id:
        return
    try:
        get_agent_registry().close_draft(draft_id)
    except Exception:  # pragma: no cover
        logger.exception("Could not close a sent draft on the agent card")


__all__ = ["AgentCard", "build_card", "note_draft_sent", "render", "render_for_prompt", "update_after_run"]
