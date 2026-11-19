# Evaluations

## Layers

| Layer | What runs | What it proves | Command |
|---|---|---|---|
| L1 unit | Pure components | The components are correct | `python -m pytest -q tests/unit` |
| L2 mechanism | Real orchestration code + `ScriptedLLM` + `FakeGmail` | Each mechanism is fixed and nothing regressed | `python -m pytest -q tests` |
| L3 behaviour | Real interaction and execution agents + **real model** (OpenRouter) + `FakeGmail` + broker fixture | What the *model* does with the fixes | `python -m evals.run_live ...` |

L2 tests encode model behaviour by hand (for example a model that spins on `wait`, or one that rewords an agent name). They test mechanisms, not models. Only L3 measures model behaviour.

## Running L3 safely

```bash
uv run python -m evals.run_live --variant baseline --scenarios all --repetitions 3 --max-llm-calls 1500
uv run python -m evals.run_live --variant fixed    --scenarios all --repetitions 3 --max-llm-calls 1500
uv run python -m evals.compare evals/results/<baseline>.json evals/results/<fixed>.json
uv run python -m evals.run_live --variant baseline --scenarios S9 --repetitions 1 --dry-run   # no API calls
```

- **Real Gmail is unreachable.** Each repetition runs in its own subprocess with `OPENPOKE_FAKE_GMAIL=1` and a fresh temporary `OPENPOKE_DATA_DIR`. The worker refuses to start otherwise (`harness.require_fake_gmail`). At the end it asserts that no Composio client was ever constructed.
- **Budget.** `--max-llm-calls` caps every HTTP attempt to the model across the whole invocation. When the budget is exhausted the run stops cleanly and is marked `partial`.
- **Model.** `--model` overrides `OPENROUTER_MODEL` (default: the value in `.env`). The key is read from `.env` by the app's normal config loader and is never written to results.
- **Variants.** `baseline` (every flag off), `fixed` (`OPENPOKE_FIXES=all`, which excludes the send gate), or a comma-separated flag list.

## Fixture and scenarios

- `fixtures/broker_inbox.json` is a fictional insurance brokerage (39 messages in 28 threads, `example.*` domains). It includes:
  - two clients called Sarah Chen (a home-insurance client and opposing counsel on a claim);
  - near-identical policy numbers (AU-77120 and AU-77210);
  - multi-message threads;
  - a certificate-of-insurance request;
  - carrier mail and noise.
- `scenarios/S1…S9.json` hold the step scripts and gold labels. S6 and S7 are L2-only (see `tests/`).
- S4 expands to one run per roster size (S4@10, S4@200; S4@500 with `--include-optional-sizes`).
- S5's 30 intervening dispatches are seeded synthetically (journal and roster entries) to bound spend.

## Outputs

Each run writes `results/<UTC timestamp>_<variant>.json`. It contains:
- raw telemetry events (sizes and hashes only);
- the fake-Gmail action log;
- per-step metadata and context checks (which required ids appeared in the first execution-agent prompt of each run);
- usage totals, flags, model, git commit and Python version.

`metrics.py` computes every metric from these files alone. `compare.py` renders the baseline-vs-fixed table.
