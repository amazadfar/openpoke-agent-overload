"""L3 live evaluation runner: the REAL interaction/execution agents and a real model, against FAKE Gmail.

    python -m evals.run_live --variant {baseline|fixed|<flag-list>} --scenarios S1,S2|all \
           --repetitions 3 --model <openrouter-id> --max-llm-calls 1500 --out evals/results/

Safety (S1, S2, S6):
- each scenario repetition runs in its own subprocess with OPENPOKE_FAKE_GMAIL=1
  (set by this runner) and a fresh temporary OPENPOKE_DATA_DIR; the worker
  refuses to start otherwise (``harness.require_fake_gmail``) and asserts at
  the end that no Composio client was ever constructed;
- every LLM call counts against ``--max-llm-calls``; on exhaustion the run
  stops cleanly and is marked partial;
- results contain telemetry (sizes/hashes), the fake-Gmail action log and
  metadata, never API keys.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import harness

VARIANT_PROFILES = {"baseline": "none", "fixed": "all"}
ALL_L3 = ["S1", "S2", "S3", "S4", "S5", "S9"]


class BudgetExhausted(RuntimeError):
    """Raised instead of making an LLM call once the run's call budget is spent."""


# ---------------------------------------------------------------------------
# Parent process: orchestrate subprocess workers
# ---------------------------------------------------------------------------

def _variant_env(variant: str) -> Dict[str, str]:
    profile = VARIANT_PROFILES.get(variant, variant)
    return {"OPENPOKE_FIXES": profile}


def _git_commit() -> Optional[str]:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=harness.EVALS_DIR.parent,
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None


def orchestrate(args: argparse.Namespace) -> Path:
    ids = ALL_L3 if args.scenarios == "all" else [s.strip() for s in args.scenarios.split(",") if s.strip()]
    runs = harness.expand_scenarios(ids, include_optional_sizes=args.include_optional_sizes)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = args.label or args.variant.replace(",", "+")
    target = out_dir / f"{stamp}_{label}.json"

    used = 0
    results: List[Dict[str, Any]] = []
    stopped_for_budget = False
    for spec in runs:
        for rep in range(args.repetitions):
            remaining = args.max_llm_calls - used
            if remaining <= 0:
                stopped_for_budget = True
                break
            print(f"[run_live] {label}: {spec['run_id']} rep {rep + 1}/{args.repetitions} (budget left {remaining})", flush=True)
            result = _run_worker(spec, rep, args, remaining)
            used += result.get("llm_calls", 0)
            results.append(result)
            _write(target, args, results, used, stopped_for_budget, stamp)
        if stopped_for_budget:
            break
    _write(target, args, results, used, stopped_for_budget, stamp)
    print(f"[run_live] wrote {target} ({used} LLM calls)", flush=True)
    return target


def _write(target: Path, args, results, used, stopped_for_budget, stamp) -> None:
    payload = {
        "variant": args.variant,
        "label": args.label or args.variant,
        "flags_profile": _variant_env(args.variant)["OPENPOKE_FIXES"],
        "model": args.model or next((r.get("model") for r in results if r.get("model")), None) or "(from .env / default)",
        "repetitions": args.repetitions,
        "max_llm_calls": args.max_llm_calls,
        "llm_calls_used": used,
        "budget_exhausted": stopped_for_budget or any(r.get("partial_reason") == "budget" for r in results),
        "started": stamp,
        "git_commit": args.commit or _git_commit(),
        "python": platform.python_version(),
        "runs": results,
    }
    target.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")


def _run_worker(spec: Dict[str, Any], rep: int, args: argparse.Namespace, budget: int) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="openpoke-eval-") as tmp:
        spec_path = Path(tmp) / "spec.json"
        out_path = Path(tmp) / "result.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENPOKE_")}
        env.update(_variant_env(args.variant))
        env.update(
            OPENPOKE_FAKE_GMAIL="1",
            OPENPOKE_FAKE_GMAIL_FIXTURE=str(args.fixture),
            OPENPOKE_DATA_DIR=str(Path(tmp) / "data"),
            OPENPOKE_EVAL_CAPTURE_PROMPTS="1",
            OPENPOKE_TELEMETRY="1",
            PYTHONPATH=str(harness.EVALS_DIR.parent),
        )
        if args.model:
            env["OPENROUTER_MODEL"] = args.model
        cmd = [sys.executable, "-m", "evals.run_live", "--worker", str(spec_path), "--worker-out", str(out_path),
               "--rep", str(rep), "--budget", str(budget), "--step-timeout", str(args.step_timeout)]
        if args.dry_run:
            cmd.append("--dry-run")
        started = time.monotonic()
        proc = subprocess.run(cmd, cwd=harness.EVALS_DIR.parent, env=env, capture_output=True, text=True,
                              timeout=args.step_timeout * (len(spec.get("steps", [])) + 2) + 120)
        if proc.returncode != 0 or not out_path.exists():
            return {"scenario": spec["run_id"], "rep": rep, "error": "worker failed", "returncode": proc.returncode,
                    "stderr_tail": proc.stderr[-3000:], "llm_calls": 0, "partial": True, "partial_reason": "worker_error",
                    "wall_s": time.monotonic() - started}
        result = json.loads(out_path.read_text(encoding="utf-8"))
        result["wall_s"] = time.monotonic() - started
        return result


