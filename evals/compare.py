"""Compare two L3 result files and print a Markdown table of deltas.

    python -m evals.compare evals/results/<baseline>.json evals/results/<fixed>.json [--out summary.md]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import metrics

ROWS = [
    ("ia_calls_per_turn_mean", "IA LLM calls per turn (mean of runs)", "median"),
    ("wasted_rounds_total", "Wasted `wait` rounds per run", "median"),
    ("limit_hits", "Turns that hit the 8-round limit per run", "median"),
    ("llm_calls", "LLM calls per run", "median"),
    ("total_tokens", "Tokens per run (provider usage)", "median"),
    ("cost", "Cost per run (provider usage, USD)", "median"),
    ("ea_prompt_chars_max", "Largest EA prompt (chars)", "median"),
    ("roster_chars_max", "Largest IA roster block (chars)", "median"),
    ("agents_created", "Agents created", "median"),
    ("fragmentation", "Fragmentation (agents ÷ gold threads)", "median"),
    ("routing_accuracy", "Routing accuracy", "median"),
    ("ask_rounds", "Resolver `ask` rounds", "median"),
    ("external_sends", "External sends", "median"),
]


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)


def _cell(entry: Optional[Dict[str, Any]], key: str, stat: str) -> str:
    if not entry or key not in entry:
        return "n/a"
    s = entry[key]
    if not isinstance(s, dict):
        return _fmt(s)
    if s.get("n", 0) == 0:
        return "n/a"
    return f"{_fmt(s[stat])} [{_fmt(s['min'])}–{_fmt(s['max'])}]"


def render(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    sa, sb = metrics.summarize(a["runs"]), metrics.summarize(b["runs"])
    lines: List[str] = []
    lines.append(f"Model: A `{a.get('model')}` / B `{b.get('model')}`. A = `{a.get('label')}` ({a.get('llm_calls_used')} calls), "
                 f"B = `{b.get('label')}` ({b.get('llm_calls_used')} calls). Values: median [min–max] over repetitions; all MEASURED.")
    lines.append("")
    for scenario in sorted(set(sa) | set(sb)):
        ea, eb = sa.get(scenario), sb.get(scenario)
        reps_a = ea["repetitions"] if ea else 0
        reps_b = eb["repetitions"] if eb else 0
        lines.append(f"### {scenario} (N = {reps_a} vs {reps_b} repetitions)")
        lines.append("")
        lines.append(f"| Metric | {a.get('label')} | {b.get('label')} |")
        lines.append("|---|---|---|")
        for key, title, stat in ROWS:
            lines.append(f"| {title} | {_cell(ea, key, stat)} | {_cell(eb, key, stat)} |")
        for key, title in (("duplicate_sends_total", "Duplicate sends (total)"),
                           ("limit_hits_total", "Limit hits (total)"),
                           ("context_hit_rate", "context_hit (pooled)"),
                           ("false_merges_total", "False merges (total)"),
                           ("labeled_runs_total", "Labeled runs (total)"),
                           ("false_merge_upper_bound_95", "False-merge 95% upper bound")):
            lines.append(f"| {title} | {_fmt(ea.get(key) if ea else None)} | {_fmt(eb.get(key) if eb else None)} |")
        partial_a = ea["partial_runs"] if ea else 0
        partial_b = eb["partial_runs"] if eb else 0
        lines.append(f"| Partial runs | {partial_a} | {partial_b} |")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("a")
    parser.add_argument("b")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    a = json.loads(Path(args.a).read_text(encoding="utf-8"))
    b = json.loads(Path(args.b).read_text(encoding="utf-8"))
    text = render(a, b)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
