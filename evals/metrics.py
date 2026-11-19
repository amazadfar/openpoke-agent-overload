"""Eval metrics, computed only from telemetry, the fake-Gmail action log and gold labels.

Every function takes one repetition result (as written by ``run_live``) and
returns a number or a small dict. ``summarize`` aggregates repetitions with
N, min / median / max. Numbers from here are MEASURED (telemetry, provider
usage); nothing is estimated.

Two metric versions (both kept so every published number can be reproduced):

- v1 (original): a run is labelled by what it TOUCHED. That leaks the outcome
  into the label: a misrouted run that acts on the wrong thread is labelled as
  that wrong thread and scored as correct. False merges are counted per run.
- v2: a run is labelled by its STEP's gold thread (never outside the step's
  gold set); merges are counted as decisions (agent, gold) and separately as
  runs; plus outcome metrics: wrong-target writes, unrequested sends, and
  whether the S8 fault actually fired.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .harness import SCENARIOS_DIR, emails_in

EXTERNAL_SENDS = ("GMAIL_SEND_DRAFT", "GMAIL_REPLY_TO_THREAD", "GMAIL_FORWARD_MESSAGE")
WRITES = ("GMAIL_CREATE_EMAIL_DRAFT",) + EXTERNAL_SENDS

NEAR_DUP_WINDOW_S = 86400.0


# ----------------------------------------------------------------- helpers
def _events(result: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    return [e for e in result.get("events", []) if e.get("event") == name]


def _step_of(result: Dict[str, Any], mono: float) -> Optional[Dict[str, Any]]:
    for step in result.get("steps", []):
        if step["mono_start"] <= mono <= step["mono_end"]:
            return step
    return None


def _gold_by_thread(result: Dict[str, Any]) -> Dict[str, str]:
    return {gold["thread_id"]: key for key, gold in result.get("gold_threads", {}).items()}


def _gold_by_email(result: Dict[str, Any]) -> Dict[str, List[str]]:
    index: Dict[str, List[str]] = defaultdict(list)
    for key, gold in result.get("gold_threads", {}).items():
        for email in gold.get("counterparties", []):
            index[email].append(key)
    return index


# ----------------------------------------------------------------- turn-level
def ia_calls_per_turn(result: Dict[str, Any]) -> List[int]:
    counts: Dict[str, int] = defaultdict(int)
    for event in _events(result, "llm_call"):
        if event.get("role") == "interaction":
            counts[event.get("turn_id")] += 1
    return list(counts.values())


def wasted_rounds(result: Dict[str, Any]) -> List[int]:
    """Per turn: rounds whose tools are only `wait` and which are not the turn's final round."""
    by_turn: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for event in _events(result, "ia_round"):
        by_turn[event.get("turn_id")].append(event)
    out = []
    for rounds in by_turn.values():
        rounds.sort(key=lambda e: e["index"])
        last = rounds[-1]["index"] if rounds else -1
        out.append(sum(1 for r in rounds if r["tools"] and set(r["tools"]) <= {"wait"} and r["index"] != last))
    return out


def limit_hits(result: Dict[str, Any]) -> int:
    return sum(1 for e in _events(result, "ia_turn") if e.get("ended_by") == "limit")


