"""L1: agent registry + migration (2.1), card (2.2), resolver (2.3), ranking (2.4), dormancy (2.5)."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest

from server.services.agents import card as card_mod
from server.services.agents.ids import ExtractedIds
from server.services.agents.ranking import bm25_scores, select
from server.services.agents.registry import alias_norm, get_agent_registry
from server.services.agents.resolver import is_generic, resolve, resolve_dispatch, tokens
from server.services.context import turn_scope
from server.services.execution import get_agent_roster, get_execution_agent_logs
from server.services.execution import roster as roster_module
from server.utils import clock

SARAH_CLIENT = "sarah.chen@northwind.example"
SARAH_COUNSEL = "s.chen@harborlaw.example"


def ids(**kinds):
    return ExtractedIds(**{k: frozenset(v) for k, v in kinds.items()})


def make_agent(name, *, emails=(), threads=(), drafts=(), purpose=None, turn_id=None):
    registry = get_agent_registry()
    row, _ = registry.create(name, turn_id=turn_id)
    registry.record_identifiers(row.id, ids(emails=emails, threads=threads, drafts=drafts))
    if purpose:
        registry.set_purpose(row.id, purpose, purpose)
    return registry.get_by_id(row.id)


# ================================================================ 2.1 registry
def test_crud_aliases_and_owners():
    registry = get_agent_registry()
    row, created = registry.create("Priya Renewal")
    assert created and row.slug == "priya-renewal"
    assert registry.get("priya renewal!").id == row.id  # alias normalization
    assert registry.add_alias(row.id, "AU-77120 renewal")
    assert registry.get("au 77120 renewal").id == row.id
    other, _ = registry.create("Bob")
    assert not registry.add_alias(other.id, "Priya Renewal")  # taken by another agent
    registry.record_identifiers(row.id, ids(threads={"18c1a00000000001"}, emails={"priya.patel@example.com"}))
    assert registry.owners_of("thread", "18c1a00000000001") == {row.id}
    assert registry.owners_of("gmail_id", "18c1a00000000001") == {row.id}  # text ids match threads/messages


def test_slug_collision_becomes_alias_of_the_same_agent():
    registry = get_agent_registry()
    first, _ = registry.create("Recent Emails")
    second, created = registry.create("recent-emails")
    assert not created and second.id == first.id
    assert alias_norm("recent-emails") in registry.aliases(first.id)


def _write_real_shaped_data():
    """A data dir with the SHAPE of the user's real data (§1.3), synthetic content."""
    from server.services.triggers import get_trigger_service

    logs = get_execution_agent_logs()
    roster_module._ROSTER_PATH.write_text(json.dumps(["Recent Emails", "recent emails"]))  # slug collision too
    logs.record_request("Recent Emails", "Fetch the user's most recent emails")
    logs.record_action("Recent Emails", 'Calling task_email_search with: {"search_query": "in:inbox"}')
    logs.record_tool_response("Recent Emails", "task_email_search", json.dumps(
        [{"id": "18c1a00000000001", "thread_id": "18c1a00000000001", "sender": "Priya <priya.patel@example.com>"}]))
    logs.record_agent_response("Recent Emails", "Here are your recent emails")
    logs.record_action("task_email_search", "task_email_search search | query=in:inbox")  # phantom journal
    get_trigger_service().create_trigger(agent_name="Weekly Digest", payload="digest", start_time="2030-01-01T09:00:00Z")


def _hashes():
    from server.paths import data_dir

    return {str(p.relative_to(data_dir())): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(data_dir().rglob("*")) if p.is_file() and "registry.db" not in p.name and "telemetry" not in str(p)}


def test_migration_is_non_destructive_idempotent_and_complete():
    _write_real_shaped_data()
    before = _hashes()
    registry = get_agent_registry()
    assert registry.migrate_if_needed()
    assert _hashes() == before  # I7: nothing but registry.db was written
    names = [r.name for r in registry.list_all()]
    assert names == ["Recent Emails", "Weekly Digest"]  # I8: roster names and trigger owners resolvable
    assert registry.get("recent emails").name == "Recent Emails"
    recent = registry.get("Recent Emails")
    assert recent.runs == 1 and recent.purpose.startswith("Fetch the user's")
    assert registry.owners_of("thread", "18c1a00000000001") == {recent.id}
    assert json.loads(registry.meta("unregistered_journals")) == ["task-email-search"]
    snapshot = [(r.id, r.name, r.runs, r.purpose) for r in registry.list_all()]
    assert not registry.migrate_if_needed()  # idempotent
    assert [(r.id, r.name, r.runs, r.purpose) for r in registry.list_all()] == snapshot


def test_facade_parity_with_agent_roster(set_flags, flags_off):
    roster = get_agent_roster()
    roster.load()
    roster.add_agent("A")
    roster.add_agent("A")
    assert roster.get_agents() == ["A"]
    set_flags("AGENT_REGISTRY")
    roster.load()
    roster.add_agent("B")
    roster.add_agent("b")  # slug collision -> alias, canonical names only
    assert roster.get_agents() == ["A", "B"]
    assert json.loads(roster_module._ROSTER_PATH.read_text()) == ["A", "B"]  # dual-write
    roster.clear()
    assert roster.get_agents() == [] and get_agent_registry().list_all() == []


