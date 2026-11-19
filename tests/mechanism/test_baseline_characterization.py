"""Characterization tests (P0.5): pin TODAY's behaviour with every flag off.

Each test reproduces one experiment from the investigation (E1-E7) or a
measured growth curve, using the real orchestration code with a scripted LLM
and a synchronous fake Gmail. Each has a paired fix test in a later file that
runs the same scenario with the fix's flag on.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from helpers import Ticker, agent_messages, dispatch, drain, journal, run_async, user_turn
from scripts import ia_relay
from server.services import telemetry
from server.services.context import turn_scope
from server.testing.scripted_llm import Step, final, respond, tool_call
from scenarios import (
    build_journal,
    build_roster,
    e1_loop_freeze,
    e2_fast_and_slow,
    e3_staggered,
    e4_trigger_storm,
    e5_same_agent_twice,
    e6_timeout_after_send,
    e7_watcher_burst,
    ea_prompt_chars,
    roster_block_chars,
    wait_spin,
)

pytestmark = pytest.mark.baseline


def test_m3_wait_spin_baseline(flags_off, scripted_llm, fake_gmail):
    outcome = wait_spin(scripted_llm)
    assert outcome["ia_calls"] == 8
    assert outcome["ended_by"] == "limit"
    assert "iteration limit" in (outcome["error"] or "")


@pytest.mark.slow
def test_m5_gmail_blocks_loop_baseline(flags_off, scripted_llm, fake_gmail):
    outcome = e1_loop_freeze(scripted_llm, fake_gmail, tool="gmail_reply_to_thread")
    assert outcome["stall_s"] >= 1.4
    assert outcome["b_finished_s"] >= 1.5  # the 0.3 s agent could not finish while the loop was frozen


@pytest.mark.slow
def test_m6_fast_result_waits_baseline(flags_off, scripted_llm, fake_gmail):
    outcome = e2_fast_and_slow(scripted_llm)
    assert outcome["fast_delivered_s"] >= 5.5
    assert outcome["follow_up_turns"] == 1


@pytest.mark.slow
def test_m6_stagger_baseline(flags_off, scripted_llm, fake_gmail):
    outcome = e3_staggered(scripted_llm)
    assert outcome["follow_up_turns"] == 1
    assert outcome["results_in_first_turn"] == 3
    assert outcome["a_delivered_s"] >= 6.5  # A finished at ~3 s


def test_m4_trigger_storm_baseline(flags_off, scripted_llm, fake_gmail):
    outcome = e4_trigger_storm(scripted_llm, count=20)
    assert outcome["follow_up_turns"] == 20
    assert outcome["peak_concurrent_runs"] == 20
    assert outcome["ia_calls"] == 40


def test_m7_same_agent_overlap_baseline(flags_off, scripted_llm, fake_gmail):
    outcome = e5_same_agent_twice(scripted_llm)
    assert outcome["prior_answers_seen"] == [0, 0]
    assert outcome["journal_tags"] == ["agent_request", "agent_request", "agent_response", "agent_response"]


@pytest.mark.slow
def test_timeout_after_send_baseline(flags_off, scripted_llm, fake_gmail):
    outcome = e6_timeout_after_send(scripted_llm, fake_gmail)
    assert outcome["sends"] == 1
    assert "[FAILED]" in outcome["reported"] and "timed out" in outcome["reported"]


@pytest.mark.slow
def test_m4_watcher_serial_turns_baseline(flags_off, scripted_llm, fake_gmail):
    outcome = e7_watcher_burst(scripted_llm, fake_gmail, emails=10)
    assert outcome["turns_inside_poll"] == 10
    assert outcome["poll_s"] >= 10 * (0.3 + 2 * 0.25) * 0.95


def test_m2_journal_growth_baseline(flags_off):
    sizes = {runs: ea_prompt_chars(runs) for runs in (1, 10, 30, 90, 180)}
    per_run = (sizes[180] - sizes[1]) / 179
    assert per_run > 3000  # each copied run adds its full request/action/response
    for runs in (10, 30, 90):
        predicted = sizes[1] + per_run * (runs - 1)
        assert abs(sizes[runs] - predicted) / predicted < 0.02  # linear


def test_m1_roster_growth_baseline(flags_off):
    sizes = {n: roster_block_chars(n) for n in (10, 100, 500, 2000)}
    per_agent = (sizes[2000] - sizes[10]) / 1990
    assert per_agent > 30
    for n in (100, 500):
        predicted = sizes[10] + per_agent * (n - 10)
        assert abs(sizes[n] - predicted) / predicted < 0.02  # linear
