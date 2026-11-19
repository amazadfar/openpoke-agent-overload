"""L2: orchestration fixes. 2.6 turn-scoped batches (M6), 2.7 one turn per poll (M4), 2.8 single-flight
watcher (M4, I6, I11), 2.9 concurrency gate with user reservation (M4, I12); S6 storm."""

from __future__ import annotations

import asyncio
import random
import re
import time

import pytest

from helpers import agent_messages, dispatch, drain, other_tasks, run_async, user_turn
from scenarios import (
    create_due_triggers,
    e2_fast_and_slow,
    e3_staggered,
    e4_trigger_storm,
    e7_watcher_burst,
    follow_up_calls,
    ia_relay,
    peak_concurrency,
)
from server.agents.execution_agent.admission import ConcurrencyGate
from server.agents.execution_agent.batch_manager import get_batch_manager
from server.openrouter_client import OpenRouterError
from server.services import telemetry
from server.services.context import turn_scope
from server.testing.scripted_llm import Step, final, respond, tool_call
from server.utils import tasks


def new_message(call) -> str:
    text = call.first_user_content
    return text[text.find("<new_agent_message>"):]


# ===================================================================== 2.6
@pytest.mark.slow
def test_m6_fast_result_not_held_by_unrelated_slow_work(set_flags, scripted_llm, fake_gmail):
    set_flags("TURN_BATCHES")
    outcome = e2_fast_and_slow(scripted_llm)
    assert outcome["fast_delivered_s"] < 1.5
    assert outcome["follow_up_turns"] == 2
    assert get_batch_manager().open_batches() == 0


@pytest.mark.slow
def test_m6_staggered_turns_get_their_own_follow_ups(set_flags, scripted_llm, fake_gmail):
    set_flags("TURN_BATCHES")
    outcome = e3_staggered(scripted_llm)
    assert outcome["follow_up_turns"] == 3
    assert outcome["results_in_first_turn"] == 1
    assert outcome["a_delivered_s"] < 4.0  # A finished at ~3 s and is delivered then


def test_each_result_delivered_exactly_once_in_its_turn(set_flags, scripted_llm, fake_gmail):
    set_flags("TURN_BATCHES")
    scripted_llm.on("interaction", ia_relay())
    scripted_llm.on("execution", lambda call: final(f"result for {call.agent}"))
    scripted_llm.set_latency(0.1, "execution")

    async def main():
        turns = []
        for group in (["A", "B"], ["C"], ["D", "E", "F"]):
            with turn_scope("user") as turn:
                turns.append(turn.turn_id)
                for name in group:
                    dispatch(name, "work")
        await drain()
        return turns

    with telemetry.capture() as events:
        run_async(main)
    flushes = [e for e in events if e["event"] == "batch_flush"]
    assert sorted(sorted(f["agents"]) for f in flushes) == [["A", "B"], ["C"], ["D", "E", "F"]]
    delivered = " ".join(agent_messages())
    for name in "ABCDEF":
        assert delivered.count(f"] {name}:") == 1
    assert get_batch_manager().open_batches() == 0


# ===================================================================== 2.7
def test_m4_one_turn_per_poll(set_flags, scripted_llm, fake_gmail):
    from server.services.triggers import get_trigger_service

    set_flags("POLL_BATCH")
    outcome = e4_trigger_storm(scripted_llm, count=20)
    assert outcome["follow_up_turns"] == 1
    assert outcome["ia_calls"] == 2
    statuses = {get_trigger_service().get_trigger(i).status for i in outcome["trigger_ids"]}
    assert statuses == {"completed"}  # per-trigger bookkeeping unchanged


