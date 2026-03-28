"""Eval pipeline orchestrator and CLI entry point."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from eval.cases import TestCase, generate_cases, load_cases
from eval import runner
from eval import judge
from eval import gap_analysis
from eval.plan_gen import generate_plan, format_plan


EVAL_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVAL_DIR / "fixtures"
REPORTS_DIR = EVAL_DIR / "reports"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(
    cases_path: str | None = None,
    output_dir: str = "eval/reports",
    run_llm_judge: bool = False,
    max_cases: int | None = None,
) -> dict:
    """Full pipeline: generate/load -> run -> evaluate -> gap -> plan."""

    # 1. Generate or load cases
    if cases_path:
        cases = load_cases(cases_path)
    else:
        cases = generate_cases()

    # 2. Truncate if max_cases set
    if max_cases is not None:
        cases = cases[:max_cases]

    # 3. Init server
    server_mod = await runner.init_server()

    # 4. Run suite
    results = await runner.run_suite(cases, server_mod)

    # 5. Evaluate each pair
    evals = []
    for case, result in zip(cases, results):
        # Downgrade llm_judge to heuristic if --llm-judge not set
        if not run_llm_judge and case.eval_strategy == "llm_judge":
            case = TestCase(**{**vars(case), "eval_strategy": "heuristic"})
        ev = await judge.evaluate(case, result)
        evals.append(ev)

    # 6. Gap analysis
    report = gap_analysis.analyze(cases, results, evals)

    # 7. Plan
    plan = generate_plan(report, cases)

    # 8. Save results
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"{timestamp}.json"

    report_data = {
        "timestamp": timestamp,
        "total_cases": len(cases),
        "passed": report.passed,
        "failed": report.failed,
        "pass_rate": report.passed / max(len(cases), 1),
        "gaps": [vars(g) for g in report.gaps],
        "plan": {
            "total_gaps": plan.total_gaps,
            "auto_fixable_count": plan.auto_fixable_count,
            "estimated_pass_rate_after": plan.estimated_pass_rate_after,
            "tasks": [vars(t) for t in plan.tasks],
        },
    }
    report_path.write_text(json.dumps(report_data, indent=2, default=str))

    # 9. Print summary and plan
    print(f"\n{'=' * 60}")
    print(f"EVAL RESULTS — {timestamp}")
    print(f"  Cases: {len(cases)}  Passed: {report.passed}  Failed: {report.failed}")
    print(f"  Pass rate: {report.passed / max(len(cases), 1):.1%}")
    print(f"{'=' * 60}")
    print(format_plan(plan))
    print(f"Report saved: {report_path}")

    # 10. Return summary
    return {
        "pass_rate": report.passed / max(len(cases), 1),
        "gaps": len(report.gaps),
        "plan_path": str(report_path),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_last_report() -> None:
    """Load and print the most recent report from the reports directory."""
    if not REPORTS_DIR.exists():
        print("No reports directory found.")
        return
    reports = sorted(REPORTS_DIR.glob("*.json"))
    if not reports:
        print("No reports found.")
        return
    latest = reports[-1]
    data = json.loads(latest.read_text())
    print(json.dumps(data, indent=2))


def main() -> None:
    args = sys.argv[1:]

    if "--report" in args:
        _print_last_report()
        return

    if "--generate" in args:
        cases = generate_cases()
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        out = FIXTURES_DIR / "cases.json"
        out.write_text(json.dumps([vars(c) for c in cases], indent=2, default=str))
        print(f"Generated {len(cases)} cases -> {out}")
        return

    # Build pipeline kwargs
    kwargs: dict = {}

    if "--run-only" in args:
        default_cases = FIXTURES_DIR / "cases.json"
        if not default_cases.exists():
            print(f"No cases file found at {default_cases}. Run with --generate first.")
            sys.exit(1)
        kwargs["cases_path"] = str(default_cases)

    if "--llm-judge" in args:
        kwargs["run_llm_judge"] = True

    if "--max" in args:
        idx = args.index("--max")
        if idx + 1 < len(args):
            kwargs["max_cases"] = int(args[idx + 1])
        else:
            print("--max requires a number argument.")
            sys.exit(1)

    result = asyncio.run(run_pipeline(**kwargs))
    print(f"\nDone. Pass rate: {result['pass_rate']:.1%}  Gaps: {result['gaps']}")


if __name__ == "__main__":
    main()
