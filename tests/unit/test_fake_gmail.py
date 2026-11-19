"""L1: FakeGmail query subset, response shapes and the real parser round-trip (P0.6)."""

from __future__ import annotations

import pytest

from helpers import load_broker_fixture
from server.services.gmail.processing import parse_gmail_fetch_response
from server.testing.fake_gmail import FakeGmail


@pytest.fixture
def inbox():
    return FakeGmail(load_broker_fixture(), mode="instant")


def _ids(gmail: FakeGmail, query: str):
    return [m.id for m in gmail.search(query).messages]


@pytest.mark.parametrize(
    "query,expected_thread",
    [
        ("from:priya.patel@example.com", "18c1a00000000001"),
        ('subject:"water damage"', "18c1a00000000005"),
        ("from:s.chen@harborlaw.example", "18c1a00000000006"),
        ("AU-77210", "18c1a00000000007"),
        ('"certificate of insurance" ridgeline', "18c1a00000000008"),
    ],
)
def test_query_operators_find_the_right_thread(inbox, query, expected_thread):
    threads = {m.thread_id for m in inbox.search(query).messages}
    assert expected_thread in threads


def test_keywords_are_anded_and_near_identical_policies_differ(inbox):
    assert {m.thread_id for m in inbox.search("AU-77120 telematics").messages} <= {"18c1a00000000001", "18c1a00000000002", "18c1a0000000000a"}
    au77210 = {m.thread_id for m in inbox.search("AU-77210").messages}
    assert "18c1a00000000001" not in au77210


def test_newer_than_and_labels(inbox):
    recent = inbox.search("newer_than:12h").messages
    assert recent and all((inbox._now() - m.timestamp).total_seconds() <= 12 * 3600 for m in recent)
    inbox_only = inbox.search("label:INBOX newer_than:30d").messages
    assert inbox_only and all("INBOX" in m.labels for m in inbox_only)
    assert {m.id for m in inbox.search("category:primary").messages} == {m.id for m in inbox.search("in:inbox").messages}
    assert all("SENT" in m.labels for m in inbox.search("in:sent").messages)


def test_unsupported_operators_are_ignored_and_counted(inbox):
    plain = _ids(inbox, "from:priya.patel@example.com")
    # is:unread has no fixture field: it is dropped from the expression (not "match all" inside OR).
    noisy = _ids(inbox, "from:priya.patel@example.com OR is:unread after:2024/01/01 -spam")
    assert noisy == plain
    assert inbox.unsupported_query_operators == 1
    assert inbox.search("is:unread larger:5M").unsupported == ["is:unread", "larger:5M"]


PRIYA_THREAD = "18c1a00000000001"


def _threads(gmail, query):
    return {m.thread_id for m in gmail.search(query).messages}


def test_real_model_query_with_or_and_parentheses_finds_priya(inbox):
    query = '"Priya Patel" (insurance OR auto OR renewal OR policy)'
    result = inbox.search(query)
    assert result.unsupported == []
    assert PRIYA_THREAD in {m.thread_id for m in result.messages}


def test_or_binds_tighter_than_implicit_and(inbox):
    # Gmail: "a OR b c" == "(a OR b) AND c"
    assert _threads(inbox, "telematics OR AU-77210 Marcus") == _threads(inbox, "(telematics OR AU-77210) Marcus")
    assert _threads(inbox, "telematics OR AU-77210 Marcus") == {"18c1a00000000007", "18c1a00000000019"}
    assert _threads(inbox, "telematics OR (AU-77210 Marcus)") != _threads(inbox, "telematics OR AU-77210 Marcus")
    assert "18c1a00000000002" in _threads(inbox, "telematics OR AU-77210")


def test_braces_are_or_and_nested_parentheses(inbox):
    assert _threads(inbox, "{HO-55102 HO-55187}") == _threads(inbox, "HO-55102 OR HO-55187")
    nested = _threads(inbox, "(from:alice.martin@example.com OR (from:bob.nguyen@example.com renewed)) HO")
    assert nested == {"18c1a00000000003", "18c1a0000000000d", "18c1a00000000004"}


def test_negation_of_words_operators_and_groups(inbox):
    priya = _threads(inbox, "from:priya.patel@example.com")
    without_driver = _threads(inbox, "from:priya.patel@example.com -driver")
    assert "18c1a00000000015" in priya and "18c1a00000000015" not in without_driver
    assert _threads(inbox, "from:priya.patel@example.com -subject:driver") == without_driver
    assert _threads(inbox, "from:priya.patel@example.com -(son OR driver)") == without_driver


def test_more_operators(inbox):
    assert _threads(inbox, "has:attachment from:alice.martin@example.com") == {"18c1a0000000000d"}
    assert _threads(inbox, "category:promotions") == {"18c1a0000000000c", "18c1a00000000018"}
    assert _threads(inbox, "older_than:200d") == {"18c1a0000000001c"}
    assert _threads(inbox, "thread:18c1a00000000006") == {"18c1a00000000006"}
    assert _threads(inbox, 'subject:"water damage"') == {"18c1a00000000005"}
    assert _threads(inbox, "subject:(ceiling stain)") == {"18c1a00000000011"}
    assert _threads(inbox, "after:2000/01/01") == _threads(inbox, "")
    assert _threads(inbox, "before:2000-01-01") == set()


