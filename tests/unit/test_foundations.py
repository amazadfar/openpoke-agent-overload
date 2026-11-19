"""L1 tests for Phase 0 foundations: data dir (P0.1), flags/config (P0.2), context (P0.3)."""

from __future__ import annotations

import asyncio
import contextvars
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import REPO_ROOT, run_async
from server import flags, overload_config
from server.paths import data_dir, default_data_dir
from server.services import context as ctx


# ---------------------------------------------------------------- P0.1 data dir
def test_default_data_dir_is_unchanged(monkeypatch):
    monkeypatch.delenv("OPENPOKE_DATA_DIR", raising=False)
    assert data_dir() == REPO_ROOT / "server" / "data"
    assert default_data_dir() == REPO_ROOT / "server" / "data"


def test_override_is_absolute_and_created(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "dir"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENPOKE_DATA_DIR", "nested/dir")
    resolved = data_dir()
    assert resolved == target.resolve()
    assert resolved.is_absolute() and resolved.is_dir()


_STORE_PROBE = r"""
import json, os, sys
import server
from server.services.conversation import get_conversation_log
from server.services.execution import get_agent_roster, get_execution_agent_logs
from server.services.triggers import get_trigger_service
from server.services.timezone_store import get_timezone_store
from server.services.gmail import get_important_email_watcher
get_conversation_log().record_user_message("hello")      # conversation log + working memory
get_execution_agent_logs().record_request("Probe", "x")  # journals
get_agent_roster().add_agent("Probe")                     # roster
get_trigger_service().create_trigger(agent_name="Probe", payload="p")  # triggers.db
get_timezone_store().set_timezone("UTC")                  # timezone.txt
get_important_email_watcher()._seen_store.mark_seen(["abc"])  # gmail_seen.json
print(json.dumps(sorted(str(p.relative_to(os.environ["OPENPOKE_DATA_DIR"])) for p in __import__("pathlib").Path(os.environ["OPENPOKE_DATA_DIR"]).rglob("*") if p.is_file())))
"""


def _probe(directory: Path) -> list:
    env = {**os.environ, "OPENPOKE_DATA_DIR": str(directory), "OPENPOKE_TELEMETRY": "0"}
    out = subprocess.run(
        [sys.executable, "-c", _STORE_PROBE], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True
    )
    import json

    return json.loads(out.stdout.strip().splitlines()[-1])


def test_all_seven_stores_write_under_override(tmp_path):
    files = _probe(tmp_path / "a")
    expected = {
        "conversation/poke_conversation.log",
        "conversation/poke_working_memory.log",
        "execution_agents/probe.log",
        "execution_agents/roster.json",
        "triggers.db",
        "timezone.txt",
        "gmail_seen.json",
    }
    assert expected <= set(files)


def test_two_data_dirs_stay_isolated(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _probe(a)
    assert not b.exists() or not any(b.rglob("*"))
    _probe(b)
    assert (a / "conversation" / "poke_conversation.log").read_text().count("hello") == 1
    assert (b / "conversation" / "poke_conversation.log").read_text().count("hello") == 1


def test_app_imports_with_default_paths_without_writing(tmp_path):
    """`python -m server.server` uses default paths; importing the app must work (no uvicorn start)."""
    env = {k: v for k, v in os.environ.items() if k != "OPENPOKE_DATA_DIR"}
    code = "import server.app, server.paths as p; print(p.data_dir())"
    # Import in a throwaway copy of the package so the real server/data is never touched.
    import shutil

    shutil.copytree(REPO_ROOT / "server", tmp_path / "server", ignore=shutil.ignore_patterns("data", "__pycache__"))
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == str(tmp_path / "server" / "data")


# -------------------------------------------------------------- P0.2 flags/config
def test_all_flags_default_off(flags_off):
    assert flags.active_flags() == []
    assert not any(flags.enabled(name) for name in flags.FLAG_NAMES)


def test_profile_all_excludes_send_gate(flags_off):
    flags_off.setenv("OPENPOKE_FIXES", "all")
    active = set(flags.active_flags())
    assert "SEND_GATE" not in active
    assert active == set(flags.FLAG_NAMES) - {"SEND_GATE"}
    flags.validate()


def test_explicit_override_beats_profile(flags_off):
    flags_off.setenv("OPENPOKE_FIXES", "all")
    flags_off.setenv("OPENPOKE_FIX_WAIT_ENDS_TURN", "0")
    assert not flags.enabled("WAIT_ENDS_TURN")
    flags_off.setenv("OPENPOKE_FIXES", "none")
    flags_off.setenv("OPENPOKE_FIX_WAIT_ENDS_TURN", "1")
    assert flags.enabled("WAIT_ENDS_TURN")


def test_env_changes_take_effect_without_reimport(flags_off):
    assert not flags.enabled("AGENT_LOCK")
    flags_off.setenv("OPENPOKE_FIXES", "agent_lock")
    assert flags.enabled("AGENT_LOCK")
    flags_off.setenv("OPENPOKE_FIXES", "none")
    assert not flags.enabled("AGENT_LOCK")


@pytest.mark.parametrize(
    "profile,missing",
    [
        ("DISPATCH_DEDUPE", "AGENT_LOCK"),
        ("GMAIL_SENDS_OFF_LOOP", "GMAIL_READS_OFF_LOOP"),
        ("SEND_GATE", "SEND_LEDGER"),
        ("AGENT_CARD,AGENT_REGISTRY,KEEP_TOOL_IDS,AGENT_LOCK", "EA_HISTORY_WINDOW"),
        ("POLL_BATCH", "TURN_BATCHES"),
    ],
)
def test_prerequisite_violations_raise(flags_off, profile, missing):
    flags_off.setenv("OPENPOKE_FIXES", profile)
    with pytest.raises(flags.FlagConfigError, match=missing):
        flags.validate()


def test_unknown_flag_names_raise(flags_off):
    flags_off.setenv("OPENPOKE_FIXES", "WAIT_ENDS_TURN,NOT_A_FLAG")
    with pytest.raises(flags.FlagConfigError, match="NOT_A_FLAG"):
        flags.validate()
    flags_off.setenv("OPENPOKE_FIXES", "none")
    flags_off.setenv("OPENPOKE_FIX_TYPO", "1")
    with pytest.raises(flags.FlagConfigError, match="OPENPOKE_FIX_TYPO"):
        flags.validate()


def test_prompt_capture_refused_without_fake_gmail(flags_off):
    flags_off.setenv("OPENPOKE_EVAL_CAPTURE_PROMPTS", "1")
    flags_off.setenv("OPENPOKE_FAKE_GMAIL", "0")
    with pytest.raises(flags.FlagConfigError, match="FAKE_GMAIL"):
        flags.validate()


def test_constants_are_env_overridable_and_validated(monkeypatch):
    assert overload_config.EA_HISTORY_REQUESTS.get() == 5
    monkeypatch.setenv("OPENPOKE_EA_HISTORY_REQUESTS", "7")
    assert overload_config.EA_HISTORY_REQUESTS.get() == 7
    monkeypatch.setenv("OPENPOKE_EA_HISTORY_REQUESTS", "seven")
    assert overload_config.EA_HISTORY_REQUESTS.get() == 5  # invalid -> default, with a warning
    with pytest.raises(ValueError):
        overload_config.validate()
    monkeypatch.delenv("OPENPOKE_EA_HISTORY_REQUESTS")
    monkeypatch.setenv("OPENPOKE_CONCURRENCY_USER_RESERVED", "4")
    with pytest.raises(ValueError, match="RESERVED"):
        overload_config.validate()


def test_every_constant_has_a_rationale():
    for constant in overload_config.all_constants():
        assert constant.rationale.strip(), constant.name


# ------------------------------------------------------------------- P0.3 context
def test_no_context_gives_synthetic_system_turn():
    assert ctx.current_turn().origin == "system"
    assert ctx.current_run() is None


def test_children_inherit_context():
    async def main():
        seen = {}
        with ctx.turn_scope("user", user_message="hi") as turn:

            async def child():
                seen["turn"] = ctx.current_turn()

            await asyncio.get_running_loop().create_task(child())
        return turn, seen["turn"]

    turn, child_turn = run_async(main)
    assert child_turn == turn
    assert child_turn.user_message == "hi"


def test_follow_up_turn_gets_new_id_parent_and_depth():
    with ctx.turn_scope("user", user_message="hi") as root:
        with ctx.turn_scope("agent", parent=root) as follow:
            with ctx.turn_scope("agent", parent=follow) as follow2:
                pass
    assert follow.turn_id != root.turn_id
    assert follow.parent_turn_id == root.turn_id and follow.depth == 1
    assert follow2.depth == 2 and follow2.root_origin == "user"
    assert follow.user_message is None  # only origin=user turns carry the raw text


def test_root_origin_propagates_from_trigger():
    with ctx.turn_scope("trigger", poll_id="poll_1") as root:
        with ctx.turn_scope("agent", parent=root) as child:
            pass
    assert child.root_origin == "trigger"
    assert child.poll_id == "poll_1"


def test_turn_scope_clears_run():
    with ctx.turn_scope("user") as root:
        with ctx.run_scope("A"):
            with ctx.turn_scope("agent", parent=root):
                assert ctx.current_run() is None
            assert ctx.current_run().agent == "A"


def test_executor_path_preserves_context():
    from server.services.gmail.executor import run_gmail

    async def main():
        with ctx.turn_scope("user"):
            with ctx.run_scope("A", run_id="run_x"):
                return await run_gmail(lambda: (ctx.current_run().run_id, ctx.current_turn().origin))

    assert run_async(main) == ("run_x", "user")


def test_plain_run_in_executor_does_not_copy_context():
    """Documents why run_gmail copies the context explicitly."""

    async def main():
        with ctx.run_scope("A"):
            return await asyncio.get_running_loop().run_in_executor(None, ctx.current_run)

    # A default executor thread was started outside the run scope, so it sees no run.
    assert run_async(main) is None
