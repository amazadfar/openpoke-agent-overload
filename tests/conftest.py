"""Test harness configuration.

The environment is fixed BEFORE anything imports ``server`` (module singletons
resolve their data directory at import time):

- OPENPOKE_DATA_DIR -> a fresh temp directory (never the user's server/data);
- OPENPOKE_FAKE_GMAIL=1 -> Composio is unreachable, the fake backend answers;
- a dummy OpenRouter key and model -> the real API can never be called by
  accident (and the real key in .env is never loaded, because .env only fills
  variables that are not already set).

Flags: tests marked with the ``flags_off``/``flags`` fixtures pin their flag
set; every other test runs under the ambient OPENPOKE_FIXES profile, which is
how ``OPENPOKE_FIXES=all python -m pytest`` exercises everything with all fixes on.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="openpoke-tests-")
os.environ["OPENPOKE_DATA_DIR"] = _TEST_DATA_DIR
os.environ["OPENPOKE_FAKE_GMAIL"] = "1"
os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
os.environ["OPENROUTER_MODEL"] = "test/scripted-model"
os.environ["COMPOSIO_API_KEY"] = ""
os.environ["COMPOSIO_GMAIL_AUTH_CONFIG_ID"] = ""
os.environ.pop("OPENPOKE_FAKE_GMAIL_FIXTURE", None)
os.environ.pop("OPENPOKE_EVAL_CAPTURE_PROMPTS", None)

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

import server  # noqa: E402,F401  (import after the environment is fixed)
from server import flags as flag_module  # noqa: E402
from server.paths import data_dir, default_data_dir  # noqa: E402
from server.testing.reset import reset_state  # noqa: E402

assert data_dir() != default_data_dir(), "tests must never use the real server/data"


@pytest.fixture(autouse=True)
def _isolated_state():
    reset_state()
    yield
    reset_state()


def _clear_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(flag_module.FLAG_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def flags_off(monkeypatch: pytest.MonkeyPatch):
    """Pin every fix flag off, whatever the ambient profile."""
    _clear_flag_env(monkeypatch)
    monkeypatch.setenv(flag_module.PROFILE_ENV, "none")
    return monkeypatch


def with_prerequisites(names) -> list:
    out: list = []

    def add(name: str) -> None:
        for req in flag_module.PREREQUISITES.get(name, ()):
            add(req)
        if name not in out:
            out.append(name)

    for name in names:
        add(name)
    return out


@pytest.fixture
def set_flags(monkeypatch: pytest.MonkeyPatch):
    """``set_flags("AGENT_LOCK", ...)`` pins exactly these flags (plus prerequisites) on, all others off."""

    def _apply(*names: str) -> list:
        _clear_flag_env(monkeypatch)
        enabled = with_prerequisites(names)
        monkeypatch.setenv(flag_module.PROFILE_ENV, ",".join(enabled) if enabled else "none")
        flag_module.validate()
        return enabled

    return _apply


@pytest.fixture
def scripted_llm(monkeypatch: pytest.MonkeyPatch):
    from server.testing.scripted_llm import ScriptedLLM

    return ScriptedLLM().install(monkeypatch)


@pytest.fixture
def fake_gmail():
    from server.testing.fake_gmail import FakeGmail, install_fake_gmail

    return install_fake_gmail(FakeGmail(mode="sync"))
