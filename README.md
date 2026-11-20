# OpenPoke 🌴

> **Agent-overload work.** This fork makes OpenPoke resilient to agent overload, using 19 flag-gated fixes measured against the unchanged baseline. Start with [`REVIEWERS_GUIDE.md`](REVIEWERS_GUIDE.md). It covers what was fixed, what was not, what was found, how to run it, and where to look. Full documentation: [`docs/overload/`](docs/overload/README.md). The upstream README follows, with my setup changes and an added agent-overload section.

OpenPoke is a simplified, open-source take on [Interaction Company’s](https://interaction.co/about) [Poke](https://poke.com/) assistant—built to show how a multi-agent orchestration stack can feel genuinely useful. It keeps the handful of things Poke is great at (email triage, reminders, and persistent agents) while staying easy to spin up locally.

- Multi-agent FastAPI backend that mirrors Poke's interaction/execution split, powered by [OpenRouter](https://openrouter.ai/).
- Gmail tooling via [Composio](https://composio.dev/) for drafting/replying/forwarding without leaving chat.
- Trigger scheduler and background watchers for reminders and "important email" alerts.
- Next.js web UI that proxies everything through the shared `.env`, so plugging in API keys is the only setup.

## Requirements
- Python 3.10+
- Node.js 18+
- npm 9+

## Quickstart
1. **Clone and enter the repo.**
   ```bash
   git clone https://github.com/shlokkhemani/OpenPoke
   cd OpenPoke
   ```
2. **Create a shared env file.** Copy the template and open it in your editor:
   ```bash
   cp .env.example .env
   ```
3. **Get your API keys and add them to `.env`:**
   
   **OpenRouter (Required)**
   - Create an account at [openrouter.ai](https://openrouter.ai/)
   - Generate an API key
   - Replace `your_openrouter_api_key_here` with your actual key in `.env`
   - Optionally set `OPENROUTER_MODEL` to a [model ID that supports tool calling](https://openrouter.ai/models?supported_parameters=tools). This setting applies to chat, execution, email search, summaries, and email classification. The default is `anthropic/claude-sonnet-4`.
   
   **Composio (Required for Gmail)**
   - Sign in at [composio.dev](https://composio.dev/)
   - Create a project API key. For a [scoped key](https://docs.composio.dev/reference/authenticating-to-composio/project-api-key-permissions), grant **Connected accounts** read and write access and **Tool execution (Legacy)** write access.
   - Under **Auth Configs**, create a Gmail OAuth2 config using Composio-managed OAuth or your own Google OAuth client. Copy the config's `ac_...` ID.
   - Replace `your_composio_api_key_here` and `your_gmail_auth_config_id_here` in `.env` with values from the same project
4. **(Required) Create and activate a Python 3.10+ virtualenv:**
   ```bash
   # Ensure you're using Python 3.10+
   python3.10 -m venv .venv
   source .venv/bin/activate
   
   # Verify Python version (should show 3.10+)
   python --version
   ```
   On Windows (PowerShell):
   ```powershell
   # Use Python 3.10+ (adjust path as needed)
   python3.10 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   
   # Verify Python version
   python --version
   ```

5. **Install backend dependencies:**
   ```bash
   pip install -r server/requirements.txt
   ```
6. **Install frontend dependencies:**
   ```bash
   npm install --prefix web
   ```
7. **Start the FastAPI server:**
   ```bash
   python -m server.server --reload
   ```
8. **Start the Next.js app (new terminal):**
   ```bash
   npm run dev --prefix web
   ```
9. **Connect Gmail for email workflows.** With both services running, open [http://localhost:3000](http://localhost:3000), head to *Settings → Gmail*, and complete the Composio OAuth flow. This step is required for email drafting, replies, and the important-email monitor.

The web app proxies API calls to the Python server using the values in `.env`, so keeping both processes running is required for end-to-end flows.

## Agent overload fixes

This branch makes OpenPoke resilient to *agent overload* and measures the difference. Overload here falls into three classes:
- **load amplifiers:** `wait` spins, one turn per trigger or important email, Gmail calls blocking the event loop, one global result batch, no retries;
- **an identity and memory weakness:** agents are bare names, and each replays its whole journal;
- **a missing safety property:** nothing stops the same email from being sent twice, and timeouts misreport sends.

Every fix sits behind a flag that is **off by default**. With every flag off, behaviour is identical to the `baseline-pre-fixes` tag (upstream plus my local setup changes: model override, Composio fixes), proven by a parity test.

```bash
OPENPOKE_FIXES=all python -m server.server          # every fix except the send gate
OPENPOKE_FIXES=all OPENPOKE_FIX_SEND_GATE=1 ...      # also require a typed approval code for every send
OPENPOKE_FIXES=WAIT_ENDS_TURN,AGENT_LOCK ...         # or pick individual fixes (prerequisites are validated)
```

| Area | Fixes (flags) |
|---|---|
| Turn loop | `WAIT_ENDS_TURN` |
| EA memory | `EA_HISTORY_WINDOW`, `KEEP_TOOL_IDS`, `AGENT_CARD` |
| Agent identity | `AGENT_REGISTRY`, `RESOLVE_ON_WRITE`, `TOPK_ROSTER`, `DORMANCY` |
| Load | `LLM_RETRY`, `GMAIL_READS_OFF_LOOP`, `GMAIL_SENDS_OFF_LOOP`, `TURN_BATCHES`, `POLL_BATCH`, `WATCHER_SINGLE_FLIGHT`, `CONCURRENCY_CAP` |
| Correctness and safety | `AGENT_LOCK`, `DISPATCH_DEDUPE`, `SEND_LEDGER` (at-most-once sends, honest `[PARTIAL]`/`[UNKNOWN]` reports, durable trigger claims), `SEND_GATE` |

**Tests and evals:**

```bash
python -m pytest -q tests                        # unit + mechanism tests (scripted LLM, fake Gmail), flags off
OPENPOKE_FIXES=all python -m pytest -q tests     # the same suite with every fix on
python -m evals.run_live --variant baseline --scenarios all --repetitions 3   # real model, FAKE Gmail
python -m evals.run_live --variant fixed    --scenarios all --repetitions 3
python -m evals.compare evals/results/<baseline>.json evals/results/<fixed>.json
```

Tests and evals never touch `server/data` or a real Gmail account. They use a temporary `OPENPOKE_DATA_DIR` and an in-process fake Gmail loaded with a fictional insurance-brokerage inbox.

Everything is documented in [`docs/overload/`](docs/overload/README.md): the problem and evidence, the design, the evaluation, future architecture, and the engineering report.

## Project Layout
- `server/` – FastAPI application and agents
- `web/` – Next.js app
- `server/data/` – runtime data (ignored by git)
- `tests/` – unit, mechanism and parity tests
- `evals/` – live evaluation runner, fixture, scenarios, metrics, results
- `docs/overload/` – design, evaluation and report for the overload fixes

## License
MIT — see [LICENSE](LICENSE).
