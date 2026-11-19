# Agent overload fixes: documentation index

Start with [`REVIEWERS_GUIDE.md`](../../REVIEWERS_GUIDE.md) at the repository root.

| Document | What it covers |
|---|---|
| [01_PROBLEM_AND_EVIDENCE.md](01_PROBLEM_AND_EVIDENCE.md) | What OpenPoke is, what "agent overload" means here (three classes), mechanisms M1–M8 with evidence, other gaps found |
| [02_DESIGN.md](02_DESIGN.md) | Target architecture, every fix with rationale and trade-offs, flags, constants (with chosen values and evidence), schemas, invariants, deviations, traceability matrix |
| [03_EVALUATION.md](03_EVALUATION.md) | Test layers (L1/L2/L3), fixture, scenarios, metrics (v1 and v2), fake-Gmail fidelity, gates, original and re-run results, what was not executed |
| [04_FUTURE_ARCHITECTURE.md](04_FUTURE_ARCHITECTURE.md) | Entity-keyed agents, a durable prioritized queue, deferred items and the evidence that would justify them |
| [05_ENGINEERING_REPORT.md](05_ENGINEERING_REPORT.md) | Final report: what was built, test results, evaluation, limitations, unresolved decisions, how to reproduce |
| [WORKLOG.md](WORKLOG.md) | Chronological log of steps, decisions and deviations |
| [../../evals/README.md](../../evals/README.md) | How to run the live evaluation safely |

## Environment setup and supported Python versions (verified on Linux, 2026-11-19)

**Supported for the tests:** Python **3.10 and 3.13** are both verified, with flags off, with `OPENPOKE_FIXES=all`, across the full flag matrix, and with parity. 3.11 and 3.12 were not run.

- **Why it was version-sensitive.** Before 3.12, `asyncio.wait_for` runs its awaitable in a new task. The test harness (`tests/helpers.py`) therefore keeps a registry of its own tasks and excludes them when it waits for or counts "other" tasks.
- **`pyproject.toml` (uv) vs `server/requirements.txt` (pip).** `pyproject.toml` declares `requires-python >= 3.13`. That line is the uv project's interpreter pin, inherited from upstream, and this work does not change it. The upstream README and the pip path state Python 3.10+.
- **Which setup each version uses.** With uv you get 3.13. For 3.10 or 3.11, use pip or `uv run --no-project` (below).

With uv (project virtualenv `.venv`, CPython 3.13):

```bash
uv sync                                   # runtime deps + the dev group (pytest)
uv run python -m pytest -q tests          # L1 + L2 + parity, every flag off (the default)
OPENPOKE_FIXES=all uv run python -m pytest -q tests
uv run python tests/run_flag_matrix.py    # every flag alone (+ prerequisites), all, all + send gate
```

With pip. On 3.10 this was verified in an equivalent venv, made with `uv venv --python 3.10` and `uv pip install -r server/requirements.txt pytest`:

```bash
python3.10 -m venv .venv310 && . .venv310/bin/activate
pip install -r server/requirements.txt -r requirements-dev.txt
python -m pytest -q tests
python tests/run_flag_matrix.py
```

On Python 3.10 without creating a venv (verified):

```bash
uv run --isolated --no-project --python 3.10 --with-requirements server/requirements.txt --with pytest python -m pytest -q tests
```

The tests never touch `server/data` or a real Gmail account. `tests/conftest.py` sets:
- a temporary `OPENPOKE_DATA_DIR`;
- `OPENPOKE_FAKE_GMAIL=1`;
- a dummy OpenRouter key, before anything imports `server`.

The parity test reads the `baseline-pre-fixes` git tag. It is skipped when git or the tag is unavailable, for example in an exported copy.
