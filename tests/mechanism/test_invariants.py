"""Cross-cutting invariants not covered by a single fix's tests: I10, I11, I12 (gate)."""

from __future__ import annotations

import asyncio

from helpers import drain, run_async, user_turn
from server.agents.execution_agent.batch_manager import ExecutionBatchManager
from server.services import telemetry
from server.services.context import turn_scope
from server.testing.scripted_llm import Step, final, respond, tool_call
from server.utils import tasks


def test_i10_telemetry_failure_never_fails_a_request(scripted_llm, fake_gmail, monkeypatch):
    def broken_sink(record):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry, "_write_file", broken_sink)
    scripted_llm.script("interaction", [respond(tool_call("send_message_to_user", message="Hello!")), final("")])
    result = run_async(lambda: user_turn("hi"))
    assert result.success and result.response == "Hello!"
    assert telemetry.dropped > 0


def test_i11_tracked_tasks_are_cancelled_at_shutdown():
    async def main():
        started = asyncio.Event()

        async def forever():
            started.set()
            await asyncio.sleep(3600)

        tasks.spawn(forever(), name="forever")
        await started.wait()
        cancelled = await tasks.shutdown(0.05)
        return cancelled, tasks.pending()

    cancelled, leftover = run_async(main)
    assert cancelled == 1 and leftover == set()


def test_i11_failed_background_task_is_surfaced():
    async def main():
        async def boom():
            raise ValueError("lost before")

        with telemetry.capture() as events:
            task = tasks.spawn(boom(), name="boom")
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
        return events

    events = run_async(main)
    assert [e["task"] for e in events if e["event"] == "background_task_failed"] == ["boom"]


def test_i12_gate_wait_is_not_charged_to_the_run(set_flags, scripted_llm, fake_gmail, monkeypatch):
    set_flags("CONCURRENCY_CAP")
    monkeypatch.setenv("OPENPOKE_CONCURRENCY_TOTAL", "2")  # one non-reserved slot
    scripted_llm.on("interaction", lambda call: final(""))
    scripted_llm.script("execution:Long", [Step(final("long done"), latency=1.5)])
    scripted_llm.script("execution:Short", [Step(final("short done"), latency=0.1)])
    long_manager, short_manager = ExecutionBatchManager(timeout_seconds=5), ExecutionBatchManager(timeout_seconds=0.5)

    async def main():
        with turn_scope("trigger"):
            first = asyncio.ensure_future(long_manager.execute_agent("Long", "x"))
            await asyncio.sleep(0.05)
            second = asyncio.ensure_future(short_manager.execute_agent("Short", "y"))
            results = await asyncio.gather(first, second)
        await drain()
        return results

    with telemetry.capture() as events:
        first, second = run_async(main)
    assert first.success and second.success, second.response
    short_start = next(e for e in events if e["event"] == "run_start" and e["agent"] == "Short")
    assert short_start["queued_ms"] >= 1300