# ---------------------------------------------------------------------------
# Worker process: one scenario repetition in a fresh data dir
# ---------------------------------------------------------------------------

def _install_budget(budget: int) -> None:
    import importlib

    from server.services import telemetry
    from server.testing.scripted_llm import CALLER_MODULES

    for module_name in CALLER_MODULES:
        module = importlib.import_module(module_name)
        real = module.request_chat_completion

        async def budgeted(*a, __real=real, **kw):
            if len(telemetry.recent("llm_call")) >= budget:
                raise BudgetExhausted(f"LLM call budget of {budget} exhausted")
            return await __real(*a, **kw)

        module.request_chat_completion = budgeted


def _install_dry_run_model() -> None:
    """--dry-run: a scripted stand-in model so the pipeline can be validated without spending."""
    from server.testing.scripted_llm import ScriptedLLM, final, respond, tool_call

    def ia(call):
        rounds = sum(1 for m in call.messages if m.get("role") == "assistant")
        text = call.first_user_content
        if rounds:
            return final("")
        if "<new_agent_message>" in text:
            return respond(tool_call("send_message_to_user", message="Here is what I found."))
        return respond(tool_call("send_message_to_agent", agent_name="Dry Run Agent", instructions=text[-400:]))

    def ea(call):
        rounds = sum(1 for m in call.messages if m.get("role") == "assistant")
        if rounds == 0:
            return respond(tool_call("task_email_search", search_query="in:inbox"))
        return final("Dry-run result.")

    ScriptedLLM().on("interaction", ia).on("execution", ea).install()


def _first_execution_prompts(prompts_dir: Path) -> Dict[str, str]:
    """run_id -> text of that run's first execution-agent prompt (system + messages)."""
    first: Dict[str, tuple] = {}
    if not prompts_dir.exists():
        return {}
    for path in prompts_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if record.get("role") != "execution" or not record.get("run_id"):
            continue
        text = (record.get("system") or "") + "\n" + json.dumps(record.get("messages"), ensure_ascii=False)
        seq = record.get("seq", 0)
        if record["run_id"] not in first or seq < first[record["run_id"]][0]:
            first[record["run_id"]] = (seq, text)
    return {run_id: text for run_id, (_, text) in first.items()}


async def _run_steps(spec: Dict[str, Any], step_timeout: float, budget: int) -> Dict[str, Any]:
    from server.agents.interaction_agent.runtime import InteractionAgentRuntime
    from server.services import telemetry
    from server.services.context import turn_scope
    from server.services.gmail import get_important_email_watcher
    from server.services.trigger_scheduler import get_trigger_scheduler
    from server.testing.fake_gmail import get_fake_gmail
    from server.utils import clock

    fake = get_fake_gmail()
    watcher = get_important_email_watcher()
    await watcher._poll_once()  # warm-up: the fixture inbox is "already seen"

    steps_meta: List[Dict[str, Any]] = []
    seeded: List[str] = []
    partial_reason: Optional[str] = None
    for index, step in enumerate(spec.get("steps", [])):
        kind = next((k for k in ("user", "seed_agents", "advance_clock", "trigger_due", "inject_email", "gmail_fault") if k in step), "unknown")
        required = harness.resolve_required_ids(step.get("required_ids", []), spec.get("gold_threads", {}), fake.actions_log())
        meta = {"index": index, "kind": kind, "gold_thread": step.get("gold_thread"), "gold_threads": step.get("gold_threads"),
                "required_ids": required, "expect_side_effects": step.get("expect_side_effects", []),
                "allowed_side_effects": step.get("allowed_side_effects", step.get("expect_side_effects", [])),
                "send_allowed_if_not_already_sent": bool(step.get("send_allowed_if_not_already_sent")),
                "confirms_resend": bool(step.get("confirms_resend")), "mono_start": time.monotonic(),
                "gmail_seq_start": len(fake.actions_log())}
        telemetry.emit("eval_step", index=index, kind=kind, gold_thread=step.get("gold_thread"))
        ok = True
        if kind == "user":
            text = step["user"]
            with turn_scope("user", user_message=text):
                try:
                    result = await asyncio.wait_for(InteractionAgentRuntime().execute(text), timeout=step_timeout)
                    meta["ia_success"] = result.success
                    meta["ia_error"] = result.error
                except asyncio.TimeoutError:
                    ok = False
                    meta["timeout"] = True
            ok = await harness.drain(step_timeout) and ok
        elif kind == "seed_agents":
            seeded += harness.seed_agents(int(step["seed_agents"]["count"]), bool(step["seed_agents"].get("near_misses")))
        elif kind == "advance_clock":
            clock.advance(float(step["advance_clock"].get("days", 0)) * 86400 + float(step["advance_clock"].get("seconds", 0)))
        elif kind == "trigger_due":
            clock.advance(float(step["trigger_due"].get("advance_seconds", 0)))
            await get_trigger_scheduler()._poll_once()
            ok = await harness.drain(step_timeout)
        elif kind == "inject_email":
            fake.inject_email(**step["inject_email"])
            await watcher._poll_once()
            ok = await harness.drain(step_timeout)
        elif kind == "gmail_fault":
            fault = step["gmail_fault"]
            fake.fail_next(fault["action"], exception=RuntimeError("Injected network error") if fault.get("exception") else None,
                           unsuccessful=not fault.get("exception"), after_effect=bool(fault.get("after_effect")))
        meta["mono_end"] = time.monotonic()
        meta["drained"] = ok
        steps_meta.append(meta)
        if len(telemetry.recent("llm_call")) >= budget:
            partial_reason = "budget"
            break
        if not ok:
            partial_reason = "step_timeout"
            break
    return {"steps": steps_meta, "seeded_agents": seeded, "partial_reason": partial_reason}