def test_next_poll_while_running_gets_its_own_turn(set_flags, scripted_llm, fake_gmail):
    from server.services.trigger_scheduler import get_trigger_scheduler

    set_flags("POLL_BATCH")
    scripted_llm.on("interaction", ia_relay())
    scripted_llm.on("execution", lambda call: final("done"))
    scripted_llm.set_latency(0.5, "execution")
    create_due_triggers(3)

    async def main():
        scheduler = get_trigger_scheduler()
        await scheduler._poll_once()
        await asyncio.sleep(0.1)
        create_due_triggers(2)
        await scheduler._poll_once()
        await drain()

    run_async(main)
    turns = follow_up_calls(scripted_llm)
    assert len(turns) == 2
    assert sorted(new_message(t).count("[SUCCESS]") for t in turns) == [2, 3]


def test_optional_max_wait_flush(set_flags, scripted_llm, fake_gmail, monkeypatch):
    from server.services.trigger_scheduler import get_trigger_scheduler

    set_flags("POLL_BATCH")
    monkeypatch.setenv("OPENPOKE_POLL_BATCH_MAX_WAIT_S", "0.3")
    scripted_llm.on("interaction", ia_relay())
    scripted_llm.script("execution:Reminder 00", [Step(final("fast"), latency=0.05)])
    scripted_llm.script("execution:Reminder 01", [Step(final("slow"), latency=1.0)])
    create_due_triggers(2)

    async def main():
        await get_trigger_scheduler()._poll_once()
        await drain()

    run_async(main)
    turns = follow_up_calls(scripted_llm)
    assert [new_message(t).count("[SUCCESS]") for t in turns] == [1, 1]


# ===================================================================== 2.8
@pytest.mark.slow
def test_m4_watcher_burst_one_turn_poll_not_blocked(set_flags, scripted_llm, fake_gmail):
    set_flags("WATCHER_SINGLE_FLIGHT")
    outcome = e7_watcher_burst(scripted_llm, fake_gmail, emails=10, classify_latency=0.05, ia_latency=0.25)
    assert outcome["turns_inside_poll"] == 0  # the poll never awaits the IA turn
    assert outcome["poll_s"] < 1.0
    assert outcome["turns_total"] == 1
    assert agent_messages()[0].count("\n- ") == 10


def _watcher_env(scripted_llm, ia_latency=0.0):
    from server.services.gmail import get_important_email_watcher

    scripted_llm.on("interaction", ia_relay())
    scripted_llm.set_latency(ia_latency, "interaction")
    scripted_llm.on("classifier", lambda call: respond(tool_call("mark_email_importance", important=True,
                                                                 summary=call.first_user_content.split("Subject: ")[1].split("\n")[0])))
    return get_important_email_watcher()


def _burst(fake_gmail, prefix, n):
    for i in range(n):
        fake_gmail.inject_email(sender=f"c{i}@example.com", subject=f"{prefix} {i}", body="urgent")


def test_burst_during_inflight_turn_is_buffered_into_one_next_turn(set_flags, scripted_llm, fake_gmail):
    set_flags("WATCHER_SINGLE_FLIGHT")
    watcher = _watcher_env(scripted_llm, ia_latency=0.4)

    async def main():
        await watcher._poll_once()
        _burst(fake_gmail, "first", 5)
        await watcher._poll_once()
        await asyncio.sleep(0.1)  # turn 1 is now running
        _burst(fake_gmail, "second", 3)
        await watcher._poll_once()
        _burst(fake_gmail, "third", 2)
        await watcher._poll_once()
        await drain()

    run_async(main)
    messages = agent_messages()
    assert len(messages) == 2
    assert messages[0].count("\n- ") == 5 and messages[1].count("\n- ") == 5


def test_buffer_overflow_is_counted_and_reported(set_flags, scripted_llm, fake_gmail, monkeypatch):
    set_flags("WATCHER_SINGLE_FLIGHT")
    monkeypatch.setenv("OPENPOKE_WATCHER_BUFFER_MAX", "3")
    watcher = _watcher_env(scripted_llm, ia_latency=0.4)

    async def main():
        await watcher._poll_once()
        _burst(fake_gmail, "first", 1)
        await watcher._poll_once()
        await asyncio.sleep(0.05)
        _burst(fake_gmail, "second", 6)
        await watcher._poll_once()
        await drain()

    with telemetry.capture() as events:
        run_async(main)
    messages = agent_messages()
    assert messages[1].count("\n- ") == 3 and "...and 3 more important emails not shown" in messages[1]
    polls = [e for e in events if e["event"] == "poll" and e["kind"] == "watcher"]
    assert polls[-1]["dropped"] == 3 and polls[-1]["buffered"] == 3


