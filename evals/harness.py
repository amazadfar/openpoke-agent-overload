"""Shared eval helpers: fake-mode guard, drain, gold-label resolution, synthetic seeding.

Importing this module does not import ``server``; the runner sets the
environment first (data dir, fake Gmail) and imports ``server`` afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

EVALS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVALS_DIR / "fixtures"
SCENARIOS_DIR = EVALS_DIR / "scenarios"
RESULTS_DIR = EVALS_DIR / "results"
RAW_RESULTS_DIR = RESULTS_DIR / "raw"  # git-ignored: raw runs stay out of the repository
DEFAULT_FIXTURE = FIXTURES_DIR / "broker_inbox.json"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


class UnsafeEvalEnvironment(RuntimeError):
    """Raised when an eval would be able to reach the real Gmail account."""


def require_fake_gmail(environ: Optional[Dict[str, str]] = None) -> None:
    """Refuse to run unless every Gmail call is routed to the fake backend (S1)."""
    env = os.environ if environ is None else environ
    if env.get("OPENPOKE_FAKE_GMAIL", "").strip() != "1":
        raise UnsafeEvalEnvironment("evals require OPENPOKE_FAKE_GMAIL=1; refusing to run against a real account")
    data_dir = env.get("OPENPOKE_DATA_DIR", "").strip()
    if not data_dir:
        raise UnsafeEvalEnvironment("evals require a throwaway OPENPOKE_DATA_DIR; refusing to touch server/data")
    real = (Path(__file__).resolve().parent.parent / "server" / "data").resolve()
    if Path(data_dir).expanduser().resolve() == real:
        raise UnsafeEvalEnvironment("OPENPOKE_DATA_DIR points at the real server/data")


def load_scenario(scenario_id: str) -> Dict[str, Any]:
    return json.loads((SCENARIOS_DIR / f"{scenario_id}.json").read_text(encoding="utf-8"))


def expand_scenarios(ids: Sequence[str], include_optional_sizes: bool = False) -> List[Dict[str, Any]]:
    """S4 expands into one run per roster size (S4@10, S4@200, ...)."""
    expanded: List[Dict[str, Any]] = []
    for scenario_id in ids:
        spec = load_scenario(scenario_id)
        if spec.get("layer") == "L2":
            continue
        sizes = list(spec.get("sizes", []))
        if include_optional_sizes:
            sizes += spec.get("optional_sizes", [])
        if sizes:
            for size in sizes:
                clone = json.loads(json.dumps(spec))
                clone["run_id"] = f"{scenario_id}@{size}"
                clone["roster_size"] = size
                for step in clone["steps"]:
                    seed = step.get("seed_agents")
                    if seed and seed.get("count") == "$N":
                        seed["count"] = size
                expanded.append(clone)
        else:
            spec["run_id"] = scenario_id
            expanded.append(spec)
    return expanded


async def drain(timeout: float) -> bool:
    """Wait for every other task (follow-up turns included). Returns False on timeout."""
    me = asyncio.current_task()
    deadline = time.monotonic() + timeout
    empty = 0
    while True:
        others = {t for t in asyncio.all_tasks() if t is not me and not t.done()}
        if not others:
            empty += 1
            if empty >= 2:
                return True
            await asyncio.sleep(0)
            continue
        empty = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.wait(others, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)


def emails_in(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            out.extend(emails_in(item))
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(emails_in(item))
        return out
    return [m.lower() for m in _EMAIL_RE.findall(str(value))]


def resolve_required_ids(required: Iterable[str], gold_threads: Dict[str, Dict[str, Any]],
                         gmail_actions: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Map ``$draft:<gold_thread>`` to the latest draft created for that thread (None if no draft exists).

    A draft belongs to a gold thread when it was created in that thread or is
    addressed to one of its counterparties. Static ids pass through unchanged.
    """
    resolved: Dict[str, Optional[str]] = {}
    for item in required:
        if not item.startswith("$draft:"):
            resolved[item] = item
            continue
        key = item.split(":", 1)[1]
        gold = gold_threads.get(key, {})
        counterparties = set(gold.get("counterparties", []))
        match = None
        for action in gmail_actions:
            if action.get("action") != "GMAIL_CREATE_EMAIL_DRAFT" or not action.get("ok"):
                continue
            if action.get("thread_id") == gold.get("thread_id") or counterparties & set(action.get("recipients", [])):
                match = action.get("draft_id")
        resolved[item] = match
    return resolved