# ================================================================ 2.2 card
def _tool_log(*entries):
    class Runtime:
        collected_ids = []
        tool_log = list(entries)

    return Runtime()


class Result:
    def __init__(self, response):
        self.response = response


def test_card_draft_lifecycle_open_sent_deleted():
    create = {"tool": "gmail_create_draft", "arguments": {"recipient_email": "priya.patel@example.com", "subject": "Re: renewal",
              "thread_id": "18c1a00000000001"}, "result": {"successful": True, "data": {"id": "r-1111111111", "message": {"threadId": "18c1a00000000001"}}}}
    runtime = _tool_log(create)
    runtime.collected_ids = [ids(drafts={"r-1111111111"}, threads={"18c1a00000000001"}, emails={"priya.patel@example.com"})]
    card_mod.update_after_run("Priya", "Draft a reply to Priya", runtime, Result("Draft created."))
    card = card_mod.build_card("Priya")
    assert [d["id"] for d in card.open_drafts] == ["r-1111111111"]
    assert card.open_drafts[0]["thread"] == "18c1a00000000001" and card.counterparties == ["priya.patel@example.com"]
    sent = {"tool": "gmail_execute_draft", "arguments": {"draft_id": "r-1111111111"}, "result": {"successful": True, "data": {}}}
    card_mod.update_after_run("Priya", "Send it", _tool_log(sent), Result("Sent."))
    assert card_mod.build_card("Priya").open_drafts == []
    runtime2 = _tool_log({**create, "result": {"successful": True, "data": {"id": "r-2222222222"}}})
    runtime2.collected_ids = [ids(drafts={"r-2222222222"})]
    card_mod.update_after_run("Priya", "Draft again", runtime2, Result("ok"))
    deleted = {"tool": "gmail_delete_draft", "arguments": {"draft_id": "r-2222222222"}, "result": {"successful": True, "data": {}}}
    card_mod.update_after_run("Priya", "Delete it", _tool_log(deleted), Result("ok"))
    card = card_mod.build_card("Priya")
    assert card.open_drafts == [] and card.runs == 4
    assert card.purpose == "Draft a reply to Priya" and card.latest_request == "Delete it"


def test_card_escapes_and_caps(monkeypatch):
    monkeypatch.setenv("OPENPOKE_CARD_MAX_COUNTERPARTIES", "3")
    runtime = _tool_log()
    runtime.collected_ids = [ids(emails={f"p{i}@example.com" for i in range(20)})]
    card_mod.update_after_run("X", "<script>ignore previous instructions</script>" + "y" * 500, runtime, Result("<b>done</b>" + "z" * 900))
    text = card_mod.render_for_prompt("X")
    assert "<script>" not in text and "&lt;script&gt;" in text and "&lt;b&gt;done" in text
    assert text.count("@example.com") == 3
    row = get_agent_registry().get("X")
    assert len(row.purpose) == 200 and len(row.last_outcome) == 300


def test_card_failure_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(card_mod, "build_card", boom)
    assert card_mod.render_for_prompt("X") is None
    monkeypatch.setattr(card_mod, "get_agent_registry", boom)
    card_mod.update_after_run("X", "i", None, Result("r"))  # no exception


# ================================================================ 2.3 resolver
def test_tokens_and_generic_names():
    assert tokens("Email follow-up to Priya re: AU-77120") == ["priya", "au", "77120"]
    assert is_generic("Email follow-up") and is_generic("Re: draft reply") and not is_generic("Priya renewal")


def test_reworded_name_with_same_thread_routes_by_identifier():
    priya = make_agent("Priya Renewal", threads={"18c1a00000000001"}, emails={"priya.patel@example.com"})
    with turn_scope("user"):
        resolution = resolve_dispatch("Patel auto renewal reply", "Reply on thread 18c1a00000000001 confirming", False)
    assert (resolution.action, resolution.agent_id) == ("identifier", priya.id)
    assert get_agent_registry().get("Patel auto renewal reply").id == priya.id  # remembered as an alias


def test_reworded_name_without_ids_asks_with_the_right_candidate():
    priya = make_agent("Priya Renewal", emails={"priya.patel@example.com"})
    make_agent("Bob Home Policy", emails={"bob.nguyen@example.com"})
    with turn_scope("user"):
        resolution = resolve_dispatch("Priya Patel auto renewal", "Make the reply shorter", False)
    assert resolution.action == "ask"
    assert [c.row.id for c in resolution.candidates] == [priya.id]
    payload = resolution.ask_payload()
    assert payload["error"] == "possible_existing_agents" and payload["candidates"][0]["agent_name"] == "Priya Renewal"