def test_classification_error_is_retried_then_dropped_with_event(set_flags, scripted_llm, fake_gmail):
    set_flags("WATCHER_SINGLE_FLIGHT")
    watcher = _watcher_env(scripted_llm)

    def classifier(call):
        if "Subject: broken" in call.first_user_content:
            raise OpenRouterError("upstream 500")
        return respond(tool_call("mark_email_importance", important=False))

    scripted_llm.on("classifier", classifier)

    async def main():
        await watcher._poll_once()
        fake_gmail.inject_email(sender="a@example.com", subject="broken", body="x")
        fake_gmail.inject_email(sender="b@example.com", subject="fine", body="x")
        for _ in range(3):
            await watcher._poll_once()
        await drain()

    with telemetry.capture() as events:
        run_async(main)
    broken = [c for c in scripted_llm.calls_for("classifier") if "Subject: broken" in c.first_user_content]
    fine = [c for c in scripted_llm.calls_for("classifier") if "Subject: fine" in c.first_user_content]
    assert len(broken) == 3 and len(fine) == 1  # retried while unseen, the good one only once
    (dropped,) = [e for e in events if e["event"] == "watcher_email_dropped"]
    assert dropped["attempts"] == 3


def test_failed_turn_is_reenqueued_once_then_dropped(set_flags, scripted_llm, fake_gmail):
    set_flags("WATCHER_SINGLE_FLIGHT")
    watcher = _watcher_env(scripted_llm)

    def broken_ia(call):
        raise RuntimeError("model unavailable")

    scripted_llm.on("interaction", broken_ia)

    async def main():
        await watcher._poll_once()
        _burst(fake_gmail, "alert", 2)
        await watcher._poll_once()
        await drain()

    with telemetry.capture() as events:
        run_async(main)
    failures = [e for e in events if e["event"] == "watcher_turn_failed"]
    assert [(f["requeued"], f["dropped"]) for f in failures] == [(2, 0), (0, 2)]
    assert len(scripted_llm.calls_for("interaction")) == 2


def test_stop_leaves_no_tasks_and_reports_lost_summaries(set_flags, scripted_llm, fake_gmail, monkeypatch):
    set_flags("WATCHER_SINGLE_FLIGHT")
    monkeypatch.setenv("OPENPOKE_WATCHER_SHUTDOWN_GRACE_S", "0.2")
    watcher = _watcher_env(scripted_llm, ia_latency=30)

    async def main():
        await watcher._poll_once()
        _burst(fake_gmail, "first", 1)
        await watcher._poll_once()
        await asyncio.sleep(0.05)
        _burst(fake_gmail, "second", 2)
        await watcher._poll_once()
        await watcher.stop()
        await asyncio.sleep(0)
        return tasks.pending()

    with telemetry.capture() as events:
        leftover = run_async(main)
    assert leftover == set()
    assert [e["lost"] for e in events if e["event"] == "watcher_shutdown"] == [2]


_OVERFLOW_RE = re.compile(r"\.\.\.and (\d+) more important emails not shown")


def delivered_and_overflow():
    """(summaries delivered as bullets, summaries reported as overflow) across all watcher turns."""
    messages = agent_messages()
    delivered = sum(m.count("\n- ") for m in messages)
    overflow = sum(int(n) for m in messages for n in _OVERFLOW_RE.findall(m))
    return delivered, overflow


