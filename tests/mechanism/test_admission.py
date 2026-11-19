"""1.6 per-agent lock (M7, I5, I12) and 1.7 duplicate-dispatch suppression (D1)."""

from __future__ import annotations

import asyncio

import pytest

from helpers import dispatch, drain, journal, run_async
from scenarios import create_due_triggers, e5_same_agent_twice, ia_relay, peak_concurrency
from server.agents.execution_agent.admission import DispatchDeduper, normalize_instructions
from server.agents.execution_agent.batch_manager import ExecutionBatchManager
from server.services import telemetry
from server.services.context import turn_scope
from server.testing.scripted_llm import Step, final
from server.utils import clock


def max_overlap_per_agent(events) -> dict:
    by_agent = {}
    for event in events:
        if event["event"] in ("run_start", "run_end"):
            by_agent.setdefault(event["agent"], []).append(event)
    return {agent: peak_concurrency(items) for agent, items in by_agent.items()}


# -------------------------------------------------------------------- 1.6
def test_m7_same_agent_serialized_fixed(set_flags, scripted_llm, fake_gmail):
    set_flags("AGENT_LOCK")
    with telemetry.capture() as events:
        outcome = e5_same_agent_twice(scripted_llm)
    assert outcome["prior_answers_seen"] == [0, 1]  # the second run sees the first answer
    assert outcome["journal_tags"] == ["agent_request", "agent_response", "agent_request", "agent_response"]
    assert max_overlap_per_agent(events)["Repeat Agent"] == 1  # I5


def test_different_agents_still_run_in_parallel(set_flags, scripted_llm, fake_gmail):
    set_flags("AGENT_LOCK")
    scripted_llm.on("interaction", ia_relay())
    scripted_llm.on("execution", lambda call: final("ok"))
    scripted_llm.set_latency(0.3, "execution")

    async def main():
        with turn_scope("user"):
            dispatch("Agent A", "x")
            dispatch("Agent B", "y")
        await drain()

    with telemetry.capture() as events:
        run_async(main)
    assert peak_concurrency(events) == 2


def test_timeout_excludes_lock_wait(set_flags, scripted_llm, fake_gmail):
    """I12: a queued run with a 0.5 s timeout waits 2 s for the lock without timing out."""
    set_flags("AGENT_LOCK")
    scripted_llm.on("interaction", lambda call: final(""))
    scripted_llm.script("execution:Shared", [Step(final("long run done"), latency=2.0), Step(final("short run done"), latency=0.1)])
    slow_manager, fast_manager = ExecutionBatchManager(timeout_seconds=5), ExecutionBatchManager(timeout_seconds=0.5)

    async def main():
        with turn_scope("user"):
            first = asyncio.ensure_future(slow_manager.execute_agent("Shared", "long"))
            await asyncio.sleep(0.05)
            second = asyncio.ensure_future(fast_manager.execute_agent("Shared", "short"))
            results = await asyncio.gather(first, second)
        await drain()
        return results

    with telemetry.capture() as events:
        first, second = run_async(main)
    assert first.success and second.success, second.response
    starts = [e for e in events if e["event"] == "run_start"]
    assert starts[1]["queued_ms"] >= 1800  # queue time is reported, not charged to the run


def test_scheduler_path_serialized_with_chat_path(set_flags, scripted_llm, fake_gmail):
    from server.services.trigger_scheduler import get_trigger_scheduler

    set_flags("AGENT_LOCK")
    create_due_triggers(1, same_agent="Priya Renewal")
    scripted_llm.on("interaction", ia_relay())
    scripted_llm.on("execution", lambda call: final("ok"))
    scripted_llm.set_latency(0.3, "execution")

    async def main():
        await get_trigger_scheduler()._poll_once()  # trigger run (its own manager instance)
        with turn_scope("user"):
            dispatch("Priya Renewal", "chat request")
        await drain()

    with telemetry.capture() as events:
        run_async(main)
    runs = [e for e in events if e["event"] == "run_start"]
    assert len(runs) == 2
    assert max_overlap_per_agent(events)["Priya Renewal"] == 1


def test_lock_released_after_timeout(set_flags, scripted_llm, fake_gmail):
    set_flags("AGENT_LOCK")
    scripted_llm.on("interaction", lambda call: final(""))
    scripted_llm.script("execution:A", [Step(final("too slow"), latency=1.0), Step(final("ok"), latency=0.0)])
    manager = ExecutionBatchManager(timeout_seconds=0.3)

    async def main():
        with turn_scope("user"):
            first = await manager.execute_agent("A", "slow")
            second = await asyncio.wait_for(manager.execute_agent("A", "fast"), timeout=2)
        await drain()
        return first, second

    first, second = run_async(main)
    assert not first.success and second.success


