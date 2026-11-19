"""L1: eval metrics on synthetic event logs with known answers, gold resolution, and the fake-mode guard."""

from __future__ import annotations

import pytest

from evals import harness, metrics

GOLD = {
    "priya": {"thread_id": "t_priya000000001", "counterparties": ["priya.patel@example.com"]},
    "sarah": {"thread_id": "t_sarah000000001", "counterparties": ["s.chen@harborlaw.example"]},
}


def _result(events, actions=(), steps=None, **extra):
    return {"events": list(events), "gmail_actions": list(actions), "gold_threads": GOLD,
            "steps": steps or [{"index": 0, "mono_start": 0, "mono_end": 1000, "gold_thread": "priya"}], **extra}


def test_ia_calls_wasted_rounds_and_limit_hits():
    events = [
        {"event": "llm_call", "role": "interaction", "turn_id": "t1"},
        {"event": "llm_call", "role": "interaction", "turn_id": "t1"},
        {"event": "llm_call", "role": "execution", "turn_id": "t1"},
        {"event": "llm_call", "role": "interaction", "turn_id": "t2"},
        {"event": "ia_round", "turn_id": "t1", "index": 0, "tools": ["send_message_to_agent"]},
        {"event": "ia_round", "turn_id": "t1", "index": 1, "tools": ["wait"]},
        {"event": "ia_round", "turn_id": "t1", "index": 2, "tools": ["wait"]},
        {"event": "ia_round", "turn_id": "t2", "index": 0, "tools": ["wait"]},
        {"event": "ia_turn", "turn_id": "t1", "ended_by": "limit"},
        {"event": "ia_turn", "turn_id": "t2", "ended_by": "wait"},
    ]
    result = _result(events)
    assert sorted(metrics.ia_calls_per_turn(result)) == [1, 2]
    assert sorted(metrics.wasted_rounds(result)) == [0, 1]  # a final `wait` round is not wasted
    assert metrics.limit_hits(result) == 1


def _send(seq, action="GMAIL_REPLY_TO_THREAD", thread="t_priya000000001", body="b1", to=("priya.patel@example.com",)):
    return {"seq": seq, "action": action, "effect": True, "thread_id": thread, "recipients": list(to), "body_sha256": body}


def test_duplicate_sends_exact_and_near_key():
    assert metrics.duplicate_sends(_result([], [_send(1), _send(2)])) == 1  # same fingerprint
    assert metrics.duplicate_sends(_result([], [_send(1), _send(2, body="b2")])) == 1  # same thread+recipients
    assert metrics.duplicate_sends(_result([], [_send(1), _send(2, to=("other@example.com",))])) == 0
    confirmed = _result([], [_send(1), _send(3, body="b2")],
                        steps=[{"index": 0, "mono_start": 0, "mono_end": 1}, {"index": 1, "mono_start": 1, "mono_end": 2,
                               "confirms_resend": True, "gmail_seq_start": 2}])
    assert metrics.duplicate_sends(confirmed) == 0  # the user explicitly confirmed a different body
    exact_even_if_confirmed = _result([], [_send(1), _send(3)], steps=confirmed["steps"])
    assert metrics.duplicate_sends(exact_even_if_confirmed) == 1


def _run(run_id, agent, mono, origin="user"):
    return {"event": "run_start", "run_id": run_id, "agent": agent, "mono": mono, "root_origin": origin}


def test_gold_labels_and_false_merges():
    events = [_run("r1", "Priya agent", 1), _run("r2", "Priya agent", 2), _run("r3", "Priya agent", 3), _run("r4", "Sarah agent", 4)]
    actions = [
        {"run_id": "r1", "action": "GMAIL_CREATE_EMAIL_DRAFT", "thread_id": "t_priya000000001", "recipients": ["priya.patel@example.com"]},
        {"run_id": "r2", "action": "GMAIL_FETCH_EMAILS", "query": "from:priya.patel@example.com", "returned_threads": []},
        {"run_id": "r3", "action": "GMAIL_FETCH_EMAILS", "query": "from:s.chen@harborlaw.example", "returned_threads": ["t_sarah000000001"]},
        {"run_id": "r4", "action": "GMAIL_REPLY_TO_THREAD", "thread_id": "t_sarah000000001", "recipients": ["s.chen@harborlaw.example"]},
    ]
    labels = {r["run_id"]: r["gold"] for r in metrics.label_runs(_result(events, actions))}
    assert labels == {"r1": "priya", "r2": "priya", "r3": "sarah", "r4": "sarah"}
    route = metrics.routing(_result(events, actions))
    # r3 (Sarah work) landed on the agent that already owned Priya's thread: one false merge.
    assert route["false_merges"] == 1 and route["labeled_runs"] == 4
    assert metrics.false_merge_bound(0, 30) == pytest.approx(0.1)


