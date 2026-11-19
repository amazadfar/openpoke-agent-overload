# 04: Future architecture (direction only, not implemented)

The fixes in this branch are incremental and flag-gated, so each one can be measured against the baseline. Two larger changes would subsume several of them. Both are written up here deliberately rather than built: neither is needed to remove the overload mechanisms measured in 01. Each would also be a large, hard-to-review change for a take-home.

## 1G: Entity-keyed agents

**Idea.** Key agents by canonical domain entities (a Gmail thread, a contact, a policy, a claim) instead of names the LLM invents. "The agent for thread 18c1…" or "the agent for policy AU-77120" would be found by key. The model would not need to remember a name, and the resolver would not need to match one.

**What it would replace.** Most of resolve-on-write (2.3) and the strong-identifier routing step. The registry's `identifiers` table already stores these keys per agent. 1G would make one of them the primary key instead of a routing hint.

**What it does NOT solve.** Entity keys prevent duplicates only for the *same canonical key*. Everything below remains:

- **Entity resolution.**
  - One person with two addresses (`sarah.chen@northwind.example` at work and a personal one).
  - Two people with the same name (the two Sarah Chens in the fixture).
  - A contact whose address changes.
  - Policy numbers that differ by one transposition (AU-77120 vs AU-77210): typo or different policy?
- **Ownership granularity.** Is the owner the contact, the thread, or the policy?
  - A renewal touches the client, the carrier and the policy.
  - A claim touches the insured, opposing counsel, the adjuster and the carrier.
  - Whichever entity is chosen, some follow-ups belong to another one.
- **Multi-party threads.** One thread with the client, the carrier and an internal colleague. Keying by thread merges three relationships; keying by contact splits one conversation.
- **Cross-entity tasks.** "Send renewal reminders to everyone renewing in December" touches N entities. It needs either a coordinator agent (a new kind of identity) or fan-out.
- **Keyless tasks.** "Summarize my inbox", "what's on my plate today". No entity exists until the work runs.

**Evidence that would justify it, which L3 now provides.** With every fix on, 41 merge events remained, and every one was the model typing an existing agent's *exact* name. Examples:
- the renewal task sent to the seeded near-miss "Priya Patel - add son as driver";
- a near miss picked from the resolver's own `ask` candidates.

The conservative resolver cannot prevent this without second-guessing an exact name, which would need semantic judgment. Entity keys would: the driver thread and the renewal thread are different keys. A cheaper first step is a product decision on the IA's instruction to reuse "a relevant existing agent … even if the name is unrelated". It should be evaluated on held-out scenarios, not the ones reported here.

## 4F: Durable prioritized work queue

**Idea.** Replace in-memory `create_task` dispatch with a SQLite jobs table and a worker pool:

```sql
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_slug TEXT NOT NULL,
  origin TEXT NOT NULL,              -- user | trigger | email | system
  priority INTEGER NOT NULL,         -- user > trigger > watcher
  instructions TEXT NOT NULL,
  turn_id TEXT, parent_turn_id TEXT, -- result batching key
  trigger_id INTEGER, scheduled_for TEXT,
  state TEXT NOT NULL CHECK (state IN ('queued','running','done','failed','abandoned')),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,                  -- worker heartbeat; expired lease => abandoned on restart
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  result TEXT
);
CREATE INDEX ix_jobs_ready ON jobs(state, priority DESC, id);
CREATE UNIQUE INDEX ux_jobs_one_running_per_agent ON jobs(agent_slug) WHERE state = 'running';
```

**Worker model.** C workers pull the highest-priority ready job whose agent has no running job (per-agent serialization comes from the unique index). Each worker runs the EA with a lease and writes the result. A batcher groups results by `turn_id` and starts the follow-up IA turn.

**What it would replace.**

| Today (this branch) | With 4F |
|---|---|
| 4A concurrency gate | Number of workers plus a priority order |
| 4C one turn per poll | Jobs inserted with the poll's `turn_id` |
| 6A turn-scoped batches | Batching keyed by `turn_id` in the table |
| 7A per-agent locks | Partial unique index on running jobs per agent |
| Trigger claims | `(trigger_id, scheduled_for)` unique on jobs |
| In-memory dedupe (D1) | A unique index on `(agent_slug, instructions_hash)` for queued/running jobs |

It would also survive restarts. Queued work resumes; running work with an expired lease is reported, not silently re-run, using the same honesty rule as trigger claims.

**What it does not change.** The ledger (D2) is still needed: a job can be retried, but a send must not be. The send gate is unchanged.

**Evidence that would justify it.**
- Queue-time telemetry (`run_start.queued_ms`) showing user work waiting behind storms even with the gate.
- Restart frequency in production. Each restart today loses in-flight work, which is reported but not resumed.
- A need for multi-process deployment, where process-global locks no longer suffice.

