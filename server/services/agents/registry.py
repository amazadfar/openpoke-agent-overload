"""Agent registry: identity, aliases, identifiers, lifecycle (task 2.1, flag AGENT_REGISTRY; single owner).

Today an agent is only a free-text name in ``roster.json`` (M1). The registry
gives each agent a row (canonical first-used name, journal slug, purpose,
last outcome, run count, last activity), its aliases, and the identifiers its
tool calls touched (threads, messages, drafts, counterparty emails). The agent
card (2.2), the resolver (2.3) and the Top-K roster (2.4) all read it.

Storage: ``data_dir()/execution_agents/registry.db`` (Appendix D, plus two
small additions documented in 02_DESIGN.md: ``agents.latest_request`` /
``agents.last_outcome_at`` and a ``drafts`` table for card draft metadata).

Compatibility: ``roster.json`` stays authoritative with the flag off and the
registry is untouched. With the flag on the roster facade is backed by the
registry and dual-writes canonical names to ``roster.json``.

Migration is idempotent and writes NOTHING except registry.db (I7); every
pre-existing roster name and trigger owner stays resolvable (I8).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ...overload_config import CARD_PURPOSE_CHARS, DORMANCY_DAYS
from ...paths import data_dir
from ...utils import clock
from ..execution.log_store import _slugify
from .ids import EMPTY, ExtractedIds, extract_from_text

SCHEMA_VERSION = "1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  slug TEXT NOT NULL UNIQUE,
  purpose TEXT, latest_request TEXT, last_outcome TEXT, last_outcome_at TEXT,
  runs INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, last_active_at TEXT,
  created_turn_id TEXT
);
CREATE TABLE IF NOT EXISTS aliases (
  alias_norm TEXT PRIMARY KEY,
  agent_id INTEGER NOT NULL REFERENCES agents(id)
);
CREATE TABLE IF NOT EXISTS identifiers (
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  kind TEXT NOT NULL CHECK (kind IN ('email','thread','message','draft','gmail_id','trigger')),
  value TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open',
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  PRIMARY KEY (agent_id, kind, value)
);
CREATE INDEX IF NOT EXISTS ix_identifiers_lookup ON identifiers(kind, value);
CREATE TABLE IF NOT EXISTS turn_spawns (
  turn_id TEXT NOT NULL, agent_id INTEGER NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (turn_id, agent_id)
);
CREATE TABLE IF NOT EXISTS drafts (
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  draft_id TEXT NOT NULL, thread_id TEXT, recipients TEXT NOT NULL, subject TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (agent_id, draft_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_PUNCT = re.compile(r"[^\w\s]")
_ID_KINDS = (("threads", "thread"), ("messages", "message"), ("drafts", "draft"), ("emails", "email"), ("gmail_ids", "gmail_id"))


def alias_norm(name: str) -> str:
    """Lowercase, punctuation stripped, whitespace collapsed."""
    return " ".join(_PUNCT.sub(" ", (name or "").lower()).split())


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now() -> str:
    return _iso(clock.now())


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class AgentRow:
    id: int
    name: str
    slug: str
    purpose: Optional[str]
    latest_request: Optional[str]
    last_outcome: Optional[str]
    last_outcome_at: Optional[str]
    runs: int
    created_at: str
    last_active_at: Optional[str]
    created_turn_id: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AgentRow":
        return cls(row["id"], row["name"], row["slug"], row["purpose"], row["latest_request"], row["last_outcome"],
                   row["last_outcome_at"], row["runs"], row["created_at"], row["last_active_at"], row["created_turn_id"])


@dataclass(frozen=True)
class IdentifierRow:
    agent_id: int
    kind: str
    value: str
    state: str
    first_seen: str
    last_seen: str


class AgentRegistry:
    def __init__(self) -> None:
        self._write_lock = threading.Lock()
        self._initialized_path: Optional[str] = None

    # ------------------------------------------------------------- storage
    @staticmethod
    def path() -> Path:
        return data_dir() / "execution_agents" / "registry.db"

    def _connect(self) -> sqlite3.Connection:
        path = self.path()
        if self._initialized_path != str(path) or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, timeout=30, isolation_level=None)
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.executescript(SCHEMA)
            finally:
                conn.close()
            self._initialized_path = str(path)
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _read(self, sql: str, params: Sequence = ()) -> List[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _write(self, operation):
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = operation(conn)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                return result
            finally:
                conn.close()

    # ------------------------------------------------------------- agents
    def get(self, name_or_alias: str) -> Optional[AgentRow]:
        rows = self._read("SELECT a.* FROM aliases al JOIN agents a ON a.id = al.agent_id WHERE al.alias_norm = ?",
                          (alias_norm(name_or_alias),))
        if rows:
            return AgentRow.from_row(rows[0])
        rows = self._read("SELECT * FROM agents WHERE slug = ?", (_slugify(name_or_alias),))
        return AgentRow.from_row(rows[0]) if rows else None

    def get_by_id(self, agent_id: int) -> Optional[AgentRow]:
        rows = self._read("SELECT * FROM agents WHERE id = ?", (agent_id,))
        return AgentRow.from_row(rows[0]) if rows else None

    def create(self, name: str, turn_id: Optional[str] = None) -> Tuple[AgentRow, bool]:
        """Create an agent. A name whose slug collides becomes an alias of that agent (they share a journal)."""
        name = name.strip() or "agent"

        def operation(conn: sqlite3.Connection) -> Tuple[int, bool]:
            existing = conn.execute("SELECT id FROM agents WHERE slug = ?", (_slugify(name),)).fetchone()
            if existing is not None:
                conn.execute("INSERT OR IGNORE INTO aliases (alias_norm, agent_id) VALUES (?, ?)", (alias_norm(name), existing["id"]))
                return existing["id"], False
            taken = conn.execute("SELECT agent_id FROM aliases WHERE alias_norm = ?", (alias_norm(name),)).fetchone()
            if taken is not None:
                return taken["agent_id"], False
            now = _now()
            cursor = conn.execute(
                "INSERT INTO agents (name, slug, runs, created_at, last_active_at, created_turn_id) VALUES (?, ?, 0, ?, ?, ?)",
                (name, _slugify(name), now, now, turn_id),
            )
            agent_id = int(cursor.lastrowid)
            conn.execute("INSERT OR IGNORE INTO aliases (alias_norm, agent_id) VALUES (?, ?)", (alias_norm(name), agent_id))
            if turn_id:
                conn.execute("INSERT OR IGNORE INTO turn_spawns (turn_id, agent_id, created_at) VALUES (?, ?, ?)", (turn_id, agent_id, now))
            return agent_id, True

        agent_id, created = self._write(operation)
        return self.get_by_id(agent_id), created

    def add_alias(self, agent_id: int, name: str) -> bool:
        """Add an alias unless it already points at another agent."""

        def operation(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT agent_id FROM aliases WHERE alias_norm = ?", (alias_norm(name),)).fetchone()
            if row is not None:
                return row["agent_id"] == agent_id
            conn.execute("INSERT INTO aliases (alias_norm, agent_id) VALUES (?, ?)", (alias_norm(name), agent_id))
            return True

        return self._write(operation)

    def aliases(self, agent_id: int) -> List[str]:
        return [r["alias_norm"] for r in self._read("SELECT alias_norm FROM aliases WHERE agent_id = ? ORDER BY alias_norm", (agent_id,))]

    def all_aliases(self) -> Dict[int, List[str]]:
        out: Dict[int, List[str]] = {}
        for r in self._read("SELECT agent_id, alias_norm FROM aliases ORDER BY alias_norm"):
            out.setdefault(r["agent_id"], []).append(r["alias_norm"])
        return out

    def all_emails(self) -> Dict[int, List[str]]:
        out: Dict[int, List[str]] = {}
        for r in self._read("SELECT agent_id, value FROM identifiers WHERE kind = 'email' ORDER BY last_seen DESC"):
            out.setdefault(r["agent_id"], []).append(r["value"])
        return out

    def list_all(self) -> List[AgentRow]:
        return [AgentRow.from_row(r) for r in self._read("SELECT * FROM agents ORDER BY id")]

    def touch(self, agent_id: int) -> None:
        """Mark active now (also revives a dormant agent)."""
        self._write(lambda conn: conn.execute("UPDATE agents SET last_active_at = ? WHERE id = ?", (_now(), agent_id)))

    def set_purpose(self, agent_id: int, first: Optional[str], latest: Optional[str]) -> None:
        limit = int(CARD_PURPOSE_CHARS.get())
        self._write(lambda conn: conn.execute(
            "UPDATE agents SET purpose = COALESCE(purpose, ?), latest_request = COALESCE(?, latest_request) WHERE id = ?",
            ((first or "")[:limit] or None, (latest or "")[:limit] or None, agent_id),
        ))

    def record_run(self, agent_id: int, outcome: Optional[str], outcome_chars: int) -> None:
        now = _now()
        self._write(lambda conn: conn.execute(
            "UPDATE agents SET runs = runs + 1, last_active_at = ?, last_outcome = ?, last_outcome_at = ? WHERE id = ?",
            (now, (outcome or "")[:outcome_chars] or None, now, agent_id),
        ))

    # ------------------------------------------------------------- spawns
    def spawned_in_turn(self, turn_id: str) -> Set[int]:
        return {r["agent_id"] for r in self._read("SELECT agent_id FROM turn_spawns WHERE turn_id = ?", (turn_id,))}

    # ------------------------------------------------------------- identifiers
    def record_identifiers(self, agent_id: int, ids: ExtractedIds, *, exclude_emails: Iterable[str] = (),
                           seen_at: Optional[str] = None) -> None:
        excluded = {e.lower() for e in exclude_emails}
        stamp = seen_at or _now()
        values = []
        for attr, kind in _ID_KINDS:
            for value in getattr(ids, attr):
                if kind == "email" and value.lower() in excluded:
                    continue
                values.append((agent_id, kind, value, stamp, stamp))
        if not values:
            return

        def operation(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "INSERT INTO identifiers (agent_id, kind, value, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id, kind, value) DO UPDATE SET last_seen = MAX(identifiers.last_seen, excluded.last_seen)",
                values,
            )

        self._write(operation)

    def identifiers(self, agent_id: int, kind: Optional[str] = None) -> List[IdentifierRow]:
        sql = "SELECT * FROM identifiers WHERE agent_id = ?"
        params: List = [agent_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY last_seen DESC, value"
        return [IdentifierRow(r["agent_id"], r["kind"], r["value"], r["state"], r["first_seen"], r["last_seen"])
                for r in self._read(sql, params)]

    def owners_of(self, kind: str, value: str) -> Set[int]:
        """Agents that hold an identifier. A text-derived ``gmail_id`` matches threads, messages and gmail ids."""
        kinds = ("thread", "message", "gmail_id") if kind in ("gmail_id", "thread", "message") else (kind,)
        placeholders = ",".join("?" for _ in kinds)
        rows = self._read(f"SELECT DISTINCT agent_id FROM identifiers WHERE value = ? AND kind IN ({placeholders})",
                          (value, *kinds))
        return {r["agent_id"] for r in rows}

    def emails(self, agent_id: int) -> Set[str]:
        return {r.value for r in self.identifiers(agent_id, "email")}

    def close_draft(self, draft_id: str, agent_id: Optional[int] = None) -> None:
        sql = "UPDATE identifiers SET state = 'closed' WHERE kind = 'draft' AND value = ?"
        params: List = [draft_id]
        if agent_id is not None:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        self._write(lambda conn: conn.execute(sql, params))

    def record_draft(self, agent_id: int, draft_id: str, thread_id: Optional[str], recipients: Sequence[str],
                     subject: Optional[str]) -> None:
        self._write(lambda conn: conn.execute(
            "INSERT OR REPLACE INTO drafts (agent_id, draft_id, thread_id, recipients, subject, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, draft_id, thread_id, json.dumps(list(recipients)), subject, _now()),
        ))

    def draft_details(self, agent_id: int) -> Dict[str, Dict[str, object]]:
        return {r["draft_id"]: {"thread": r["thread_id"], "recipients": json.loads(r["recipients"]), "subject": r["subject"]}
                for r in self._read("SELECT * FROM drafts WHERE agent_id = ?", (agent_id,))}

    # ------------------------------------------------------------- dormancy (2.5)
    def is_dormant(self, row: AgentRow, trigger_owners: Set[str], now: Optional[datetime] = None) -> bool:
        """Derived lazily: idle for DORMANCY_DAYS and owning no active trigger. Nothing is deleted."""
        if row.name in trigger_owners or any(alias_norm(o) == alias_norm(row.name) for o in trigger_owners):
            return False
        last = parse_time(row.last_active_at) or parse_time(row.created_at)
        if last is None:
            return False
        return last < (now or clock.now()) - timedelta(days=float(DORMANCY_DAYS.get()))

    # ------------------------------------------------------------- meta / migration
    def meta(self, key: str) -> Optional[str]:
        rows = self._read("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def _set_meta(self, key: str, value: str) -> None:
        self._write(lambda conn: conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)))

    def migrate_if_needed(self) -> bool:
        """Import roster.json, trigger owners and journals once. Returns True if it ran."""
        if self.meta("schema_version") == SCHEMA_VERSION:
            return False
        from ..execution.roster import _ROSTER_PATH

        names = _read_roster_file(_ROSTER_PATH)
        for name in names:
            self._import(name)
        for owner in _trigger_owners():
            if self.get(owner) is None:
                self._import(owner)
        registered = {row.slug for row in self.list_all()}
        journal_dir = _ROSTER_PATH.parent
        unregistered = sorted(p.stem for p in journal_dir.glob("*.log") if p.stem not in registered) if journal_dir.exists() else []
        self._set_meta("unregistered_journals", json.dumps(unregistered))
        self._set_meta("migrated_at", _now())
        self._set_meta("schema_version", SCHEMA_VERSION)
        return True

    def sync_from_roster(self, names: Iterable[str]) -> List[str]:
        """Import roster names the registry does not know (written while the flag was off, or seeded)."""
        known_aliases = {r["alias_norm"] for r in self._read("SELECT alias_norm FROM aliases")}
        known_slugs = {r["slug"] for r in self._read("SELECT slug FROM agents")}
        imported = []
        for name in names:  # one query for the whole roster instead of one per name (runs on every dispatch)
            if alias_norm(name) in known_aliases or _slugify(name) in known_slugs:
                continue
            row = self._import(name)
            known_aliases.add(alias_norm(name))
            known_slugs.add(row.slug)
            imported.append(name)
        return imported

    def _import(self, name: str) -> AgentRow:
        row, created = self.create(name)
        if created:
            self.index_journal(row)
        return row

    def index_journal(self, row: AgentRow) -> None:
        """Best-effort identifiers, purpose, runs and last activity from an agent's journal (read only)."""
        from ..execution import get_execution_agent_logs

        entries = list(get_execution_agent_logs().iter_entries(row.name))
        if not entries:
            return
        ids = EMPTY
        first_request = None
        runs = 0
        for tag, _, payload in entries:
            if tag == "tool_ids":
                ids = ids.merge(ExtractedIds.from_journal(payload))
            elif tag in ("tool_response", "agent_action"):
                ids = ids.merge(extract_from_text(payload))
            elif tag == "agent_request" and first_request is None:
                first_request = payload
            elif tag == "agent_response":
                runs += 1
        last_seen = _journal_time_to_iso(entries[-1][1])
        self.record_identifiers(row.id, ids, exclude_emails=self_emails(), seen_at=last_seen)
        limit = int(CARD_PURPOSE_CHARS.get())
        self._write(lambda conn: conn.execute(
            "UPDATE agents SET purpose = COALESCE(purpose, ?), runs = ?, last_active_at = COALESCE(?, last_active_at) WHERE id = ?",
            ((first_request or "")[:limit] or None, runs, last_seen, row.id),
        ))

    def clear(self) -> None:
        """DELETE /chat/history: drop every agent (journals are cleared by their own store)."""

        def operation(conn: sqlite3.Connection) -> None:
            for table in ("identifiers", "aliases", "turn_spawns", "drafts", "agents", "meta"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))

        self._write(operation)