def worker(args: argparse.Namespace) -> None:
    harness.require_fake_gmail()
    spec = json.loads(Path(args.worker).read_text(encoding="utf-8"))

    import server  # noqa: F401  (environment is fixed; import the app now)
    from server import flags
    from server.paths import data_dir
    from server.services import telemetry
    from server.services.gmail import client as gmail_client
    from server.services.execution import get_agent_roster
    from server.testing.fake_gmail import get_fake_gmail

    flags.validate()
    if args.dry_run:
        _install_dry_run_model()
    _install_budget(args.budget)
    started = datetime.now(timezone.utc).isoformat()
    outcome = asyncio.run(_run_steps(spec, args.step_timeout, args.budget))

    events = telemetry.read_events()
    roster = get_agent_roster()
    roster.load()
    prompts = _first_execution_prompts(data_dir() / "telemetry" / "prompts")
    context_checks = []
    for meta in outcome["steps"]:
        wanted = {k: v for k, v in (meta.get("required_ids") or {}).items()}
        if not wanted:
            continue
        runs = [e for e in events if e["event"] == "run_start" and meta["mono_start"] <= e["mono"] <= meta["mono_end"]]
        context_checks.append({
            "step": meta["index"],
            "required": wanted,
            "runs": [{"run_id": r["run_id"], "agent": r["agent"],
                      "present": {k: (v is not None and v in prompts.get(r["run_id"], "")) for k, v in wanted.items()}}
                     for r in runs],
        })
    llm_events = [e for e in events if e["event"] == "llm_call"]
    usage = {f: sum((e.get(f) or 0) for e in llm_events) for f in ("prompt_tokens", "completion_tokens", "total_tokens", "cost")}
    result = {
        "scenario": spec["run_id"],
        "scenario_id": spec["id"],
        "roster_size": spec.get("roster_size"),
        "rep": args.rep,
        "flags": flags.active_flags(),
        "model": os.environ.get("OPENROUTER_MODEL"),
        "started": started,
        "partial": outcome["partial_reason"] is not None,
        "partial_reason": outcome["partial_reason"],
        "steps": outcome["steps"],
        "gold_threads": spec.get("gold_threads", {}),
        "seeded_agents": outcome["seeded_agents"],
        "agents_final": roster.get_agents(),
        "events": events,
        "gmail_actions": get_fake_gmail().actions_log(),
        "context_checks": context_checks,
        "llm_calls": len(llm_events),
        "usage": usage,
        "rate_limited_calls": sum(1 for e in llm_events if e.get("http_status") == 429),
        "composio_client_constructed": gmail_client._CLIENT is not None,
    }
    if result["composio_client_constructed"]:
        raise harness.UnsafeEvalEnvironment("a Composio client was constructed during an eval run")
    Path(args.worker_out).write_text(json.dumps(result, default=str), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--variant", default="baseline", help="baseline | fixed | comma-separated flag list")
    parser.add_argument("--label", default=None, help="name used in the results file (default: variant)")
    parser.add_argument("--scenarios", default="all")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-llm-calls", type=int, default=1500)
    parser.add_argument("--out", default=str(harness.RAW_RESULTS_DIR), help="raw results (git-ignored by default)")
    parser.add_argument("--fixture", default=str(harness.DEFAULT_FIXTURE))
    parser.add_argument("--step-timeout", type=float, default=180.0)
    parser.add_argument("--include-optional-sizes", action="store_true", help="S4: also run optional roster sizes (500)")
    parser.add_argument("--dry-run", action="store_true", help="use a scripted stand-in model (no API calls, no cost)")
    parser.add_argument("--commit", default=None, help="record this commit id (when running from an exported copy without .git)")
    parser.add_argument("--worker", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-out", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--rep", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--budget", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        worker(args)
    else:
        orchestrate(args)


if __name__ == "__main__":
    main()
