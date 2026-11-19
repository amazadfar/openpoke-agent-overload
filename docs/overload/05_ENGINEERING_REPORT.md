# 05: Engineering report

## 1. Summary

**The problem, in three classes** (evidence in 01):
1. **Load amplifiers.**
   - The IA busy-waits on `wait`.
   - One IA turn starts per trigger and per important email.
   - Synchronous Composio calls freeze the event loop.
   - One global result batch couples unrelated work.
   - There are no LLM retries.
2. **An identity and memory weakness.** An agent is a bare name matched exactly; the IA sees every name ever created; each agent replays its entire journal, which truncates away the ids follow-ups need. Two runs of one agent can overlap.
3. **A missing safety property.** Sends have no identity: nothing prevents the same email from going out twice, and timeouts misreport sends as failures.

**What was built.** Nineteen fixes, each behind a flag that defaults to off, plus the measurement infrastructure to evaluate them:
- data-dir isolation, flags and constants, correlation context, telemetry;
- a fake Gmail backend, a scripted LLM, a parity harness;
- a live-evaluation runner with gold-labelled scenarios on a fictional insurance inbox.

A parity test against the `baseline-pre-fixes` tag proves that with every flag off, behaviour is unchanged.

**Headline results.**
- **Source:** the re-run with the corrected fake Gmail, scored with metrics v2. All values are MEASURED; full tables are in 03 and `evals/results/summary.md`, itemised in `evals/results/audit.md`.
- **Setup:** `openai/gpt-6-luna`, 27 runs per variant (7 scenarios × 3 repetitions, with S4 at 10, 200 and 500 agents), fake Gmail.
- **Labels:** "original" is the first run set, scored with metrics v1 as first published.

| | Baseline, re-run (v2) | Fixed, re-run (v2) | Original runs (v1): baseline → fixed |
|---|---|---|---|
| **Fixed** | | | |
| LLM calls (total) | 1,798 | 1,120 (**−38%**) | 1,737 → 1,139 |
| Tokens (provider usage) | 8,455,794 | 4,346,686 (**−49%**) | 7,997,544 → 4,298,950 |
| Cost (provider usage, USD) | 0.355 | 0.250 (**−30%**) | 0.333 → 0.242 |
| IA LLM calls per turn (range of scenario medians) | 5.1–6.5 | 2.4–3.0 | 5.2–6.0 → 2.7–3.0 |
| Turns that hit the 8-round limit | 110 | **0** | 102 → 0 |
| IA roster block at 10 / 200 / 500 agents (chars) | 492 / 9,577 / 19,316 | 1,213 / 1,170 / 1,196 (flat) | same shape |
| S8 retry after a lost send response: fault fired / duplicates | 1 of 3 / 0 | 1 of 3 / 0 | 2 of 3 / 2 → 1 of 3 / 0 (v2) |
| **Not fixed** | | | |
| Merge decisions (v2) | 40 | 20 | 39 → 26 (v2) |
| False-merge runs / labelled runs | 57 / 93 | 27 / 91 | 61 / 96 → 41 / 88 (v1) |
| Wrong-target writes / scored writes (v2) | 0 / 70 | 1 / 68 | 0 / 73 → 0 / 64 (v2) |
| **Found** | | | |
| Unrequested sends: a draft was asked for, an email was sent (v2) | 4 | 6 | 3 → 2 (v2) |
| Duplicate sends, all scenarios | 1 | 1 | 2 → 0 |

**What the table means.**
- **Waste and cost** are fixed: the `wait` spin is gone, IA calls are halved, and the roster is flat in N.
- **Routing** is not fixed:
  - the model still re-uses existing agents' exact names for unrelated threads, so the hard gate "0 false merges" fails;
  - those agents still wrote to the right person and thread: 0 wrong-target writes in the baseline, and in the fixed variant 1, a deferred Priya revision charged to the next step (03).
