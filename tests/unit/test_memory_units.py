"""L1: bounded EA history (1.2) and identifier extraction (1.3)."""

from __future__ import annotations

import json

import pytest

from server.agents.execution_agent.agent import ExecutionAgent
from server.services.agents.ids import email_refs, extract_from_text, extract_from_tool_call
from server.services.execution import get_execution_agent_logs


def _journal(agent: str, requests: int, payload_size: int = 50) -> None:
    logs = get_execution_agent_logs()
    for i in range(requests):
        logs.record_request(agent, f"request {i} " + "x" * payload_size)
        logs.record_agent_response(agent, f"response {i} " + "y" * payload_size)


# ------------------------------------------------------------------ 1.2 window
def test_request_window_keeps_last_k_including_current():
    _journal("A", 8)
    prompt = ExecutionAgent("A", conversation_limit=5, max_history_chars=100000).build_system_prompt_with_history()
    assert prompt.count("<agent_request") == 5
    assert "request 3 " in prompt and "request 2 " not in prompt
    assert "response 7 " in prompt


def test_window_matches_legacy_line_based_limit():
    """The entry-based window gives exactly the legacy conversation_limit result when the cap does not bind."""
    _journal("A", 9)
    get_execution_agent_logs().record_action("A", "multi\nline\naction")
    legacy = ExecutionAgent("A", conversation_limit=5).build_system_prompt_with_history()
    windowed = ExecutionAgent("A", conversation_limit=5, max_history_chars=10**9).build_system_prompt_with_history()
    assert windowed == legacy


def test_fewer_than_k_requests_is_unchanged():
    _journal("A", 2)
    full = ExecutionAgent("A").build_system_prompt_with_history()
    windowed = ExecutionAgent("A", conversation_limit=5, max_history_chars=100000).build_system_prompt_with_history()
    assert windowed == full


def test_char_cap_drops_oldest_whole_entries():
    _journal("A", 5, payload_size=400)
    agent = ExecutionAgent("A", conversation_limit=5, max_history_chars=1500)
    prompt = agent.build_system_prompt_with_history()
    history = prompt.split("# Execution History\n\n", 1)[1]
    assert len(history) <= 1500
    assert "response 4 " in history  # newest kept
    assert all(line.startswith("<") and line.endswith(">") for line in history.splitlines())  # whole entries only
    assert agent.last_prompt_parts["history_chars"] == len(history)


def test_single_huge_entry_is_truncated_with_marker():
    get_execution_agent_logs().record_agent_response("A", "z" * 5000)
    prompt = ExecutionAgent("A", conversation_limit=5, max_history_chars=1000).build_system_prompt_with_history()
    history = prompt.split("# Execution History\n\n", 1)[1]
    assert "truncated" in history and len(history) <= 1000
    assert history.startswith("<agent_response") and history.endswith("</agent_response>")


def test_runtime_uses_window_only_with_flag(set_flags, flags_off):
    from server.agents.execution_agent.runtime import ExecutionAgentRuntime

    assert ExecutionAgentRuntime("A").agent.conversation_limit is None  # baseline never passes a limit
    set_flags("EA_HISTORY_WINDOW")
    agent = ExecutionAgentRuntime("A").agent
    assert agent.conversation_limit == 5 and agent.max_history_chars == 40000


# ----------------------------------------------------------------- 1.3 ids
REAL_SEARCH_SHAPE = [
    {"id": "18c1a00000000001", "thread_id": "18c1a00000000001", "query": "from:priya", "subject": "Auto policy AU-77120",
     "sender": "Priya Patel <priya.patel@example.com>", "recipient": "jordan@brightpath-brokers.example",
     "timestamp": "2026-11-17T10:00:00Z", "label_ids": ["INBOX"], "clean_text": "Hi Jordan..."},
    {"id": "18c1b00000000101", "thread_id": "18c1a00000000001", "query": "from:priya", "subject": "Re: Auto policy",
     "sender": "Priya Patel <priya.patel@example.com>", "recipient": "jordan@brightpath-brokers.example",
     "timestamp": "2026-11-17T11:00:00Z", "label_ids": ["INBOX"], "clean_text": "Thanks"},
]