@pytest.mark.parametrize("query", ["(renewal OR", "renewal)", "{a b", "OR", "-", "subject:(", '"unterminated', "((()))"])
def test_malformed_queries_degrade_gracefully(inbox, query):
    result = inbox.search(query)  # never raises
    assert isinstance(result.messages, list)


def test_unbalanced_parentheses_are_recorded(inbox):
    assert any("unbalanced" in u for u in inbox.search("(renewal OR auto").unsupported)
    assert any("unbalanced" in u for u in inbox.search("renewal)").unsupported)


def test_fetch_output_parses_through_the_real_parser(inbox):
    raw = inbox.execute("GMAIL_FETCH_EMAILS", "fake-user", {"query": "from:priya.patel@example.com", "include_payload": True, "max_results": 5})
    assert raw["successful"] is True and set(raw) == {"data", "error", "log_id", "session_info", "successful"}
    emails, _ = parse_gmail_fetch_response(raw, query="q")
    assert emails and all(e.sender.endswith("<priya.patel@example.com>") for e in emails)
    assert all(e.clean_text for e in emails)  # body decoded from the base64 payload
    assert emails[0].timestamp >= emails[-1].timestamp  # newest first, like Gmail
    assert len(emails) <= 5


def test_draft_lifecycle_mirrors_gmail(inbox):
    created = inbox.execute("GMAIL_CREATE_EMAIL_DRAFT", "fake-user", {"recipient_email": "priya.patel@example.com",
                            "subject": "Re: renewal", "body": "Hi", "thread_id": "18c1a00000000001"})
    draft_id = created["data"]["id"]
    assert draft_id.startswith("r-") and created["data"]["message"]["threadId"] == "18c1a00000000001"
    listed = inbox.execute("GMAIL_LIST_DRAFTS", "fake-user", {})
    assert [d["id"] for d in listed["data"]["drafts"]] == [draft_id]
    sent = inbox.execute("GMAIL_SEND_DRAFT", "fake-user", {"draft_id": draft_id})
    assert sent["successful"] and sent["data"]["labelIds"] == ["SENT"]
    assert inbox.execute("GMAIL_LIST_DRAFTS", "fake-user", {})["data"]["drafts"] == []  # deleted when sent
    again = inbox.execute("GMAIL_SEND_DRAFT", "fake-user", {"draft_id": draft_id})
    assert again["successful"] is False  # explicit failure, nothing sent
    assert len(inbox.external_sends()) == 1


def test_reply_forward_and_failure_injection(inbox):
    reply = inbox.execute("GMAIL_REPLY_TO_THREAD", "fake-user", {"thread_id": "18c1a00000000008",
                          "recipient_email": "dan@okafor-builders.example", "message_body": "On it"})
    assert reply["successful"] and reply["data"]["threadId"] == "18c1a00000000008"
    inbox.fail_next("GMAIL_FORWARD_MESSAGE", exception=RuntimeError("network"), after_effect=True)
    with pytest.raises(RuntimeError):
        inbox.execute("GMAIL_FORWARD_MESSAGE", "fake-user", {"message_id": "18c1a00000000008", "recipient_email": "x@example.com"})
    # The forward happened even though the caller saw an exception (an "unknown" outcome).
    assert [a["action"] for a in inbox.external_sends()] == ["GMAIL_REPLY_TO_THREAD", "GMAIL_FORWARD_MESSAGE"]
    inbox.fail_next("GMAIL_REPLY_TO_THREAD", unsuccessful=True)
    result = inbox.execute("GMAIL_REPLY_TO_THREAD", "fake-user", {"thread_id": "18c1a00000000008",
                           "recipient_email": "dan@okafor-builders.example", "message_body": "x"})
    assert result["successful"] is False and len(inbox.external_sends()) == 2


def test_ids_are_deterministic():
    a, b = FakeGmail(mode="instant"), FakeGmail(mode="instant")
    ma = a.inject_email(sender="a@example.com", subject="s", body="b")
    mb = b.inject_email(sender="a@example.com", subject="s", body="b")
    assert ma.id == mb.id and len(ma.id) == 16 and int(ma.id, 16) >= 0


def test_execute_gmail_tool_routes_to_fake_and_never_builds_composio(fake_gmail):
    from server.services.gmail import client

    fake_gmail.inject_email(sender="a@example.com", subject="hello", body="b")
    result = client.execute_gmail_tool("GMAIL_FETCH_EMAILS", client.get_active_gmail_user_id(), arguments={"query": "hello"})
    assert result["data"]["messages"]
    assert client.get_active_gmail_user_id() == "fake-user"
    with pytest.raises(RuntimeError, match="FAKE_GMAIL"):
        client._get_composio_client()
    assert client._CLIENT is None