- **Unrequested sends** were found in both variants: a draft was asked for and an email was sent, to the correct recipient, with no confirmation.
  - Both re-run duplicates followed one of them, when the user later asked to send the draft.
  - With the fixes, the ledger blocked the second send. An agent-started follow-up turn then overrode the block with `confirm_duplicate_of`, which the override rule then accepted for any user-rooted chain.
  - **That rule is fixed after the re-run** (commit "Fix: duplicate override requires a fresh user turn"). An override is now accepted only in a turn started by a user message, against a record from an earlier turn, and only after a block for that record was reported to an earlier turn, so neither an agent-started follow-up nor the run that just received the block can confirm it; the accepted cost is one extra confirmation when a user asks up front to resend. Evidence:
    - L2 tests for the re-run sequence, the same-run self-confirm and the up-front cost, each failing before its part of the fix;
    - a post-fix targeted re-run of S4: 9 runs, 532 LLM calls, $0.115: 0 duplicate sends, 0 unrequested sends, 0 blocked sends. No block occurred, so the override path was not exercised live; this run shows no regression, not the fix itself (03).
  - `SEND_GATE` blocks the unrequested sends and both duplicates.
- **S8 caveat.** The injected fault fires only when the model picks the reply action. Over both run sets, it fired in 3 of 6 baseline runs (2 duplicates) and 2 of 6 fixed runs (0 duplicates). The live evidence for the retry case is thin; the deterministic L2 tests carry it.

**Corrections to earlier claims** (in the first version of this report):
- **"Duplicate emails sent: 2 → 0" and "it removed every duplicate send"** came from the original runs.
  - **What the re-run shows:** 1 → 1. The fixed variant's duplicate came through the override path after an unrequested send. That path is now fixed (above).
  - **What still holds:** the retry case (S8) has 0 duplicates in the fixed runs.
- **"False merges 46 of 75 → 32 of 67; S1: 9/15 → 0/15"** used metric v1.
  - **What v1 got wrong:** it counted every later run of a merged agent, including runs back on its own thread.
  - **v2, original runs:** S1 has 6 merge decisions in 6 of 15 runs in the baseline, against 0 of 15 with the fixes.
  - **v2, re-run:** 6 of 15 against 0 of 14. So the S1 result holds; the 9/15 baseline figure does not.
  - **Gate scenarios, re-run (v2):** 42 of 71 against 23 of 69.
- **"0 of the 41 merge events came from code routing"** counted v1 runs. With v2, every merge decision went to an agent reached by an exact-name dispatch: 26 in the original fixed runs and 20 in the re-run. None came from identifier or alias routing.
- **The first fake Gmail ignored `OR`, parentheses and negation.** In an offline replay, 50% of the model's queries returned nothing on the old matcher and 26% on the corrected one. The re-run uses the corrected fake. The original numbers are kept, labelled, in 03.

**Recommended defaults (U2).** Turn on `OPENPOKE_FIXES=all` (every fix except the send gate) once reviewed. Evidence:
- it removed the `wait` spin and every limit hit;
- it halved IA calls and cut tokens by about half;
- it bounded the roster and EA prompts;
- it halved merge decisions, and code caused none of them;
- every flag is independently revertible.

Not solved by these fixes:
- the model re-using an existing agent's exact name for unrelated work (next steps in §6 and 04);
- unrequested sends.

**The send gate.** Turn it on wherever a human must authorize consequential actions, as a licensed broker must for many insurance tasks. Given the unrequested sends, the default is worth revisiting. It is a product decision (U1) and stays off in code.

## 2. What was implemented

