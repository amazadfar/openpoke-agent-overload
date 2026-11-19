"""SQLite access for ``data_dir()/safety.db`` (ledger, trigger claims, approvals).

Rules (same as the existing TriggerStore): one connection per operation,
``timeout=30``, WAL, a process-wide ``threading.Lock`` around writes,
parameterized SQL only. A write that still sees ``database is locked`` after
``SQLITE_BUSY_RETRIES`` attempts raises :class:`SafetyStoreUnavailable`; send
paths treat that as "do not send".
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, TypeVar

from ...overload_config import SQLITE_BUSY_RETRIES, SQLITE_BUSY_SLEEP_S
from ...paths import data_dir

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS side_effects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL,
  near_key TEXT,
  action TEXT NOT NULL,
  target TEXT,
  recipients TEXT NOT NULL,
  body_sha256 TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','sent','failed','unknown')),
  error TEXT,
  agent TEXT, run_id TEXT, turn_id TEXT, root_origin TEXT,
  trigger_id INTEGER, scheduled_for TEXT,
  override_of INTEGER REFERENCES side_effects(id),
  reported_unknown INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_side_effects_active_fp
  ON side_effects(fingerprint) WHERE status IN ('pending','sent','unknown');
CREATE INDEX IF NOT EXISTS ix_side_effects_near ON side_effects(near_key, created_at);
CREATE INDEX IF NOT EXISTS ix_side_effects_run ON side_effects(run_id);

-- Which turns were told that a send was blocked as a duplicate of side_effects.id. An override
-- (confirm_duplicate_of) is accepted only in a LATER user turn than one of these (the user was asked).
CREATE TABLE IF NOT EXISTS side_effect_blocks (
  previous_id INTEGER NOT NULL REFERENCES side_effects(id),
  turn_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (previous_id, turn_id)
);

CREATE TABLE IF NOT EXISTS drafts_meta (
  draft_id TEXT PRIMARY KEY, thread_id TEXT, recipients TEXT NOT NULL,
  subject_norm TEXT, body_sha256 TEXT, agent TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trigger_runs (
  trigger_id INTEGER NOT NULL, scheduled_for TEXT NOT NULL,
  run_id TEXT, status TEXT NOT NULL CHECK (status IN ('claimed','done','failed','abandoned')),
  claimed_at TEXT NOT NULL, finished_at TEXT,
  PRIMARY KEY (trigger_id, scheduled_for)
);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, action TEXT NOT NULL, args_json TEXT NOT NULL,
  fingerprint TEXT NOT NULL, summary TEXT NOT NULL, code TEXT NOT NULL,
  agent TEXT, created_turn_id TEXT, root_origin TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','executed','expired','rejected')),
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, executed_ledger_id INTEGER
);
"""


class SafetyStoreUnavailable(RuntimeError):
    """The safety database could not be written; callers must not perform the side effect."""


_write_lock = threading.Lock()
_init_lock = threading.Lock()  # separate from _write_lock: write() initializes inside its own lock
_initialized: set = set()


def db_path() -> Path:
    """Resolved at call time (new modules never bind the data dir at import)."""
    return data_dir() / "safety.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(path: Path) -> None:
    key = str(path)
    if key in _initialized and path.exists():
        return
    with _init_lock:
        conn = _connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        _initialized.add(key)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    path = db_path()
    _ensure(path)
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


def write(operation: Callable[[sqlite3.Connection], T]) -> T:
    """Run ``operation`` in one IMMEDIATE transaction under the write lock, retrying 'database is locked'.

    BEGIN IMMEDIATE takes SQLite's write lock up front, so a check-then-insert
    inside ``operation`` is atomic across threads and processes.
    """
    attempts = int(SQLITE_BUSY_RETRIES.get())
    last: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            with _write_lock, connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = operation(conn)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                return result
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last = exc
            if attempt + 1 < attempts:
                time.sleep(float(SQLITE_BUSY_SLEEP_S.get()))
    raise SafetyStoreUnavailable(f"safety database unavailable: {last}")


def read(operation: Callable[[sqlite3.Connection], T]) -> T:
    with connection() as conn:
        return operation(conn)


def reset_for_tests() -> None:
    _initialized.clear()
    path = db_path()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


__all__ = ["SafetyStoreUnavailable", "connection", "db_path", "read", "reset_for_tests", "write"]
