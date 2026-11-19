"""L2: registry (2.1), card (2.2), resolve-on-write (2.3), Top-K roster (2.4), dormancy (2.5).

The scripted IA encodes name drift BY HAND (it rewords agent names on purpose):
these tests prove the mechanisms; only L3 measures what a real model does.
"""

from __future__ import annotations

import json

import pytest

from helpers import dispatch, drain, journal, run_async, user_turn
from scenarios import ea_prompt_chars_with_card, old_draft_follow_up, roster_block_chars, seed_thread
from scripts import round_index
from server.services import telemetry
from server.services.agents.registry import get_agent_registry
from server.services.context import turn_scope
from server.services.execution import get_agent_roster
from server.services.execution import roster as roster_module
from server.testing.scripted_llm import final, respond, tool_call
from server.utils import clock

CARD = ("AGENT_CARD",)
RESOLVE = ("RESOLVE_ON_WRITE",)
TOPK = ("TOPK_ROSTER",)


def agents():
    return get_agent_roster().get_agents()


def test_flag_off_on_off_leaves_roster_json_consistent(set_flags, flags_off, scripted_llm, fake_gmail):
    scripted_llm.on("interaction", lambda call: final(""))

    def go(name):
        async def main():
            with turn_scope("user"):
                dispatch(name, "x")
            await drain()

        run_async(main)

    go("Agent A")
    set_flags("AGENT_REGISTRY")
    go("Agent B")
    assert [r.name for r in get_agent_registry().list_all()] == ["Agent A", "Agent B"]
    set_flags()
    go("Agent C")
    assert json.loads(roster_module._ROSTER_PATH.read_text()) == ["Agent A", "Agent B", "Agent C"]
    set_flags("AGENT_REGISTRY")
    get_agent_roster().load()
    assert agents() == ["Agent A", "Agent B", "Agent C"]  # names added while off are synced back


def test_ea_prompt_flat_over_runs_with_card(set_flags):
    set_flags(*CARD)
    sizes = {runs: ea_prompt_chars_with_card(runs) for runs in (1, 10, 30, 90, 180)}
    assert max(sizes[r] for r in (10, 30, 90, 180)) - min(sizes[r] for r in (10, 30, 90, 180)) < 200
    assert sizes[180] < 30000


def test_old_draft_found_on_card(set_flags, scripted_llm, fake_gmail):
    """The card-on variant of the 1.2 known limit: the draft older than K is on the card."""
    set_flags(*CARD)
    outcome = old_draft_follow_up(scripted_llm, fake_gmail)
    assert outcome["found_in_prompt"]
    assert outcome["prompt_chars"] < 20000


def _ia_with_drift(scripted_llm, plan):
    """plan: list of (proposed_name, instructions); the IA re-calls with the first candidate on `ask`."""
    steps = iter(plan)

    def ia(call):
        results = call.tool_results()
        if results and results[-1].get("error", {}) and isinstance(results[-1].get("error"), dict) \
                and results[-1]["error"].get("error") == "possible_existing_agents":
            candidate = results[-1]["error"]["candidates"][0]["agent_name"]
            instructions = json.loads(json.dumps(results[-1]["arguments"]))["instructions"]
            return respond(tool_call("send_message_to_agent", agent_name=candidate, instructions=instructions))
        if round_index(call) == 0 and "<new_user_message>" in call.first_user_content:
            name, instructions = next(steps)
            return respond(tool_call("send_message_to_agent", agent_name=name, instructions=instructions))
        return final("")

    scripted_llm.on("interaction", ia)


def test_s1_follow_up_with_reworded_names_reaches_one_agent(set_flags, scripted_llm, fake_gmail):
    set_flags(*RESOLVE)
    msg = seed_thread(fake_gmail)
    _ia_with_drift(scripted_llm, [
        ("Priya Patel auto renewal", "Draft a reply to Priya (priya.patel@example.com) about her renewal"),
        ("Priya renewal reply", "Make it shorter"),
        ("Patel draft", f"Send the draft in thread {msg.thread_id}"),
    ])

    def ea(call):
        if call.first_user_content.startswith("Draft") and round_index(call) == 0:
            return respond(tool_call("gmail_create_draft", recipient_email="priya.patel@example.com", subject="Re",
                                     body="Hi", thread_id=msg.thread_id))
        return final("ok")

    scripted_llm.on("execution", ea)

    async def main():
        for text in ("draft a reply to Priya", "make it shorter", "send it"):
            await user_turn(text)
            await drain()

    with telemetry.capture() as events:
        run_async(main)
    assert agents() == ["Priya Patel auto renewal"]
    actions = [e["action"] for e in events if e["event"] == "dispatch"]
    assert actions == ["create", "ask", "exact", "identifier"]
    assert [tag for tag, _, _ in journal("Priya Patel auto renewal")].count("agent_request") == 3


def test_s2_parallel_spawns_and_targeted_follow_up(set_flags, scripted_llm, fake_gmail):
    set_flags(*RESOLVE)
    scripted_llm.script("interaction", [
        respond(tool_call("send_message_to_agent", agent_name="Alice Martin renewal reminder",
                          instructions="Draft a renewal reminder to alice.martin@example.com for HO-55102"),
                tool_call("send_message_to_agent", agent_name="Bob Nguyen renewal reminder",
                          instructions="Draft a renewal reminder to bob.nguyen@example.com for HO-55187")),
        final(""),
    ])
    scripted_llm.on("interaction", lambda call: final(""))

    async def main():
        await user_turn("draft reminders for Alice and Bob")
        await drain()
        with turn_scope("user"):
            result = dispatch("Bob Nguyen renewal reminder", "Bob already renewed: delete his draft")
        await drain()
        return result

    result = run_async(main)
    assert agents() == ["Alice Martin renewal reminder", "Bob Nguyen renewal reminder"]
    assert result.payload["agent_name"] == "Bob Nguyen renewal reminder" and result.payload["routed"] == "exact"


