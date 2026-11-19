"""Build every committed evaluation artifact from raw run files (one command; no hand-typed numbers).

    python -m evals.report_tables --original <dir with baseline/ phase1/ fixed/ cross_model/> \
        --rerun <dir with *_rerun-baseline.json and *_rerun-fixed.json> --out evals/results

Writes to ``--out``:
- ``summary.md``: run sets, the headline comparison (re-run, metrics v2), original runs
  under v1 and v2 side by side, the gates exactly as declared, per-scenario tables;
- ``audit.md``: every merge decision, wrong-target write, unrequested send and duplicate
  send as scenario/rep/step/agent/action/target ids (no message or email text);
- ``<runset>_<variant>_metrics.json``: per-run metric rows (numbers and ids only).

Raw files (telemetry + fake-Gmail logs) are not committed; they are kept outside the
repository (see docs/overload/03_EVALUATION.md for their location and checksums).
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import metrics

GATE_SCENARIOS = ["S1", "S2", "S3", "S4@10", "S4@200", "S5", "S9"]


# ------------------------------------------------------------------ loading
def _read(path: Path) -> Dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _latest(directory: Path, pattern: str) -> Optional[Path]:
    files = sorted(directory.glob(pattern))
    return files[-1] if files else None


def load_run_sets(original: Optional[Path], rerun: Optional[Path]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    sets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if original:
        for variant in ("baseline", "phase1", "fixed", "cross_model"):
            path = _latest(original / variant, "*.json*")
            if path:
                sets[("original", variant)] = {**_read(path), "_file": path.name}
    if rerun:
        for variant in ("baseline", "fixed"):
            path = _latest(rerun, f"*_rerun-{variant}.json*")
            if path:
                sets[("rerun", variant)] = {**_read(path), "_file": path.name}
    return sets


def _runs(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in data["runs"] if not r.get("error")]


# ------------------------------------------------------------------ formatting
def _f(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _med(entry: Optional[Dict[str, Any]], key: str) -> Optional[float]:
    value = (entry or {}).get(key)
    return value.get("median") if isinstance(value, dict) else value


def _total(summary: Dict[str, Dict[str, Any]], key: str, scenarios: Optional[List[str]] = None) -> int:
    return sum((e.get(key) or 0) for s, e in summary.items() if scenarios is None or s in scenarios)


def _run_totals(data: Dict[str, Any]) -> Dict[str, Any]:
    runs = _runs(data)
    return {
        "runs": len(runs),
        "errors": len(data["runs"]) - len(runs),
        "partial": sum(1 for r in runs if r.get("partial")),
        "calls": sum(r.get("llm_calls", 0) for r in runs),
        "tokens": sum((r.get("usage") or {}).get("total_tokens") or 0 for r in runs),
        "cost": sum((r.get("usage") or {}).get("cost") or 0 for r in runs),
        "rate_limited": sum(r.get("rate_limited_calls", 0) for r in runs),
    }


def _range(summary: Dict[str, Dict[str, Any]], key: str, scenarios: List[str]) -> str:
    values = [_med(summary.get(s), key) for s in scenarios if s in summary]
    values = [v for v in values if v is not None]
    return f"{min(values):.1f}–{max(values):.1f}" if values else "n/a"


# ------------------------------------------------------------------ gates (as declared)
def phase1_gates(sb, sf) -> List[Tuple[str, bool, str]]:
    scen = [s for s in GATE_SCENARIOS if s in sf]
    out = []
    limit = _total(sf, "limit_hits_total", scen)
    out.append(("limit_hits = 0", limit == 0, f"{limit} across {', '.join(scen)}"))
    dup = _total(sf, "duplicate_sends_total")
    out.append(("duplicate_sends = 0 (hard)", dup == 0, f"{dup} (baseline {_total(sb, 'duplicate_sends_total')})"))
    lower = [(s, _med(sb.get(s), "ia_calls_per_turn_mean"), _med(sf[s], "ia_calls_per_turn_mean")) for s in scen if s in sb]
    out.append(("IA calls per turn lower than baseline", all(f < b for _, b, f in lower if b is not None and f is not None),
                "; ".join(f"{s}: {_f(b)} → {_f(f)}" for s, b, f in lower)))
    ctx = [(s, sb.get(s, {}).get("context_hit_rate"), sf[s].get("context_hit_rate")) for s in scen]
    out.append(("context_hit ≥ baseline", all(f is None or b is None or f >= b for _, b, f in ctx),
                "; ".join(f"{s}: {_f(b)} → {_f(f)}" for s, b, f in ctx if b is not None or f is not None)))
    return out


def phase2_gates(sb, sf) -> List[Tuple[str, bool, str]]:
    scen = [s for s in GATE_SCENARIOS if s in sf]
    out = []
    merges, labeled = _total(sf, "false_merges_total", scen), _total(sf, "labeled_runs_total", scen)
    bound = metrics.false_merge_bound(merges, labeled)
    out.append(("false merges = 0 of N (hard)", merges == 0,
                f"{merges} of {labeled} labelled runs" + (f"; 95% upper bound {bound:.3f}" if bound else "")
                + f" (baseline {_total(sb, 'false_merges_total', scen)} of {_total(sb, 'labeled_runs_total', scen)})"))
    frag = [(s, _med(sb.get(s), "fragmentation"), _med(sf[s], "fragmentation")) for s in scen]
    out.append(("fragmentation below baseline", all(f is None or b is None or f < b for _, b, f in frag),
                "; ".join(f"{s}: {_f(b)} → {_f(f)}" for s, b, f in frag)))
    out.append(("fragmentation ≤ 1.2", all(f is None or f <= 1.2 for _, _, f in frag), "; ".join(f"{s}: {_f(f)}" for s, _, f in frag)))
    for s in ("S1", "S5"):
        if s in sf:
            rate = sf[s].get("context_hit_rate")
            out.append((f"context_hit ≥ 0.9 on {s}", rate is not None and rate >= 0.9, f"{_f(rate)} (baseline {_f(sb.get(s, {}).get('context_hit_rate'))})"))
    sizes = [s for s in ("S4@10", "S4@200", "S4@500") if s in sf]
    values = [_med(sf[s], "roster_chars_max") for s in sizes]
    values = [v for v in values if v is not None]
    if values:
        out.append(("roster tokens flat across N", max(values) - min(values) <= 0.1 * max(values),
                    "; ".join(f"{s}: {_f(_med(sf[s], 'roster_chars_max'), 0)}" for s in sizes)))
    if "S4@10" in sf and "S4@200" in sf:
        a10, a200 = _med(sf["S4@10"], "routing_accuracy"), _med(sf["S4@200"], "routing_accuracy")
        out.append(("routing accuracy at N=200 ≥ N=10", a10 is not None and a200 is not None and a200 >= a10, f"{_f(a10)} → {_f(a200)}"))
    out.append(("IA calls per turn below baseline", phase1_gates(sb, sf)[2][1], "see above"))
    out.append(("duplicate sends = 0", _total(sf, "duplicate_sends_total") == 0, f"{_total(sf, 'duplicate_sends_total')}"))
    return out


def _gate_table(rows: List[Tuple[str, bool, str]]) -> List[str]:
    lines = ["| Gate | Result | Detail |", "|---|---|---|"]
    lines += [f"| {name} | {'PASS' if ok else '**FAIL**'} | {detail} |" for name, ok, detail in rows]
    return lines


# ------------------------------------------------------------------ report sections
HEADLINE_ROWS = [
    # (title, how) how = ("total", key) | ("range", key) | ("median_by_size", key)
    ("LLM calls (total)", ("runtotal", "calls")),
    ("Tokens (total, provider usage)", ("runtotal", "tokens")),
    ("Cost USD (total, provider usage)", ("runtotal", "cost")),
    ("IA LLM calls per turn (range of scenario medians)", ("range", "ia_calls_per_turn_mean")),
    ("Turns hitting the 8-round limit (total)", ("total", "limit_hits_total")),
    ("Largest EA prompt, chars (max of scenario medians)", ("max", "ea_prompt_chars_max")),
    ("Roster block chars at N = 10 / 200 / 500 (medians)", ("sizes", "roster_chars_max")),
    ("Duplicate sends (total)", ("total", "duplicate_sends_total")),
    ("S8 runs where the injected fault fired / duplicates among them", ("s8", None)),
    ("Merge decisions (v2, total)", ("total", "merge_decisions_total")),
    ("False-merge runs / labelled runs (v2, total)", ("ratio", ("false_merges_total", "labeled_runs_total"))),
    ("Runs on seeded distractors (v2, total)", ("sum_med", "runs_on_seeded_agents")),
    ("Wrong-target writes / scored writes (v2, total)", ("ratio", ("wrong_target_writes_total", "writes_scored_total"))),
    ("Unrequested sends (draft asked, email sent; total)", ("total", "unrequested_sends_total")),
    ("context_hit (pooled over measurable follow-ups)", ("ctx", None)),
]


def _cell(data: Dict[str, Any], summary: Dict[str, Dict[str, Any]], how) -> str:
    kind, key = how
    if kind == "runtotal":
        value = _run_totals(data)[key]
        return f"{value:.3f}" if key == "cost" else f"{value:,}"
    if kind == "total":
        return _f(_total(summary, key)) if any(key in e for e in summary.values()) else "n/a"
    if kind == "range":
        return _range(summary, key, [s for s in summary if s != "S9"])
    if kind == "max":
        values = [_med(e, key) for e in summary.values()]
        values = [v for v in values if v is not None]
        return _f(max(values), 0) if values else "n/a"
    if kind == "sizes":
        return " / ".join(_f(_med(summary.get(s), key), 0) for s in ("S4@10", "S4@200", "S4@500"))
    if kind == "s8":
        s8 = summary.get("S8") or {}
        if "fault_fired_runs" not in s8:
            return "n/a"
        return f"{s8['fault_fired_runs']} of {s8['repetitions']} / {s8['duplicate_sends_fault_fired']}"
    if kind == "ratio":
        a, b = key
        if not any(a in e for e in summary.values()):
            return "n/a"
        return f"{_total(summary, a)} / {_total(summary, b)}"
    if kind == "sum_med":
        return _f(sum(round((e.get(key) or {}).get("mean", 0) * (e.get(key) or {}).get("n", 0)) for e in summary.values()))
    if kind == "ctx":
        hits = sum((e.get("context_hits") or {}).get("mean", 0) * (e.get("context_hits") or {}).get("n", 0) for e in summary.values())
        meas = sum((e.get("context_measurable") or {}).get("mean", 0) * (e.get("context_measurable") or {}).get("n", 0) for e in summary.values())
        return f"{hits / meas:.2f} ({round(hits)}/{round(meas)})" if meas else "n/a"
    return "n/a"


def render_summary(sets, summaries) -> str:
    out: List[str] = ["# Evaluation results (generated)", "",
                      "Generated by `python -m evals.report_tables`. All values are MEASURED (telemetry, provider usage, "
                      "fake-Gmail logs); nothing is estimated. Model: `openai/gpt-6-luna` unless stated.", "",
                      "**Run sets.** *original* = the first runs (old fake-Gmail matcher, which ignored `OR`/parentheses). "
                      "*rerun* = the same scenarios with the corrected fake (Gmail query parser). **Metric versions.** "
                      "*v1* labels a run by what it touched; *v2* labels it by its step's gold thread and adds outcome "
                      "metrics (see docs/overload/03_EVALUATION.md).", "", "## Run sets", "",
                      "| Run set | Variant | Commit | Runs | Worker errors | Partial | LLM calls | Tokens | Cost (USD) | 429s | Raw file |",
                      "|---|---|---|---|---|---|---|---|---|---|---|"]
    for (runset, variant), data in sets.items():
        t = _run_totals(data)
        out.append(f"| {runset} | {variant} | `{data.get('git_commit') or 'n/a'}` | {t['runs']} | {t['errors']} | {t['partial']} | "
                   f"{t['calls']:,} | {t['tokens']:,} | {t['cost']:.3f} | {t['rate_limited']} | `{data['_file']}` |")

    if ("rerun", "baseline") in sets and ("rerun", "fixed") in sets:
        out += ["", "## Headline: re-run (corrected fake), metrics v2, with the original runs (v1) for reference", "",
                "| Metric | rerun baseline (v2) | rerun fixed (v2) | rerun change | original baseline (v1) | original fixed (v1) |",
                "|---|---|---|---|---|---|"]
        for title, how in HEADLINE_ROWS:
            cells = []
            for key in (("rerun", "baseline", 2), ("rerun", "fixed", 2), ("original", "baseline", 1), ("original", "fixed", 1)):
                if (key[0], key[1]) not in sets:
                    cells.append("n/a")
                    continue
                cells.append(_cell(sets[(key[0], key[1])], summaries[key], how))
            change = ""
            if how[0] == "runtotal":
                b, f = _run_totals(sets[("rerun", "baseline")])[how[1]], _run_totals(sets[("rerun", "fixed")])[how[1]]
                change = f"{(f - b) / b:+.0%}" if b else ""
            cells.insert(2, change)
            out.append(f"| {title} | " + " | ".join(cells) + " |")

    out += ["", "## Safety by run set (metric v2)", "",
            "S8 injects a fault into the reply action (it raises *after* the email went out), so it only fires when the model "
            "chooses that action. Duplicate sends are counted over all scenarios.", "",
            "| Run set | Variant | S8 runs where the fault fired | Duplicates in those runs | Duplicate sends (all scenarios) | Unrequested sends (draft asked, email sent) |",
            "|---|---|---|---|---|---|"]
    pooled: Dict[str, List[int]] = {}
    for (runset, variant), data in sets.items():
        if variant == "cross_model" or (runset, variant, 2) not in summaries:
            continue
        s2 = summaries[(runset, variant, 2)]
        s8 = s2.get("S8") or {}
        row = [s8.get("fault_fired_runs", 0), s8.get("repetitions", 0), s8.get("duplicate_sends_fault_fired", 0),
               _total(s2, "duplicate_sends_total"), _total(s2, "unrequested_sends_total")]
        out.append(f"| {runset} | {variant} | {row[0]} of {row[1]} | {row[2]} | {row[3]} | {row[4]} |")
        if variant in ("baseline", "fixed"):
            pooled[variant] = [a + b for a, b in zip(pooled.get(variant, [0] * 5), row)]
    for variant, row in pooled.items():
        out.append(f"| original + rerun | {variant} | {row[0]} of {row[1]} | {row[2]} | {row[3]} | {row[4]} |")

    out += ["", "## Original runs: metric v1 vs v2 (same raw data)", "",
            "| Variant | v1 false-merge runs / labelled | v2 false-merge runs / labelled | v2 merge decisions | v2 ambiguous runs | v2 wrong-target writes / scored | v2 unrequested sends |",
            "|---|---|---|---|---|---|---|"]
    for variant in ("baseline", "phase1", "fixed"):
        if (("original", variant, 1)) not in summaries:
            continue
        s1, s2 = summaries[("original", variant, 1)], summaries[("original", variant, 2)]
        out.append(f"| {variant} | {_total(s1, 'false_merges_total')} / {_total(s1, 'labeled_runs_total')} | "
                   f"{_total(s2, 'false_merges_total')} / {_total(s2, 'labeled_runs_total')} | {_total(s2, 'merge_decisions_total')} | "
                   f"{_total(s2, 'ambiguous_runs_total')} | {_total(s2, 'wrong_target_writes_total')} / {_total(s2, 'writes_scored_total')} | "
                   f"{_total(s2, 'unrequested_sends_total')} |")

    out += ["", "## Gates, exactly as declared", "", "Original runs, metric v1 (as first published):", ""]
    if ("original", "baseline", 1) in summaries:
        ob, of = summaries[("original", "baseline", 1)], summaries[("original", "fixed", 1)]
        out += ["Phase 1 gates (phase1 vs baseline):", ""] + _gate_table(phase1_gates(ob, summaries[("original", "phase1", 1)])) if ("original", "phase1", 1) in summaries else []
        out += ["", "Phase 1 gates on the full fix set (fixed vs baseline):", ""] + _gate_table(phase1_gates(ob, of))
        out += ["", "Phase 2 gates (fixed vs baseline):", ""] + _gate_table(phase2_gates(ob, of))
    if ("rerun", "baseline", 2) in summaries:
        rb, rf = summaries[("rerun", "baseline", 2)], summaries[("rerun", "fixed", 2)]
        out += ["", "The same gates on the re-run, metric v2 (false merges = v2 false-merge runs). No threshold changed:", ""]
        out += _gate_table(phase1_gates(rb, rf) + phase2_gates(rb, rf)[:-2])

    for key in (("rerun", "baseline", 2), ("rerun", "fixed", 2)):
        pass
    if ("rerun", "baseline", 2) in summaries:
        out += ["", "## Per-scenario detail: re-run, metrics v2 (median over repetitions unless marked total)", ""]
        rb, rf = summaries[("rerun", "baseline", 2)], summaries[("rerun", "fixed", 2)]
        rows = [("IA calls per turn", "ia_calls_per_turn_mean", 2), ("LLM calls per run", "llm_calls", 0),
                ("Tokens per run", "total_tokens", 0), ("Limit hits (total)", "limit_hits_total", 0),
                ("Largest EA prompt chars", "ea_prompt_chars_max", 0), ("Roster block chars", "roster_chars_max", 0),
                ("Merge decisions (total)", "merge_decisions_total", 0), ("False-merge runs (total)", "false_merges_total", 0),
                ("Labelled runs (total)", "labeled_runs_total", 0), ("Routing accuracy", "routing_accuracy", 2),
                ("Agents created", "agents_created", 0), ("Resolver ask rounds", "ask_rounds", 1),
                ("Wrong-target writes (total)", "wrong_target_writes_total", 0), ("Unrequested sends (total)", "unrequested_sends_total", 0),
                ("Duplicate sends (total)", "duplicate_sends_total", 0), ("context_hit (pooled)", "context_hit_rate", 2)]
        for scenario in sorted(set(rb) | set(rf)):
            out += [f"### {scenario} (N = {rb.get(scenario, {}).get('repetitions', 0)} baseline, {rf.get(scenario, {}).get('repetitions', 0)} fixed)", "",
                    "| Metric | baseline | fixed |", "|---|---|---|"]
            for title, key, digits in rows:
                out.append(f"| {title} | {_f(_med(rb.get(scenario), key), digits)} | {_f(_med(rf.get(scenario), key), digits)} |")
            out.append("")
    return "\n".join(out) + "\n"


def render_audit(sets) -> str:
    out = ["# Audit (generated)", "", "Every merge decision, wrong-target write, unrequested send and duplicate send, per run set. "
           "Identifiers only (scenario, repetition, step, agent, action, thread id, recipients, gold labels); no message or "
           "email text. Metric v2. Step attribution: an action belongs to the step during which it happened "
           "(fake-Gmail sequence number > the step's `gmail_seq_start`), which agrees with the run timestamps for every action.", ""]
    for (runset, variant), data in sets.items():
        if variant == "cross_model":
            continue
        runs = _runs(data)
        merges, wrong, unrequested, duplicates = [], [], [], []
        for r in runs:
            for d in metrics.routing_v2(r)["merge_decision_items"]:
                merges.append({"scenario": r["scenario"], "rep": r["rep"], **d})
            wrong += metrics.wrong_target_writes(r)["wrong"]
            unrequested += metrics.unrequested_sends(r)
            duplicates += metrics.duplicate_send_items(r)
        out += [f"## {runset} / {variant}", "",
                f"Merge decisions: {len(merges)}. Wrong-target writes: {len(wrong)}. Unrequested sends: {len(unrequested)}. "
                f"Duplicate sends: {len(duplicates)}.", ""]
        if merges:
            out += ["**Merge decisions** (agent received gold work while already owning other gold work or being a seeded distractor):", "",
                    "| Scenario | Rep | Step | Agent | Gold of this run | Already owned |", "|---|---|---|---|---|---|"]
            out += [f"| {m['scenario']} | {m['rep']} | {m['step']} | {m['agent']} | {m['gold']} | {', '.join(m['already_owned'])} |" for m in merges]
            out.append("")
        for title, items, extra in (("Wrong-target writes", wrong, "targets"), ("Unrequested sends", unrequested, None),
                                    ("Duplicate sends", duplicates, "duplicate_of_seq")):
            if not items:
                continue
            out += [f"**{title}:**", "", "| Scenario | Rep | Step | Agent | Action | Thread | Recipients | " + (extra or "") + " |",
                    "|---|---|---|---|---|---|---|" + ("---|" if extra else "|")]
            for i in items:
                out.append(f"| {i['scenario']} | {i['rep']} | {i['step']} | {i.get('agent') or ''} | {i['action']} | "
                           f"{i.get('thread_id') or ''} | {', '.join(i.get('recipients', []))} | " + (str(i.get(extra, "")) if extra else "") + " |")
            out.append("")
    return "\n".join(out) + "\n"


def metric_rows(data: Dict[str, Any], version: int) -> List[Dict[str, Any]]:
    rows = []
    for r in _runs(data):
        row = metrics.per_run(r, version)
        row["metric_version"] = version
        rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", default=None, help="dir with baseline/ phase1/ fixed/ cross_model/ (json or json.gz)")
    parser.add_argument("--rerun", default=None, help="dir with *_rerun-baseline.json and *_rerun-fixed.json")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"))
    args = parser.parse_args()
    sets = load_run_sets(Path(args.original) if args.original else None, Path(args.rerun) if args.rerun else None)
    summaries = {}
    for (runset, variant), data in sets.items():
        for version in ((1, 2) if runset == "original" else (2,)):
            summaries[(runset, variant, version)] = metrics.summarize(_runs(data), version)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text(render_summary(sets, summaries), encoding="utf-8")
    (out / "audit.md").write_text(render_audit(sets), encoding="utf-8")
    for (runset, variant), data in sets.items():
        rows = {f"v{v}": metric_rows(data, v) for v in ((1, 2) if runset == "original" else (2,))}
        payload = {"run_set": runset, "variant": variant, "model": data.get("model"), "git_commit": data.get("git_commit"),
                   "raw_file": data["_file"], "rows": rows}
        (out / f"{runset}_{variant}_metrics.json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    print(f"wrote {out}/summary.md, audit.md and {len(sets)} metrics files")


if __name__ == "__main__":
    main()
