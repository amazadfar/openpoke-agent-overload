# Reviewer's guide

## TL;DR

- **Overload in OpenPoke** is three separate problems:
  - load amplifiers, which multiply LLM calls and freeze the event loop;
  - an identity and memory weakness: agents are bare names, and each replays its whole history;
  - unsafe side effects: nothing stops a duplicate email.
- **Fixed and measured.** Nineteen flag-gated fixes. Compared with the same real model on the same scenarios against a fake Gmail, the fixes cut LLM calls by 38%, tokens by 49% and cost by 30%. They remove every 8-round-limit hit (110 → 0), halve interaction-agent calls per turn, and keep the agent roster flat as agents accumulate. Deterministic tests prove each mechanism, and with flags off a parity test proves behaviour is identical to the `baseline-pre-fixes` tag (upstream plus my local setup changes: model override, Composio fixes).
- **Not solved.**
  - **Routing.** The model still re-uses existing agents for unrelated threads: merge decisions fell from 40 to 20, so the hard gate fails. Those agents still wrote to the right person and thread.
  - **Sends.** Both variants sent emails when only a draft was asked for, and in each variant one such send was followed by a duplicate. The fixed variant's duplicate got through a duplicate-override rule that trusted agent-started turns. **That rule is fixed** after the re-run: an L2 regression test covers it, plus a post-fix targeted re-run.
  - **What would block both.** The off-by-default `SEND_GATE`.

## The problem in three classes

1. **Load amplifiers.** The IA busy-waits with LLM calls on `wait`; one IA turn runs per trigger and per important email; synchronous Gmail calls freeze the event loop; one global result batch makes fast work wait for slow work; there are no LLM retries.
2. **Identity and memory.** An agent is a free-text name matched exactly, and the IA sees every name ever created. Each agent replays its whole journal, which truncates the ids that follow-ups need.
3. **Side-effect safety.** Sends have no identity, so nothing prevents duplicates. Timeouts report emails that did go out as `[FAILED]`. Confirm-before-send exists only in prompt text.

## Results

Commit hashes cited in docs/ and evals/results/ (e.g. 9eeec00, c98557b) refer to the development branch the work was done on; the submitted history squashes that work into five commits on top of the baseline, so those hashes don't resolve in this repository.

**Setup.** Model `openai/gpt-6-luna`, a fake Gmail and a fictional brokerage inbox. 27 runs per variant: 7 scenarios × 3 repetitions, with S4 at 10, 200 and 500 agents. These are re-run numbers, scored with metrics v2. All values are MEASURED. Sources: [`evals/results/summary.md`](evals/results/summary.md), itemised in [`audit.md`](evals/results/audit.md).

| | Baseline (flags off) | Fixed (`OPENPOKE_FIXES=all`) |
|---|---|---|
| **Fixed** | | |
| LLM calls | 1,798 | 1,120 (−38%) |
| Tokens / cost (provider usage) | 8,455,794 / $0.355 | 4,346,686 / $0.250 (−49% / −30%) |
| IA LLM calls per turn (range of scenario medians) | 5.1–6.5 | 2.4–3.0 |
| Turns hitting the 8-round limit | 110 | **0** |
| IA roster block at 10 / 200 / 500 agents (chars) | 492 / 9,577 / 19,316 | 1,213 / 1,170 / 1,196 (flat) |
| S8 retry after a lost send response: runs where the fault fired / duplicates (original + re-run) | 3 of 6 / 2 | 2 of 6 / 0 |
| **Not fixed** | | |
| Merge decisions (an agent re-used for another gold thread) | 40 | 20 (hard gate "0 false merges" **failed**) |
| False-merge runs / labelled runs | 57 / 93 | 27 / 91 |
| Wrong-target writes / scored writes | 0 / 70 | 1 / 68 (see below) |
| **Found** | | |
| Unrequested sends (a draft was asked for, an email was sent) | 4 | 6 |
| Duplicate emails, both following an unrequested send | 1 | 1 (hard gate "0 duplicates" **failed**; the override path is now fixed, see below) |

**Fixed.**
- **Waste and cost:** see the table.
- **Loop freezes** (L2 tests): a 1.5 s Gmail call stalls the event loop for ≥ 1.4 s in the baseline and < 0.1 s with the fix.
- **EA prompt growth** (L2): with the fix the prompt is flat after K = 5 requests; in the baseline it grows linearly. These live scenarios are too short to show it: the largest EA prompt was 15,831 vs 16,380 chars.
- **Retry duplicates:** a send whose response was lost is recorded as `unknown` and never retried. The live N is small, and the deterministic tests carry the weight.

**Not fixed: routing.**
- **What happens.** The IA re-uses an existing agent's **exact name** for unrelated work, which its upstream system prompt encourages ("prefer … an existing agent … even if the name is unrelated"). Every merge decision was routed that way. Code deliberately never auto-routes on fuzzy evidence.
- **Consequence.** Re-used agents still wrote to the right person and thread. The one fixed wrong-target write was a correct Priya draft for "Make it shorter.": the IA only called `wait` at that step and dispatched the revision in the next one, so the step-based metric charges it to that step.

**Found: unrequested sends.** This happened in both variants: a draft was asked for and an email was sent, to the correct recipient, with no confirmation. Both re-run duplicates followed one, when the user later said "send the draft":
- **Baseline:** nothing stopped the second email.
- **Fixed:** the ledger blocked it as a near-duplicate. Then, in a follow-up turn started by an agent (no user message in between), the execution agent retried with `confirm_duplicate_of`. The ledger accepted that override because the chain began with a user message.

