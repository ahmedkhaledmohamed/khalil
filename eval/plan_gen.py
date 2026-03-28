"""Improvement task generator — converts gap analysis into a prioritized action plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections import defaultdict

from eval.gap_analysis import Gap, GapReport
from eval.cases import TestCase


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ImprovementTask:
    id: str                 # "task-001"
    title: str              # "Add weather pattern for 'will it rain'"
    task_type: str          # "add_pattern" | "fix_handler" | "add_skill" | "fix_auth" | "improve_prompt"
    priority: int           # 1=critical, 2=high, 3=medium, 4=low
    target_file: str        # "actions/weather.py"
    description: str        # specific change description
    affected_cases: int     # count of test cases this would fix
    auto_fixable: bool      # safe to apply without human review
    gap_category: str       # from GapCategory


@dataclass
class ImprovementPlan:
    tasks: list[ImprovementTask]
    total_gaps: int
    auto_fixable_count: int
    estimated_pass_rate_after: float  # projected pass rate if all auto-fixable tasks applied
    generated_at: str                 # ISO timestamp


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------

_PRIORITY_LABELS = {1: "CRITICAL", 2: "HIGH", 3: "MEDIUM", 4: "LOW"}


def generate_plan(report: GapReport, cases: list[TestCase]) -> ImprovementPlan:
    """Convert a GapReport into a prioritised list of ImprovementTasks."""

    tasks: list[ImprovementTask] = []
    task_counter = 0

    # Group gaps by category value for batch processing
    by_category: dict[str, list[Gap]] = defaultdict(list)
    for gap in report.gaps:
        key = gap.category.value if hasattr(gap.category, "value") else str(gap.category)
        by_category[key].append(gap)

    # --- PATTERN_GAP: group by affected_skill, one task per skill -----------
    pattern_gaps = by_category.get("pattern_gap", [])
    by_skill: dict[str, list[Gap]] = defaultdict(list)
    for g in pattern_gaps:
        skill = getattr(g, "affected_skill", "unknown")
        by_skill[skill].append(g)

    for skill, gaps in by_skill.items():
        task_counter += 1
        queries = [getattr(g, "query", g.detail) for g in gaps]
        tasks.append(ImprovementTask(
            id=f"task-{task_counter:03d}",
            title=f"Add {len(gaps)} patterns to {skill} SKILL dict",
            task_type="add_pattern",
            priority=2,
            target_file=f"actions/{skill}.py",
            description="Failing queries that need new patterns:\n"
                        + "\n".join(f"  - {q}" for q in queries),
            affected_cases=len(gaps),
            auto_fixable=True,
            gap_category="PATTERN_GAP",
        ))

    # --- HANDLER_ERROR ------------------------------------------------------
    for g in by_category.get("handler_error", []):
        task_counter += 1
        skill = getattr(g, "affected_skill", "unknown")
        error_summary = g.detail[:80] if g.detail else "unknown error"
        tasks.append(ImprovementTask(
            id=f"task-{task_counter:03d}",
            title=f"Fix handler error in {skill}: {error_summary}",
            task_type="fix_handler",
            priority=2,
            target_file=f"actions/{skill}.py",
            description=g.detail,
            affected_cases=1,
            auto_fixable=False,
            gap_category="HANDLER_ERROR",
        ))

    # --- ROUTING_WRONG ------------------------------------------------------
    for g in by_category.get("routing_wrong", []):
        task_counter += 1
        query = getattr(g, "query", "?")
        wrong = getattr(g, "actual_action", "?")
        expected = getattr(g, "expected_action", "?")
        tasks.append(ImprovementTask(
            id=f"task-{task_counter:03d}",
            title=f"Fix routing conflict: {query} hits {wrong} instead of {expected}",
            task_type="improve_prompt",
            priority=3,
            target_file="core/router.py",
            description=g.detail,
            affected_cases=1,
            auto_fixable=False,
            gap_category="ROUTING_WRONG",
        ))

    # --- SAFETY_LEAK --------------------------------------------------------
    for g in by_category.get("safety_leak", []):
        task_counter += 1
        tasks.append(ImprovementTask(
            id=f"task-{task_counter:03d}",
            title=f"CRITICAL: Safety leak in {g.detail[:60]}",
            task_type="fix_handler",
            priority=1,
            target_file="core/safety.py",
            description=g.detail,
            affected_cases=1,
            auto_fixable=False,
            gap_category="SAFETY_LEAK",
        ))

    # --- MISSING_SKILL ------------------------------------------------------
    for g in by_category.get("missing_skill", []):
        task_counter += 1
        category = getattr(g, "affected_skill", g.detail[:40])
        tasks.append(ImprovementTask(
            id=f"task-{task_counter:03d}",
            title=f"Add new skill for {category}",
            task_type="add_skill",
            priority=4,
            target_file="actions/",
            description=g.detail,
            affected_cases=1,
            auto_fixable=False,
            gap_category="MISSING_SKILL",
        ))

    # --- LLM_QUALITY_LOW ----------------------------------------------------
    for g in by_category.get("llm_quality_low", []):
        task_counter += 1
        tasks.append(ImprovementTask(
            id=f"task-{task_counter:03d}",
            title=f"Improve LLM prompt quality: {g.detail[:50]}",
            task_type="improve_prompt",
            priority=4,
            target_file="core/llm.py",
            description=g.detail,
            affected_cases=1,
            auto_fixable=False,
            gap_category="LLM_QUALITY_LOW",
        ))

    # --- SERVICE_UNAVAIL ----------------------------------------------------
    for g in by_category.get("service_unavailable", []):
        task_counter += 1
        tasks.append(ImprovementTask(
            id=f"task-{task_counter:03d}",
            title=f"Fix service auth/availability: {g.detail[:50]}",
            task_type="fix_auth",
            priority=3,
            target_file="core/config.py",
            description=g.detail,
            affected_cases=1,
            auto_fixable=False,
            gap_category="SERVICE_UNAVAIL",
        ))

    # --- TIMEOUT: group by affected_skill -----------------------------------
    timeout_gaps = by_category.get("timeout", [])
    timeout_by_skill: dict[str, list[Gap]] = defaultdict(list)
    for g in timeout_gaps:
        skill = getattr(g, "affected_skill", None) or "unknown"
        timeout_by_skill[skill].append(g)

    for skill, gaps in timeout_by_skill.items():
        task_counter += 1
        tasks.append(ImprovementTask(
            id=f"task-{task_counter:03d}",
            title=f"Fix timeout in {skill} handler ({len(gaps)} cases)",
            task_type="fix_handler",
            priority=3,
            target_file=f"actions/{skill}.py",
            description=f"{len(gaps)} cases timing out. Check handler performance or increase timeout.",
            affected_cases=len(gaps),
            auto_fixable=False,
            gap_category="TIMEOUT",
        ))

    # Sort: priority ASC, then affected_cases DESC
    tasks.sort(key=lambda t: (t.priority, -t.affected_cases))

    # Metrics
    auto_fixable_count = sum(1 for t in tasks if t.auto_fixable)
    auto_fixable_cases = sum(t.affected_cases for t in tasks if t.auto_fixable)
    total = report.total_cases if report.total_cases > 0 else 1
    estimated_pass_rate = (report.passed + auto_fixable_cases) / total

    return ImprovementPlan(
        tasks=tasks,
        total_gaps=len(report.gaps),
        auto_fixable_count=auto_fixable_count,
        estimated_pass_rate_after=min(estimated_pass_rate, 1.0),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_plan(plan: ImprovementPlan) -> str:
    """Human-readable plan output for terminal."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("IMPROVEMENT PLAN")
    lines.append(f"  Total gaps: {plan.total_gaps}")
    lines.append(f"  Auto-fixable tasks: {plan.auto_fixable_count}")
    lines.append(f"  Projected pass rate after auto-fix: {plan.estimated_pass_rate_after:.1%}")
    lines.append(f"  Generated: {plan.generated_at}")
    lines.append("=" * 60)

    if not plan.tasks:
        lines.append("\n  No improvement tasks generated.\n")
        return "\n".join(lines)

    current_priority = None
    for i, task in enumerate(plan.tasks, 1):
        if task.priority != current_priority:
            current_priority = task.priority
            label = _PRIORITY_LABELS.get(current_priority, f"P{current_priority}")
            lines.append(f"\n--- {label} (P{current_priority}) ---")

        auto_tag = " [AUTO]" if task.auto_fixable else ""
        lines.append(f"  {i}. [{task.id}] {task.title}{auto_tag}")
        lines.append(f"     type={task.task_type}  file={task.target_file}  cases={task.affected_cases}")

    lines.append("")
    return "\n".join(lines)
