# Work log

A record of steps, decisions, deviations and test results, in order. Times are local (America/Toronto).

## 2026-11-19: Step 0 (inspect and preserve)

- Saved `git status`, `git diff --stat` and `git log -1` to a file outside the repo.
- Backed up `server/data` outside the repo: 9 files, 61,992 bytes. The file count and total size match the original.
- Environment: `uv` 0.12 with the project venv `openpoke/.venv` (CPython 3.13.15). `uv add --dev pytest` installed pytest 9.2.0. I also added `requirements-dev.txt` for pip users.
- Created branch `overload-fixes`. Committed the pre-existing local setup changes plus `pyproject.toml` and `uv.lock` as `baseline: pre-existing local setup changes`. Tagged that commit `baseline-pre-fixes`.
- Re-verified the facts in the spec (§1.3 and §2.1). Line numbers match. Differences:
  - `INVESTIGATION.md` does not exist in the repo, so there was nothing to exclude.
  - `tasks/search_email/gmail_internal.py::gmail_fetch_emails` is dead code. Only its schema is imported. It would also raise `TypeError` if called, because it passes `arguments` positionally to a keyword-only parameter. It is left unchanged and documented.
  - Trigger runs never call `record_request`, so trigger instructions are not journaled. This is baseline behaviour and is kept.

## Phase 0

