"""Human send authorization (task 1.10, flag SEND_GATE; OFF by default and not in `all`).

The ledger (D2) makes a send happen AT MOST ONCE. The gate decides whether it
may happen AT ALL without a human: with SEND_GATE on, the three external-send
tools never execute directly. They store the exact action and arguments as a
pending approval with a short code and return ``approval_required``. Only the
IA tool ``approve_action`` executes it, and only when:

- the current turn is a user turn (origin == "user");
- the code appears in that turn's RAW HTTP-received user message;
- the approval is pending, unexpired and unused.

A compromised or confused model can call ``approve_action`` but cannot forge
the user's HTTP message, so it cannot approve. The STORED arguments are executed
verbatim, whatever arguments the model passes.

Whether this exact UX is the product decision is open (U1); see 02_DESIGN.md.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from ...overload_config import APPROVAL_CODE_LENGTH, APPROVAL_SUMMARY_BODY_CHARS, APPROVAL_TTL_S
from ...utils import clock
from ..context import TurnContext, current_run, current_turn
from . import db

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


@dataclass(frozen=True)
class Approval:
    id: str
    action: str
    tool_name: str
    args: Dict[str, Any]
    fingerprint: str
    summary: str
    code: str
    agent: Optional[str]
    created_turn_id: Optional[str]
    root_origin: Optional[str]
    status: str
    created_at: str
    expires_at: str
    executed_ledger_id: Optional[int]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Approval":
        stored = json.loads(row["args_json"])
        return cls(row["id"], row["action"], stored["tool"], stored["args"], row["fingerprint"], row["summary"],
                   row["code"], row["agent"], row["created_turn_id"], row["root_origin"], row["status"],
                   row["created_at"], row["expires_at"], row["executed_ledger_id"])

    def expired(self) -> bool:
        return clock.now() >= datetime.fromisoformat(self.expires_at)


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(int(APPROVAL_CODE_LENGTH.get())))


def summarize(action: str, args: Dict[str, Any], recipients: Tuple[str, ...], subject: Optional[str]) -> str:
    verb = {"GMAIL_REPLY_TO_THREAD": "Reply", "GMAIL_FORWARD_MESSAGE": "Forward", "GMAIL_SEND_DRAFT": "Send draft"}[action]
    parts = [f"{verb} to {', '.join(recipients) or '(draft recipients)'}"]
    if subject:
        parts.append(f"subject: {subject}")
    if args.get("thread_id"):
        parts.append(f"thread {args['thread_id']}")
    if args.get("message_id"):
        parts.append(f"message {args['message_id']}")
    if args.get("draft_id"):
        parts.append(f"draft {args['draft_id']}")
    body = args.get("message_body") or args.get("additional_text") or ""
    if body:
        limit = int(APPROVAL_SUMMARY_BODY_CHARS.get())
        parts.append("body: " + (body[:limit] + ("…" if len(body) > limit else "")))
    return "; ".join(parts)


def code_in_message(code: str, message: Optional[str]) -> bool:
    if not message:
        return False
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", message.upper()) is not None


class Approvals:
    def create(self, *, tool_name: str, action: str, args: Dict[str, Any], fingerprint: str, summary: str) -> Approval:
        """Store a pending approval; an identical pending one (same fingerprint) is reused, not duplicated."""
        turn = current_turn()
        run = current_run()
        now = clock.now()

        def operation(conn: sqlite3.Connection) -> Approval:
            for row in conn.execute(
                "SELECT * FROM approvals WHERE fingerprint = ? AND status = 'pending' ORDER BY created_at DESC", (fingerprint,)
            ).fetchall():
                existing = Approval.from_row(row)
                if not existing.expired():
                    return existing
            approval_id = "ap_" + uuid.uuid4().hex[:10]
            created = now.isoformat(timespec="microseconds")
            expires = (now + timedelta(seconds=float(APPROVAL_TTL_S.get()))).isoformat(timespec="microseconds")
            conn.execute(
                "INSERT INTO approvals (id, action, args_json, fingerprint, summary, code, agent, created_turn_id, root_origin, "
                "status, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (approval_id, action, json.dumps({"tool": tool_name, "args": args}, sort_keys=True), fingerprint, summary,
                 new_code(), run.agent if run else None, turn.turn_id, turn.root_origin, created, expires),
            )
            return Approval.from_row(conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())

        return db.write(operation)

    def get(self, approval_id: str) -> Optional[Approval]:
        row = db.read(lambda conn: conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())
        return Approval.from_row(row) if row else None

    def check(self, approval_id: str, code: str, turn: Optional[TurnContext] = None) -> Tuple[Optional[Approval], Optional[str]]:
        """(approval, None) if it may execute now, else (approval-or-None, reason)."""
        turn = turn or current_turn()
        approval = self.get(approval_id)
        if approval is None:
            return None, "not_found"
        if approval.status != "pending":
            return approval, "already_used" if approval.status == "executed" else approval.status
        if approval.expired():
            db.write(lambda conn: conn.execute("UPDATE approvals SET status = 'expired' WHERE id = ?", (approval.id,)))
            return approval, "expired"
        if turn.origin != "user":
            return approval, "not_a_user_turn"
        if (code or "").strip().upper() != approval.code:
            return approval, "wrong_code"
        if not code_in_message(approval.code, turn.user_message):
            return approval, "code_not_in_user_message"
        return approval, None

    def mark_executed(self, approval_id: str, ledger_id: Optional[int]) -> bool:
        """Single use: only a pending approval can be marked executed."""

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE approvals SET status = 'executed', executed_ledger_id = ? WHERE id = ? AND status = 'pending'",
                (ledger_id, approval_id),
            )
            return cursor.rowcount == 1

        return db.write(operation)


_approvals = Approvals()


def get_approvals() -> Approvals:
    return _approvals


__all__ = ["Approval", "Approvals", "CODE_ALPHABET", "code_in_message", "get_approvals", "new_code", "summarize"]
