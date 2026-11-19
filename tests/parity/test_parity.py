"""Invariant I1: with every flag off, the current tree behaves exactly like the baseline.

The same scripted scenario (several user turns, a dispatch, a search, a draft,
a trigger that fires, a watcher email, a send) runs twice, each in a
subprocess on a throwaway copy of a tree:

- the ``baseline-pre-fixes`` tag, extracted with ``git archive`` (read-only:
  no worktree registration is left behind);
- the current working tree's ``server/`` package.

Normalized persistent state must be identical.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import io
from pathlib import Path

import pytest

from helpers import REPO_ROOT

pytestmark = pytest.mark.parity

DRIVER = Path(__file__).with_name("parity_driver.py")
TESTING_DIR = REPO_ROOT / "server" / "testing"
BASELINE_REF = "baseline-pre-fixes"


def _extract_baseline(target: Path) -> None:
    try:
        archive = subprocess.run(
            ["git", "archive", "--format=tar", BASELINE_REF, "server"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"cannot read {BASELINE_REF} from git: {exc}")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data") if sys.version_info >= (3, 12) else tar.extractall(target)


def _copy_current(target: Path) -> None:
    shutil.copytree(REPO_ROOT / "server", target / "server", ignore=shutil.ignore_patterns("data", "__pycache__"))


def _run(tree: Path, out: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENPOKE_", "OPENROUTER_", "COMPOSIO_"))}
    env.update(
        PYTHONPATH=str(tree),
        OPENROUTER_API_KEY="parity-key-not-real",
        OPENROUTER_MODEL="parity/scripted-model",
        COMPOSIO_API_KEY="",
        COMPOSIO_GMAIL_AUTH_CONFIG_ID="",
    )
    proc = subprocess.run(
        [sys.executable, str(DRIVER), "--testing-dir", str(TESTING_DIR), "--out", str(out)],
        cwd=tree, env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"driver failed on {tree}:\n{proc.stderr[-4000:]}"
    return json.loads(out.read_text())


def test_flags_off_matches_baseline(tmp_path):
    base_tree, current_tree = tmp_path / "baseline", tmp_path / "current"
    _extract_baseline(base_tree)
    _copy_current(current_tree)

    baseline = _run(base_tree, tmp_path / "baseline.json")
    current = _run(current_tree, tmp_path / "current.json")

    # The scenario really exercised everything it claims to.
    assert baseline["roster"] == ["Priya Renewal"]
    assert baseline["triggers"] and baseline["triggers"][0]["status"] == "completed"
    actions = [a["action"] for a in baseline["fake_gmail_actions"]]
    assert {"GMAIL_FETCH_EMAILS", "GMAIL_CREATE_EMAIL_DRAFT", "GMAIL_SEND_DRAFT"} <= set(actions)
    assert any("Important email watcher notification" in line for line in baseline["conversation_log"])

    for key in baseline:
        assert current[key] == baseline[key], f"parity mismatch in {key}"


def test_parity_harness_detects_a_behaviour_change(tmp_path):
    """Negative control: a one-word change in the result format must break parity."""
    base_tree, mutated_tree = tmp_path / "baseline", tmp_path / "mutated"
    _extract_baseline(base_tree)
    _copy_current(mutated_tree)
    batch = mutated_tree / "server" / "agents" / "execution_agent" / "batch_manager.py"
    original = batch.read_text()
    mutated = original.replace('entries.append(f"[{status}] {result.agent_name}: {response_text}")',
                               'entries.append(f"[{status}!] {result.agent_name}: {response_text}")')
    assert mutated != original, "negative control mutation did not apply"
    batch.write_text(mutated)
    baseline = _run(base_tree, tmp_path / "baseline.json")
    mutated = _run(mutated_tree, tmp_path / "mutated.json")
    assert mutated["conversation_log"] != baseline["conversation_log"]
