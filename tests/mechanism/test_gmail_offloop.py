"""1.5 / M5: Gmail reads off the event loop (flag GMAIL_READS_OFF_LOOP). Paired with test_m5_gmail_blocks_loop_baseline."""

from __future__ import annotations

import asyncio

import pytest

from helpers import Ticker, dispatch, drain, run_async
from scenarios import e1_loop_freeze, e7_watcher_burst, search_responder, seed_thread
from server.services.context import turn_scope
from server.testing.scripted_llm import final, respond, tool_call

pytestmark = pytest.mark.slow


def test_read_blocks_loop_baseline(flags_off, scripted_llm, fake_gmail):
    outcome = e1_loop_freeze(scripted_llm, fake_gmail, tool="gmail_list_drafts")
    assert outcome["stall_s"] >= 1.4


def test_read_does_not_block_loop_fixed(set_flags, scripted_llm, fake_gmail):
    set_flags("GMAIL_READS_OFF_LOOP")
    outcome = e1_loop_freeze(scripted_llm, fake_gmail, tool="gmail_list_drafts")
    assert outcome["stall_s"] < 0.1
    assert outcome["b_finished_s"] < 0.6  # the 0.3 s agent finishes on time
    assert outcome["gmail_calls"] == ["GMAIL_LIST_DRAFTS"]


def test_sends_still_block_until_1_9(set_flags, scripted_llm, fake_gmail):
    set_flags("GMAIL_READS_OFF_LOOP")
    outcome = e1_loop_freeze(scripted_llm, fake_gmail, tool="gmail_reply_to_thread")
    assert outcome["stall_s"] >= 1.4


def test_concurrent_reads_return_correct_results(set_flags, scripted_llm, fake_gmail):
    set_flags("GMAIL_READS_OFF_LOOP")
    fake_gmail.set_latency(0.5)
    fake_gmail.execute("GMAIL_CREATE_EMAIL_DRAFT", "u", {"recipient_email": "a@example.com", "subject": "A", "body": "x"})
    results = {}

    def ea(call):
        name = call.agent
        if call.tool_results():
            results[name] = call.tool_results()[-1]
            return final("done")
        tool = "gmail_list_drafts" if name == "Lister" else "gmail_search_people"
        return respond(tool_call(tool, **({} if tool == "gmail_list_drafts" else {"query": "nobody"})))

    scripted_llm.on("execution", ea)
    scripted_llm.on("interaction", lambda call: final(""))

    async def main():
        ticker = Ticker()
        t = ticker.start()
        with turn_scope("user"):
            dispatch("Lister", "list drafts")
            dispatch("Searcher", "search people")
        await drain(ignore=[t])
        await ticker.stop()
        return ticker.max_lateness

    stall = run_async(main)
    assert stall < 0.1
    assert len(results["Lister"]["result"]["data"]["drafts"]) == 1
    assert results["Searcher"]["result"]["data"] == {"results": []}


def test_watcher_fetch_does_not_block_loop(set_flags, scripted_llm, fake_gmail):
    set_flags("GMAIL_READS_OFF_LOOP")
    fake_gmail.set_latency(1.0, "GMAIL_FETCH_EMAILS")
    from server.services.gmail import get_important_email_watcher

    async def main():
        ticker = Ticker()
        t = ticker.start()
        await get_important_email_watcher()._poll_once()
        await ticker.stop()
        return ticker.max_lateness

    assert run_async(main) < 0.1


def test_search_task_fetch_does_not_block_loop(set_flags, scripted_llm, fake_gmail):
    set_flags("GMAIL_READS_OFF_LOOP")
    fake_gmail.set_latency(1.0, "GMAIL_FETCH_EMAILS")
    seed_thread(fake_gmail)
    scripted_llm.on("search", search_responder("renewal"))
    scripted_llm.script("execution:Finder", [respond(tool_call("task_email_search", search_query="renewal")), final("found")])
    scripted_llm.on("interaction", lambda call: final(""))

    async def main():
        ticker = Ticker()
        t = ticker.start()
        with turn_scope("user"):
            dispatch("Finder", "find renewal")
        await drain(ignore=[t])
        await ticker.stop()
        return ticker.max_lateness

    assert run_async(main) < 0.1
    fetches = [a for a in fake_gmail.actions_log() if a["action"] == "GMAIL_FETCH_EMAILS"]
    assert fetches and fetches[0]["run_id"]  # context (run id) visible inside the worker thread


def test_gmail_status_route_runs_off_loop(set_flags, monkeypatch):
    set_flags("GMAIL_READS_OFF_LOOP")
    import threading

    from server.models import GmailStatusPayload
    from server.routes import gmail as gmail_routes

    seen = {}

    def fake_status(payload):
        seen["thread"] = threading.current_thread().name
        return "ok"

    monkeypatch.setattr(gmail_routes, "fetch_status", fake_status)
    assert run_async(lambda: gmail_routes.gmail_status(GmailStatusPayload(user_id="u"))) == "ok"
    assert seen["thread"].startswith("gmail")
