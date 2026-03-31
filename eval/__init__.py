"""Eval pipeline orchestrator and CLI entry point."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Heavy imports deferred to main() / run_pipeline() to allow
# lightweight subcommands (--shell-safety) without pulling in yaml, etc.


EVAL_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVAL_DIR / "fixtures"
REPORTS_DIR = EVAL_DIR / "reports"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_previous_report(reports_dir: Path, current_timestamp: str) -> str | None:
    """Find the most recent report before the current one."""
    reports = sorted(reports_dir.glob("*.json"))
    for r in reversed(reports):
        if r.stem != current_timestamp:
            return str(r)
    return None


def _print_trend(reports_dir: Path, n: int = 10) -> None:
    """Print pass rate trend across last N runs."""
    reports = sorted(reports_dir.glob("*.json"))[-n:]
    if not reports:
        print("No reports found.")
        return
    print(f"\nPass Rate Trend (last {len(reports)} runs):")
    print("-" * 50)
    for rp in reports:
        data = json.loads(rp.read_text())
        ts = data["timestamp"]
        rate = data.get("pass_rate", 0)
        total = data.get("total_cases", 0)
        bar = "\u2588" * int(rate * 30)
        print(f"  {ts}  {rate:5.1%}  ({total} cases) {bar}")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(
    cases_path: str | None = None,
    output_dir: str = "eval/reports",
    run_llm_judge: bool = False,
    max_cases: int | None = None,
    parallel: bool = False,
    cycle: int = 0,
) -> dict:
    """Full pipeline: generate/load -> run -> evaluate -> gap -> plan."""
    from eval.cases import TestCase, generate_cases, load_cases
    from eval import runner, judge, gap_analysis
    from eval.gap_analysis import diff_reports
    from eval.plan_gen import generate_plan, format_plan

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
    if parallel:
        results = await runner.run_suite_parallel(cases, server_mod)
    else:
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

    # 8. Regression tracking
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    previous_report = _find_previous_report(out_dir, timestamp)
    diff = diff_reports(previous_report, cases, evals)

    # Per-skill breakdown
    skill_stats: dict[str, dict] = {}
    for case, ev in zip(cases, evals):
        skill = case.expected_action or case.category
        if skill not in skill_stats:
            skill_stats[skill] = {"total": 0, "passed": 0}
        skill_stats[skill]["total"] += 1
        if ev.passed:
            skill_stats[skill]["passed"] += 1

    # 9. Save results
    report_path = out_dir / f"{timestamp}.json"

    report_data = {
        "timestamp": timestamp,
        "total_cases": len(cases),
        "passed": report.passed,
        "failed": report.failed,
        "pass_rate": report.passed / max(len(cases), 1),
        "all_case_ids": [c.id for c in cases],
        "passed_case_ids": [e.case_id for e in evals if e.passed],
        "gaps": [vars(g) for g in report.gaps],
        "by_skill": {k: v for k, v in skill_stats.items()},
        "regressions": diff.regressions,
        "fixes_since_last": diff.fixes,
        "plan": {
            "total_gaps": plan.total_gaps,
            "auto_fixable_count": plan.auto_fixable_count,
            "estimated_pass_rate_after": plan.estimated_pass_rate_after,
            "tasks": [vars(t) for t in plan.tasks],
        },
    }
    report_path.write_text(json.dumps(report_data, indent=2, default=str))

    # 10. Print summary and plan
    print(f"\n{'=' * 60}")
    print(f"EVAL RESULTS — {timestamp}")
    print(f"  Cases: {len(cases)}  Passed: {report.passed}  Failed: {report.failed}")
    print(f"  Pass rate: {report.passed / max(len(cases), 1):.1%}")

    if diff.regressions:
        print(f"  REGRESSIONS: {len(diff.regressions)} cases regressed since last run")
    if diff.fixes:
        print(f"  FIXES: {len(diff.fixes)} cases fixed since last run")

    print(f"{'=' * 60}")

    # Per-skill breakdown
    print(f"\nPer-skill breakdown:")
    for skill, stats in sorted(skill_stats.items(), key=lambda x: x[1]["passed"] / max(x[1]["total"], 1)):
        rate = stats["passed"] / max(stats["total"], 1)
        print(f"  {skill:25s} {stats['passed']:4d}/{stats['total']:<4d} ({rate:.0%})")

    print(format_plan(plan))
    print(f"Report saved: {report_path}")

    # 11. Auto-fix cycle
    if cycle > 0:
        from eval.autofix import run_autofix_cycle, rollback_fix
        for iteration in range(cycle):
            print(f"\n{'='*60}")
            print(f"AUTO-FIX CYCLE {iteration + 1}/{cycle}")
            print(f"{'='*60}")

            attempts = await run_autofix_cycle(
                report, cases,
                confidence_threshold=0.6,
                dry_run=False,
            )

            applied = [a for a in attempts if a.applied]
            if not applied:
                print("  No fixes applied. Stopping cycle.")
                break

            # Re-run only to check for regressions
            print(f"\n  Re-running eval to check for regressions...")
            re_results = await runner.run_suite(cases, server_mod)
            re_evals = []
            for case, result in zip(cases, re_results):
                if not run_llm_judge and case.eval_strategy == "llm_judge":
                    case = TestCase(**{**vars(case), "eval_strategy": "heuristic"})
                ev = await judge.evaluate(case, result)
                re_evals.append(ev)

            re_report = gap_analysis.analyze(cases, re_results, re_evals)

            new_pass_rate = re_report.passed / max(len(cases), 1)
            delta = new_pass_rate - (report.passed / max(len(cases), 1))

            print(f"  Pass rate: {new_pass_rate:.1%} (delta: {delta:+.1%})")

            if new_pass_rate < (report.passed / max(len(cases), 1)):
                print("  REGRESSION DETECTED — rolling back fixes")
                for a in applied:
                    rollback_fix(a)
                break

            report = re_report  # Use new report for next cycle

    # 12. Return summary
    return {
        "pass_rate": report.passed / max(len(cases), 1),
        "gaps": len(report.gaps),
        "plan_path": str(report_path),
        "regressions": diff.regressions,
        "fixes": diff.fixes,
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


def _print_coverage() -> None:
    """Show action type coverage: which skills have golden/generated test cases."""
    sys.path.insert(0, str(EVAL_DIR.parent))
    from skills import get_registry
    from eval.cases import load_golden_cases

    registry = get_registry()

    # All action types from registry
    all_actions: dict[str, str] = {}  # action_type -> skill_name
    for skill in registry.list_skills():
        for action_type in skill.actions:
            all_actions[action_type] = skill.name

    # Golden cases coverage
    golden = load_golden_cases()
    golden_actions = {c.expected_action for c in golden if c.expected_action}

    # Generated cases (from patterns)
    generated_actions: set[str] = set()
    for skill in registry.list_skills():
        for _, action_type in skill.patterns:
            generated_actions.add(action_type)

    covered = golden_actions | generated_actions
    uncovered = set(all_actions.keys()) - covered

    total = len(all_actions)
    print(f"\nAction Type Coverage")
    print(f"{'=' * 60}")
    print(f"  Total action types: {total}")
    print(f"  Golden cases:       {len(golden_actions)} ({len(golden_actions)/total*100:.0f}%)")
    print(f"  Generated (pattern):{len(generated_actions)} ({len(generated_actions)/total*100:.0f}%)")
    print(f"  Any coverage:       {len(covered)} ({len(covered)/total*100:.0f}%)")
    print(f"  Uncovered:          {len(uncovered)}")

    if uncovered:
        print(f"\nUncovered action types ({len(uncovered)}):")
        for action in sorted(uncovered):
            print(f"  - {action} (skill: {all_actions[action]})")

    # Per-skill summary
    print(f"\nPer-skill golden case count:")
    skill_golden: dict[str, int] = {}
    for c in golden:
        if c.expected_action and c.expected_action in all_actions:
            skill = all_actions[c.expected_action]
            skill_golden[skill] = skill_golden.get(skill, 0) + 1

    for skill in sorted(registry.list_skills(), key=lambda s: s.name):
        count = skill_golden.get(skill.name, 0)
        action_count = len(skill.actions)
        marker = "\u2714" if count > 0 else "\u2718"
        print(f"  {marker} {skill.name:25s} {count:3d} golden / {action_count} actions")


def main() -> None:
    args = sys.argv[1:]

    if "--coverage" in args:
        _print_coverage()
        return

    if "--report" in args:
        _print_last_report()
        return

    if "--trend" in args:
        n = 10
        idx = args.index("--trend")
        if idx + 1 < len(args) and args[idx + 1].isdigit():
            n = int(args[idx + 1])
        _print_trend(REPORTS_DIR, n)
        return

    if "--generate" in args:
        from eval.cases import generate_cases
        cases = generate_cases()
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        out = FIXTURES_DIR / "cases.json"
        out.write_text(json.dumps([vars(c) for c in cases], indent=2, default=str))
        print(f"Generated {len(cases)} cases -> {out}")
        return

    # Build pipeline kwargs
    kwargs: dict = {}

    if "--cases" in args:
        idx = args.index("--cases")
        if idx + 1 < len(args):
            kwargs["cases_path"] = args[idx + 1]
        else:
            print("--cases requires a file path argument.")
            sys.exit(1)
    elif "--run-only" in args:
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

    if "--parallel" in args:
        kwargs["parallel"] = True

    if "--cycle" in args:
        idx = args.index("--cycle")
        if idx + 1 < len(args):
            kwargs["cycle"] = int(args[idx + 1])
        else:
            kwargs["cycle"] = 1

    result = asyncio.run(run_pipeline(**kwargs))
    print(f"\nDone. Pass rate: {result['pass_rate']:.1%}  Gaps: {result['gaps']}")


if __name__ == "__main__":
    main()