def test_tasks_stay_bounded_across_many_polls(set_flags, scripted_llm, fake_gmail):
    """I6 under sustained load: at most one watcher turn in flight, bounded tasks, nothing silently lost.

    Whether a summary is delivered or reported as overflow depends on timing (the
    buffer is bounded by design), so the invariant is delivered + overflow == injected.
    """
    set_flags("WATCHER_SINGLE_FLIGHT")
    watcher = _watcher_env(scripted_llm, ia_latency=0.3)
    injected = 100

    async def main():
        await watcher._poll_once()
        peak_turns = peak_tasks = 0
        for i in range(injected):
            fake_gmail.inject_email(sender=f"x{i}@example.com", subject=f"poll {i}", body="urgent")
            await watcher._poll_once()
            await asyncio.sleep(0.01)
            running = other_tasks()
            peak_turns = max(peak_turns, sum(1 for t in running if t.get_name() == "watcher-turn"))
            peak_tasks = max(peak_tasks, len(running))
        await drain()
        return peak_turns, peak_tasks

    peak_turns, peak_tasks = run_async(main)
    assert peak_turns <= 1  # I6: single flight
    # One watcher turn plus at most one summarization worker (it never runs twice concurrently).
    assert peak_tasks <= 2
    delivered, overflow = delivered_and_overflow()
    assert delivered + overflow == injected  # nothing silently lost


def test_overflow_counts_are_exact_with_a_small_buffer(set_flags, scripted_llm, fake_gmail, monkeypatch):
    set_flags("WATCHER_SINGLE_FLIGHT")
    monkeypatch.setenv("OPENPOKE_WATCHER_BUFFER_MAX", "3")
    watcher = _watcher_env(scripted_llm, ia_latency=0.5)

    async def main():
        await watcher._poll_once()
        _burst(fake_gmail, "first", 1)
        await watcher._poll_once()  # turn 1 starts and stays in flight for ~1 s
        await asyncio.sleep(0.05)
        _burst(fake_gmail, "burst", 10)
        await watcher._poll_once()  # 3 buffered, 7 overflow
        await drain()

    run_async(main)
    messages = agent_messages()
    assert len(messages) == 2
    assert messages[0].count("\n- ") == 1
    assert messages[1].count("\n- ") == 3 and "...and 7 more important emails not shown" in messages[1]
    assert delivered_and_overflow() == (4, 7)


# ===================================================================== 2.9
def test_storm_peak_runs_capped(set_flags, scripted_llm, fake_gmail):
    set_flags("CONCURRENCY_CAP")
    outcome = e4_trigger_storm(scripted_llm, count=20, ea_latency=0.1)
    assert outcome["peak_concurrent_runs"] <= 3  # trigger-rooted runs leave the user's reserved slot free
    assert outcome["follow_up_turns"] == 20


def test_user_dispatch_during_storm_uses_reserved_slot(set_flags, scripted_llm, fake_gmail):
    from server.services.trigger_scheduler import get_trigger_scheduler

    set_flags("CONCURRENCY_CAP")
    create_due_triggers(20)
    scripted_llm.on("interaction", lambda call: final(""))
    scripted_llm.on("execution", lambda call: final("ok"))
    scripted_llm.set_latency(1.0, "execution")

    async def main():
        await get_trigger_scheduler()._poll_once()
        await asyncio.sleep(0.1)
        with turn_scope("user", user_message="urgent"):
            dispatch("User Agent", "urgent request")
        await drain()

    with telemetry.capture() as events:
        run_async(main)
    user_start = next(e for e in events if e["event"] == "run_start" and e["agent"] == "User Agent")
    assert user_start["queued_ms"] < 1000  # within one run duration, despite 20 queued trigger runs
    assert peak_concurrency(events) <= 4


