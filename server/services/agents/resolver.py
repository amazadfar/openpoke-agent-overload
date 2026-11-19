"""Conservative resolve-on-write: code proposes, the model decides (task 2.3, flag RESOLVE_ON_WRITE).

Called by ``send_message_to_agent`` BEFORE dispatch dedupe, so D1 keys on the
canonical agent. Rules, in order (no numeric similarity thresholds anywhere):

1. exact canonical name or alias (and the name is not generic) -> route;
2. ``create_new=true`` -> create (honour the model, U11; log ``route_override``
   if step 3 would have routed);
3. strong identifiers in the name/instructions (Gmail thread, message, draft
   ids) owned by exactly ONE agent, not spawned in this turn, not vetoed ->
   route, and remember the proposed name as an alias;
4. fuzzy evidence (a shared content token in the name, or a shared
   counterparty email) only produces CANDIDATES returned to the IA (``ask``);
   a second call with the same name in the same turn creates (loop guard);
5. otherwise create.

Hard vetoes: agents created earlier in the same turn (parallel spawns such as
"Alice" and "Bob"), and agents whose counterparty emails are disjoint from the
instruction's emails (the two Sarah Chens).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ...overload_config import LLM_REQUEST_TIMEOUT_S, RESOLVER_MAX_CANDIDATES
from ...utils import clock
from ..context import current_turn
from .ids import extract_from_text
from .registry import AgentRow, alias_norm, get_agent_registry, parse_time, self_emails

STOPWORDS = frozenset(
    "email emails mail to re reply draft follow up followup agent task the a an about for with and of on in new".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> List[str]:
    """Content tokens: lowercase alphanumeric runs, stopwords and single characters removed."""
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in STOPWORDS and len(t) > 1]


def is_generic(name: str) -> bool:
    """A name with no content tokens (e.g. "Email follow-up") never auto-routes on the name alone."""
    return not tokens(name)


@dataclass
class Candidate:
    row: AgentRow
    shared_strong: int = 0
    shared_emails: int = 0
    shared_tokens: int = 0
    counterparties: List[str] = field(default_factory=list)

    def sort_key(self) -> Tuple:
        last = parse_time(self.row.last_active_at)
        return (self.shared_strong, self.shared_emails, self.shared_tokens, last.timestamp() if last else 0.0)

    def payload(self) -> Dict[str, Any]:
        return {
            "agent_name": self.row.name,
            "purpose": self.row.latest_request or self.row.purpose,
            "counterparties": self.counterparties,
            "last_active": self.row.last_active_at,
            "runs": self.row.runs,
            "shared": {"identifiers": self.shared_strong, "emails": self.shared_emails, "name_words": self.shared_tokens},
        }


@dataclass
class Resolution:
    action: str  # exact | alias | identifier | exact_after_ask | create | create_new | create_after_ask | ask
    agent_name: Optional[str] = None
    agent_id: Optional[int] = None
    created: bool = False
    candidates: List[Candidate] = field(default_factory=list)
    route_override: bool = False

    def ask_payload(self) -> Dict[str, Any]:
        return {
            "error": "possible_existing_agents",
            "candidates": [c.payload() for c in self.candidates],
            "how_to_proceed": "Call send_message_to_agent again with agent_name set to one of these to reuse it, "
                              "or set create_new=true to start a separate agent.",
        }


_asked: Dict[str, Tuple[float, Set[str]]] = {}


def _remember_ask(turn_id: str, norm: str) -> None:
    # An IA turn lasts at most MAX_TOOL_ITERATIONS LLM calls of at most LLM_REQUEST_TIMEOUT_S each;
    # loop-guard memory older than that can no longer belong to a live turn (derived, not tuned).
    from ...agents.interaction_agent.runtime import InteractionAgentRuntime

    horizon = InteractionAgentRuntime.MAX_TOOL_ITERATIONS * float(LLM_REQUEST_TIMEOUT_S.get())
    now = clock.monotonic()
    for key in [k for k, (at, _) in _asked.items() if now - at > horizon]:
        del _asked[key]
    _asked.setdefault(turn_id, (now, set()))[1].add(norm)


def _was_asked(turn_id: str, norm: str) -> bool:
    entry = _asked.get(turn_id)
    return entry is not None and norm in entry[1]


def _strong_owners(registry, ids, exclude: Set[int]) -> Dict[int, int]:
    counts: Dict[int, int] = {}
    for attr, kind in (("threads", "thread"), ("messages", "message"), ("drafts", "draft"), ("gmail_ids", "gmail_id")):
        for value in getattr(ids, attr):
            for owner in registry.owners_of(kind, value):
                if owner not in exclude:
                    counts[owner] = counts.get(owner, 0) + 1
    return counts


def resolve(proposed: str, instructions: str, create_new: bool) -> Resolution:
    """Decide what to do with a proposed agent name. Pure decision; see resolve_dispatch for the writes."""
    registry = get_agent_registry()
    turn = current_turn()
    norm = alias_norm(proposed)
    existing = registry.get(proposed)
    generic = is_generic(proposed)
    spawned = registry.spawned_in_turn(turn.turn_id)
    mine = self_emails()
    ids = extract_from_text(f"{proposed}\n{instructions}")
    wanted_emails = set(ids.emails) - mine

    def vetoed(agent_id: int) -> bool:
        theirs = registry.emails(agent_id) - mine
        return bool(wanted_emails) and bool(theirs) and not (wanted_emails & theirs)

    # 1. exact / alias
    if existing is not None and not generic:
        action = "exact" if alias_norm(existing.name) == norm else "alias"
        return Resolution(action, existing.name, existing.id)

    owners = _strong_owners(registry, ids, exclude=spawned)
    unique_owner = next(iter(owners)) if len(owners) == 1 else None
    if unique_owner is not None and vetoed(unique_owner):
        unique_owner = None

    # 2. the model explicitly asked for a new agent
    if create_new:
        return Resolution("create_new", proposed, created=True, route_override=unique_owner is not None)

    # 3. identity-level evidence
    if unique_owner is not None:
        row = registry.get_by_id(unique_owner)
        action = "exact" if existing is not None and existing.id == unique_owner else "identifier"
        return Resolution(action, row.name, row.id)

    # 4. fuzzy evidence -> candidates (never an automatic route)
    if _was_asked(turn.turn_id, norm):
        if existing is not None:
            return Resolution("exact_after_ask", existing.name, existing.id)
        return Resolution("create_after_ask", proposed, created=True)
    wanted_tokens = set(tokens(proposed))
    candidates: List[Candidate] = []
    for row in registry.list_all():
        if row.id in spawned or vetoed(row.id):
            continue
        name_tokens = set(tokens(row.name))
        for alias in registry.aliases(row.id):
            name_tokens |= set(tokens(alias))
        theirs = registry.emails(row.id) - mine
        candidate = Candidate(
            row=row,
            shared_strong=owners.get(row.id, 0),
            shared_emails=len(wanted_emails & theirs),
            shared_tokens=len(wanted_tokens & name_tokens),
            counterparties=sorted(theirs)[:5],
        )
        is_exact_generic = existing is not None and existing.id == row.id
        if candidate.shared_strong or candidate.shared_emails or candidate.shared_tokens or is_exact_generic:
            candidates.append(candidate)
    if candidates:
        candidates.sort(key=Candidate.sort_key, reverse=True)
        return Resolution("ask", candidates=candidates[: int(RESOLVER_MAX_CANDIDATES.get())])

    # 5. nothing similar
    return Resolution("create", proposed, created=True)


def _unique_name(registry, proposed: str) -> str:
    if registry.get(proposed) is None:
        return proposed
    n = 2
    while registry.get(f"{proposed} #{n}") is not None:
        n += 1
    return f"{proposed} #{n}"


def resolve_dispatch(proposed: str, instructions: str, create_new: bool) -> Resolution:
    """Resolve and apply: create/alias/revive in the registry, record the turn spawn, remember asks."""
    from ..execution import get_agent_roster

    registry = get_agent_registry()
    turn = current_turn()
    resolution = resolve(proposed, instructions, create_new)
    if resolution.action == "ask":
        _remember_ask(turn.turn_id, alias_norm(proposed))
        return resolution
    if resolution.created:
        name = _unique_name(registry, resolution.agent_name or proposed)
        row, _ = registry.create(name, turn_id=turn.turn_id)
        get_agent_roster().add_agent(row.name)  # dual-write roster.json with the canonical name
        resolution.agent_name, resolution.agent_id = row.name, row.id
        return resolution
    if resolution.action == "identifier" and registry.get(proposed) is None:
        registry.add_alias(resolution.agent_id, proposed)
    registry.touch(resolution.agent_id)  # routing to a dormant agent revives it (2.5)
    return resolution


def reset_for_tests() -> None:
    _asked.clear()


__all__ = ["Candidate", "Resolution", "STOPWORDS", "is_generic", "resolve", "resolve_dispatch", "tokens"]
