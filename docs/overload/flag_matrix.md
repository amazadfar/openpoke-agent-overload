| Configuration | Flags | Result | Wall time |
|---|---|---|---|
| all flags off | `none` | 208 passed, 2 skipped, 1 xfailed in 83.34s (0:01:23) | 84 s |
| WAIT_ENDS_TURN (+ prerequisites) | `WAIT_ENDS_TURN` | 208 passed, 2 skipped, 1 xfailed in 83.15s (0:01:23) | 83 s |
| EA_HISTORY_WINDOW (+ prerequisites) | `EA_HISTORY_WINDOW` | 208 passed, 2 skipped, 1 xfailed in 83.12s (0:01:23) | 83 s |
| KEEP_TOOL_IDS (+ prerequisites) | `KEEP_TOOL_IDS` | 208 passed, 2 skipped, 1 xfailed in 83.10s (0:01:23) | 83 s |
| LLM_RETRY (+ prerequisites) | `LLM_RETRY` | 208 passed, 2 skipped, 1 xfailed in 83.14s (0:01:23) | 83 s |
| GMAIL_READS_OFF_LOOP (+ prerequisites) | `GMAIL_READS_OFF_LOOP` | 208 passed, 2 skipped, 1 xfailed in 83.00s (0:01:23) | 83 s |
| AGENT_LOCK (+ prerequisites) | `AGENT_LOCK` | 208 passed, 2 skipped, 1 xfailed in 83.10s (0:01:23) | 83 s |
| DISPATCH_DEDUPE (+ prerequisites) | `AGENT_LOCK,DISPATCH_DEDUPE` | 208 passed, 2 skipped, 1 xfailed in 83.16s (0:01:23) | 83 s |
| SEND_LEDGER (+ prerequisites) | `SEND_LEDGER` | 208 passed, 2 skipped, 1 xfailed in 82.96s (0:01:22) | 83 s |
| GMAIL_SENDS_OFF_LOOP (+ prerequisites) | `GMAIL_READS_OFF_LOOP,SEND_LEDGER,GMAIL_SENDS_OFF_LOOP` | 208 passed, 2 skipped, 1 xfailed in 83.15s (0:01:23) | 83 s |
| SEND_GATE (+ prerequisites) | `SEND_LEDGER,SEND_GATE` | 208 passed, 2 skipped, 1 xfailed in 83.12s (0:01:23) | 83 s |
| AGENT_REGISTRY (+ prerequisites) | `KEEP_TOOL_IDS,AGENT_REGISTRY` | 208 passed, 2 skipped, 1 xfailed in 83.34s (0:01:23) | 84 s |
| AGENT_CARD (+ prerequisites) | `KEEP_TOOL_IDS,AGENT_REGISTRY,EA_HISTORY_WINDOW,AGENT_LOCK,AGENT_CARD` | 208 passed, 2 skipped, 1 xfailed in 83.13s (0:01:23) | 83 s |
| RESOLVE_ON_WRITE (+ prerequisites) | `KEEP_TOOL_IDS,AGENT_REGISTRY,EA_HISTORY_WINDOW,AGENT_LOCK,AGENT_CARD,RESOLVE_ON_WRITE` | 208 passed, 2 skipped, 1 xfailed in 83.32s (0:01:23) | 84 s |
| TOPK_ROSTER (+ prerequisites) | `KEEP_TOOL_IDS,AGENT_REGISTRY,EA_HISTORY_WINDOW,AGENT_LOCK,AGENT_CARD,RESOLVE_ON_WRITE,TOPK_ROSTER` | 208 passed, 2 skipped, 1 xfailed in 83.25s (0:01:23) | 84 s |
| DORMANCY (+ prerequisites) | `KEEP_TOOL_IDS,AGENT_REGISTRY,EA_HISTORY_WINDOW,AGENT_LOCK,AGENT_CARD,RESOLVE_ON_WRITE,TOPK_ROSTER,DORMANCY` | 208 passed, 2 skipped, 1 xfailed in 83.30s (0:01:23) | 84 s |
| TURN_BATCHES (+ prerequisites) | `TURN_BATCHES` | 208 passed, 2 skipped, 1 xfailed in 82.96s (0:01:22) | 83 s |
| POLL_BATCH (+ prerequisites) | `TURN_BATCHES,POLL_BATCH` | 208 passed, 2 skipped, 1 xfailed in 82.93s (0:01:22) | 83 s |
| WATCHER_SINGLE_FLIGHT (+ prerequisites) | `GMAIL_READS_OFF_LOOP,WATCHER_SINGLE_FLIGHT` | 208 passed, 2 skipped, 1 xfailed in 82.96s (0:01:22) | 83 s |
| CONCURRENCY_CAP (+ prerequisites) | `AGENT_LOCK,CONCURRENCY_CAP` | 208 passed, 2 skipped, 1 xfailed in 82.95s (0:01:22) | 83 s |
| OPENPOKE_FIXES=all | `all` | 208 passed, 2 skipped, 1 xfailed in 83.37s (0:01:23) | 84 s |
| all + SEND_GATE | `all` + SEND_GATE | 208 passed, 2 skipped, 1 xfailed in 83.35s (0:01:23) | 84 s |