| Task | Mechanism | Flag | Main files | Key decisions / deviations |
|---|---|---|---|---|
| P0.1 | (isolation) | `OPENPOKE_DATA_DIR` | `server/paths.py` + 7 stores | Resolved at import, as before; tests set it first |
| P0.2 | — | `OPENPOKE_FIXES`, `OPENPOKE_FIX_*` | `server/flags.py`, `server/overload_config.py` | `validate()` fails fast at startup and in evals |
| P0.3 | — | — | `server/services/context.py` | Follow-up turns are children of the batch's turn, not of the finishing run |
| P0.4 | — | `OPENPOKE_TELEMETRY` | `server/services/telemetry.py`, `openrouter_client/client.py` | Sizes and hashes only; usage copied; never raises |
| P0.5 | — | — | `tests/`, `server/testing/` | Parity via `git archive` (read-only) instead of a worktree; negative control |
| P0.6 | — | `OPENPOKE_FAKE_GMAIL*` | `server/testing/fake_gmail.py`, `evals/` | Subprocess per repetition; call budget; dry-run mode |
| P0.7 | — | — | `evals/results/` (raw runs archived outside the repo) | K confirmed by probe; C left provisional (no 429s) |
| 1.1 | M3 | `WAIT_ENDS_TURN` | `interaction_agent/runtime.py` | `send_message_to_user` not terminal |
| 1.2 | M2 | `EA_HISTORY_WINDOW` | `execution_agent/agent.py`, `runtime.py` | Entry-based window, identical to the legacy line-based cut when the cap doesn't bind |
| 1.3 | M2 | `KEEP_TOOL_IDS` | `services/agents/ids.py`, `execution_agent/agent.py` | Adds compact email `refs` (deviation) |
| 1.4 | M4 | `LLM_RETRY` | `openrouter_client/client.py` | Deadline-aware; never 4xx; never Gmail |
| 1.5 | M5 | `GMAIL_READS_OFF_LOOP` | `services/gmail/executor.py`, call sites, `routes/gmail.py` | One worker (U5) |
| 1.6 | M7 | `AGENT_LOCK` | `execution_agent/admission.py`, `batch_manager.py`, `interaction_agent/tools.py` | Request journaled inside the lock |
| 1.7 | dup. dispatch | `DISPATCH_DEDUPE` | `admission.py`, `tools.py` | Exact repeats only; keyed on the canonical agent |
| 1.8 | side effects | `SEND_LEDGER` | `services/safety/{db,ledger,trigger_claims,send_guard}.py`, `batch_manager.py`, `trigger_scheduler.py` | Atomic near check (closes a race); `[UNKNOWN]` also for successful runs with an in-doubt send; cancelled trigger runs stay claimed |
| 1.9 | M5 | `GMAIL_SENDS_OFF_LOOP` | `send_guard.py`, `lifecycle.py` | Callback before shield; late journaling and notice |
| 1.10 | authorization | `SEND_GATE` | `services/safety/approvals.py`, `interaction_agent/tools.py` | Code must be in the raw HTTP user text; IA loop supports awaitable tools |
| 2.1 | M1/M2 | `AGENT_REGISTRY` | `services/agents/registry.py`, `services/execution/roster.py` | Schema additions documented; facade syncs names written while the flag was off |
| 2.2 | M1+M2 | `AGENT_CARD` | `services/agents/card.py`, `batch_manager.py` | Built by code; escaped; failures never fail a run |
| 2.3 | M1 | `RESOLVE_ON_WRITE` | `services/agents/resolver.py`, `tools.py` | No thresholds; vetoes; loop guard horizon derived from IA limits |
| 2.4 | M1 | `TOPK_ROSTER` | `services/agents/ranking.py`, `interaction_agent/agent.py` | Textbook BM25 parameters |
| 2.5 | M1 | `DORMANCY` | `registry.py` | Derived lazily; revive on route |
| 2.6 | M6 | `TURN_BATCHES` | `batch_manager.py` | Same-turn work still batches together (by design) |
| 2.7 | M4 | `POLL_BATCH` | `trigger_scheduler.py`, `batch_manager.py` | Register before start, so one flush per poll; max-wait option off |
| 2.8 | M4 | `WATCHER_SINGLE_FLIGHT` | `services/gmail/importance_watcher.py`, `importance_classifier.py` | Classifier errors distinguished from "not important" |
| 2.9 | M4 | `CONCURRENCY_CAP` | `admission.py`, `batch_manager.py` | Lock, then gate; deadline after both |

## 3. Tests