def test_gate_fifo_within_class_and_user_priority():
    async def main():
        gate = ConcurrencyGate(total=2, reserved_for_user=1)
        order = []
        await gate.acquire("trigger")  # takes the only non-reserved slot

        async def wait(name, origin):
            await gate.acquire(origin)
            order.append(name)

        waiters = [asyncio.ensure_future(wait(f"t{i}", "trigger")) for i in range(3)]
        await asyncio.sleep(0)
        user = asyncio.ensure_future(wait("user", "user"))
        await asyncio.sleep(0)
        assert order == ["user"]  # the reserved slot goes to the user at once
        gate.release()  # user done
        gate.release()  # first trigger done
        for _ in range(3):
            await asyncio.sleep(0)
            if order[-1] != "user":
                gate.release()
        await asyncio.gather(*waiters, user)
        return order

    assert run_async(main) == ["user", "t0", "t1", "t2"]


def test_gate_randomized_stress_no_deadlock_no_leaks():
    async def main():
        gate = ConcurrencyGate(total=4, reserved_for_user=1)
        rng = random.Random(42)
        in_use = []

        async def worker(i):
            origin = rng.choice(["user", "trigger", "email"])
            try:
                await asyncio.wait_for(gate.acquire(origin), timeout=rng.uniform(0.001, 0.05))
            except asyncio.TimeoutError:
                return "timeout"
            try:
                in_use.append(gate.in_use)
                await asyncio.sleep(rng.uniform(0, 0.01))
            finally:
                gate.release()
            return "ok"

        jobs = [asyncio.ensure_future(worker(i)) for i in range(400)]
        await asyncio.sleep(0.02)
        for job in rng.sample(jobs, 50):
            job.cancel()
        results = await asyncio.gather(*jobs, return_exceptions=True)
        return gate, in_use, results

    gate, in_use, results = run_async(main)
    assert max(in_use) <= 4
    assert gate.in_use == 0 and gate.waiting == 0  # fully free again: no leaked permits
    assert "ok" in results


# ===================================================================== S6 storm
@pytest.mark.slow
@pytest.mark.parametrize("fixed", [False, True])
def test_s6_storm_with_rate_limiter(set_flags, flags_off, fake_gmail, monkeypatch, fixed):
    """15 triggers due at once plus a user message, against a model that answers 429 above 4 concurrent calls.

    The provider limit equals CONCURRENCY_TOTAL here: the gate can only protect a limit it is sized to
    (U4: size C from observed rate limits). Unrelated IA calls are not gated, so retries absorb the rest.
    """
    from server.openrouter_client import client as or_client
    from server.services.trigger_scheduler import get_trigger_scheduler
    from server.testing.scripted_llm import ScriptedLLM

    if fixed:
        set_flags("LLM_RETRY", "CONCURRENCY_CAP", "POLL_BATCH")

    async def short_sleep(seconds):
        await asyncio.sleep(min(seconds, 0.5))  # real jittered backoff, capped to keep the test short

    monkeypatch.setattr(or_client, "_sleep", short_sleep)
    llm = ScriptedLLM(max_concurrent=4).install_transport(monkeypatch)
    llm.on("interaction", lambda call: final(""))
    llm.on("execution", lambda call: final("ok"))
    llm.set_latency(0.3, "execution")
    create_due_triggers(15)

    async def main():
        await get_trigger_scheduler()._poll_once()
        await user_turn_dispatch()
        await drain(timeout=60)

    async def user_turn_dispatch():
        with turn_scope("user", user_message="check my inbox"):
            dispatch("User Agent", "check the inbox")

    with telemetry.capture() as events:
        run_async(main)
    ends = [e for e in events if e["event"] == "run_end"]
    failed = sum(1 for e in ends if e["outcome"] != "success")
    user_end = next(e for e in ends if e["agent"] == "User Agent")
    if fixed:
        assert failed == 0 and user_end["outcome"] == "success"
        turns = [e for e in events if e["event"] == "ia_turn"]
        assert len(turns) <= 3  # one for the poll, one for the user dispatch (+ nothing per trigger)
    else:
        assert failed > 0  # baseline: rate-limited runs fail outright (no retries, no cap)
    assert llm.rate_limited >= 0