## Deferred items

| Item | Why deferred | What would justify it |
|---|---|---|
| **8A tool capability sets** (read-only agents get no send tools) | About 2.3k tokens (est.) of schemas per EA call is a fixed cost, not a growth term. The send gate (1.10) already removes the safety part for consequential actions. | Per-call token telemetry showing schema tokens dominate after the history fix, or a policy requirement that some agents cannot send at all |
| **Embeddings behind the resolver** (1E) | Speculative. The conservative resolver plus Top-K BM25 handles rewordings through identifiers and shared names or emails. | L3 showing rewordings with no shared identifier or token that the model then fragments. A drop-in place exists: a fourth candidate source in `resolver.resolve()`, still producing candidates only, never routes. |
| **Turn budgets and chain-depth limits** (4E) | Runaway chains were never observed. With `WAIT_ENDS_TURN` a turn ends as soon as the model is done. | Telemetry already records `depth` on every event: a distribution of chain depth with a long tail |
| **Per-agent rolling LLM summaries** (2D) | The card plus the window covers what follow-ups needed in every scenario (context hit, 03) at zero LLM cost | Follow-ups that need prose context older than K and not on the card |
| **Parallel importance classification** | The single-flight watcher already stops the poll from awaiting IA turns. Classification stays serial, which is acceptable at 60 s cadence. | Bursts large enough that serial classification exceeds the poll interval |
| **Live Composio thread-safety probe** (U13) | It would touch the real Gmail account. It was not authorized, so it was not run. | Explicit authorization (`OPENPOKE_ALLOW_LIVE_GMAIL_READS=1` plus confirmation), then raising `GMAIL_WORKERS` above 1 |

## Next steps, in order, with the evidence for each

The counts come from `evals/results/audit.md` (metric v2). Everything below is a proposal; none of it is implemented in this branch.

1. **Exact-name routes should yield to conflicting identity evidence.**
   - **Evidence, S3.** In all 3 S3 repetitions with every fix on, in both the original runs and the re-run, the IA re-used the exact name "Sarah Chen water stain email" (the client's agent) for the Harbor Law counsel's task. The counsel's work went to the right address, but both Sarahs' work now lives in one agent's memory.
   - **Evidence, S4.** The seeded near-miss agent "Priya Patel - add son as driver" received Priya's renewal work in 9 of 9 baseline and 5 of 9 fixed S4 repetitions of the re-run (original runs: 9 and 8). In one fixed re-run (S4@500 rep 0), the send step went to that agent: it wrote a new email to Priya and sent it, and the draft the user had reviewed was never sent.
   - **The gap.** The resolver's step 1 routes exact names unconditionally. Its counterparty veto (disjoint counterparty emails) runs only for identifier matches.
   - **Proposal.** Applying the veto to step 1 too would return `ask` when the instruction names a counterparty the agent has never dealt with. The trade-off is one extra IA round when the model is right.
2. **Duplicate overrides: done after the re-run.**
   - **What was fixed.** The override path behind the fixed re-run's duplicate (S4@500 rep 2) was fixed by commit "Fix: duplicate override requires a fresh user turn", and so was the same-run self-confirm path.
   - **The rule now.** An override needs a user-started turn later than the turn that was told about the block.
   - **The accepted trade-off.** A user who asks up front to resend is asked once more.
3. **Domain identifiers as strong identifiers.**
   - **Why.** Policy and claim numbers (`AU-77120`, `HO-48213`, `CL-2291`) are as identifying as thread ids in this domain, but the resolver only knows Gmail ids today.
   - **Proposal.** Add a small pattern set, configurable per deployment, to `ids.py`. Then "the CL-2291 task" would route by identity rather than by name.
4. **Entity-keyed agents** (1G above), including what keys do not solve: entity resolution, ownership granularity, and cross-entity and keyless tasks.
5. **The IA's "reuse even if unrelated" prompt policy.**
   - **The policy.** The upstream system prompt tells the model to prefer an existing agent "even if the name is unrelated".
   - **Evidence.** Every remaining merge decision is exact-name reuse: 26 in the original fixed runs, 20 in the re-run.
   - **How to evaluate a change.** On **held-out** scenarios: alternate client names and fixture permutations with a fixed seed. Never on S1–S9, which are the reported set.
6. **A send policy, and a check that the sent email is the reviewed draft.**
   - **Evidence.** Both variants sent emails when the user had asked only for a draft:
     - original runs: 3 baseline, 2 fixed;
     - re-run: 4 baseline, 6 fixed.

     All went to the correct recipient. Both re-run duplicates followed one of them.
   - **Options.** `SEND_GATE` blocks the class. Lighter policies are possible: forcing reply and forward through drafts, or gating only scheduled sends. This is a product decision (U1).
   - **A missing metric.** "Sent body = the last draft the user reviewed".
