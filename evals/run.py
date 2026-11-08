"""Evaluation CLI.

    python -m evals.run math                        # free, deterministic, ~5 s
    python -m evals.run report [--no-judge] [--max-spend 2.00] [--scenario default ...]
    python -m evals.run feedback                    # summarize logs/feedback.jsonl

Each run prints a summary and writes evals/results/<UTC timestamp>-<kind>.{json,md}.
`report` calls Claude (costs money): it stops before any call that could exceed --max-spend.
Exit code is non-zero if any check fails, so it can gate a release.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from biocycle.config import EVAL_RESULTS_DIR, EVAL_SPEND_CAP_USD, MODEL, JUDGE_MODEL
from biocycle.data import load_outlets, load_sources
from biocycle.feedback import load as load_feedback
from biocycle.llm import LLMError
from biocycle.report import build_payload, generate_report
from biocycle.valuation import evaluate
from evals import math_checks
from evals.feedback_summary import summarize, to_markdown
from evals.judge import CRITERIA, judge_report
from evals.report_graders import grade_report
from evals.scenarios import SCENARIOS, Scenario

FIRST_CALL_ESTIMATE_USD = 0.10  # assumed cost of a report + judge pair before we've seen one


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _write(kind: str, data: Any, markdown: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"{_stamp()}-{kind}"
    base.with_suffix(".json").write_text(json.dumps(data, indent=2, default=str) + "\n")
    base.with_suffix(".md").write_text(markdown)
    return base


# ---------- math ----------


def run_math(out_dir: Path) -> bool:
    results = math_checks.run_all()
    ok = all(r.passed for r in results)
    lines = ["# Math validation", "",
             f"{sum(r.passed for r in results)}/{len(results)} checks passed.", "",
             "| Group | Check | Result | Detail |", "|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.group} | {r.name} | {'PASS' if r.passed else '**FAIL**'} | "
                     f"{r.detail.replace('|', '/')} |")
    bt = next((r for r in results if r.group == "backtest"), None)
    if bt:
        lines += ["", "## Backtest: best option at each historical local hull price", "",
                  "| Period | Local price | Source | Best option | vs. local $/t |",
                  "|---|---:|---|---|---:|"]
        for row in bt.data["rows"]:
            lines.append(f"| {row['period']} | ${row['local_price']:g} | {row['source_id']} | "
                         f"{row['best']} | {row['vs_local_per_ton']:+g} |")
    md = "\n".join(lines) + "\n"
    base = _write("math", [asdict(r) for r in results], md, out_dir)
    print(md)
    print(f"Wrote {base}.json/.md")
    return ok


# ---------- report ----------


def run_report(scenarios: list[Scenario], judge: bool, max_spend: float, out_dir: Path,
               client: Any = None) -> bool:
    sources = load_sources()
    outlets = load_outlets(sources)
    spent, observed = 0.0, []  # observed: cost of each finished scenario
    rows: list[dict[str, Any]] = []

    for sc in scenarios:
        next_cost = max(observed) if observed else FIRST_CALL_ESTIMATE_USD
        if spent + next_cost > max_spend:
            rows.append({"scenario": sc.id, "status": "skipped",
                         "reason": f"spend cap ${max_spend:.2f} reached (${spent:.4f} spent)"})
            continue
        results = evaluate(sc.lot, outlets, sc.overrides)
        payload = build_payload(sc.lot, results, sources)
        row: dict[str, Any] = {"scenario": sc.id, "description": sc.description}
        cost = 0.0
        try:
            rep = generate_report(sc.lot, results, sources, client=client)
            cost += rep.llm.cost_usd
            grades = grade_report(rep.llm.text, payload)
            row.update(status="ok", report=rep.llm.text,
                       grades=[asdict(g) for g in grades],
                       code_pass=all(g.passed for g in grades))
            if judge:
                v = judge_report(rep.llm.text, payload, client=client)
                cost += v.llm.cost_usd
                row.update(judge_scores=v.scores, judge_issues=v.issues,
                           judge_pass=v.passed, judge_model_pass=v.model_pass)
        except LLMError as e:
            row.update(status="error", error=str(e))
        spent += cost
        if cost:
            observed.append(cost)
        row["cost_usd"] = round(cost, 4)
        rows.append(row)

    done = [r for r in rows if r["status"] == "ok"]
    ok = bool(done) and len(done) == len(rows) and all(
        r["code_pass"] and r.get("judge_pass", True) for r in done)
    md = report_markdown(rows, judge, spent, max_spend)
    base = _write("report", {"report_model": MODEL, "judge_model": JUDGE_MODEL if judge else None,
                             "spent_usd": round(spent, 4), "max_spend_usd": max_spend,
                             "scenarios": rows}, md, out_dir)
    print(md)
    print(f"Wrote {base}.json/.md")
    return ok


def report_markdown(rows: list[dict[str, Any]], judge: bool, spent: float,
                    max_spend: float) -> str:
    done = [r for r in rows if r["status"] == "ok"]
    grader_names = [g["name"] for g in done[0]["grades"]] if done else []
    lines = ["# AI report evaluation", "",
             f"Report model `{MODEL}`" + (f", judge `{JUDGE_MODEL}`" if judge else "") +
             f". Spent ${spent:.4f} of a ${max_spend:.2f} cap.", ""]
    if done:
        lines += ["## Pass rates", "", "| Check | Passed |", "|---|---:|"]
        for name in grader_names:
            n = sum(next(g for g in r["grades"] if g["name"] == name)["passed"] for r in done)
            lines.append(f"| {name} | {n}/{len(done)} |")
        if judge:
            for c in CRITERIA:
                scores = [r["judge_scores"][c] for r in done if "judge_scores" in r]
                if scores:
                    lines.append(f"| judge: {c} (mean of 5) | {sum(scores)/len(scores):.1f} |")
            jp = sum(r.get("judge_pass", False) for r in done)
            lines.append(f"| judge: overall pass | {jp}/{len(done)} |")
    lines += ["", "## By scenario", ""]
    for r in rows:
        if r["status"] != "ok":
            lines.append(f"- **{r['scenario']}**: {r['status']} — "
                         f"{r.get('reason') or r.get('error')}")
            continue
        failed = [f"{g['name']} ({g['detail']})" for g in r["grades"] if not g["passed"]]
        verdict = "PASS" if r["code_pass"] and r.get("judge_pass", True) else "**FAIL**"
        lines.append(f"- **{r['scenario']}** {verdict} — {r['description']} "
                     f"(${r['cost_usd']:.4f})")
        if failed:
            lines.append(f"  - code checks failed: {'; '.join(failed)}")
        if judge and "judge_scores" in r:
            lines.append("  - judge: " + ", ".join(f"{k} {v}" for k, v in r["judge_scores"].items()))
            for issue in r["judge_issues"][:5]:
                lines.append(f"  - issue: {issue}")
    return "\n".join(lines) + "\n"


# ---------- feedback ----------


def run_feedback(out_dir: Path) -> bool:
    s = summarize(load_feedback())
    md = to_markdown(s)
    base = _write("feedback", s, md, out_dir)
    print(md)
    print(f"Wrote {base}.json/.md")
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="kind", required=True)
    sub.add_parser("math", help="golden cases, invariants, LP cross-check, backtest")
    rp = sub.add_parser("report", help="generate and grade AI reports (calls Claude)")
    rp.add_argument("--no-judge", action="store_true", help="code graders only (cheaper)")
    rp.add_argument("--max-spend", type=float, default=EVAL_SPEND_CAP_USD,
                    help=f"stop before spending more than this many USD "
                         f"(default {EVAL_SPEND_CAP_USD:.2f})")
    rp.add_argument("--scenario", action="append", choices=[s.id for s in SCENARIOS],
                    help="run only these scenarios (repeatable)")
    sub.add_parser("feedback", help="summarize in-app feedback and outcomes")
    p.add_argument("--out", type=Path, default=EVAL_RESULTS_DIR, help="results directory")
    args = p.parse_args(argv)

    if args.kind == "math":
        ok = run_math(args.out)
    elif args.kind == "report":
        chosen = [s for s in SCENARIOS if not args.scenario or s.id in args.scenario]
        ok = run_report(chosen, not args.no_judge, args.max_spend, args.out)
    else:
        ok = run_feedback(args.out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
