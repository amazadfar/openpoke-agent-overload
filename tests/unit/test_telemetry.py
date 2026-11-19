"""L1 tests for telemetry (P0.4)."""

from __future__ import annotations

import json

import httpx
import pytest

from helpers import run_async
from server.openrouter_client import client as or_client
from server.services import telemetry
from server.services.context import run_scope, turn_scope


def test_emit_attaches_context_and_timestamps():
    with telemetry.capture() as events:
        with turn_scope("user") as turn:
            with run_scope("Agent A", run_id="run_1"):
                telemetry.emit("probe", size=3)
    (event,) = events
    assert event["event"] == "probe" and event["size"] == 3
    assert event["turn_id"] == turn.turn_id and event["origin"] == "user" and event["root_origin"] == "user"
    assert event["run_id"] == "run_1" and event["agent"] == "Agent A"
    assert "ts" in event and "mono" in event


def test_emit_writes_jsonl_and_survives_unwritable_dir(monkeypatch, tmp_path):
    telemetry.emit("first")
    lines = telemetry.events_path().read_text().splitlines()
    assert json.loads(lines[-1])["event"] == "first"

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(telemetry, "events_path", lambda: blocker / "telemetry" / "events.jsonl")
    before = telemetry.dropped
    telemetry.emit("second")  # must not raise (I10)
    assert telemetry.dropped == before + 1
    assert telemetry.recent("telemetry_dropped")[-1]["dropped_event"] == "second"


def test_file_sink_can_be_disabled(monkeypatch):
    monkeypatch.setenv("OPENPOKE_TELEMETRY", "0")
    telemetry.emit("quiet")
    assert not telemetry.events_path().exists()
    assert telemetry.recent("quiet")


def test_rotation_keeps_one_backup(monkeypatch):
    monkeypatch.setenv("OPENPOKE_TELEMETRY_MAX_BYTES", "2048")
    for i in range(200):
        telemetry.emit("filler", i=i, pad="x" * 50)
    path = telemetry.events_path()
    assert path.exists() and path.with_suffix(".jsonl.1").exists()
    assert path.stat().st_size <= 2048


def _mock_openrouter(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(or_client, "_http_client", lambda: httpx.AsyncClient(transport=transport))


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13, "cost": 0.001}},
         {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13, "cost": 0.001}),
        ({"choices": [{"message": {"content": "hi"}}]},
         {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None, "cost": None}),
    ],
)
def test_llm_call_usage_parsed_when_present_and_absent(monkeypatch, body, expected):
    _mock_openrouter(monkeypatch, lambda request: httpx.Response(200, json=body))
    with telemetry.capture() as events:
        run_async(lambda: or_client.request_chat_completion(model="m", messages=[{"role": "user", "content": "abc"}], system="sys", role="interaction"))
    (event,) = [e for e in events if e["event"] == "llm_call"]
    for key, value in expected.items():
        assert event[key] == value
    assert event["role"] == "interaction" and event["ok"] is True and event["http_status"] == 200
    assert event["prompt_chars"] == len("sys") + len("abc")
    assert "abc" not in json.dumps(event)  # sizes, never content (S5)


def test_llm_call_failure_is_recorded_and_raised(monkeypatch):
    _mock_openrouter(monkeypatch, lambda request: httpx.Response(400, json={"error": "bad"}))
    with telemetry.capture() as events:
        with pytest.raises(or_client.OpenRouterError):
            run_async(lambda: or_client.request_chat_completion(model="m", messages=[], role="execution"))
    (event,) = [e for e in events if e["event"] == "llm_call"]
    assert event["ok"] is False and event["http_status"] == 400 and event["error_class"] == "OpenRouterError"


def test_role_is_attributed_for_all_five_callers(scripted_llm, fake_gmail):
    """Every caller passes role=...; ScriptedLLM records whether it came from the kwarg."""
    from server.services.conversation.summarization import summarizer
    from server.services.gmail.importance_classifier import classify_email_importance
    from server.services.gmail.processing import ProcessedEmail
    from server.agents.execution_agent.tasks.search_email.tool import task_email_search
    from server.testing.scripted_llm import final, respond, tool_call
    from helpers import dispatch, drain, user_turn
    from datetime import datetime, timezone

    scripted_llm.script("interaction", [respond(tool_call("send_message_to_agent", agent_name="A", instructions="search")), final("ok"), final("ok")])
    scripted_llm.script("execution", [respond(tool_call("task_email_search", search_query="x")), final("done")])

    async def main():
        await user_turn("hello")
        await drain()
        email = ProcessedEmail(id="1", thread_id="t", query="q", subject="s", sender="a@b.c", recipient="me",
                               timestamp=datetime.now(timezone.utc), label_ids=[], clean_text="body",
                               has_attachments=False, attachment_count=0, attachment_filenames=[])
        await classify_email_importance(email)
        await summarizer._call_openrouter(summarizer.build_summarization_prompt("", []), "m", "k")

    run_async(main)
    roles = {call.role for call in scripted_llm.calls if call.role_from_kwarg}
    assert roles == {"interaction", "execution", "search", "classifier", "summarizer"}
