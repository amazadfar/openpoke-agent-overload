"""L2: correlation context and telemetry across the real entry points (P0.3, P0.4)."""

from __future__ import annotations

import pytest

from helpers import drain, run_async, user_turn
from scenarios import create_due_triggers, e7_watcher_burst, ia_relay
from server.services import telemetry
from server.testing.scripted_llm import final, respond, tool_call


def test_user_turn_produces_correlated_events(scripted_llm, fake_gmail):
    scripted_llm.script(
        "interaction",
        [respond(tool_call("send_message_to_user", message="On it"), tool_call("send_message_to_agent", agent_name="Inbox", instructions="Fetch recent emails")), final("")],
    )
    scripted_llm.on("interaction", ia_relay())
    scripted_llm.script("execution:Inbox", [final("Here are your emails")])

    async def main():
        await user_turn("can you fetch my most recent emails?")
        await drain()

    with telemetry.capture() as events:
        run_async(main)

    names = {e["event"] for e in events}
    assert {"ia_turn", "ia_round", "llm_call", "dispatch", "run_start", "run_end", "batch_flush", "prompt_parts"} <= names

    user_turn_id = next(e for e in events if e["event"] == "dispatch")["turn_id"]
    root = [e for e in events if e["turn_id"] == user_turn_id]
    assert all(e["origin"] == "user" for e in root)
    run = next(e for e in events if e["event"] == "run_start")
    assert run["turn_id"] == user_turn_id and run["agent"] == "Inbox" and run["run_id"]
    ea_calls = [e for e in events if e["event"] == "llm_call" and e["role"] == "execution"]
    assert ea_calls and all(e["run_id"] == run["run_id"] for e in ea_calls)
    ea_parts = [e for e in events if e["event"] == "prompt_parts" and e["role"] == "execution"]
    assert ea_parts and ea_parts[0]["run_id"] == run["run_id"]

    follow_up = [e for e in events if e["event"] == "ia_turn" and e["origin"] == "agent"]
    assert len(follow_up) == 1
    assert follow_up[0]["parent_turn_id"] == user_turn_id
    assert follow_up[0]["depth"] == 1 and follow_up[0]["root_origin"] == "user"
    assert follow_up[0]["run_id"] is None  # the follow-up turn is not inside the finished run


def test_trigger_entry_point_labels_origin(scripted_llm, fake_gmail):
    from server.services.trigger_scheduler import get_trigger_scheduler

    create_due_triggers(1)
    scripted_llm.on("interaction", ia_relay())

    async def main():
        await get_trigger_scheduler()._poll_once()
        await drain()

    with telemetry.capture() as events:
        run_async(main)
    run = next(e for e in events if e["event"] == "run_start")
    assert run["origin"] == "trigger" and run["poll_id"]
    follow = next(e for e in events if e["event"] == "ia_turn")
    assert follow["origin"] == "agent" and follow["root_origin"] == "trigger"


def test_watcher_entry_point_labels_origin(scripted_llm, fake_gmail):
    with telemetry.capture() as events:
        e7_watcher_burst(scripted_llm, fake_gmail, emails=1, classify_latency=0.0, ia_latency=0.0)
    turns = [e for e in events if e["event"] == "ia_turn"]
    assert turns and all(t["origin"] == "email" and t["poll_id"] for t in turns)
    poll = [e for e in events if e["event"] == "poll" and e["kind"] == "watcher"]
    assert poll and poll[-1]["new"] == 1


def test_chat_handler_labels_user_origin_with_raw_text(scripted_llm, fake_gmail):
    from server.models import ChatMessage, ChatRequest
    from server.services.conversation.chat_handler import handle_chat_request

    seen = {}

    def ia(call):
        from server.services.context import current_turn

        seen["turn"] = current_turn()
        return final("hi")

    scripted_llm.on("interaction", ia)

    async def main():
        response = await handle_chat_request(ChatRequest(messages=[ChatMessage(role="user", content="  Hello there  ")]))
        await drain()
        return response.status_code

    assert run_async(main) == 202
    assert seen["turn"].origin == "user"
    assert seen["turn"].user_message == "Hello there"