- P0.1: `server/paths.py::data_dir()`. Seven modules now resolve their data directory through it (still at import time). Tests set `OPENPOKE_DATA_DIR` before importing `server`.
- P0.2: `server/flags.py` (profile `OPENPOKE_FIXES`, per-flag overrides, `validate()`) and `server/overload_config.py` (every constant, env-overridable, each with a rationale).
- P0.3: `server/services/context.py`. Turn and run scopes are opened in the chat handler, in the batch-manager follow-up turn (as a child of the batch's turn, which fixes the context-leak gotcha), per trigger, and per watcher notification.
- P0.4: `server/services/telemetry.py`. Events have sizes and hashes only. Every LLM caller passes `role=`. `llm_call` copies `usage` when present.
- P0.5: tests:
  - `conftest.py` sets a temp data dir, fake Gmail and a dummy API key.
  - `tests/scenarios.py` reproduces E1–E7.
  - Ten characterization tests pin baseline behaviour.
  - Parity test: deviation from the spec. It uses `git archive` of the tag instead of a `git worktree`. This is read-only and leaves no worktree metadata behind. It is the same comparison.
  - Added a negative control: a one-word change to the result format breaks parity.

## Phase 0, continued

- **P0.6.**
  - Fixture: a fictional brokerage, 39 messages in 28 threads, with two Sarah Chens and AU-77120 vs AU-77210.
  - Scenarios S1–S9.
  - L3 runner: one subprocess per repetition, a fake-mode guard, an LLM call budget, and a `--dry-run` with a scripted stand-in model.
  - `metrics.py`, `compare.py` and `report_tables.py`. FakeGmail records run/turn correlation for gold labelling.
- **P0.7 baseline L3.** Run from a frozen export of commit `e7a5004` with `openai/gpt-6-luna`, scenarios S1, S2, S3, S4@{10,200,500}, S5, S8 and S9, 3 repetitions each. MEASURED: 1,737 calls, about 8.0M tokens, $0.333, 0 rate-limit responses.
  - The original trace reproduced live: S9 spins on `wait` to the 8-round limit.
  - The model over-merges unrelated tasks into existing agents (S1, S3, S4).
  - S8 produced 2 duplicate sends in 3 repetitions.
- **Probes.**
  - Follow-up reach: at most 4 requests back (S3). This confirms K = 5 (U3).
  - Rate limits: 0 responses with status 429 in all L3 runs, so C = 4 stays provisional (U4).
  - Composio thread-safety probe: **not executed** (U13: no authorization to touch the real account).
- **Second model (optional).**
  - `anthropic/claude-sonnet-4` (the upstream default) returns 404 on OpenRouter now.
  - S9 baseline with `anthropic/claude-haiku-5.5`, 3 repetitions, 22 calls: **no** `wait` spin (1.5 IA calls per turn, 0 limit hits).
  - So M3's cost depends on the model; with `WAIT_ENDS_TURN` the turn length is bounded either way.

## Phase 1

- 1.1–1.3 (`8b07055`), 1.4–1.7 (`b68fcfa`), 1.8–1.10 (`27e6bbc`).
- Decisions and fixes along the way:
  - The safety DB write path deadlocked on first use: schema init took the same non-reentrant lock as `write()`. Init now has its own lock.
  - Successful runs that leave a send in doubt are reported `[UNKNOWN]`, a stricter form of I4.
  - A trigger run cancelled at shutdown keeps its claim `claimed`, so it is reported on restart.
  - `task_email_search` falls back to a `repr` string because `json.dumps` fails on datetimes. This is pre-existing and documented, not fixed.
- L3 run: the `phase1` variant (every Phase 1 flag except the gate), from a frozen export of `27e6bbc`.

## Phase 2

- 2.1–2.9 (`d744018`).
- Decisions:
  - Registry additions: `latest_request`, `last_outcome_at`, and a `drafts` table.
  - The roster facade syncs names that were written while the flag was off.
  - E2 now has one user task and one trigger task, because same-turn work is batched together by design.
  - The fake provider limit in S6 is sized to C (U4).
- Metric fixes. These were applied after inspecting the baseline and the first two `fixed` S1 repetitions. S1 has no seeded agents, so those repetitions are unaffected. No gate threshold was changed.
  - Routing gold work into a seeded distractor counts as a false merge and is never "correct".
  - `summarize` no longer drops metrics whose first value is `None`.
- Per-flag test matrix run from a frozen export (`02db50f`); results are in `03_EVALUATION.md`.
- L3 run: the `fixed` variant (`OPENPOKE_FIXES=all`), from a frozen export of `d744018`.

## Evaluation results

- **`phase1` L3** (export of `27e6bbc`): 1,144 calls, $0.267. Every Phase 1 gate PASSES.
- **`fixed` L3** (export of `d744018`): 1,139 calls, $0.242.
  - **PASS:**
    - limit hits 102 → 0;
    - duplicate sends 2 → 0;
    - IA calls per turn halved;
    - roster flat at ~1.2k chars;
    - context_hit 1.0 on S1 and S5;
    - routing accuracy at N = 200 ≥ N = 10.
  - **FAIL** (reported as failed, nothing loosened):
    - false merges 32/67 (baseline 46/75). All remaining merges came from the model typing an existing agent's exact name, none from code routing.
    - "fragmentation below baseline": the baseline over-merges, so the gate as written would reward merging.
    - context_hit ≥ baseline on S4@10 (1.00 → 0.67): the IA started no run in that step.
- **Flag matrix** (export of `02db50f`): all 22 configurations green.
- `evals/results/summary.md` is generated by `python -m evals.report_tables`.

## Submission pass (branch `submission-prep`)

An independent review raised findings about measurement, tests, reporting and packaging. Each was verified before acting on it:

- **F1 (async harness on Python 3.10/3.11): confirmed and fixed.** `tests/helpers.py` keeps one registry of harness tasks, because before 3.12 `wait_for` runs the test body in a new task. `drain`, the crash simulation and task counting all exclude those tasks. 3.10 and 3.13 both pass.
- **F2 (watcher test timing): confirmed** (99/100 on 3.13 at the start of the pass). The test now asserts delivered + reported overflow == injected, and an exact small-buffer test was added.
- **F3 (fake Gmail ignored `OR`/parentheses): confirmed and fixed** with a query parser. Offline replay of the 2,310 recorded agent queries: unsupported-token queries 943 (41%) → 0; zero-result queries 1,160 (50%) → 595 (26%).
- **F4, F6 and the "wrong person" part of F7: not used.** These came from an attribution error the review made.
  - The review assigned fake-Gmail actions to steps with `seq >= gmail_seq_start`. The runner records `gmail_seq_start` as the number of actions *before* the step, so the correct rule is `seq > gmail_seq_start`.
  - The correct rule agrees with the run timestamps for every agent action (911/911 baseline, 830/830 phase1, 876/876 fixed). The review's rule disagrees with them in about 70 actions per run set.
  - With correct attribution, the original runs have **0 wrong-target writes** (0/73 baseline, 0/64 fixed). Reused agents still wrote to the right person and thread.
  - The unrequested-send *counts* in F7 are correct (3 baseline, 2 fixed). Every one went to the correct recipient: a draft was asked for and an email was sent without confirmation.
  - The user verified the attribution independently.
  - The synthetic leakage unit test (`test_s1_leakage_case_v1_scores_correct_v2_does_not`) is kept. It checks the metric's logic, not the data.
- **F5 (runs vs decisions): confirmed.** Metric v2 counts merge decisions once per (agent, gold thread) and does not let a merged run make its agent the owner of the merged thread. Original runs: 39 decisions (baseline) → 26 (fixed). False-merge runs: 55/96 → 38/88.
- **F8 (S8 duplicate-send evidence is thin): confirmed.** The injected fault fired in 2 of 3 baseline repetitions, both of which resent, and in 1 of 3 fixed repetitions (no resend; the ledger recorded `unknown`).
- **F9 (raw JSON in git): fixed.** The raw runs were archived outside the repository (gzipped, sha256 verified) and removed from the branch.
- **F10 (review surface): addressed** with `REVIEWERS_GUIDE.md`.

### Submission pass: re-run and packaging

**L3 re-run (T6).**
- **What ran.** Baseline and fixed ran in parallel from a `git archive` export of `4484426`, which has the corrected fake. Model `openai/gpt-6-luna`, all scenarios, 3 repetitions, S4 including N = 500, `--max-llm-calls 2500`. A dry run passed first.
- **Results.**
  - **baseline:** 27 runs, 1,798 calls, $0.355;
  - **fixed:** 27 runs, 1,120 calls, $0.250;
  - no worker errors and no 429s.
- **Cleanup.** The `.env` copy in the export was deleted with the export.
- **Phase1.** Not re-run; it was optional.

**What the re-run changed** (details in 03 and 05):
- **Duplicate sends.** 1 → 1, so the hard gate fails. Both duplicates followed an unrequested send. In the fixed run, an agent-started follow-up turn overrode the ledger's near-duplicate block with `confirm_duplicate_of`: the override rule accepts any user-rooted chain (`ledger.py:238`). That is a next step; it was not changed in this pass.
- **context_hit** fails on S1 (5 of 6). Every miss except one is a step where the IA started no execution run. The exception is a send step routed to a near-miss agent, which wrote and sent a new email instead of the reviewed draft.
- **The single fixed wrong-target write.** The IA deferred "Make it shorter." (it only called `wait`) and dispatched it in the next step, so step-based attribution charges the draft to that step. The draft has the right thread and recipient.
- **Merge decisions.** All of them, in every run set, went to agents reached by exact-name dispatch.

**Packaging (T7).**
- **Archive.** The original raw runs went to `../openpoke-eval-raw/20261119T204035Z/original/`, gzipped, with `SHA256SUMS.txt` (verified), and were removed from the branch.
- **Committed artifacts.** `summary.md`, `audit.md`, `query_replay.md` and the per-run metric rows, all generated by `python -m evals.report_tables` and `python -m evals.replay_queries`.

**Not changed (rules of this pass).** No product code, prompts or flag defaults. `pyproject.toml`'s `requires-python` stays at ≥ 3.13 (upstream). Supported test versions are documented in `docs/overload/README.md`.