def test_execution_agents_cannot_dispatch():
    """Lock ordering relies on EAs never calling send_message_to_agent (no nested agent locks)."""
    from server.agents.execution_agent.tools import get_tool_registry, get_tool_schemas

    names = {s["function"]["name"] for s in get_tool_schemas()} | set(get_tool_registry("x"))
    assert "send_message_to_agent" not in names


# -------------------------------------------------------------------- 1.7
def test_normalization_is_exact_not_semantic():
    assert normalize_instructions("  Send   the DRAFT\n") == "send the draft"
    assert DispatchDeduper.key("A", "send the draft") == DispatchDeduper.key("a", " SEND the  draft ")
    assert DispatchDeduper.key("A", "send the draft") != DispatchDeduper.key("A", "send the draft now")


def test_window_expires_after_completion():
    deduper = DispatchDeduper()
    deduper.register("A", "x", "r1")
    assert deduper.check("A", "x").state == "running"
    clock.advance(10_000)
    assert deduper.check("A", "x") is not None  # still running: never expires while in flight
    deduper.complete("A", "x", "r1")
    clock.advance(119)
    assert deduper.check("A", "x").state == "completed"
    clock.advance(2)
    assert deduper.check("A", "x") is None


def _run_dispatches(scripted_llm, batches, latency=0.2):
    scripted_llm.on("interaction", lambda call: final(""))
    scripted_llm.on("execution", lambda call: final("done"))
    scripted_llm.set_latency(latency, "execution")
    results = []

    async def main():
        for batch in batches:
            if batch == "advance":
                clock.advance(121)
                continue
            if batch == "settle":
                await drain()
                continue
            with turn_scope("user"):
                for agent, text in batch:
                    results.append(dispatch(agent, text))
        await drain()

    run_async(main)
    return results


def test_identical_dispatches_in_one_round_run_once(set_flags, scripted_llm, fake_gmail):
    set_flags("DISPATCH_DEDUPE")
    results = _run_dispatches(scripted_llm, [[("A", "Send the draft"), ("A", "send the  draft")]])
    assert [r.payload["status"] for r in results] == ["submitted", "duplicate_suppressed"]
    assert results[1].payload["state"] == "running"
    assert len(scripted_llm.calls_for("execution", "A")) == 1
    assert [tag for tag, _, _ in journal("A")].count("agent_request") == 1  # suppressed one never journaled


def test_identical_dispatches_from_two_concurrent_turns_run_once(set_flags, scripted_llm, fake_gmail):
    set_flags("DISPATCH_DEDUPE")
    results = _run_dispatches(scripted_llm, [[("A", "Reply to Dan")], [("A", "Reply to Dan")]])
    assert results[1].payload["status"] == "duplicate_suppressed"
    assert len(scripted_llm.calls_for("execution", "A")) == 1


def test_different_instructions_run_twice(set_flags, scripted_llm, fake_gmail):
    set_flags("DISPATCH_DEDUPE")
    results = _run_dispatches(scripted_llm, [[("A", "Reply to Dan"), ("A", "Reply to Ridgeline")]])
    assert [r.payload["status"] for r in results] == ["submitted", "submitted"]
    assert len(scripted_llm.calls_for("execution", "A")) == 2


def test_repeat_after_window_runs_again(set_flags, scripted_llm, fake_gmail):
    set_flags("DISPATCH_DEDUPE")
    results = _run_dispatches(scripted_llm, [[("A", "check inbox")], "settle", "advance", [("A", "check inbox")]])
    assert [r.payload["status"] for r in results] == ["submitted", "submitted"]
    assert len(scripted_llm.calls_for("execution", "A")) == 2


def test_repeat_within_window_after_completion_is_suppressed(set_flags, scripted_llm, fake_gmail):
    set_flags("DISPATCH_DEDUPE")
    results = _run_dispatches(scripted_llm, [[("A", "check inbox")], "settle", [("A", "check inbox")]])
    assert results[1].payload == {**results[1].payload, "status": "duplicate_suppressed", "state": "completed"}


def test_dedupe_flag_off_runs_duplicates(flags_off, scripted_llm, fake_gmail):
    results = _run_dispatches(scripted_llm, [[("A", "Send the draft"), ("A", "Send the draft")]])
    assert [r.payload["status"] for r in results] == ["submitted", "submitted"]
    assert len(scripted_llm.calls_for("execution", "A")) == 2
