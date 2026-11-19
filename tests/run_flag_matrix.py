"""Run the whole suite once per flag configuration (§7.2 / phase gates).

    python tests/run_flag_matrix.py [--out docs/overload/flag_matrix.md]

Configurations: every flag off; each flag alone with its prerequisites;
OPENPOKE_FIXES=all; all plus SEND_GATE. Prints one Markdown table row per run.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Importing `server` opens the upstream stores at import time (e.g. TriggerStore -> triggers.db),
# so this parent process must never resolve the real server/data. The pytest children are
# isolated by tests/conftest.py, which sets its own temporary OPENPOKE_DATA_DIR.
os.environ["OPENPOKE_DATA_DIR"] = tempfile.mkdtemp(prefix="openpoke-flag-matrix-")

from server import flags  # noqa: E402


def closure(name: str) -> list:
    out: list = []

    def add(n: str) -> None:
        for req in flags.PREREQUISITES.get(n, ()):
            add(req)
        if n not in out:
            out.append(n)

    add(name)
    return out


def configurations():
    yield "all flags off", {"OPENPOKE_FIXES": "none"}
    for name in flags.FLAG_NAMES:
        yield f"{name} (+ prerequisites)", {"OPENPOKE_FIXES": ",".join(closure(name))}
    yield "OPENPOKE_FIXES=all", {"OPENPOKE_FIXES": "all"}
    yield "all + SEND_GATE", {"OPENPOKE_FIXES": "all", "OPENPOKE_FIX_SEND_GATE": "1"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--require", default=None, help="only configurations that enable this flag (e.g. SEND_LEDGER)")
    args = parser.parse_args()
    rows = ["| Configuration | Flags | Result | Wall time |", "|---|---|---|---|"]
    for label, overrides in configurations():
        enabled = overrides["OPENPOKE_FIXES"]
        if args.require and not (enabled == "all" or args.require in enabled.split(",")):
            continue
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENPOKE_FIX")}
        env.update(overrides)
        started = time.monotonic()
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=REPO, env=env,
                              capture_output=True, text=True)
        summary = next((line for line in reversed(proc.stdout.splitlines()) if re.search(r"\d+ (passed|failed)", line)), None)
        if summary is None:
            # Keep the evidence: exit code in the table, full output next to it.
            log = Path(args.out or "flag_matrix").with_suffix(f".{label.split()[0]}.log")
            log.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr, encoding="utf-8")
            summary = f"no pytest summary (exit {proc.returncode}; output in {log.name})"
        elif proc.returncode != 0:
            summary += f" (exit {proc.returncode})"
        summary = summary.strip("= ").strip()
        row = f"| {label} | `{overrides['OPENPOKE_FIXES']}`{' + SEND_GATE' if 'OPENPOKE_FIX_SEND_GATE' in overrides else ''} | {summary} | {time.monotonic() - started:.0f} s |"
        rows.append(row)
        print(row, flush=True)
    if args.out:
        Path(args.out).write_text("\n".join(rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
