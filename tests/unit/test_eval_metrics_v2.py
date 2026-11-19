"""L1: metrics v2 (step-based labels, merge decisions, wrong-target writes, unrequested sends, fault_fired).

Synthetic results with known answers. Step boundaries follow the runner: a step's
``gmail_seq_start`` is the number of fake-Gmail actions BEFORE the step, so its
first action has ``seq == gmail_seq_start + 1``.
"""

from __future__ import annotations

from evals import metrics

GOLD = {
    "priya": {"thread_id": "t_priya", "counterparties": ["priya.patel@example.com"]},
    "sarah": {"thread_id": "t_sarah", "counterparties": ["s.chen@harborlaw.example"]},
    "alice": {"thread_id": "t_alice", "counterparties": ["alice.martin@example.com"]},
    "bob": {"thread_id": "t_bob", "counterparties": ["bob.nguyen@example.com"]},
}


def step(index, gold=None, golds=None, seq_start=0, allowed=(), conditional=False, start=None):
    start = index * 10 if start is None else start
    return {"index": index, "kind": "user", "gold_thread": gold, "gold_threads": golds, "mono_start": start,
            "mono_end": start + 9, "gmail_seq_start": seq_start, "allowed_side_effects": list(allowed),
            "send_allowed_if_not_already_sent": conditional}


def run(run_id, agent, mono):
    return {"event": "run_start", "run_id": run_id, "agent": agent, "mono": mono, "root_origin": "user"}


def write(seq, run_id, action="GMAIL_CREATE_EMAIL_DRAFT", thread=None, to=(), agent=None, outcome="ok"):
    return {"seq": seq, "run_id": run_id, "agent": agent, "action": action, "effect": True, "ok": True,
            "thread_id": thread, "recipients": list(to), "outcome": outcome, "body_sha256": "b"}


def result(steps, events, actions, **extra):
    return {"scenario": "SX", "scenario_id": "SX", "rep": 0, "steps": steps, "events": events,
            "gmail_actions": actions, "gold_threads": GOLD, **extra}


def test_s1_leakage_case_v1_scores_correct_v2_does_not():
    """A run for gold B (sarah) on the agent that owns A (priya) writes to A's thread."""
    r = result(
        [step(0, "priya", seq_start=0), step(1, "sarah", seq_start=1)],
        [run("r1", "Priya agent", 1), run("r2", "Priya agent", 11)],
        [write(1, "r1", thread="t_priya", to=["priya.patel@example.com"]),
         write(2, "r2", thread="t_priya", to=["priya.patel@example.com"])],
    )
    v1 = metrics.routing_v1(r)
    assert v1["false_merges"] == 0 and v1["routing_accuracy"] == 1.0  # the leak: labelled by what it touched
    assert [x["gold"] for x in metrics.label_runs_v2(r)] == ["priya", "sarah"]
    v2 = metrics.routing_v2(r)
    assert v2["merge_decisions"] == 1 and v2["false_merge_runs"] == 1 and v2["routing_accuracy"] == 0.5
    wrong = metrics.wrong_target_writes(r)["wrong"]
    assert len(wrong) == 1 and wrong[0]["step"] == 1 and wrong[0]["targets"] == ["priya"]


def test_merge_decisions_count_once_runs_count_every_time():
    r = result(
        [step(0, "priya"), step(1, "sarah"), step(2, "sarah"), step(3, "sarah")],
        [run("r1", "A", 1), run("r2", "A", 11), run("r3", "A", 21), run("r4", "A", 31)],
        [],
    )
    v2 = metrics.routing_v2(r)
    assert v2["merge_decisions"] == 1 and v2["false_merge_runs"] == 3


def test_seeded_distractor_counts_as_a_merge_decision():
    r = result([step(0, "priya")], [run("r1", "Priya - add driver", 1)], [], seeded_agents=["Priya - add driver"])
    v2 = metrics.routing_v2(r)
    assert v2["merge_decisions"] == 1 and v2["routing_accuracy"] == 0.0 and v2["runs_on_seeded_agents"] == 1


def test_s2_multi_gold_step_chooses_within_the_step_or_is_ambiguous():
    r = result(
        [step(0, golds={"alice": "alice", "bob": "bob"}, seq_start=0)],
        [run("r1", "Alice", 1), run("r2", "Bob", 2), run("r3", "Both", 3)],
        [write(1, "r1", to=["alice.martin@example.com"]), write(2, "r2", thread="t_bob"),
         write(3, "r3", to=["priya.patel@example.com"])],  # outside the step's gold set
    )
    labels = {x["run_id"]: x["gold"] for x in metrics.label_runs_v2(r)}
    assert labels == {"r1": "alice", "r2": "bob", "r3": "ambiguous"}  # never "priya"
    assert metrics.routing_v2(r)["ambiguous_runs"] == 1
    assert [w["step"] for w in metrics.wrong_target_writes(r)["wrong"]] == [0]  # the priya write is wrong-target