```bash
python -m pytest -q tests                                              # flags off
OPENPOKE_FIXES=all python -m pytest -q tests                           # every fix except the gate
OPENPOKE_FIXES=all OPENPOKE_FIX_SEND_GATE=1 python -m pytest -q tests  # every fix + the gate
python tests/run_flag_matrix.py                                        # each flag alone + prerequisites, all, all + gate (table in 03)
```

**Final counts.** Repository at the final tree (after the override fix), parity included. MEASURED:

| Configuration | Python 3.13 | Python 3.10 |
|---|---|---|
| Flags off | 244 passed, 1 xfailed | 244 passed, 1 xfailed |
| `OPENPOKE_FIXES=all` | 244 passed, 1 xfailed | 244 passed, 1 xfailed |
| `all` + `SEND_GATE` | 244 passed, 1 xfailed | 244 passed, 1 xfailed |
| The 5 matrix configurations that enable `SEND_LEDGER`, re-run after the fix (repository, parity included) | 244 passed, 1 xfailed in each | not run |
| Flag matrix: 22 configurations, before the override fix (`c98557b`); export without git, so parity is skipped | 238 passed, 2 skipped, 1 xfailed in each | the same in 21 of 22; one run printed no summary, and 3 re-runs passed (03) |
| Watcher tests, 10 consecutive runs | 10/10 | 10/10 |

- **Counts by layer.** 143 L1 unit tests, 100 L2 mechanism tests (including one strict xfail), and 2 parity tests.
- **The strict xfail** is the documented known limit: the 1.2 window alone forgets a draft older than K requests. The card-on variant of the same test passes.
- **Parity** passes on both versions, and its negative control detects a one-word behaviour change. The override fix sits behind `SEND_LEDGER`, so flags-off behaviour is unchanged.
- **Python support.**
  - The suite is verified on **3.10 and 3.13**; 3.11 and 3.12 were not run.
  - `pyproject.toml` keeps upstream's `requires-python >= 3.13`, which is the uv interpreter pin. The pip path (`server/requirements.txt` + `requirements-dev.txt`) is the 3.10 route.
  - **Fix made in this pass:** the harness was not portable before 3.12, because `asyncio.wait_for` wraps the test body in a task the harness counted as "other work". `tests/helpers.py` now excludes its own tasks. See `docs/overload/README.md`.

## 4. Evaluation

See 03 for the full tables. All numbers are MEASURED from telemetry, provider usage and fake-Gmail logs.

| Detail | Value |
|---|---|
| Model | `openai/gpt-6-luna` |
| Repetitions | 3 per scenario |
| Scenarios | S1, S2, S3, S4@{10,200,500}, S5, S8, S9 |
| Cross-model check | S9 on `anthropic/claude-haiku-5.5` (original runs only) |

| Run set | Variant | Runs | LLM calls | Tokens | Cost (USD) | 429s |
|---|---|---|---|---|---|---|
| original | baseline | 27 | 1,737 | 7,997,544 | 0.333 | 0 |
| original | phase1 | 27 | 1,144 | 4,803,438 | 0.267 | 0 |
| original | fixed | 27 | 1,139 | 4,298,950 | 0.242 | 0 |
| original | cross-model S9 | 3 | 22 | 124,736 | 0.017 | 0 |
| rerun | baseline | 27 | 1,798 | 8,455,794 | 0.355 | 0 |
| rerun | fixed | 27 | 1,120 | 4,346,686 | 0.250 | 0 |

**Gates** were declared before the first run. None was loosened, and the thresholds are unchanged for the re-run. "False merges" is v1 runs in the original column and v2 false-merge runs in the re-run column.

