"""Durable trigger-occurrence claims (task 1.8, decision 11; single owner).

Today ``next_trigger`` advances only after the run, and the scheduler's
``_in_flight`` set lives in memory, so a crash after an email was sent re-fires
the trigger on restart. With SEND_LEDGER on, the scheduler first INSERTs a
claim keyed by ``(trigger_id, scheduled_for)``. A second claim of the same
occurrence fails. On start, claims still ``claimed`` (the process died mid-run)
become ``abandoned``: they are advanced and REPORTED, never silently re-run and
never silently dropped (U10: at-most-once plus a user-visible report).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import List, Optional

from ...utils import clock
from . import db


@dataclass(frozen=True)
class TriggerClaim:
    trigger_id: int
    scheduled_for: str
    run_id: Optional[str]
    status: str
    claimed_at: str
    finished_at: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TriggerClaim":
        return cls(row["trigger_id"], row["scheduled_for"], row["run_id"], row["status"], row["claimed_at"], row["finished_at"])


def _now() -> str:
    return clock.now().isoformat(timespec="seconds")


class TriggerClaims:
    def claim(self, trigger_id: int, scheduled_for: str, run_id: str) -> bool:
        """INSERT-or-fail on the primary key. False means this occurrence was already claimed."""

        def operation(conn: sqlite3.Connection) -> bool:
            try:
                conn.execute(
                    "INSERT INTO trigger_runs (trigger_id, scheduled_for, run_id, status, claimed_at) VALUES (?, ?, ?, 'claimed', ?)",
                    (trigger_id, scheduled_for, run_id, _now()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

        return db.write(operation)

    def get(self, trigger_id: int, scheduled_for: str) -> Optional[TriggerClaim]:
        row = db.read(lambda conn: conn.execute(
            "SELECT * FROM trigger_runs WHERE trigger_id = ? AND scheduled_for = ?", (trigger_id, scheduled_for)).fetchone())
        return TriggerClaim.from_row(row) if row else None

    def finish(self, trigger_id: int, scheduled_for: str, status: str) -> None:
        if status not in ("done", "failed"):
            raise ValueError(status)
        db.write(lambda conn: conn.execute(
            "UPDATE trigger_runs SET status = ?, finished_at = ? WHERE trigger_id = ? AND scheduled_for = ?",
            (status, _now(), trigger_id, scheduled_for),
        ))

    def sweep_abandoned(self) -> List[TriggerClaim]:
        """claimed -> abandoned (only call at process start: nothing of ours can be running)."""

        def operation(conn: sqlite3.Connection) -> List[TriggerClaim]:
            rows = conn.execute("SELECT * FROM trigger_runs WHERE status = 'claimed' ORDER BY claimed_at").fetchall()
            conn.execute("UPDATE trigger_runs SET status = 'abandoned', finished_at = ? WHERE status = 'claimed'", (_now(),))
            return [TriggerClaim.from_row(r) for r in rows]

        return db.write(operation)

    def all(self) -> List[TriggerClaim]:
        rows = db.read(lambda conn: conn.execute("SELECT * FROM trigger_runs ORDER BY claimed_at").fetchall())
        return [TriggerClaim.from_row(r) for r in rows]

    def clear(self) -> None:
        """DELETE /chat/history clears claims together with triggers (U12: the ledger is kept)."""
        db.write(lambda conn: conn.execute("DELETE FROM trigger_runs"))


_claims = TriggerClaims()


def get_trigger_claims() -> TriggerClaims:
    return _claims


__all__ = ["TriggerClaim", "TriggerClaims", "get_trigger_claims"]