def test_unlabeled_runs_fall_back_to_step_gold_and_trigger_runs_are_ignored():
    events = [_run("r1", "A", 5), _run("r2", "B", 6, origin="trigger")]
    labels = metrics.label_runs(_result(events))
    assert [(r["run_id"], r["gold"]) for r in labels] == [("r1", "priya")]


def test_fragmentation_counts_created_agents_per_gold_thread():
    events = [
        _run("r1", "A", 1), _run("r2", "B", 2),
        {"event": "dispatch", "action": "create", "resolved_agent": "A"},
        {"event": "dispatch", "action": "create", "resolved_agent": "B"},
        {"event": "dispatch", "action": "create", "resolved_agent": "Seeded"},
    ]
    route = metrics.routing(_result(events, seeded_agents=["Seeded"]))
    assert route["agents_created"] == 2 and route["gold_threads_touched"] == 1 and route["fragmentation"] == 2.0


def test_context_hit_counts_only_measurable_steps():
    checks = [
        {"step": 1, "required": {"$draft:priya": "r-1"}, "runs": [{"present": {"$draft:priya": True}}]},
        {"step": 2, "required": {"$draft:priya": "r-1"}, "runs": [{"present": {"$draft:priya": False}}]},
        {"step": 3, "required": {"$draft:sarah": None}, "runs": []},
    ]
    assert metrics.context_hit({"context_checks": checks}) == {"hits": 1, "measurable": 2, "unmeasurable": 1, "rate": 0.5}


def test_draft_gold_resolution_uses_thread_or_counterparty():
    actions = [
        {"action": "GMAIL_CREATE_EMAIL_DRAFT", "ok": True, "draft_id": "r-1", "thread_id": "t_priya000000001", "recipients": []},
        {"action": "GMAIL_CREATE_EMAIL_DRAFT", "ok": True, "draft_id": "r-2", "thread_id": None, "recipients": ["priya.patel@example.com"]},
        {"action": "GMAIL_CREATE_EMAIL_DRAFT", "ok": False, "draft_id": "r-3", "thread_id": "t_priya000000001", "recipients": []},
    ]
    resolved = harness.resolve_required_ids(["$draft:priya", "$draft:sarah", "static123"], GOLD, actions)
    assert resolved == {"$draft:priya": "r-2", "$draft:sarah": None, "static123": "static123"}


def test_eval_guard_refuses_real_gmail_or_real_data(tmp_path):
    with pytest.raises(harness.UnsafeEvalEnvironment):
        harness.require_fake_gmail({"OPENPOKE_DATA_DIR": str(tmp_path)})
    with pytest.raises(harness.UnsafeEvalEnvironment):
        harness.require_fake_gmail({"OPENPOKE_FAKE_GMAIL": "1"})
    real = harness.EVALS_DIR.parent / "server" / "data"
    with pytest.raises(harness.UnsafeEvalEnvironment):
        harness.require_fake_gmail({"OPENPOKE_FAKE_GMAIL": "1", "OPENPOKE_DATA_DIR": str(real)})
    harness.require_fake_gmail({"OPENPOKE_FAKE_GMAIL": "1", "OPENPOKE_DATA_DIR": str(tmp_path)})


def test_s4_expands_per_roster_size():
    runs = harness.expand_scenarios(["S4", "S6"])
    assert [r["run_id"] for r in runs] == ["S4@10", "S4@200"]  # S6 is L2-only
    assert runs[1]["steps"][0]["seed_agents"]["count"] == 200


def test_distractors_are_deterministic_and_include_near_misses():
    a = harness.distractor_specs(12, near_misses=True)
    assert a == harness.distractor_specs(12, near_misses=True)
    assert any("Priya Patel" in s["name"] for s in a) and len({s["name"] for s in a}) == 12