# ---------------------------------------------------------------------------
# Synthetic seeding (S4 distractor rosters, S5 intervening dispatches)
# ---------------------------------------------------------------------------

_TOPICS = [
    ("home renewal", "HO"), ("auto renewal", "AU"), ("certificate of insurance", "GL"),
    ("claim follow-up", "CL"), ("payment plan", "AU"), ("address change", "HO"),
    ("umbrella quote", "UM"), ("commercial property quote", "CP"),
]
_FIRST = ["Avery", "Blake", "Casey", "Devon", "Emery", "Finley", "Harper", "Jules", "Kendall", "Logan",
          "Morgan", "Noel", "Parker", "Quinn", "Reese", "Rowan", "Sage", "Taylor", "Winter", "Zion"]
_LAST = ["Abbott", "Barnes", "Castillo", "Dalton", "Ellison", "Foster", "Garner", "Holloway", "Ingram", "Jensen",
         "Keller", "Lambert", "Mercer", "Nolan", "Osborne", "Pruitt", "Quintero", "Ramsey", "Sutton", "Thornton"]


def _hex(n: int, salt: int) -> str:
    return f"{(0x5eed000000000000 + salt * 0x10000 + n):016x}"


def distractor_specs(count: int, near_misses: bool) -> List[Dict[str, str]]:
    """Deterministic distractor agents. Near misses share a client OR a topic with the S1 gold work."""
    specs: List[Dict[str, str]] = []
    if near_misses:
        specs += [
            {"name": "Priya Patel - add son as driver", "client": "Priya Patel", "email": "priya.patel@example.com",
             "topic": "adding a driver to AU-77120", "thread": "18c1a00000000015"},
            {"name": "Marcus Webb auto renewal AU-77210", "client": "Marcus Webb", "email": "marcus.webb@example.com",
             "topic": "auto renewal payment plan AU-77210", "thread": "18c1a00000000007"},
            {"name": "Harbor Law records request", "client": "Harbor Law", "email": "records@harborlaw.example",
             "topic": "records request on claim CL-1984", "thread": _hex(1, 9)},
            {"name": "Elena Rossi auto quote", "client": "Elena Rossi", "email": "elena.rossi@example.com",
             "topic": "auto quote for a second car", "thread": _hex(2, 9)},
        ]
    i = 0
    while len(specs) < count:
        first, last = _FIRST[i % len(_FIRST)], _LAST[(i // len(_FIRST)) % len(_LAST)]
        topic, prefix = _TOPICS[i % len(_TOPICS)]
        policy = f"{prefix}-{10000 + i}"
        specs.append({
            "name": f"{first} {last} {topic}",
            "client": f"{first} {last}",
            "email": f"{first.lower()}.{last.lower()}{i}@example.org",
            "topic": f"{topic} {policy}",
            "thread": _hex(i, 1),
        })
        i += 1
    return specs[:count]


def seed_agents(count: int, near_misses: bool) -> List[str]:
    """Create agents as if earlier dispatches had run: journal entries + roster.

    Uses the public journal/roster APIs only. With AGENT_REGISTRY on, the roster
    facade's load() imports the new names and indexes their journals, exactly
    like the migration does for pre-existing data.
    """
    from server.services.execution import get_agent_roster, get_execution_agent_logs

    logs = get_execution_agent_logs()
    roster = get_agent_roster()
    roster.load()
    names = []
    for spec in distractor_specs(count, near_misses):
        name = spec["name"]
        logs.record_request(name, f"Help with {spec['client']}'s {spec['topic']}. Their email is {spec['email']}.")
        logs.record_action(name, "Calling task_email_search with: " + json.dumps({"search_query": f"from:{spec['email']}"}))
        # Baseline-format entries only, so both variants see the same seeded journals.
        logs.record_tool_response(name, "task_email_search", json.dumps(
            [{"id": spec["thread"], "thread_id": spec["thread"], "sender": f"{spec['client']} <{spec['email']}>",
              "subject": spec["topic"]}]))
        logs.record_agent_response(name, f"Found the thread about {spec['topic']} with {spec['client']} ({spec['email']}). No action needed yet.")
        roster.add_agent(name)
        names.append(name)
    roster.load()
    return names
