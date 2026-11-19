"""Side-effect ledger: at-most-once external sends and honest status (D2, task 1.8; single owner).

Every external send (send draft, reply, forward) gets an identity BEFORE it
runs. The send happens only if :meth:`SideEffectLedger.begin` returns
``Started``; its outcome is then recorded with :meth:`finish`:

    pending --> sent      the normalized response reports success
            \\-> failed    an explicit unsuccessful response (nothing went out)
            \\-> unknown   exception / timeout / cancellation / process died

Invariants enforced here:
- I2: a partial UNIQUE index on ``fingerprint WHERE status IN (pending, sent,
  unknown)`` makes "the same send twice" impossible at the database level.
- I3: ``unknown`` is never retried automatically: it stays in the unique index,
  so a retry is Blocked until a user-originated override names it explicitly.
- Near duplicates (same thread + recipients, different body, within 24 h)
  return ConfirmationRequired instead of sending.

``confirm_duplicate_of`` is DUPLICATE protection, not authorization: it is only
honoured in a turn started by a user message (origin "user", not merely a
user-rooted chain), against a record created in an earlier turn, and only after
a block for that record was reported to an EARLIER turn (``side_effect_blocks``).
So neither an agent-started follow-up turn nor the run that just received the
block can confirm a duplicate on the user's behalf; an up-front "resend even if
it is a duplicate" costs one extra confirmation.
Human authorization is the send gate (1.10).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ...overload_config import NEAR_DUP_WINDOW_S, PENDING_STALE_AFTER_S
from ...utils import clock
from ..context import current_run, current_turn
from . import db

EXTERNAL_ACTIONS = ("GMAIL_SEND_DRAFT", "GMAIL_REPLY_TO_THREAD", "GMAIL_FORWARD_MESSAGE")
ACTIVE_STATUSES = ("pending", "sent", "unknown")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_SUBJECT_PREFIX_RE = re.compile(r"^\s*((re|fwd?|fw)\s*:\s*)+", re.IGNORECASE)


# --------------------------------------------------------------------------- canonical forms
def normalize_recipients(*values: Any) -> Tuple[str, ...]:
    """Lowercase, deduplicate, sort; accepts strings ("Name <a@b>"), lists, None."""
    found: List[str] = []
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple, set)) else [value]
        for item in items:
            found.extend(m.lower() for m in _EMAIL_RE.findall(str(item)))
    return tuple(sorted(set(found)))


def normalize_body(text: Optional[str]) -> str:
    return " ".join((text or "").replace("\r\n", "\n").replace("\r", "\n").split())


def body_sha(text: Optional[str]) -> str:
    return hashlib.sha256(normalize_body(text).encode("utf-8")).hexdigest()


def normalize_subject(subject: Optional[str]) -> Optional[str]:
    if subject is None:
        return None
    return " ".join(_SUBJECT_PREFIX_RE.sub("", subject).lower().split())


def _now_iso() -> str:
    return clock.now().isoformat(timespec="microseconds")


@dataclass(frozen=True)
class DraftMeta:
    draft_id: str
    thread_id: Optional[str]
    recipients: Tuple[str, ...]
    subject_norm: Optional[str]
    body_sha256: Optional[str]


@dataclass(frozen=True)
class SendIntent:
    action: str
    target: Optional[str]  # thread id, message id, "draft:<id>" (no metadata) or None (new email)
    recipients: Tuple[str, ...]
    subject_norm: Optional[str]
    body_sha256: Optional[str]
    draft_id: Optional[str] = None
    has_draft_meta: bool = True


def intent_for(action: str, args: Dict[str, Any], draft_meta: Optional[DraftMeta]) -> SendIntent:
    if action == "GMAIL_REPLY_TO_THREAD":
        return SendIntent(
            action=action,
            target=args.get("thread_id"),
            recipients=normalize_recipients(args.get("recipient_email"), args.get("cc"), args.get("bcc"),
                                            args.get("extra_recipients")),
            subject_norm=None,
            body_sha256=body_sha(args.get("message_body")),
        )
    if action == "GMAIL_FORWARD_MESSAGE":
        return SendIntent(
            action=action,
            target=args.get("message_id"),
            recipients=normalize_recipients(args.get("recipient_email")),
            subject_norm=None,
            body_sha256=body_sha(args.get("additional_text")),
        )
    if action == "GMAIL_SEND_DRAFT":
        draft_id = args.get("draft_id")
        if draft_meta is None:  # a draft created outside OpenPoke: identity is the draft id only
            return SendIntent(action, f"draft:{draft_id}", (), None, None, draft_id=draft_id, has_draft_meta=False)
        return SendIntent(
            action=action,
            target=draft_meta.thread_id,
            recipients=draft_meta.recipients,
            subject_norm=draft_meta.subject_norm,
            body_sha256=draft_meta.body_sha256,
            draft_id=draft_id,
        )
    raise ValueError(f"not an external send action: {action}")


def fingerprint(intent: SendIntent) -> str:
    """sha256 of the canonical JSON of what would be sent (the draft id only when there is no metadata)."""
    canonical = {
        "action": intent.action,
        "target": intent.target,
        "recipients": list(intent.recipients),
        "subject": intent.subject_norm,
        "body": intent.body_sha256,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode("utf-8")).hexdigest()


def near_key(intent: SendIntent) -> Optional[str]:
    """Reply and send-draft share the ``email|`` space, so a regenerated draft to the same thread is caught."""
    recipients = ",".join(intent.recipients)
    if intent.action == "GMAIL_FORWARD_MESSAGE":
        return f"forward|{intent.target}|{recipients}"
    if intent.action == "GMAIL_SEND_DRAFT" and not intent.has_draft_meta:
        return None
    if intent.target:
        return f"email|{intent.target}|{recipients}"
    return f"email|new|{recipients}|{intent.subject_norm or ''}"


# --------------------------------------------------------------------------- rows and results
@dataclass(frozen=True)
class SideEffect:
    id: int
    fingerprint: str
    near_key: Optional[str]
    action: str
    target: Optional[str]
    recipients: Tuple[str, ...]
    body_sha256: Optional[str]
    status: str
    error: Optional[str]
    agent: Optional[str]
    run_id: Optional[str]
    turn_id: Optional[str]
    root_origin: Optional[str]
    override_of: Optional[int]
    reported_unknown: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SideEffect":
        return cls(
            id=row["id"], fingerprint=row["fingerprint"], near_key=row["near_key"], action=row["action"],
            target=row["target"], recipients=tuple(json.loads(row["recipients"])), body_sha256=row["body_sha256"],
            status=row["status"], error=row["error"], agent=row["agent"], run_id=row["run_id"],
            turn_id=row["turn_id"], root_origin=row["root_origin"], override_of=row["override_of"],
            reported_unknown=bool(row["reported_unknown"]), created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def public(self) -> Dict[str, Any]:
        """What the model sees about a previous record (ids, status, time, recipients; never content)."""
        return {"id": self.id, "status": self.status, "at": self.created_at, "action": self.action,
                "target": self.target, "recipients": list(self.recipients)}


@dataclass(frozen=True)
class Started:
    record_id: int
    override_of: Optional[int] = None


@dataclass(frozen=True)
class Blocked:
    previous: SideEffect
    override_rejected: bool = False


@dataclass(frozen=True)
class ConfirmationRequired:
    previous: SideEffect
    override_rejected: bool = False


BeginResult = Union[Started, Blocked, ConfirmationRequired]


@dataclass
class RunSideEffects:
    sent: List[SideEffect] = field(default_factory=list)
    in_doubt: List[SideEffect] = field(default_factory=list)  # pending or unknown
    failed: List[SideEffect] = field(default_factory=list)


def describe_side_effect(row: SideEffect, *, with_time: bool = False) -> str:
    to = ", ".join(row.recipients) or "the draft's recipients"
    if row.action == "GMAIL_REPLY_TO_THREAD":
        text = f"the reply on thread {row.target} to {to}"
    elif row.action == "GMAIL_FORWARD_MESSAGE":
        text = f"the forward of message {row.target} to {to}"
    else:
        text = f"sending a draft to {to}"
    if with_time:
        text += f" (status {row.status}, {row.created_at[:19].replace('T', ' ')} UTC)"
    return text


# --------------------------------------------------------------------------- the ledger
class SideEffectLedger:
    """All methods are synchronous and thread safe (Gmail executor threads use them)."""

    def begin(self, intent: SendIntent, *, confirm_duplicate_of: Optional[int] = None) -> BeginResult:
        turn = current_turn()
        run = current_run()
        # Overrides are duplicate protection only. Honour one only from a turn started by a
        # user message, against a record from an EARLIER turn, and only after a block for that
        # record was reported to an EARLIER turn. A follow-up turn started by an agent result
        # (origin "agent", even in a user-rooted chain) and the run that just received the
        # block both get ConfirmationRequired/Blocked back, so the IA has to ask the user.
        fresh_user_turn = turn.origin == "user"

        def turn_rule_ok(conn: sqlite3.Connection, previous: SideEffect) -> bool:
            if not fresh_user_turn or previous.turn_id == turn.turn_id:
                return False
            return conn.execute(
                "SELECT 1 FROM side_effect_blocks WHERE previous_id = ? AND turn_id != ? LIMIT 1",
                (previous.id, turn.turn_id),
            ).fetchone() is not None

        def refuse(conn: sqlite3.Connection, previous: SideEffect, kind: type) -> BeginResult:
            rejected = confirm_duplicate_of is not None and not turn_rule_ok(conn, previous)
            conn.execute(  # this turn has now been told; a later user turn may confirm
                "INSERT OR IGNORE INTO side_effect_blocks (previous_id, turn_id, created_at) VALUES (?, ?, ?)",
                (previous.id, turn.turn_id, now.isoformat(timespec="microseconds")),
            )
            return kind(previous, override_rejected=rejected)

        fp = fingerprint(intent)
        nk = near_key(intent)
        now = clock.now()
        cutoff = (now - timedelta(seconds=float(NEAR_DUP_WINDOW_S.get()))).isoformat(timespec="microseconds")

        def operation(conn: sqlite3.Connection) -> BeginResult:
            exact = conn.execute(
                "SELECT * FROM side_effects WHERE fingerprint = ? AND status IN ('pending','sent','unknown')", (fp,)
            ).fetchone()
            row_fp, override_of = fp, None
            if exact is not None:
                previous = SideEffect.from_row(exact)
                if not (confirm_duplicate_of == previous.id and turn_rule_ok(conn, previous)):
                    return refuse(conn, previous, Blocked)
                row_fp, override_of = f"{fp}#override:{previous.id}", previous.id
            elif nk is not None:
                near = conn.execute(
                    "SELECT * FROM side_effects WHERE near_key = ? AND created_at >= ? "
                    "AND status IN ('pending','sent','unknown') ORDER BY id DESC LIMIT 1",
                    (nk, cutoff),
                ).fetchone()
                if near is not None:
                    previous = SideEffect.from_row(near)
                    if not (confirm_duplicate_of == previous.id and turn_rule_ok(conn, previous)):
                        return refuse(conn, previous, ConfirmationRequired)
                    override_of = previous.id
            trigger_id, scheduled_for = (run.trigger_occurrence if run and run.trigger_occurrence else (None, None))
            stamp = now.isoformat(timespec="microseconds")
            try:
                cursor = conn.execute(
                    "INSERT INTO side_effects (fingerprint, near_key, action, target, recipients, body_sha256, status, "
                    "agent, run_id, turn_id, root_origin, trigger_id, scheduled_for, override_of, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row_fp, nk, intent.action, intent.target, json.dumps(list(intent.recipients)), intent.body_sha256,
                     run.agent if run else None, run.run_id if run else None, turn.turn_id, turn.root_origin,
                     trigger_id, scheduled_for, override_of, stamp, stamp),
                )
            except sqlite3.IntegrityError:
                # The unique index caught a concurrent identical send (I2).
                existing = conn.execute(
                    "SELECT * FROM side_effects WHERE fingerprint = ? AND status IN ('pending','sent','unknown')", (row_fp,)
                ).fetchone()
                return refuse(conn, SideEffect.from_row(existing), Blocked)
            return Started(record_id=int(cursor.lastrowid), override_of=override_of)

        return db.write(operation)

    def finish(self, record_id: int, status: str, error: Optional[str] = None) -> None:
        if status not in ("sent", "failed", "unknown"):
            raise ValueError(status)
        db.write(lambda conn: conn.execute(
            "UPDATE side_effects SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, (error or None) and str(error)[:500], _now_iso(), record_id),
        ))

    def get(self, record_id: int) -> Optional[SideEffect]:
        row = db.read(lambda conn: conn.execute("SELECT * FROM side_effects WHERE id = ?", (record_id,)).fetchone())
        return SideEffect.from_row(row) if row else None

    def all(self) -> List[SideEffect]:
        rows = db.read(lambda conn: conn.execute("SELECT * FROM side_effects ORDER BY id").fetchall())
        return [SideEffect.from_row(r) for r in rows]

    def run_summary(self, run_id: str) -> RunSideEffects:
        rows = db.read(lambda conn: conn.execute(
            "SELECT * FROM side_effects WHERE run_id = ? ORDER BY id", (run_id,)).fetchall())
        summary = RunSideEffects()
        for row in map(SideEffect.from_row, rows):
            if row.status == "sent":
                summary.sent.append(row)
            elif row.status in ("pending", "unknown"):
                summary.in_doubt.append(row)
            else:
                summary.failed.append(row)
        return summary

    def mark_reported_unknown(self, record_ids: Sequence[int]) -> None:
        if not record_ids:
            return
        placeholders = ",".join("?" for _ in record_ids)
        db.write(lambda conn: conn.execute(
            f"UPDATE side_effects SET reported_unknown = 1 WHERE id IN ({placeholders})", tuple(record_ids)))

    def sweep_stale_pending(self, older_than_s: Optional[float] = None) -> int:
        """pending -> unknown for rows older than the threshold (process start uses 0)."""
        threshold = float(PENDING_STALE_AFTER_S.get()) if older_than_s is None else float(older_than_s)
        cutoff = (clock.now() - timedelta(seconds=threshold)).isoformat(timespec="microseconds")

        def operation(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "UPDATE side_effects SET status = 'unknown', error = COALESCE(error, 'outcome lost (stale pending)'), "
                "updated_at = ? WHERE status = 'pending' AND updated_at <= ?",
                (_now_iso(), cutoff),
            )
            return cursor.rowcount

        return db.write(operation)

    # ---------------------------------------------------------------- drafts
    def record_draft(self, draft_id: str, thread_id: Optional[str], recipients: Iterable[str],
                     subject_norm: Optional[str], body_sha256: Optional[str], agent: Optional[str] = None) -> None:
        db.write(lambda conn: conn.execute(
            "INSERT OR REPLACE INTO drafts_meta (draft_id, thread_id, recipients, subject_norm, body_sha256, agent, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (draft_id, thread_id, json.dumps(list(recipients)), subject_norm, body_sha256, agent, _now_iso()),
        ))

    def draft_meta(self, draft_id: Optional[str]) -> Optional[DraftMeta]:
        if not draft_id:
            return None
        row = db.read(lambda conn: conn.execute("SELECT * FROM drafts_meta WHERE draft_id = ?", (draft_id,)).fetchone())
        if row is None:
            return None
        return DraftMeta(row["draft_id"], row["thread_id"], tuple(json.loads(row["recipients"])), row["subject_norm"],
                         row["body_sha256"])


_ledger = SideEffectLedger()


def get_ledger() -> SideEffectLedger:
    return _ledger


def reset_for_tests() -> None:
    db.reset_for_tests()


__all__ = [
    "Blocked",
    "ConfirmationRequired",
    "DraftMeta",
    "RunSideEffects",
    "SendIntent",
    "SideEffect",
    "SideEffectLedger",
    "Started",
    "body_sha",
    "describe_side_effect",
    "fingerprint",
    "get_ledger",
    "intent_for",
    "near_key",
    "normalize_body",
    "normalize_recipients",
    "normalize_subject",
]