| Gate | Original runs (v1, as first published) | Re-run (v2) |
|---|---|---|
| P1: limit hits = 0 | PASS | PASS |
| P1: duplicate sends = 0 (hard) | PASS (baseline 2 → 0) | **FAIL** (1 → 1; the override path after an unrequested send, fixed afterwards) |
| P1: IA calls per turn lower | PASS, every scenario | PASS, every scenario |
| P1: EA prompt flat after K | PASS in L2 (`test_m2_journal_growth_flat_after_k`); L3 horizons are shorter than K | same |
| P1: context_hit ≥ baseline | PASS with Phase 1 flags; **FAIL** with all fixes (S4@10 1.00 → 0.67) | **FAIL** (S1 1.00 → 0.83, S4@200 0.83 → 0.67) |
| P2: false merges = 0 of N (hard) | **FAIL**: 32 of 67 (baseline 46 of 75) | **FAIL**: 23 of 69 (baseline 42 of 71) |
| P2: fragmentation below baseline | **FAIL as written** (the baseline over-merges, so "below" rewards merging) | **FAIL as written** |
| P2: fragmentation ≤ 1.2 | PASS | PASS |
| P2: context_hit ≥ 0.9 on S1 and S5 | PASS (1.00, 1.00) | S1 **FAIL** (0.83, 5 of 6); S5 PASS (1.00) |
| P2: roster tokens flat across N | PASS (1,213 / 1,200 / 1,207 chars) | PASS (1,213 / 1,170 / 1,196 chars) |
| P2: routing accuracy at N = 200 ≥ N = 10 | PASS (0.00 → 0.20) | PASS (0.00 → 0.25) |
| P2: IA calls per turn below baseline | PASS | PASS |
| P2: duplicate sends = 0 | PASS | **FAIL** (1) |

The context_hit misses are follow-up steps where the IA started no execution run (all 5 baseline and 6 of 7 fixed). The seventh is a send step routed to a near-miss agent (03).

**Routing evidence.**
- **Original runs.** Every v2 merge decision went to an agent reached by an exact-name dispatch: 39 baseline, 41 phase1, 26 fixed.
- **Re-run.** The same holds: 40 baseline, 20 fixed.
- **Code routing.** No merge came from identifier or alias routing.

## 5. What remains unverified

- **Composio.** Thread safety and real per-tool response payloads; U13 was not authorized, so it was not run. The fake mirrors Gmail API resources inside Composio's verified `{data, error, successful, …}` envelope.
- **Live Gmail behaviour.** Real latencies and real draft, reply and forward semantics; only the fake was exercised.
- **Provider rate limits** at higher concurrency: no 429 was seen, so C = 4 is unvalidated.
- **Cross-model behaviour** beyond one S9 check on a second model.
- **Whether the email sent at a send step is the draft the user reviewed.** No metric checks this. One traced fixed re-run (S4@500 rep 0) sent a newly written email instead (03).
- **The web UI.** It was not changed and not re-tested in a browser. Its 30-attempt polling cap remains, a pre-existing silent failure.

## 6. Known limitations and risks

- **Residual false-merge paths.** None of these are prevented by code, because the design is "code proposes, the model decides":
  - the IA reuses an existing agent's *exact* name for unrelated work (the baseline's dominant error);
  - identical generic names reused for different threads;
  - strong ids legitimately shared by two tasks;
  - `create_new` misuse.

  A finite evaluation with zero false merges is not proof that they cannot happen. The 03 tables give the counts and the 3/N bound.
- **Unrequested sends are not prevented without the gate.**
  - **Why they happen.** `gmail_reply_to_thread` (and forward) send immediately, and "draft first" exists only in prompt text.
  - **What was measured.** In both variants an agent sometimes replied when the user had asked for a draft: re-run 4 baseline and 6 fixed, all to the correct recipient. A later "send the draft" then produced a duplicate.
  - **Duplicate overrides** from agent-started turns, and from the run that received the block, are fixed. The cost: a user who asks up front to resend is asked once more.
