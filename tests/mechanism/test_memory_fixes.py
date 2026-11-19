"""1.2 / 1.3 (M2): bounded EA history and preserved identifiers. Paired with test_m2_journal_growth_baseline."""

from __future__ import annotations

import pytest

from helpers import dispatch, drain, run_async
from scenarios import ea_prompt_chars, follow_up_search_calls, old_draft_follow_up, seed_thread, CLIENT
from server.services.context import turn_scope
from server.testing.scripted_llm import final, respond, tool_call


def test_m2_journal_growth_flat_after_k(set_flags):
    set_flags("EA_HISTORY_WINDOW")
    sizes = {runs: ea_prompt_chars(runs) for runs in (1, 10, 30, 90, 180)}
    assert sizes[10] == sizes[30] == sizes[90] == sizes[180]  # flat once the journal has >= K requests
    assert sizes[180] < 6 * (sizes[1] - ea_prompt_chars(0))  # bounded by ~K runs, not 180
    assert sizes[180] < 40000 + 5000  # under the char cap plus template


def test_baseline_remembers_old_draft_but_prompt_grows(flags_off, scripted_llm, fake_gmail):
    outcome = old_draft_follow_up(scripted_llm, fake_gmail)
    assert outcome["found_in_prompt"]  # the full journal is replayed (M2's cost, not a memory loss)


@pytest.mark.xfail(strict=True, reason="Known limit of the window alone: facts older than K requests are forgotten. "
                   "The agent card (2.2) fixes this; see test_old_draft_found_on_card.")
def test_window_only_forgets_draft_older_than_k(set_flags, scripted_llm, fake_gmail):
    set_flags("EA_HISTORY_WINDOW", "KEEP_TOOL_IDS")
    outcome = old_draft_follow_up(scripted_llm, fake_gmail)
    assert outcome["found_in_prompt"]


def test_send_follow_up_prompt_contains_draft_id(set_flags, scripted_llm, fake_gmail):
    set_flags("KEEP_TOOL_IDS")
    msg = seed_thread(fake_gmail)
    seen = {}

    def ea(call):
        if call.first_user_content.startswith("Draft"):
            if call.tool_results():
                return final("Draft created.")
            return respond(tool_call("gmail_create_draft", recipient_email=CLIENT, subject="Re", body="Hi", thread_id=msg.thread_id))
        seen["prompt"] = call.system
        return final("ok")

    scripted_llm.on("execution", ea)
    scripted_llm.on("interaction", lambda call: final(""))

    async def main():
        for text in ("Draft a reply to Priya", "Send it"):
            with turn_scope("user"):
                dispatch("Priya Renewal", text)
            await drain()

    run_async(main)
    draft_id = next(iter(fake_gmail.drafts))
    tool_ids_lines = [l for l in seen["prompt"].splitlines() if l.startswith("<tool_ids")]
    assert any(draft_id in line for line in tool_ids_lines)


def test_follow_up_search_calls_drop(scripted_llm, fake_gmail, set_flags, flags_off):
    baseline = follow_up_search_calls(scripted_llm, fake_gmail)
    assert baseline["follow_up_searches"] == 1  # the needed thread id was truncated out of the journal
    assert baseline["replies"] == 1


def test_follow_up_search_calls_drop_fixed(scripted_llm, fake_gmail, set_flags):
    set_flags("KEEP_TOOL_IDS")
    fixed = follow_up_search_calls(scripted_llm, fake_gmail)
    assert fixed["follow_up_searches"] == 0
    assert fixed["replies"] == 1