def test_unattributed_and_unscored_writes():
    r = result([step(0, "priya", seq_start=0), step(1, None, seq_start=1)], [],
               [write(1, None, to=["someone@example.org"]), write(2, None, to=["priya.patel@example.com"])])
    writes = metrics.wrong_target_writes(r)
    assert len(writes["unattributed"]) == 1 and writes["wrong"] == []  # step 1 has no gold: not scored


def test_step_boundaries_use_actions_before_the_step():
    """seq == gmail_seq_start belongs to the PREVIOUS step (it happened before the step began)."""
    r = result([step(0, "priya", seq_start=0), step(1, "sarah", seq_start=2)], [],
               [write(1, None, thread="t_priya"), write(2, None, thread="t_priya"), write(3, None, thread="t_sarah")])
    assert [metrics._action_step(r, a)["index"] for a in r["gmail_actions"]] == [0, 0, 1]
    assert metrics.wrong_target_writes(r)["wrong"] == []


def test_unrequested_send_in_a_draft_only_step():
    r = result([step(0, "priya", seq_start=0), step(1, "priya", seq_start=1, allowed=["GMAIL_SEND_DRAFT", "GMAIL_REPLY_TO_THREAD"])],
               [], [write(1, None, action="GMAIL_REPLY_TO_THREAD", thread="t_priya", to=["priya.patel@example.com"]),
                    write(2, None, action="GMAIL_SEND_DRAFT", thread="t_priya", to=["priya.patel@example.com"])])
    items = metrics.unrequested_sends(r)
    assert [(i["step"], i["action"]) for i in items] == [(0, "GMAIL_REPLY_TO_THREAD")]


def test_conditional_resend_allowed_vs_duplicate():
    send = dict(action="GMAIL_REPLY_TO_THREAD", thread="t_priya", to=["priya.patel@example.com"])
    steps = [step(0, "priya", seq_start=0, allowed=["GMAIL_REPLY_TO_THREAD"]),
             step(1, "priya", seq_start=1, allowed=["GMAIL_REPLY_TO_THREAD"], conditional=True)]
    # The first reply failed before it went out, so the conditional resend is legitimate.
    failed_first = {**write(1, None, **send), "effect": False, "ok": False, "outcome": "injected_failure"}
    allowed = result(steps, [], [failed_first, write(2, None, **send)])
    assert metrics.unrequested_sends(allowed) == [] and metrics.duplicate_sends(allowed) == 0
    # The first reply DID go out (response lost): the resend is a duplicate, not an unrequested send.
    duplicate = result(steps, [], [write(1, None, outcome="injected_failure_after_effect", **send), write(2, None, **send)])
    assert metrics.unrequested_sends(duplicate) == [] and metrics.duplicate_sends(duplicate) == 1
    assert [i["step"] for i in metrics.duplicate_send_items(duplicate)] == [1]


def test_s8_fault_fired_and_not_fired():
    fired = result([step(0, "priya")], [], [write(1, None, action="GMAIL_REPLY_TO_THREAD", outcome="injected_failure_after_effect")])
    not_fired = result([step(0, "priya")], [], [write(1, None, action="GMAIL_SEND_DRAFT")])
    assert metrics.fault_fired(fired) and not metrics.fault_fired(not_fired)
    summary = metrics.summarize([{**fired, "scenario": "S8"}, {**not_fired, "scenario": "S8", "rep": 1}])["S8"]
    assert summary["fault_fired_runs"] == 1 and summary["repetitions"] == 2


def test_policy_falls_back_to_the_scenario_file_for_original_runs():
    """Original raw runs have no allowed_side_effects in their step records: read the scenario file."""
    s1_steps = [{"index": i, "kind": "user", "gold_thread": "priya", "mono_start": i * 10, "mono_end": i * 10 + 9,
                 "gmail_seq_start": i} for i in range(5)]
    r = {"scenario": "S1", "scenario_id": "S1", "rep": 0, "steps": s1_steps, "events": [], "gold_threads": GOLD,
         "gmail_actions": [write(5, None, action="GMAIL_SEND_DRAFT", thread="t_priya"),
                           write(1, None, action="GMAIL_REPLY_TO_THREAD", thread="t_priya")]}
    assert [i["step"] for i in metrics.unrequested_sends(r)] == [0]  # step 4 ("send the Priya draft") allows it


def test_v1_numbers_are_reproducible():
    r = result([step(0, "priya")], [run("r1", "A", 1)], [write(1, "r1", thread="t_priya")])
    row = metrics.per_run(r, version=1)
    assert "merge_decisions" not in row and row["false_merges"] == 0
    assert metrics.per_run(r, version=2)["merge_decisions"] == 0