- **Near-duplicate sends** are caught only within 24 h, and only with the same thread and recipients (or the same message for forwards). A reworded email to a different thread is a new email.
- **Trigger semantics after a crash** are at-most-once plus a report. A crash after the run finished but before its claim was marked done is reported as "interrupted", together with the ledger summary of what it did.
- **The dispatch dedupe window is in memory.** After a restart only the ledger protects sends.
- **The card can be stale** for work done outside an EA run, for example a draft deleted directly in Gmail. It says so ("may be incomplete").
- **`KEEP_TOOL_IDS` makes short-horizon prompts larger:** `<tool_ids>` entries add chars until the window binds. It is a memory-for-tokens trade.
- **Approval UX.** A 6-character code typed by the user; alternatives in U1.
- **Classification in the watcher is serial.** That is fine at 60 s cadence with small bursts.
- **Process-global state** (locks, dedupe, gate) assumes one process. A multi-process deployment needs 4F (see 04).

## 7. Unresolved decisions

| # | Question | Default used | What would change it |
|---|---|---|---|
| U1 | Send authorization UX | Gate implemented, off; code typed by the user; trigger sends also gated | Product and compliance decision |
| U2 | Default flags | All off in code; `all` recommended | Review sign-off |
| U3 | EA history K | 5 (probe: max reach 4) | Longer real follow-up chains |
| U4 | C / R | 4 / 1 (no 429 evidence) | Observed 429s or `x-ratelimit-*` headers, now recorded in `llm_call` |
| U5 | Gmail workers | 1 | A U13 probe proving thread safety |
| U6 | Dedupe window | 120 s | Telemetry on suppressed vs legitimate repeats |
| U7 | Near-duplicate window | 24 h | User feedback on confirmation prompts |
| U8 | Dormancy | 14 days | Roster-recall telemetry |
| U9 | Watcher buffer / attempts / grace | 50 / 3 / 10 s | Burst sizes in production |
| U10 | Triggers after a crash | at-most-once + report | Product decision on catch-up |
| U11 | `create_new` overriding a strong-id match | honoured, `route_override` logged | L3 evidence of misuse |
| U12 | DELETE history clears the ledger? | No | Product decision |
| U13 | Live read-only Gmail probe | Not run | Explicit authorization |

## 8. How to reproduce

```bash
git checkout submission
uv sync                                       # Python ≥ 3.13 per pyproject.toml; or pip (tested on 3.10)
uv run python -m pytest -q tests
OPENPOKE_FIXES=all uv run python -m pytest -q tests
uv run python tests/run_flag_matrix.py
uv run python -m evals.run_live --variant baseline --label rerun-baseline --out evals/results/raw/rerun --scenarios all --include-optional-sizes --repetitions 3 --max-llm-calls 2500
uv run python -m evals.run_live --variant fixed    --label rerun-fixed    --out evals/results/raw/rerun --scenarios all --include-optional-sizes --repetitions 3 --max-llm-calls 2500
uv run python -m evals.run_live --variant fixed --label postfix-fixed --out evals/results/raw/postfix --scenarios S4 --include-optional-sizes --repetitions 3 --max-llm-calls 1000
uv run python -m evals.report_tables --original ../openpoke-eval-raw/20261119T204035Z/original \
  --rerun ../openpoke-eval-raw/20261119T221148Z/rerun --postfix ../openpoke-eval-raw/20261119T223528Z/postfix --out evals/results
uv run python -m evals.replay_queries <raw dirs>...   # old vs new fake-Gmail matcher on recorded queries
```

**How the live runs here were executed.**
- **From frozen exports.** Each ran from a `git archive` export of the commit named in 03, so ongoing edits could not change code mid-run. A `.env` copy was placed in the export for `OPENROUTER_API_KEY` and deleted afterwards. Composio is never used.
- **Where the raw output goes.** Raw output is written to the git-ignored `evals/results/raw/`. All three raw run sets (original, re-run, post-fix) are archived gzipped outside the repository, with SHA-256 checksums (03).

## 9. Future work

See [04_FUTURE_ARCHITECTURE.md](04_FUTURE_ARCHITECTURE.md):
- entity-keyed agents, and what they would not solve;
- a durable prioritized work queue that would subsume the gate, poll batching, turn batches and agent locks;
- the deferred items with the evidence that would justify them.