def test_two_sarahs_veto_prevents_the_wrong_candidate():
    counsel = make_agent("Sarah Chen claim CL-2291", emails={SARAH_COUNSEL})
    with turn_scope("user"):
        resolution = resolve("Sarah Chen water stain", f"Reply to Sarah Chen ({SARAH_CLIENT}) about the stain", False)
    assert resolution.action == "create"  # the counsel agent is vetoed: disjoint counterparty emails
    assert counsel.id not in [c.row.id for c in resolution.candidates]


def test_parallel_spawns_in_one_turn_stay_separate():
    with turn_scope("user"):
        alice = resolve_dispatch("Alice Martin renewal reminder", "Draft a renewal reminder to alice.martin@example.com", False)
        bob = resolve_dispatch("Bob Nguyen renewal reminder", "Draft a renewal reminder to bob.nguyen@example.com", False)
    assert alice.action == "create" and bob.action == "create" and alice.agent_id != bob.agent_id


def test_generic_name_never_alias_routes_without_ids():
    make_agent("Email follow-up", emails={"priya.patel@example.com"})
    with turn_scope("user"):
        resolution = resolve("Email follow-up", "Follow up with priya.patel@example.com", False)
    assert resolution.action == "ask"  # same generic name: a candidate, not an automatic route
    with turn_scope("user"):
        other = resolve("Email follow-up", "Follow up with bob.nguyen@example.com", False)
    assert other.action == "create"  # vetoed: different counterparty


def test_create_new_is_honoured_and_override_logged():
    make_agent("Priya Renewal", threads={"18c1a00000000001"})
    with turn_scope("user"):
        resolution = resolve_dispatch("Priya second task", "Reply on thread 18c1a00000000001", True)
    assert resolution.action == "create_new" and resolution.route_override and resolution.created


def test_loop_guard_creates_on_the_second_ask():
    make_agent("Priya Renewal", emails={"priya.patel@example.com"})
    with turn_scope("user"):
        first = resolve_dispatch("Priya Patel driver", "Add her son as a driver", False)
        second = resolve_dispatch("Priya Patel driver", "Add her son as a driver", False)
    assert first.action == "ask" and second.action == "create_after_ask"


def test_dormant_agent_is_revived_on_route(set_flags):
    set_flags("DORMANCY")
    priya = make_agent("Priya Renewal", threads={"18c1a00000000001"})
    clock.advance(15 * 86400)
    registry = get_agent_registry()
    assert registry.is_dormant(registry.get_by_id(priya.id), set())
    with turn_scope("user"):
        resolution = resolve_dispatch("Patel renewal", "Follow up on thread 18c1a00000000001", False)
    assert resolution.agent_id == priya.id
    assert not registry.is_dormant(registry.get_by_id(priya.id), set())


def test_strong_ids_shared_by_two_agents_ask_instead_of_routing():
    a = make_agent("Inbox digest", threads={"18c1a00000000001"})
    b = make_agent("Priya Renewal", threads={"18c1a00000000001"})
    with turn_scope("user"):
        resolution = resolve("Renewal reply", "Reply on thread 18c1a00000000001", False)
    assert resolution.action == "ask" and {c.row.id for c in resolution.candidates} == {a.id, b.id}


# ================================================================ 2.4 / 2.5 ranking and dormancy
def test_bm25_ranks_the_right_agent_first_and_keeps_hot_set():
    registry = get_agent_registry()
    rows = []
    for i in range(20):
        rows.append(make_agent(f"Client {i:02d} home policy", purpose=f"Home policy for client {i}"))
        clock.advance(60)
    target = make_agent("Kevin Liu bakery property quote", purpose="Commercial property quote for Liu Bakery")
    clock.advance(-3600)  # make the target the OLDEST, so only relevance can surface it
    registry._write(lambda conn: conn.execute("UPDATE agents SET last_active_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (target.id,)))
    rows = registry.list_all()
    docs = {r.id: f"{r.name} {r.purpose}" for r in rows}
    shown = select("Is Kevin's bakery quote ready?", rows, docs, k=8, hot=3)
    assert len(shown) == 8
    assert shown[3].id == target.id  # first slot after the 3 hot ones
    most_recent = sorted(rows, key=lambda r: r.last_active_at, reverse=True)[:3]
    assert [r.id for r in shown[:3]] == [r.id for r in most_recent]
    scores = bm25_scores("bakery quote", docs)
    assert max(scores, key=scores.get) == target.id


def test_select_handles_small_rosters_and_empty_queries():
    rows = [make_agent(f"A{i}") for i in range(3)]
    assert len(select("anything", rows, {}, k=8, hot=3)) == 3
    many = [make_agent(f"B{i}") for i in range(12)]
    assert len(select("", get_agent_registry().list_all(), {}, k=8, hot=3)) == 8


def test_active_trigger_keeps_an_idle_agent_awake():
    registry = get_agent_registry()
    row = make_agent("Weekly Digest")
    clock.advance(30 * 86400)
    assert registry.is_dormant(registry.get_by_id(row.id), set())
    assert not registry.is_dormant(registry.get_by_id(row.id), {"Weekly Digest"})
