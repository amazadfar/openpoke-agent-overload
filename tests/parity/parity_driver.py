"""Parity driver (I1): runs ONE fixed scripted scenario against whichever tree is on PYTHONPATH.

Run as a subprocess with ``cwd`` and ``PYTHONPATH`` set to a copy of a tree
(the ``baseline-pre-fixes`` tag or the current working tree). It must run
unmodified on the baseline, so it uses monkeypatching only: no new env
switches, no new modules from the tree. FakeGmail and ScriptedLLM are loaded
by file path from the current repo (they are self-contained for this reason).

Output: normalized persistent state (conversation log, working memory, roster,
journals, trigger rows) as JSON, compared by tests/parity/test_parity.py.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

_TS_ATTR = re.compile(r'timestamp="[^"]*"')
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

GMAIL_CALLER_MODULES = (
    "server.services.gmail.client",
    "server.services.gmail.importance_watcher",
    "server.agents.execution_agent.tools.gmail",
    "server.agents.execution_agent.tasks.search_email.tool",
    "server.agents.execution_agent.tasks.search_email.gmail_internal",
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def normalize(text: str) -> str:
    text = _TS_ATTR.sub('timestamp="<TS>"', text)
    text = _ISO.sub("<ISO>", text)
    return _UUID.sub("<UUID>", text)


async def _drain() -> None:
    me = asyncio.current_task()
    for _ in range(2000):
        others = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        if not others:
            await asyncio.sleep(0)
            if not [t for t in asyncio.all_tasks() if t is not me and not t.done()]:
                return
            continue
        await asyncio.wait(others, timeout=10)
    raise RuntimeError("parity drain did not settle")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--testing-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    testing = Path(args.testing_dir)

    fake_mod = _load(testing / "fake_gmail.py", "parity_fake_gmail")
    llm_mod = _load(testing / "scripted_llm.py", "parity_scripted_llm")

    import server  # noqa: F401  (the tree under test)

    gmail = fake_mod.FakeGmail(mode="instant", seed="parity")
    priya = gmail.inject_email(
        sender="Priya Patel <priya.patel@example.com>", subject="Auto renewal AU-77120",
        body="Hi, can you confirm my auto renewal?", message_id="aaaaaaaaaaaaaaa1", thread_id="bbbbbbbbbbbbbbb1",
    )

    def fake_execute(tool_name, composio_user_id, *, arguments=None):
        prepared = {k: v for k, v in (arguments or {}).items() if v is not None}
        prepared.setdefault("user_id", "me")
        return gmail.execute(tool_name, composio_user_id, prepared)

    def no_composio(*_a, **_k):
        raise RuntimeError("parity harness must never construct a Composio client")

    for name in GMAIL_CALLER_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "execute_gmail_tool"):
            setattr(module, "execute_gmail_tool", fake_execute)
        if hasattr(module, "get_active_gmail_user_id"):
            setattr(module, "get_active_gmail_user_id", lambda: "fake-user")
    importlib.import_module("server.services.gmail.client")._get_composio_client = no_composio

    S = llm_mod
    llm = S.ScriptedLLM()

    def current_turn(content: str) -> str:
        for tag in ("new_user_message", "new_agent_message"):
            start, end = content.find(f"<{tag}>"), content.find(f"</{tag}>")
            if start != -1 and end != -1:
                return f"<{tag}>" + content[start + len(tag) + 2:end]
        return content

    def ia(call):
        text = current_turn(call.first_user_content)
        rounds = sum(1 for m in call.messages if m.get("role") == "assistant")
        if rounds:
            return S.final("")
        if "<new_agent_message>" in text:
            if "Draft ready" in text:
                return S.respond(
                    S.tool_call("send_draft", to="priya.patel@example.com", subject="Re: Auto renewal AU-77120",
                                body="Hi Priya, your renewal is confirmed."),
                    S.tool_call("send_message_to_user", message="Here is the draft. Shall I send it?"),
                )
            return S.respond(S.tool_call("send_message_to_user", message="Update received."))
        if "hello" in text:
            return S.respond(S.tool_call("send_message_to_user", message="Hello! How can I help?"))
        if "Draft a reply" in text:
            return S.respond(
                S.tool_call("send_message_to_user", message="On it."),
                S.tool_call("send_message_to_agent", agent_name="Priya Renewal", instructions="Draft a reply to Priya confirming her auto renewal."),
            )
        if "Remind me" in text:
            return S.respond(S.tool_call("send_message_to_agent", agent_name="Priya Renewal", instructions="Create a reminder to follow up with Priya."))
        if "send it" in text:
            return S.respond(S.tool_call("send_message_to_agent", agent_name="Priya Renewal", instructions="Send the draft to Priya."))
        return S.final("")

    def ea(call):
        rounds = sum(1 for m in call.messages if m.get("role") == "assistant")
        instruction = call.first_user_content
        if "Draft a reply" in instruction:
            steps = [
                S.respond(S.tool_call("task_email_search", search_query="emails from Priya about renewal")),
                S.respond(S.tool_call("gmail_create_draft", recipient_email="priya.patel@example.com",
                                      subject="Re: Auto renewal AU-77120", body="Hi Priya, your renewal is confirmed.",
                                      thread_id=priya.thread_id)),
                S.final("Draft ready: To priya.patel@example.com, Subject Re: Auto renewal AU-77120."),
            ]
        elif "Create a reminder" in instruction:
            steps = [
                S.respond(S.tool_call("createTrigger", payload="Follow up with Priya", start_time="2020-01-01T09:00:00Z")),
                S.final("Reminder created."),
            ]
        elif "Trigger fired" in instruction:
            steps = [S.final("Reminder: follow up with Priya about her renewal.")]
        elif "Send the draft" in instruction:
            draft_id = next(iter(gmail.drafts), "missing")
            steps = [S.respond(S.tool_call("gmail_execute_draft", draft_id=draft_id)), S.final("Sent the draft to Priya.")]
        else:
            steps = [S.final("Done.")]
        return steps[min(rounds, len(steps) - 1)]

    def search(call):
        rounds = sum(1 for m in call.messages if m.get("role") == "assistant")
        if rounds == 0:
            return S.respond(S.tool_call("gmail_fetch_emails", query="from:priya renewal"))
        return S.respond(S.tool_call("return_search_results", message_ids=[priya.id]))

    llm.on("interaction", ia).on("execution", ea).on("search", search)
    llm.on("classifier", lambda call: S.respond(S.tool_call("mark_email_importance", important=True, summary="Carrier needs signed forms today.")))
    llm.install()

    from server.agents.interaction_agent.runtime import InteractionAgentRuntime
    from server.services.gmail import get_important_email_watcher
    from server.services.trigger_scheduler import get_trigger_scheduler

    async def scenario() -> None:
        for text in ("hello", "Draft a reply to Priya about her renewal", "Remind me to follow up with Priya"):
            await InteractionAgentRuntime().execute(text)
            await _drain()
        await get_trigger_scheduler()._poll_once()
        await _drain()
        watcher = get_important_email_watcher()
        await watcher._poll_once()
        gmail.inject_email(sender="Maple Mutual <renewals@maplemutual.example>", subject="Signature needed",
                           body="Please sign today.", message_id="aaaaaaaaaaaaaaa2", thread_id="bbbbbbbbbbbbbbb2")
        await watcher._poll_once()
        await _drain()
        await InteractionAgentRuntime().execute("send it")
        await _drain()

    asyncio.run(scenario())

    data = Path("server") / "data"
    conversation = data / "conversation"
    agents_dir = data / "execution_agents"
    journals = {
        p.name: [normalize(line) for line in p.read_text(encoding="utf-8").splitlines()]
        for p in sorted(agents_dir.glob("*.log"))
    }
    with sqlite3.connect(data / "triggers.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = [{k: normalize(str(v)) if v is not None else None for k, v in dict(r).items()}
                for r in conn.execute("SELECT * FROM triggers ORDER BY id")]
    result = {
        "conversation_log": [normalize(l) for l in (conversation / "poke_conversation.log").read_text(encoding="utf-8").splitlines()],
        "working_memory": [normalize(l) for l in (conversation / "poke_working_memory.log").read_text(encoding="utf-8").splitlines()],
        "roster": json.loads((agents_dir / "roster.json").read_text(encoding="utf-8")),
        "journals": journals,
        "triggers": rows,
        # Correlation fields only exist on the current tree (fake reads them from context when present).
        "fake_gmail_actions": [{k: v for k, v in e.items() if k not in ("at", "run_id", "agent", "turn_id")}
                               for e in gmail.actions_log()],
        "llm_calls": [(c.role, c.agent) for c in llm.calls],
    }
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