def test_extracts_from_search_results_like_the_real_journal():
    ids = extract_from_tool_call("task_email_search", {"search_query": "emails from priya"}, REAL_SEARCH_SHAPE)
    assert ids.messages == {"18c1a00000000001", "18c1b00000000101"}
    assert ids.threads == {"18c1a00000000001"}
    assert ids.emails == {"priya.patel@example.com", "jordan@brightpath-brokers.example"}
    refs = email_refs(REAL_SEARCH_SHAPE)
    assert refs[0] == {"message": "18c1a00000000001", "thread": "18c1a00000000001",
                       "from": "Priya Patel <priya.patel@example.com>", "subject": "Auto policy AU-77120"}


def test_extracts_from_fake_gmail_draft_and_reply_responses():
    from server.testing.fake_gmail import FakeGmail

    gmail = FakeGmail(mode="instant")
    msg = gmail.inject_email(sender="dan@okafor-builders.example", subject="COI", body="please")
    args = {"recipient_email": "coi@ridgeline.example", "subject": "COI", "body": "attached", "cc": ["dan@okafor-builders.example"],
            "thread_id": msg.thread_id}
    created = gmail.execute("GMAIL_CREATE_EMAIL_DRAFT", "u", args)
    ids = extract_from_tool_call("gmail_create_draft", args, created)
    assert ids.drafts == {created["data"]["id"]}
    assert msg.thread_id in ids.threads
    assert ids.emails == {"coi@ridgeline.example", "dan@okafor-builders.example"}
    reply_args = {"thread_id": msg.thread_id, "recipient_email": "dan@okafor-builders.example", "message_body": "ok"}
    replied = gmail.execute("GMAIL_REPLY_TO_THREAD", "u", reply_args)
    rids = extract_from_tool_call("gmail_reply_to_thread", reply_args, replied)
    assert replied["data"]["id"] not in rids.drafts and msg.thread_id in rids.threads


def test_errors_extract_from_args_only_and_never_raise():
    ids = extract_from_tool_call("gmail_execute_draft", {"draft_id": "r-1234567890123"}, {"error": "boom 18c1a00000000009"})
    assert ids.drafts == {"r-1234567890123"} and not ids.messages
    weird = extract_from_tool_call("x", object(), {1: {2: [object()]}})
    assert weird.is_empty()
    assert extract_from_tool_call("x", "not json {", "also not json").is_empty()


def test_cap_per_kind():
    many = [{"id": f"{i:016x}", "thread_id": f"{i + 1000:016x}"} for i in range(200)]
    ids = extract_from_tool_call("task_email_search", {}, many, cap=50)
    assert len(ids.messages) == 50 and len(ids.threads) == 50


def test_extract_from_text():
    ids = extract_from_text("Reply on thread 18c1a00000000006 to s.chen@harborlaw.example; send draft r-1234567890123456789")
    assert ids.gmail_ids == {"18c1a00000000006"}
    assert ids.emails == {"s.chen@harborlaw.example"}
    assert ids.drafts == {"r-1234567890123456789"}
    assert extract_from_text("sha 3f2a9c1d0b4e5f60718293a4b5c6d7e8f9").gmail_ids == set()  # 16-hex inside a longer hash is not an id


def test_tool_ids_journal_entry_only_with_flag(set_flags, flags_off):
    agent = ExecutionAgent("A")
    agent.record_tool_execution("task_email_search", "{}", "[...]", raw_arguments={}, raw_result=REAL_SEARCH_SHAPE)
    assert [tag for tag, _, _ in get_execution_agent_logs().iter_entries("A")] == ["agent_action", "tool_response"]
    set_flags("KEEP_TOOL_IDS")
    agent.record_tool_execution("task_email_search", "{}", "[...]", raw_arguments={}, raw_result=REAL_SEARCH_SHAPE)
    entries = list(get_execution_agent_logs().iter_entries("A"))
    tag, _, payload = entries[-1]
    assert tag == "tool_ids"
    data = json.loads(payload)
    assert data["threads"] == ["18c1a00000000001"] and len(data["refs"]) == 2
    assert "<tool_ids" in get_execution_agent_logs().load_transcript("A")  # rendered into EA history
