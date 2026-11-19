"""Offline replay of recorded agent Gmail queries through the old and the new fake-Gmail matcher (no LLM).

    python -m evals.replay_queries <raw results dir or file>... [--old-ref 5dc365c] [--out evals/results/query_replay.md]

The old matcher is loaded from git (``<old-ref>:server/testing/fake_gmail.py``),
so the comparison uses the exact code that produced the original runs. Both run
against the same broker fixture with the same clock.
"""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .harness import DEFAULT_FIXTURE, EVALS_DIR


def _load_results(paths: List[str]) -> List[Dict[str, Any]]:
    files: List[Path] = []
    for raw in paths:
        path = Path(raw)
        files += sorted(path.rglob("*.json*")) if path.is_dir() else [path]
    out = []
    for file in files:
        opener = gzip.open if file.suffix == ".gz" else open
        with opener(file, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and "runs" in data:
            out.append(data)
    return out


def _module_from_source(source: str, name: str):
    tmp = Path(tempfile.mkdtemp(prefix="replay-")) / f"{name}.py"
    tmp.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, tmp)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def replay(result_sets: List[Dict[str, Any]], old_ref: str) -> Dict[str, Any]:
    repo = EVALS_DIR.parent
    old_source = subprocess.run(["git", "show", f"{old_ref}:server/testing/fake_gmail.py"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout
    old_mod = _module_from_source(old_source, "replay_old_fake_gmail")
    new_mod = _module_from_source((repo / "server/testing/fake_gmail.py").read_text(encoding="utf-8"), "replay_new_fake_gmail")
    fixture = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    old = old_mod.FakeGmail(fixture, mode="instant", now_fn=lambda: now)
    new = new_mod.FakeGmail(fixture, mode="instant", now_fn=lambda: now)

    queries: List[str] = []
    for data in result_sets:
        for run in data["runs"]:
            for action in run.get("gmail_actions", []):
                if action.get("action") == "GMAIL_FETCH_EMAILS" and action.get("run_id"):
                    queries.append(action.get("query") or "")

    rows = {"old": Counter(), "new": Counter()}
    remaining: Counter = Counter()
    for query in queries:
        for label, gmail in (("old", old), ("new", new)):
            result = gmail.search(query)
            rows[label]["queries"] += 1
            rows[label]["with_unsupported"] += bool(result.unsupported)
            rows[label]["zero_results"] += not result.messages
            if label == "new":
                for token in result.unsupported:
                    remaining[token.split(":")[0] + (":" if ":" in token else "")] += 1
    return {"queries": len(queries), "rows": rows, "remaining_unsupported": remaining, "old_ref": old_ref}


def render(report: Dict[str, Any]) -> str:
    n = report["queries"]
    lines = [
        f"Replay of {n} recorded agent `GMAIL_FETCH_EMAILS` queries against the broker fixture "
        f"(old matcher = `{report['old_ref']}`, new = current parser). MEASURED (deterministic).",
        "",
        "| Matcher | Queries | With unsupported tokens | Returning zero results |",
        "|---|---|---|---|",
    ]
    for label in ("old", "new"):
        row = report["rows"][label]
        lines.append(f"| {label} | {row['queries']} | {row['with_unsupported']} ({row['with_unsupported'] / max(n, 1):.0%}) | "
                     f"{row['zero_results']} ({row['zero_results'] / max(n, 1):.0%}) |")
    if report["remaining_unsupported"]:
        lines += ["", "Remaining unsupported operators with the new parser (count of occurrences):", ""]
        lines += [f"- `{op}` × {count}" for op, count in report["remaining_unsupported"].most_common()]
    return "\n".join(lines) + "\n"


def main() -> None:
    # The fake imports `server` lazily for telemetry: never let that touch server/data.
    import os

    os.environ["OPENPOKE_DATA_DIR"] = tempfile.mkdtemp(prefix="openpoke-replay-")
    os.environ["OPENPOKE_TELEMETRY"] = "0"
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--old-ref", default="5dc365c")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    text = render(replay(_load_results(args.paths), args.old_ref))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