def usage_totals(result: Dict[str, Any]) -> Dict[str, float]:
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0, "latency_ms": 0.0}
    for event in _events(result, "llm_call"):
        totals["calls"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost", "latency_ms"):
            totals[key] += event.get(key) or 0
    return totals


def tokens_per_turn(result: Dict[str, Any]) -> List[int]:
    by_turn: Dict[str, int] = defaultdict(int)
    for event in _events(result, "llm_call"):
        by_turn[event.get("turn_id")] += event.get("total_tokens") or 0
    return list(by_turn.values())


def ea_prompt_chars(result: Dict[str, Any]) -> List[Tuple[str, int]]:
    out = []
    for event in _events(result, "prompt_parts"):
        if event.get("role") == "execution":
            total = sum(event.get(k) or 0 for k in ("template_chars", "card_chars", "history_chars", "instruction_chars"))
            out.append((event.get("agent"), total))
    return out


def roster_chars(result: Dict[str, Any]) -> List[int]:
    return [e["roster_chars"] for e in _events(result, "prompt_parts") if e.get("role") == "interaction"]


# ----------------------------------------------------------------- side effects
def _send_identity(action: Dict[str, Any]) -> Tuple[str, str, str]:
    target = action.get("thread_id") or action.get("forwarded_message_id") or "new"
    return target, ",".join(sorted(action.get("recipients", []))), action.get("body_sha256") or ""


def duplicate_sends(result: Dict[str, Any]) -> int:
    """Sends with an equal fingerprint, plus sends sharing a near key (thread + recipients)
    within 24 h with no intervening user step that explicitly confirms a resend."""
    sends = [a for a in result.get("gmail_actions", []) if a.get("effect") and a.get("action") in
             ("GMAIL_SEND_DRAFT", "GMAIL_REPLY_TO_THREAD", "GMAIL_FORWARD_MESSAGE")]
    confirm_steps = [s for s in result.get("steps", []) if s.get("confirms_resend")]
    duplicates = 0
    seen: List[Tuple[Tuple[str, str, str], Dict[str, Any]]] = []
    for send in sends:
        identity = _send_identity(send)
        for previous_identity, previous in seen:
            same_fingerprint = previous_identity == identity
            same_near_key = previous_identity[:2] == identity[:2]
            if same_fingerprint or same_near_key:
                # A confirming user step started after the previous send and before this one.
                confirmed = any(previous["seq"] <= s.get("gmail_seq_start", -1) < send["seq"] for s in confirm_steps)
                if same_fingerprint or not confirmed:
                    duplicates += 1
                    break
        seen.append((identity, send))
    return duplicates


# ----------------------------------------------------------------- routing / memory
def label_runs_v1(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Assign each user-rooted run a gold thread (or 'unlabeled') from what it actually touched.

    Precedence: thread ids/recipients of write actions, then thread ids/emails in
    search queries, then returned threads; a unique gold match labels the run.
    With no match the run gets its step's single gold thread.
    """
    by_thread = _gold_by_thread(result)
    by_email = _gold_by_email(result)
    actions_by_run: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for action in result.get("gmail_actions", []):
        if action.get("run_id"):
            actions_by_run[action["run_id"]].append(action)

    labeled = []
    for run in _events(result, "run_start"):
        if run.get("root_origin") != "user":
            continue
        step = _step_of(result, run["mono"])
        actions = actions_by_run.get(run["run_id"], [])
        label = None
        for tier in ("write", "query", "returned"):
            matches = set()
            for action in actions:
                is_write = action["action"] not in ("GMAIL_FETCH_EMAILS", "GMAIL_LIST_DRAFTS", "GMAIL_GET_CONTACTS",
                                                    "GMAIL_GET_PEOPLE", "GMAIL_SEARCH_PEOPLE", "GMAIL_GET_PROFILE")
                if tier == "write" and is_write:
                    if action.get("thread_id") in by_thread:
                        matches.add(by_thread[action["thread_id"]])
                    for email in action.get("recipients", []):
                        matches.update(by_email.get(email, []))
                elif tier == "query" and action["action"] == "GMAIL_FETCH_EMAILS":
                    query = action.get("query") or ""
                    for thread_id, key in by_thread.items():
                        if thread_id in query:
                            matches.add(key)
                    for email in emails_in(query):
                        matches.update(by_email.get(email, []))
                elif tier == "returned" and action["action"] == "GMAIL_FETCH_EMAILS":
                    for thread_id in action.get("returned_threads", []):
                        if thread_id in by_thread:
                            matches.add(by_thread[thread_id])
            if len(matches) == 1:
                label = next(iter(matches))
                break
            if len(matches) > 1 and tier == "write":
                label = "unlabeled"
                break
        if label is None and step is not None:
            label = step.get("gold_thread") or "unlabeled"
        labeled.append({"run_id": run["run_id"], "agent": run.get("agent"), "mono": run["mono"],
                        "gold": label or "unlabeled", "step": step["index"] if step else None})
    return labeled


def routing_v1(result: Dict[str, Any]) -> Dict[str, Any]:
    runs = sorted(label_runs_v1(result), key=lambda r: r["mono"])
    # Seeded distractor agents (S4/S5) already own their own, different threads: routing
    # gold work into one of them is a false merge too.
    owners: Dict[str, str] = {f"seeded:{name}": name for name in result.get("seeded_agents", [])}
    labeled = [r for r in runs if r["gold"] != "unlabeled"]
    false_merges = 0
    correct = 0
    # Time order: a dispatch is a false merge when its agent ALREADY owns a different gold thread.
    for run in labeled:
        if any(agent == run["agent"] and gold != run["gold"] for gold, agent in owners.items()):
            false_merges += 1
        owners.setdefault(run["gold"], run["agent"])
        if owners[run["gold"]] == run["agent"] and run["agent"] not in result.get("seeded_agents", []):
            correct += 1  # a seeded distractor can never be the right owner of gold work
    seeded = set(result.get("seeded_agents", []))
    created = {e.get("resolved_agent") for e in _events(result, "dispatch")
               if e.get("action") in ("create", "create_new", "create_after_ask") and e.get("resolved_agent") not in seeded}
    threads_touched = {r["gold"] for r in labeled}
    merged_into_seeded = sum(1 for r in labeled if r["agent"] in set(result.get("seeded_agents", [])))
    asks = sum(1 for e in _events(result, "dispatch") if e.get("action") == "ask")
    return {
        "labeled_runs": len(labeled),
        "unlabeled_runs": len(runs) - len(labeled),
        "false_merges": false_merges,
        "routing_correct": correct,
        "routing_accuracy": (correct / len(labeled)) if labeled else None,
        "agents_created": len(created),
        "gold_threads_touched": len(threads_touched),
        "fragmentation": (len(created) / len(threads_touched)) if threads_touched else None,
        "ask_rounds": asks,
        "runs_on_seeded_agents": merged_into_seeded,
    }


label_runs = label_runs_v1  # names used by v1 callers
routing = routing_v1


# ----------------------------------------------------------------- v2: step-based labels
@lru_cache(maxsize=None)
def _scenario_steps(scenario_id: str) -> Tuple[Dict[str, Any], ...]:
    path = SCENARIOS_DIR / f"{scenario_id}.json"
    if not path.exists():
        return ()
    return tuple(json.loads(path.read_text(encoding="utf-8")).get("steps", []))


def step_gold_set(step: Optional[Dict[str, Any]]) -> Set[str]:
    if not step:
        return set()
    golds = set((step.get("gold_threads") or {}).values())
    if step.get("gold_thread"):
        golds.add(step["gold_thread"])
    return golds


def _action_targets(result: Dict[str, Any], action: Dict[str, Any]) -> Set[str]:
    """Gold threads an action is aimed at: same thread id, or a counterparty among its recipients."""
    by_thread, by_email = _gold_by_thread(result), _gold_by_email(result)
    targets: Set[str] = set()
    if action.get("thread_id") in by_thread:
        targets.add(by_thread[action["thread_id"]])
    for email in action.get("recipients", []):
        targets.update(by_email.get(email, []))
    return targets


def _query_targets(result: Dict[str, Any], action: Dict[str, Any]) -> Set[str]:
    by_thread, by_email = _gold_by_thread(result), _gold_by_email(result)
    query = action.get("query") or ""
    targets = {key for thread_id, key in by_thread.items() if thread_id in query}
    for email in emails_in(query):
        targets.update(by_email.get(email, []))
    return targets


def label_runs_v2(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Each user-rooted run gets its STEP's gold thread. Multi-gold steps (e.g. S2's
    Alice/Bob) choose among that step's own gold threads by the run's write targets,
    then its query targets; otherwise the run is "ambiguous". Never a label outside
    the step's gold set, so a misrouted run can no longer be scored as correct."""
    actions_by_run: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for action in result.get("gmail_actions", []):
        if action.get("run_id"):
            actions_by_run[action["run_id"]].append(action)
    labeled = []
    for run in _events(result, "run_start"):
        if run.get("root_origin") != "user":
            continue
        step = _step_of(result, run["mono"])
        golds = step_gold_set(step)
        if not golds:
            continue  # steps without gold (seeding, S9) are not scored
        if len(golds) == 1:
            label = next(iter(golds))
        else:
            label = "ambiguous"
            actions = actions_by_run.get(run["run_id"], [])
            for targets_of in (
                lambda: set().union(*[_action_targets(result, a) for a in actions if a["action"] in WRITES] or [set()]),
                lambda: set().union(*[_query_targets(result, a) for a in actions if a["action"] == "GMAIL_FETCH_EMAILS"] or [set()]),
            ):
                matches = targets_of() & golds
                if len(matches) == 1:
                    label = next(iter(matches))
                    break
        labeled.append({"run_id": run["run_id"], "agent": run.get("agent"), "mono": run["mono"], "gold": label,
                        "step": step["index"]})
    return labeled


def routing_v2(result: Dict[str, Any]) -> Dict[str, Any]:
    runs = sorted(label_runs_v2(result), key=lambda r: r["mono"])
    seeded = set(result.get("seeded_agents", []))
    owners: Dict[str, str] = {f"seeded:{name}": name for name in seeded}
    labeled = [r for r in runs if r["gold"] != "ambiguous"]
    decisions: List[Dict[str, Any]] = []
    seen_decisions: Set[Tuple[str, str]] = set()
    false_merge_runs = correct = 0
    for run in labeled:
        owns_other = [g for g, a in owners.items() if a == run["agent"] and g != run["gold"]]
        if owns_other:
            false_merge_runs += 1
            key = (run["agent"], run["gold"])
            if key not in seen_decisions:  # a decision is counted once, not once per later run
                seen_decisions.add(key)
                decisions.append({"agent": run["agent"], "gold": run["gold"], "step": run["step"],
                                  "already_owned": sorted(owns_other)})
            continue  # a merged run is never correct and never makes its agent the owner of this gold
        owners.setdefault(run["gold"], run["agent"])
        if owners[run["gold"]] == run["agent"] and run["agent"] not in seeded:
            correct += 1
    created = {e.get("resolved_agent") for e in _events(result, "dispatch")
               if e.get("action") in ("create", "create_new", "create_after_ask") and e.get("resolved_agent") not in seeded}
    touched = {r["gold"] for r in labeled}
    return {
        "labeled_runs": len(labeled),
        "ambiguous_runs": len(runs) - len(labeled),
        "merge_decisions": len(decisions),
        "merge_decision_items": decisions,
        "false_merge_runs": false_merge_runs,
        "routing_correct": correct,
        "routing_accuracy": (correct / len(labeled)) if labeled else None,
        "agents_created": len(created),
        "gold_threads_touched": len(touched),
        "fragmentation": (len(created) / len(touched)) if touched else None,
        "runs_on_seeded_agents": sum(1 for r in labeled if r["agent"] in seeded),
        "ask_rounds": sum(1 for e in _events(result, "dispatch") if e.get("action") == "ask"),
    }


# ----------------------------------------------------------------- v2: outcome metrics
def _action_step(result: Dict[str, Any], action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The step during which an action happened (by the runner's action-sequence boundaries)."""
    found = None
    for step in result.get("steps", []):
        if step.get("gmail_seq_start", 0) < action["seq"]:
            found = step
    return found


def _step_policy(result: Dict[str, Any], step: Dict[str, Any]) -> Tuple[Set[str], bool]:
    """(allowed external-send actions, conditional resend) from the step record or the scenario file."""
    if "allowed_side_effects" in step:
        return set(step["allowed_side_effects"] or []), bool(step.get("send_allowed_if_not_already_sent"))
    spec_steps = _scenario_steps(result.get("scenario_id") or str(result.get("scenario", "")).split("@")[0])
    spec = spec_steps[step["index"]] if step["index"] < len(spec_steps) else {}
    allowed = spec.get("allowed_side_effects", spec.get("expect_side_effects", step.get("expect_side_effects", [])))
    return set(allowed or []), bool(spec.get("send_allowed_if_not_already_sent"))


def _item(result: Dict[str, Any], action: Dict[str, Any], step: Optional[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    return {"scenario": result.get("scenario"), "rep": result.get("rep"), "step": step["index"] if step else None,
            "agent": action.get("agent"), "action": action["action"], "thread_id": action.get("thread_id"),
            "recipients": action.get("recipients", []), **extra}


def wrong_target_writes(result: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Writes (draft create, send, reply, forward) aimed at a gold thread OUTSIDE their step's gold set."""
    wrong, unattributed = [], []
    for action in result.get("gmail_actions", []):
        if action.get("action") not in WRITES or not action.get("effect"):
            continue
        step = _action_step(result, action)
        golds = step_gold_set(step)
        if not golds:
            continue
        targets = _action_targets(result, action)
        if not targets:
            unattributed.append(_item(result, action, step, step_gold=sorted(golds)))
        elif not targets & golds:
            wrong.append(_item(result, action, step, targets=sorted(targets), step_gold=sorted(golds)))
    return {"wrong": wrong, "unattributed": unattributed}


def unrequested_sends(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """External sends in a step whose metadata does not allow sending (e.g. the user asked for a draft).

    In a conditional step ("if not, send it again") a send is allowed; if an earlier
    send to the same thread exists it is a DUPLICATE (counted by duplicate_sends), not here.
    """
    items = []
    for action in result.get("gmail_actions", []):
        if action.get("action") not in EXTERNAL_SENDS or not action.get("effect"):
            continue
        step = _action_step(result, action)
        if step is None:
            continue
        allowed, _conditional = _step_policy(result, step)
        if action["action"] not in allowed:
            items.append(_item(result, action, step, user_step_kind=step.get("kind")))
    return items


def fault_fired(result: Dict[str, Any]) -> bool:
    """Whether an injected fake-Gmail fault actually triggered in this run (S8)."""
    return any(str(a.get("outcome", "")).startswith("injected_failure") for a in result.get("gmail_actions", []))


def duplicate_send_items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The sends counted by duplicate_sends (same definition), itemised for the audit."""
    sends = [a for a in result.get("gmail_actions", []) if a.get("effect") and a.get("action") in EXTERNAL_SENDS]
    confirm_steps = [s for s in result.get("steps", []) if s.get("confirms_resend")]
    items, seen = [], []
    for send in sends:
        identity = _send_identity(send)
        for previous_identity, previous in seen:
            same_fp = previous_identity == identity
            if same_fp or previous_identity[:2] == identity[:2]:
                confirmed = any(previous["seq"] <= s.get("gmail_seq_start", -1) < send["seq"] for s in confirm_steps)
                if same_fp or not confirmed:
                    items.append(_item(result, send, _action_step(result, send), duplicate_of_seq=previous["seq"]))
                    break
        seen.append((identity, send))
    return items


def context_hit(result: Dict[str, Any]) -> Dict[str, Any]:
    """Follow-up steps whose EA prompt contains every required id ÷ measurable follow-up steps."""
    hits = measurable = unmeasurable = 0
    for check in result.get("context_checks", []):
        if any(value is None for value in check["required"].values()):
            unmeasurable += 1  # the required object (e.g. the draft) was never created
            continue
        measurable += 1
        if any(all(run["present"].values()) for run in check["runs"]):
            hits += 1
    return {"hits": hits, "measurable": measurable, "unmeasurable": unmeasurable,
            "rate": (hits / measurable) if measurable else None}


def false_merge_bound(false_merges: int, labeled: int) -> Optional[float]:
    """95% upper bound on the false-merge rate. Rule of three when none were observed."""
    if labeled == 0:
        return None
    if false_merges == 0:
        return 3.0 / labeled
    return None


# ----------------------------------------------------------------- aggregation
def _stats(values: Sequence[float]) -> Dict[str, Any]:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"n": 0, "min": None, "median": None, "max": None, "mean": None}
    return {"n": len(clean), "min": min(clean), "median": statistics.median(clean), "max": max(clean),
            "mean": sum(clean) / len(clean)}


def per_run(result: Dict[str, Any], version: int = 2) -> Dict[str, Any]:
    calls = ia_calls_per_turn(result)
    wasted = wasted_rounds(result)
    ctx = context_hit(result)
    usage = usage_totals(result)
    row: Dict[str, Any] = {
        "scenario": result.get("scenario"),
        "rep": result.get("rep"),
        "partial": result.get("partial"),
        "ia_calls_per_turn_mean": (sum(calls) / len(calls)) if calls else None,
        "ia_turns": len(calls),
        "wasted_rounds_per_turn_max": max(wasted) if wasted else 0,
        "wasted_rounds_total": sum(wasted),
        "limit_hits": limit_hits(result),
        "duplicate_sends": duplicate_sends(result),
        "external_sends": sum(1 for a in result.get("gmail_actions", []) if a.get("effect") and a.get("action") in EXTERNAL_SENDS),
        "context_hits": ctx["hits"],
        "context_measurable": ctx["measurable"],
        "context_unmeasurable": ctx["unmeasurable"],
        "llm_calls": usage["calls"],
        "total_tokens": usage["total_tokens"],
        "cost": usage["cost"],
        "ea_prompt_chars_max": max((c for _, c in ea_prompt_chars(result)), default=None),
        "roster_chars_max": max(roster_chars(result), default=None),
    }
    if version == 1:
        row.update(routing_v1(result))
        return row
    route = routing_v2(result)
    route.pop("merge_decision_items")
    writes = wrong_target_writes(result)
    row.update(route)
    row.update(
        false_merges=route["false_merge_runs"],  # v2 run-level count, kept under the v1 name for tables
        wrong_target_writes=len(writes["wrong"]),
        unattributed_writes=len(writes["unattributed"]),
        writes_scored=sum(1 for a in result.get("gmail_actions", []) if a.get("action") in WRITES and a.get("effect")
                          and step_gold_set(_action_step(result, a))),
        unrequested_sends=len(unrequested_sends(result)),
        fault_fired=fault_fired(result),
    )
    return row


def summarize(results: Iterable[Dict[str, Any]], version: int = 2) -> Dict[str, Dict[str, Any]]:
    """Aggregate per scenario run id (S4@10, S4@200, ...)."""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for result in results:
        if result.get("error"):
            continue
        groups[result["scenario"]].append(per_run(result, version))
    summary: Dict[str, Dict[str, Any]] = {}
    for scenario, rows in groups.items():
        keys = [k for k in rows[0] if any(isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool) for r in rows)]
        entry: Dict[str, Any] = {"repetitions": len(rows), "partial_runs": sum(1 for r in rows if r["partial"])}
        for key in keys:
            entry[key] = _stats([r[key] for r in rows])
        labeled = sum(r["labeled_runs"] for r in rows)
        merges = sum(r["false_merges"] for r in rows)
        entry["false_merges_total"] = merges
        entry["labeled_runs_total"] = labeled
        entry["false_merge_upper_bound_95"] = false_merge_bound(merges, labeled)
        hits = sum(r["context_hits"] for r in rows)
        measurable = sum(r["context_measurable"] for r in rows)
        entry["context_hit_rate"] = (hits / measurable) if measurable else None
        entry["duplicate_sends_total"] = sum(r["duplicate_sends"] for r in rows)
        entry["limit_hits_total"] = sum(r["limit_hits"] for r in rows)
        if version == 2:
            for key in ("merge_decisions", "wrong_target_writes", "unattributed_writes", "writes_scored",
                        "unrequested_sends", "ambiguous_runs"):
                entry[f"{key}_total"] = sum(r[key] for r in rows)
            fired = [r for r in rows if r["fault_fired"]]
            entry["fault_fired_runs"] = len(fired)
            entry["duplicate_sends_fault_fired"] = sum(r["duplicate_sends"] for r in fired)
        summary[scenario] = entry
    return summary


__all__ = [
    "context_hit",
    "duplicate_send_items",
    "duplicate_sends",
    "fault_fired",
    "ia_calls_per_turn",
    "label_runs_v1",
    "label_runs_v2",
    "limit_hits",
    "per_run",
    "routing_v1",
    "routing_v2",
    "summarize",
    "unrequested_sends",
    "wasted_rounds",
    "wrong_target_writes",
]
