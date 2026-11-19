"""Reusable L2 scenarios reproducing the investigation's experiments (E1-E7).

Each function drives the REAL orchestration code (interaction agent loop,
send_message_to_agent, batch manager, execution runtime, scheduler, watcher)
with a ScriptedLLM and a FakeGmail, and returns measured outcomes. The
baseline characterization tests and the paired fix tests call the same
function under different flags, so each pair differs only in the flag.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

from helpers import Ticker, agent_messages, dispatch, drain, journal, run_async, user_turn
from scripts import ia_relay, is_agent_turn, round_index
from server.services import telemetry
from server.services.context import turn_scope
from server.testing.scripted_llm import ScriptedLLM, Step, final, respond, tool_call

CLIENT = "priya.patel@example.com"


def peak_concurrency(events: List[Dict[str, Any]]) -> int:
    marks = []
    for event in events:
        if event["event"] == "run_start":
            marks.append((event["mono"], 1))
        elif event["event"] == "run_end":
            marks.append((event["mono"], -1))
    peak = current = 0
    for _, delta in sorted(marks, key=lambda m: (m[0], m[1])):
        current += delta
        peak = max(peak, current)
    return peak


def follow_up_calls(llm: ScriptedLLM) -> list:
    """IA calls that opened an agent-message turn (round 0)."""
    return [c for c in llm.calls_for("interaction") if is_agent_turn(c) and round_index(c) == 0]


def seed_thread(fake_gmail, sender: str = f"Priya Patel <{CLIENT}>"):
    return fake_gmail.inject_email(sender=sender, subject="Auto renewal AU-77120", body="Please confirm my renewal.")


def send_args(tool: str, msg) -> Dict[str, Any]:
    if tool == "gmail_reply_to_thread":
        return {"thread_id": msg.thread_id, "recipient_email": CLIENT, "message_body": "Confirmed, thank you."}
    if tool == "gmail_list_drafts":
        return {}
    if tool == "gmail_forward_email":
        return {"message_id": msg.id, "recipient_email": "colleague@example.com"}
    raise ValueError(tool)


# --------------------------------------------------------------------- M3
def wait_spin(llm: ScriptedLLM) -> Dict[str, Any]:
    llm.on("interaction", lambda call: respond(tool_call("wait", reason="Waiting for results")))
    with telemetry.capture() as events:
        result = run_async(lambda: user_turn("can you fetch my most recent emails?"))
    (turn_event,) = [e for e in events if e["event"] == "ia_turn"]
    return {
        "ia_calls": len(llm.calls_for("interaction")),
        "ended_by": turn_event["ended_by"],
        "rounds": turn_event["rounds"],
        "error": result.error,
        "events": events,
    }


# --------------------------------------------------------------------- E1 / M5
def e1_loop_freeze(llm: ScriptedLLM, fake_gmail, *, tool: str, latency: float = 1.5) -> Dict[str, Any]:
    msg = seed_thread(fake_gmail)
    action = {"gmail_reply_to_thread": "GMAIL_REPLY_TO_THREAD", "gmail_list_drafts": "GMAIL_LIST_DRAFTS",
              "gmail_forward_email": "GMAIL_FORWARD_MESSAGE"}[tool]
    fake_gmail.set_latency(latency, action)
    llm.on("interaction", ia_relay())
    llm.script("execution:Sender", [Step(respond(tool_call(tool, **send_args(tool, msg))), latency=0.05), final("done")])
    llm.script("execution:Thinker", [Step(final("thought about it"), latency=0.3)])

    async def main():
        ticker = Ticker()
        ticker_task = ticker.start()
        await asyncio.sleep(0.1)  # ticker warm-up
        t0 = time.monotonic()
        with turn_scope("user"):
            dispatch("Sender", "Do the Gmail action")
            dispatch("Thinker", "Think")
        await drain(ignore=[ticker_task])
        await ticker.stop()
        thinker = llm.calls_for("execution", "Thinker")[0]
        return {"stall_s": ticker.max_lateness, "b_finished_s": thinker.ended - t0}

    outcome = run_async(main)
    outcome["gmail_calls"] = [e["action"] for e in fake_gmail.actions_log()]
    return outcome


# --------------------------------------------------------------------- E2 / M6
def e2_fast_and_slow(llm: ScriptedLLM, fast: float = 0.5, slow: float = 6.0) -> Dict[str, Any]:
    llm.on("interaction", ia_relay())
    llm.script("execution:Fast", [Step(final("fast result"), latency=fast)])
    llm.script("execution:Slow", [Step(final("slow result"), latency=slow)])

    async def main():
        # Unrelated work arriving together: a user request and a trigger-started task (M6).
        t0 = time.monotonic()
        with turn_scope("user", user_message="quick question"):
            dispatch("Fast", "quick task")
        with turn_scope("trigger"):
            dispatch("Slow", "slow task")
        await drain()
        return t0

    t0 = run_async(main)
    turns = follow_up_calls(llm)
    fast_turn = next(c for c in turns if "Fast:" in c.first_user_content)
    return {"fast_delivered_s": fast_turn.started - t0, "follow_up_turns": len(turns)}


# --------------------------------------------------------------------- E3 / M6
def e3_staggered(llm: ScriptedLLM, gap: float = 2.0, duration: float = 3.0) -> Dict[str, Any]:
    llm.on("interaction", ia_relay())
    for name in ("Agent A", "Agent B", "Agent C"):
        llm.script(f"execution:{name}", [Step(final(f"{name} done"), latency=duration)])

    async def main():
        t0 = time.monotonic()
        for index, name in enumerate(("Agent A", "Agent B", "Agent C")):
            if index:
                await asyncio.sleep(gap)
            with turn_scope("user", user_message=f"task {index}"):
                dispatch(name, f"task for {name}")
        await drain()
        return t0

    t0 = run_async(main)
    turns = follow_up_calls(llm)
    first = turns[0].first_user_content
    a_turn = next(c for c in turns if "Agent A:" in c.first_user_content)
    return {
        "follow_up_turns": len(turns),
        "results_in_first_turn": first.count("[SUCCESS]"),
        "a_delivered_s": a_turn.started - t0,
    }


# --------------------------------------------------------------------- E4 / M4
def create_due_triggers(count: int, *, same_agent: Optional[str] = None) -> List[int]:
    from server.services.triggers import get_trigger_service
    from server.utils import clock

    past = (clock.now() - timedelta(minutes=1)).isoformat()
    service = get_trigger_service()
    ids = []
    for i in range(count):
        record = service.create_trigger(
            agent_name=same_agent or f"Reminder {i:02d}", payload=f"Remind the user about task {i}", start_time=past
        )
        ids.append(record.id)
    return ids


def e4_trigger_storm(llm: ScriptedLLM, count: int = 20, ea_latency: float = 0.1) -> Dict[str, Any]:
    from server.services.trigger_scheduler import get_trigger_scheduler

    trigger_ids = create_due_triggers(count)
    llm.on("interaction", ia_relay())
    llm.on("execution", lambda call: final("Reminder delivered"))
    llm.set_latency(ea_latency, "execution")

    async def main():
        await get_trigger_scheduler()._poll_once()
        await drain()

    with telemetry.capture() as events:
        run_async(main)
    return {
        "follow_up_turns": len(follow_up_calls(llm)),
        "peak_concurrent_runs": peak_concurrency(events),
        "ia_calls": len(llm.calls_for("interaction")),
        "events": events,
        "trigger_ids": trigger_ids,
    }


# --------------------------------------------------------------------- E5 / M7
def e5_same_agent_twice(llm: ScriptedLLM, ea_latency: float = 0.2) -> Dict[str, Any]:
    llm.on("interaction", ia_relay())
    llm.on("execution", lambda call: final("Answer for: " + call.first_user_content))
    llm.set_latency(ea_latency, "execution")

    async def main():
        with turn_scope("user"):
            dispatch("Repeat Agent", "first question")
            dispatch("Repeat Agent", "second question")
        await drain()

    run_async(main)
    calls = llm.calls_for("execution", "Repeat Agent")
    return {
        "prior_answers_seen": [c.system.count("<agent_response") for c in calls],
        "journal_tags": [tag for tag, _, _ in journal("Repeat Agent")],
        "runs": len(calls),
    }


# --------------------------------------------------------------------- E6
def e6_timeout_after_send(llm: ScriptedLLM, fake_gmail, *, timeout: float = 3.0, send_latency: float = 1.5,
                          tool: str = "gmail_reply_to_thread") -> Dict[str, Any]:
    from server.agents.execution_agent.batch_manager import get_batch_manager

    msg = seed_thread(fake_gmail)
    action = {"gmail_reply_to_thread": "GMAIL_REPLY_TO_THREAD", "gmail_forward_email": "GMAIL_FORWARD_MESSAGE"}[tool]
    fake_gmail.set_latency(send_latency, action)
    get_batch_manager().timeout_seconds = timeout
    llm.on("interaction", ia_relay())
    llm.script(
        "execution:Mailer",
        [Step(respond(tool_call(tool, **send_args(tool, msg))), latency=0.2), Step(final("sent"), latency=timeout + 2)],
    )

    async def main():
        with turn_scope("user", user_message="reply to Priya"):
            dispatch("Mailer", "Reply to Priya confirming the renewal")
        await drain()

    run_async(main)
    messages = agent_messages()
    return {
        "sends": len(fake_gmail.external_sends()),
        "reported": messages[0] if messages else "",
        "all_reports": messages,
        "thread_id": msg.thread_id,
    }


# --------------------------------------------------------------------- E7 / M4
def e7_watcher_burst(llm: ScriptedLLM, fake_gmail, *, emails: int = 10, classify_latency: float = 0.3,
                     ia_latency: float = 0.25) -> Dict[str, Any]:
    from server.services.gmail import get_important_email_watcher

    llm.on("interaction", ia_relay())
    llm.set_latency(ia_latency, "interaction")
    llm.on("classifier", lambda call: respond(tool_call("mark_email_importance", important=True,
                                                        summary="Client needs a certificate of insurance today.")))
    llm.set_latency(classify_latency, "classifier")
    watcher = get_important_email_watcher()

    async def main():
        await watcher._poll_once()  # warm-up poll (marks the existing inbox seen)
        for i in range(emails):
            fake_gmail.inject_email(sender=f"Client {i} <client{i}@example.com>", subject=f"Urgent COI request {i}",
                                    body="Please send a certificate of insurance today.")
        t0 = time.monotonic()
        await watcher._poll_once()
        poll_s = time.monotonic() - t0
        inside = len(agent_messages())
        await drain()
        return poll_s, inside

    poll_s, inside = run_async(main)
    return {"poll_s": poll_s, "turns_inside_poll": inside, "turns_total": len(agent_messages()), "watcher": watcher}


# ------------------------------------------------------------- growth curves
_REQUEST = "Fetch the user's most recent emails from the inbox and summarize them briefly, including senders, subjects and any action items. " * 2
_ACTION = "Calling task_email_search with: " + json.dumps({"search_query": "in:inbox category:primary newer_than:7d most recent emails " * 3})
_TOOL_RESPONSE = "task_email_search: " + json.dumps(
    [{"id": f"{i:016x}", "thread_id": f"{i + 100:016x}", "sender": f"Sender {i} <s{i}@example.com>", "subject": f"Subject {i}"} for i in range(6)]
)[:560]
_RESPONSE = "Here are the 10 most recent emails:\n" + "\n".join(
    f"{i}. From Sender {i} <s{i}@example.com> - Subject line number {i} about a policy renewal, received today; action: review and reply." for i in range(18)
)


def build_journal(agent: str, runs: int) -> None:
    from server.services.execution import get_execution_agent_logs

    logs = get_execution_agent_logs()
    for _ in range(runs):
        logs.record_request(agent, _REQUEST)
        logs.record_action(agent, _ACTION)
        logs.record_tool_response(agent, "task_email_search", _TOOL_RESPONSE)
        logs.record_agent_response(agent, _RESPONSE)


def ea_prompt_chars(runs: int, agent: str = "Recent Emails") -> int:
    from server.agents.execution_agent.runtime import ExecutionAgentRuntime
    from server.services.execution import get_execution_agent_logs

    get_execution_agent_logs().clear_all()
    build_journal(agent, runs)
    return len(ExecutionAgentRuntime(agent).agent.build_system_prompt_with_history())


def build_roster(count: int) -> List[str]:
    from server.services.execution import get_agent_roster, roster as roster_module

    names = [f"Client {i:04d} renewal follow-up" for i in range(count)]
    roster_module._ROSTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    roster_module._ROSTER_PATH.write_text(json.dumps(names))
    get_agent_roster().load()
    return names


def roster_block_chars(count: int, query: str = "hi") -> int:
    from server.agents.interaction_agent.agent import prepare_message_with_history

    build_roster(count)
    with telemetry.capture() as events:
        prepare_message_with_history(query, "", "user")
    (parts,) = [e for e in events if e["event"] == "prompt_parts"]
    return parts["roster_chars"]


# ------------------------------------------------------------ memory (M2)
def search_responder(query: str):
    """Scripted search sub-agent: one Gmail query, then return every message it found."""

    def handler(call):
        if round_index(call) == 0:
            return respond(tool_call("gmail_fetch_emails", query=query, max_results=20))
        # Baseline quirk: search results contain datetimes, json.dumps fails and the
        # search tool falls back to {"repr": "..."}; ids are read from either form.
        found = []
        for result in call.tool_results():
            found += [m["id"] for m in result.get("messages", [])]
            found += re.findall(r"'id': '([0-9a-f]{16})'", result.get("repr", ""))
        return respond(tool_call("return_search_results", message_ids=found))

    return handler


def old_draft_follow_up(llm: ScriptedLLM, fake_gmail, *, filler_runs: int = 6) -> Dict[str, Any]:
    """Run 1 creates a draft; `filler_runs` unrelated runs follow; the last run must send that draft.

    Returns whether the last run's FIRST prompt contained the draft id (memory, not re-search).
    """
    msg = seed_thread(fake_gmail)
    llm.on("interaction", lambda call: final(""))
    state: Dict[str, Any] = {}

    def ea(call):
        instruction = call.first_user_content
        if instruction.startswith("Draft"):
            if round_index(call) == 0:
                return respond(tool_call("gmail_create_draft", recipient_email=CLIENT, subject="Re: Auto renewal",
                                         body="Hi Priya, confirmed.", thread_id=msg.thread_id))
            return final("Draft created for Priya.")
        if instruction.startswith("Send"):
            if round_index(call) == 0:
                state["first_prompt"] = call.system
            return final("ok")
        return final("Noted.")

    llm.on("execution", ea)

    async def main():
        for text in ["Draft a reply to Priya"] + [f"Unrelated note {i}" for i in range(filler_runs)] + ["Send the Priya draft"]:
            with turn_scope("user", user_message=text):
                dispatch("Priya Renewal", text)
            await drain()

    run_async(main)
    draft_id = next(iter(fake_gmail.drafts))
    return {"draft_id": draft_id, "found_in_prompt": draft_id in state.get("first_prompt", ""),
            "prompt_chars": len(state.get("first_prompt", ""))}


def follow_up_search_calls(llm: ScriptedLLM, fake_gmail, target_thread: str = "18c1a00000000007") -> Dict[str, Any]:
    """Run 1 searches broadly; run 2 needs one thread from those results.

    The scripted EA is 'rational': it replies directly when the thread id is in
    its prompt, otherwise it searches again (what a real model must do).
    """
    from helpers import load_broker_fixture

    fake_gmail.load_fixture(load_broker_fixture())
    llm.on("interaction", lambda call: final(""))
    llm.on("search", search_responder("renewal"))
    searches = {"follow_up": 0}

    def ea(call):
        instruction = call.first_user_content
        if instruction.startswith("Find"):
            return respond(tool_call("task_email_search", search_query="renewal emails")) if round_index(call) == 0 \
                else final("Found the renewal emails.")
        if round_index(call) == 0:
            if target_thread in call.system:
                return respond(tool_call("gmail_reply_to_thread", thread_id=target_thread,
                                         recipient_email="marcus.webb@example.com", message_body="Yes, monthly works."))
            searches["follow_up"] += 1
            return respond(tool_call("task_email_search", search_query="Marcus Webb AU-77210"))
        already_replied = "gmail_reply_to_thread" in json.dumps([m.get("tool_calls") for m in call.messages])
        if not already_replied and target_thread in json.dumps(call.tool_results()):
            return respond(tool_call("gmail_reply_to_thread", thread_id=target_thread,
                                     recipient_email="marcus.webb@example.com", message_body="Yes, monthly works."))
        return final("Replied to Marcus.")

    llm.on("execution", ea)

    async def main():
        for text in ("Find all renewal emails", "Reply to Marcus Webb's AU-77210 payment question"):
            with turn_scope("user", user_message=text):
                dispatch("Renewals", text)
            await drain()

    run_async(main)
    return {"follow_up_searches": searches["follow_up"], "replies": len(fake_gmail.external_sends())}


def ea_prompt_chars_with_card(runs: int, agent: str = "Recent Emails") -> int:
    """EA prompt size when the card (2.2) is on: journal + registry indexed from it + card in the prompt."""
    from server.agents.execution_agent.runtime import ExecutionAgentRuntime
    from server.services.agents import card
    from server.services.agents.registry import get_agent_registry
    from server.services.execution import get_execution_agent_logs

    get_execution_agent_logs().clear_all()
    get_agent_registry().clear()
    build_journal(agent, runs)
    registry = get_agent_registry()
    row, _ = registry.create(agent)
    registry.index_journal(row)
    return len(ExecutionAgentRuntime(agent, card_text=card.render_for_prompt(agent)).agent.build_system_prompt_with_history())