def test_s3_two_sarahs_never_merge(set_flags, scripted_llm, fake_gmail):
    set_flags(*RESOLVE)
    client = fake_gmail.inject_email(sender="Sarah Chen <sarah.chen@northwind.example>", subject="Water stain", body="stain")
    counsel = fake_gmail.inject_email(sender="Sarah Chen <s.chen@harborlaw.example>", subject="CL-2291", body="docs")

    def ea(call):
        text = call.first_user_content
        if round_index(call) == 0 and "Draft" in text:
            msg = client if "northwind" in text else counsel
            to = "sarah.chen@northwind.example" if msg is client else "s.chen@harborlaw.example"
            return respond(tool_call("gmail_create_draft", recipient_email=to, subject="Re", body="x", thread_id=msg.thread_id))
        return final("ok")

    scripted_llm.on("execution", ea)
    scripted_llm.on("interaction", lambda call: final(""))
    results = []

    async def main():
        for name, text in [
            ("Sarah Chen", "Draft a reply to sarah.chen@northwind.example about the water stain"),
            ("Sarah Chen Harbor Law", "Draft a reply to s.chen@harborlaw.example acknowledging CL-2291"),
            ("Sarah Chen water stain", "Add to the draft for sarah.chen@northwind.example that she can call me"),
        ]:
            with turn_scope("user"):
                results.append(dispatch(name, text))
            await drain()

    run_async(main)
    assert results[1].payload["new_agent_created"]  # the counsel is NOT merged into the client's agent
    third = results[2].payload
    assert third["error"] == "possible_existing_agents"
    assert [c["agent_name"] for c in third["candidates"]] == ["Sarah Chen"]  # the vetoed counsel agent is not offered
    assert len(agents()) == 2


def test_dedupe_keys_on_the_canonical_agent(set_flags, scripted_llm, fake_gmail):
    set_flags(*RESOLVE, "DISPATCH_DEDUPE")
    get_agent_roster().add_agent("Priya Renewal")
    get_agent_registry().add_alias(get_agent_registry().get("Priya Renewal").id, "Priya")
    scripted_llm.on("interaction", lambda call: final(""))
    scripted_llm.on("execution", lambda call: final("ok"))
    scripted_llm.set_latency(0.2, "execution")

    async def main():
        with turn_scope("user"):
            first = dispatch("Priya Renewal", "send the draft")
            second = dispatch("priya", "Send the draft")
        await drain()
        return first, second

    first, second = run_async(main)
    assert first.payload["status"] == "submitted" and second.payload["status"] == "duplicate_suppressed"
    assert second.payload["agent_name"] == "Priya Renewal"


def test_roster_tokens_flat_with_top_k(set_flags):
    set_flags(*TOPK)
    sizes = {n: roster_block_chars(n, query="follow up on client 0005 renewal") for n in (10, 100, 500, 2000)}
    assert max(sizes.values()) - min(sizes.values()) < 60  # only the "more agents" count's digits differ
    assert sizes[2000] < 2000


def test_top_k_shows_the_relevant_agent_among_many(set_flags):
    from server.agents.interaction_agent.agent import prepare_message_with_history

    set_flags(*TOPK)
    roster_block_chars(200)
    content = prepare_message_with_history("What's the status of the client 0123 renewal?", "", "user")[0]["content"]
    assert 'name="Client 0123 renewal follow-up"' in content
    assert '<more_agents count="192">' in content


def test_dormant_agents_hidden_from_roster_but_resolvable(set_flags, scripted_llm, fake_gmail):
    from server.agents.interaction_agent.agent import prepare_message_with_history

    set_flags("DORMANCY")
    msg = seed_thread(fake_gmail)
    roster = get_agent_roster()
    roster.add_agent("Kevin bakery quote")
    registry = get_agent_registry()
    kevin = registry.get("Kevin bakery quote")
    registry.record_identifiers(kevin.id, __import__("server.services.agents.ids", fromlist=["x"]).ExtractedIds(threads=frozenset({msg.thread_id})))
    for i in range(10):
        roster.add_agent(f"Other {i}")
    clock.advance(15 * 86400)
    for i in range(10):
        registry.touch(registry.get(f"Other {i}").id)
    content = prepare_message_with_history("kevin bakery quote", "", "user")[0]["content"]
    assert "Kevin bakery quote" not in content.split("<active_agents>")[1].split("</active_agents>")[0]
    scripted_llm.on("interaction", lambda call: final(""))

    async def main():
        with turn_scope("user"):
            return dispatch("Liu bakery follow-up", f"Follow up on thread {msg.thread_id}")

    result = run_async(main)
    assert result.payload["agent_name"] == "Kevin bakery quote"  # identifier route revives the dormant agent
    content = prepare_message_with_history("kevin bakery quote", "", "user")[0]["content"]
    assert 'name="Kevin bakery quote"' in content