def _read_roster_file(path: Path) -> List[str]:
    """Read roster.json WITHOUT AgentRoster.load(), which would create the file (I7)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []
    return [str(name) for name in data] if isinstance(data, list) else []


def _trigger_owners() -> List[str]:
    from ..triggers import get_trigger_service

    return get_trigger_service().agent_names()


def active_trigger_owners() -> Set[str]:
    from ..triggers import get_trigger_service

    return set(get_trigger_service().agent_names(active_only=True))


def _journal_time_to_iso(timestamp: str) -> Optional[str]:
    """Journal timestamps are naive local times in the user's timezone."""
    if not timestamp:
        return None
    try:
        from ...utils.timezones import resolve_user_timezone

        local = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=resolve_user_timezone())
        return _iso(local)
    except Exception:
        return None


def self_emails() -> Set[str]:
    """The connected account's own address(es): never a counterparty."""
    found: Set[str] = set()
    try:
        from ... import flags

        if flags.fake_gmail_enabled():
            from ...testing.fake_gmail import get_fake_gmail

            found.add(get_fake_gmail().account_email.lower())
        from ..gmail import client as gmail_client

        with gmail_client._PROFILE_CACHE_LOCK:  # noqa: SLF001
            for entry in gmail_client._PROFILE_CACHE.values():  # noqa: SLF001
                email = gmail_client._extract_email(entry.get("profile"))  # noqa: SLF001
                if email:
                    found.add(email.lower())
    except Exception:
        pass
    return found


_registry: Optional[AgentRegistry] = None


def get_agent_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry


def reset_for_tests() -> None:
    global _registry
    _registry = None
    path = AgentRegistry.path()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


__all__ = ["AgentRegistry", "AgentRow", "IdentifierRow", "active_trigger_owners", "alias_norm", "get_agent_registry",
           "parse_time", "self_emails"]
