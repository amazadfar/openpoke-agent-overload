"""1.1 / M3: `wait` ends the interaction turn (flag WAIT_ENDS_TURN). Paired with test_m3_wait_spin_baseline."""

from __future__ import annotations

from helpers import agent_messages, conversation_entries, drain, run_async, user_turn
from scenarios import wait_spin
from scripts import ia_waiter, is_agent_turn, round_index
from server.services import telemetry
from server.services.execution import get_agent_roster
from server.testing.scripted_llm import final, respond, tool_call


def test_m3_wait_ends_turn_fixed(set_flags, scripted_llm, fake_gmail):
    set_flags("WAIT_ENDS_TURN")
    outcome = wait_spin(scripted_llm)
    assert outcome["ia_calls"] == 1
    assert outcome["ended_by"] == "wait"
    assert outcome["error"] is None


def test_mixed_round_executes_every_tool_then_ends(set_flags, scripted_llm, fake_gmail):
    set_flags("WAIT_ENDS_TURN")
    scripted_llm.script("interaction", [
        respond(tool_call("send_message_to_agent", agent_name="Inbox", instructions="Fetch emails"),
                tool_call("wait", reason="Agent is working")),
    ])
    scripted_llm.on("interaction", lambda call: final(""))

    async def main():
        result = await user_turn("fetch my emails")
        await drain()
        return result

    with telemetry.capture() as events:
        run_async(main)
    first_turn = [e for e in events if e["event"] == "ia_turn"][0]
    assert first_turn["ended_by"] == "wait" and first_turn["rounds"] == 1
    assert get_agent_roster().get_agents() == ["Inbox"]  # the dispatch in the same round ran
    assert any(tag == "wait" for tag, _, _ in conversation_entries())
    assert len(scripted_llm.calls_for("execution", "Inbox")) == 1


def test_reply_then_dispatch_across_rounds_still_dispatches(set_flags, scripted_llm, fake_gmail):
    """The real run replied in round 1 and dispatched in round 2: replying must not end the turn."""
    set_flags("WAIT_ENDS_TURN")
    scripted_llm.script("interaction", [
        respond(tool_call("send_message_to_user", message="Checking your inbox")),
        respond(tool_call("send_message_to_agent", agent_name="Inbox", instructions="Fetch emails")),
        respond(tool_call("wait", reason="Waiting")),
    ])
    scripted_llm.on("interaction", ia_waiter())

    async def main():
        await user_turn("can you fetch my most recent emails?")
        await drain()

    with telemetry.capture() as events:
        run_async(main)
    turns = [e for e in events if e["event"] == "ia_turn"]
    assert [t["ended_by"] for t in turns] == ["wait", "wait"]
    assert turns[0]["rounds"] == 3 and turns[0]["dispatches"] == 1
    assert len(agent_messages()) == 1


def test_wait_in_first_round_of_agent_turn_ends_it(set_flags, scripted_llm, fake_gmail):
    set_flags("WAIT_ENDS_TURN")
    scripted_llm.on("interaction", lambda call: respond(tool_call("wait", reason="Already told the user"))
                    if is_agent_turn(call) else final(""))
    scripted_llm.script("interaction", [respond(tool_call("send_message_to_agent", agent_name="A", instructions="x")), final("")])

    async def main():
        await user_turn("do x")
        await drain()

    run_async(main)
    agent_turn_calls = [c for c in scripted_llm.calls_for("interaction") if is_agent_turn(c)]
    assert len(agent_turn_calls) == 1


def test_s9_traced_request_calls_drop(scripted_llm, fake_gmail, set_flags, flags_off):
    """S9 shape (reply, dispatch, then wait): 15 LLM calls in the real trace; far fewer with the fix."""

    def run_once():
        scripted_llm.calls.clear()
        scripted_llm.script("interaction", [
            respond(tool_call("send_message_to_user", message="Checking your inbox")),
            respond(tool_call("send_message_to_agent", agent_name="Recent Emails", instructions="Fetch recent emails")),
        ])
        scripted_llm.on("interaction", ia_waiter())

        async def main():
            await user_turn("can you fetch my most recent emails?")
            await drain()

        run_async(main)
        return len(scripted_llm.calls_for("interaction"))

    baseline_calls = run_once()
    set_flags("WAIT_ENDS_TURN")
    fixed_calls = run_once()
    assert baseline_calls == 8 + 8  # both turns spin to the limit
    assert fixed_calls == 3 + 2