**Fixed after the re-run** (commit "Fix: duplicate override requires a fresh user turn").
- **New rule.** An override is accepted only in a turn started by a user message, against a record from an earlier turn, and only after the block was reported to an earlier turn. Neither an agent-started follow-up nor the run that received the block can confirm it, so the IA has to ask the user. The cost: one extra confirmation when a user asks up front to resend.
- **Evidence.**
  - L2 tests: `test_override_from_agent_follow_up_turn_needs_a_fresh_user_turn` (the re-run case), `test_agent_cannot_confirm_its_own_block_within_the_users_turn` and `test_up_front_resend_request_needs_one_more_confirmation`. Each fails before its part of the fix and passes after.
  - Post-fix targeted re-run of S4: 9 runs, 532 LLM calls, $0.115: 0 duplicate sends, 0 unrequested sends, 0 blocked sends. No block occurred, so the override path was not exercised live; this run shows no regression, not the fix itself. It was made before the same-run tightening, which needs a block to matter ([`summary.md`](evals/results/summary.md)).

`SEND_GATE` blocks the unrequested sends and both duplicates, because every send needs a code the user types. Leaving it off by default is a product decision.

## How to run

```bash
OPENPOKE_FIXES=all python -m server.server             # the app with every fix except the send gate
python -m pytest -q tests                              # unit + mechanism + parity, flags off
OPENPOKE_FIXES=all python -m pytest -q tests           # the same with every fix on
python tests/run_flag_matrix.py                        # each flag alone (+ prerequisites), all, all + gate
python -m evals.run_live --variant fixed --scenarios S9 --repetitions 1 --dry-run   # eval pipeline, no API calls
python -m evals.run_live --variant fixed --scenarios all --repetitions 3            # real model (needs OPENROUTER_API_KEY); never real Gmail
```

**Python versions.** The suite passes on **3.10 and 3.13**, with flags off, with `all`, and across the flag matrix. `pyproject.toml` (the project's uv config) requires ≥ 3.13. The pip path (`pip install -r server/requirements.txt -r requirements-dev.txt`) is tested on 3.10.

## Where to look, in reading order

1. `server/agents/interaction_agent/runtime.py`: the `wait` fix, the smallest change (search `WAIT_ENDS_TURN`).
2. `server/services/agents/card.py` and `resolver.py`: identity and memory. The card is built by code; the resolver never routes on fuzzy evidence.
3. `server/services/safety/ledger.py` and `send_guard.py`: at-most-once sends and honest `[PARTIAL]`/`[UNKNOWN]` reports.
4. `server/agents/execution_agent/batch_manager.py` and `admission.py`: orchestration (per-agent lock, concurrency gate, turn-scoped batches).
5. `evals/metrics.py`: how success is measured (v1 and v2).
6. `tests/mechanism/test_baseline_characterization.py`, then `test_safety_fixes.py`: paired baseline and fix tests.

## Flags

Everything is flag-gated, for two reasons:
- the baseline and the fixes run on the same code, so the comparison is before/after on identical code;
- a parity test proves flags-off behaviour is identical to the `baseline-pre-fixes` tag (upstream plus my local setup changes: model override, Composio fixes).

`OPENPOKE_FIXES=all` enables every group except the last.

| Group | Flags (`OPENPOKE_FIX_…`) |
|---|---|
| Cost and waste | `WAIT_ENDS_TURN`, `EA_HISTORY_WINDOW`, `KEEP_TOOL_IDS`, `LLM_RETRY`, `GMAIL_READS_OFF_LOOP`, `TURN_BATCHES`, `POLL_BATCH`, `WATCHER_SINGLE_FLIGHT`, `CONCURRENCY_CAP` |
| Identity and memory | `AGENT_REGISTRY`, `AGENT_CARD`, `RESOLVE_ON_WRITE`, `TOPK_ROSTER`, `DORMANCY` |
| Side-effect safety | `AGENT_LOCK`, `DISPATCH_DEDUPE`, `SEND_LEDGER`, `GMAIL_SENDS_OFF_LOOP` |
| Human authorization (off) | `SEND_GATE`: every send needs a code typed by the user. Its default is a product decision. |

## What I'd do next (evidence in `evals/results/audit.md`)

1. **Exact-name routes should yield to conflicting identity evidence.**
   - **Evidence.** In S3 (all 3 fixed re-run repetitions), the client Sarah's agent ("Sarah Chen water stain email") was re-used for the Harbor Law counsel's task. In S4, the seeded near-miss agent "Priya Patel - add son as driver" received Priya's renewal work.
   - **Proposal.** The resolver's step 1 routes exact names before its counterparty veto (disjoint counterparty emails) runs. Applying the veto there would return `ask` when the instruction names a counterparty the agent has never dealt with.
2. **Domain identifiers** (policy and claim numbers such as `CL-2291`, `AU-77120`) as strong identifiers alongside Gmail ids.
3. **Entity-keyed agents.** See [`docs/overload/04_FUTURE_ARCHITECTURE.md`](docs/overload/04_FUTURE_ARCHITECTURE.md), including what keys don't solve.
4. **The IA's "reuse even if unrelated" prompt policy,** evaluated on held-out scenarios, never the reported ones.
5. **A send-policy decision,** given the unrequested sends: the full gate, drafts-only for reply and forward, or a gate on scheduled sends only.

## Limitations

- **Small N:** 3 repetitions per scenario.
- **One model** (one S9 check on a second).
- **A synthetic inbox** and a fake Gmail. The fake still ignores `is:unread`, `larger:` and similar operators, and its payload shapes follow the Gmail API rather than verified Composio output.
- **Rate-limit sizing is unverified:** no 429s were seen, so the concurrency cap C = 4 is provisional.
- **The Composio thread-safety probe was not run**: it would touch a real account. Gmail calls therefore stay on one worker.
- More detail is in [`docs/overload/`](docs/overload/README.md): the problem and evidence, the design, the evaluation, future architecture, and the engineering report.
