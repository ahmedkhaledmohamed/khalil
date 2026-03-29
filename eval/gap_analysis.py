"""Failure categorization engine for PharoClaw's eval pipeline.

Classifies eval failures into actionable gap categories and produces
a structured report for prioritization and self-healing.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from eval.cases import TestCase
from eval.runner import TestResult
from eval.judge import EvalResult


# ---------------------------------------------------------------------------
# Regression tracking
# ---------------------------------------------------------------------------

@dataclass
class ReportDiff:
    regressions: list[str]      # case_ids that passed before but fail now
    fixes: list[str]            # case_ids that failed before but pass now
    new_cases: list[str]        # case_ids not in previous run
    removed_cases: list[str]    # case_ids no longer tested


def diff_reports(
    previous_path: str | None,
    current_cases: list[TestCase],
    current_evals: list[EvalResult],
) -> ReportDiff:
    """Compare current eval results against the most recent previous report."""
    current_passed_ids = {e.case_id for e in current_evals if e.passed}
    current_failed_ids = {e.case_id for e in current_evals if not e.passed}
    current_all_ids = current_passed_ids | current_failed_ids

    if previous_path is None:
        return ReportDiff([], [], list(current_all_ids), [])

    import json as _json
    with open(previous_path) as f:
        prev = _json.load(f)

    prev_all_ids = set(prev.get("all_case_ids", []))
    prev_passed_ids = set(prev.get("passed_case_ids", []))
    prev_failed_ids = {g["case_id"] for g in prev.get("gaps", [])}

    # If the previous report lacks all_case_ids, fall back to gaps only
    if not prev_all_ids:
        prev_all_ids = prev_failed_ids

    regressions = []
    for case_id in current_failed_ids:
        if case_id in prev_passed_ids or (case_id in prev_all_ids and case_id not in prev_failed_ids):
            regressions.append(case_id)

    fixes = []
    for case_id in current_passed_ids:
        if case_id in prev_failed_ids:
            fixes.append(case_id)

    new_cases = [cid for cid in current_all_ids if cid not in prev_all_ids]
    removed = [cid for cid in prev_all_ids if cid not in current_all_ids]

    return ReportDiff(regressions, fixes, new_cases, removed)


# ---------------------------------------------------------------------------
# Gap taxonomy
# ---------------------------------------------------------------------------

class GapCategory(str, Enum):
    PATTERN_GAP = "pattern_gap"
    ROUTING_WRONG = "routing_wrong"
    HANDLER_ERROR = "handler_error"
    HANDLER_BAD_OUTPUT = "handler_bad_output"
    MISSING_SKILL = "missing_skill"
    LLM_QUALITY_LOW = "llm_quality_low"
    SERVICE_UNAVAIL = "service_unavailable"
    SAFETY_LEAK = "safety_leak"
    TIMEOUT = "timeout"


# ---------------------------------------------------------------------------
# Gap & report schemas
# ---------------------------------------------------------------------------

@dataclass
class Gap:
    case_id: str
    category: GapCategory
    detail: str
    affected_skill: str | None  # skill name if identifiable


@dataclass
class GapReport:
    total_cases: int
    passed: int
    failed: int
    pass_rate: float
    gaps: list[Gap]
    by_category: dict[str, int]         # category -> count
    by_skill: dict[str, int]            # skill -> failure count
    top_gaps: list[tuple[str, int]]     # (category, count) sorted desc


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_failure(
    case: TestCase,
    result: TestResult,
    eval_result: EvalResult,
) -> GapCategory:
    """Classify a failed eval into a gap category using a decision tree."""
    error = result.error or ""

    # 1. Timeout
    if "Timeout" in error or "TIMEOUT" in error:
        return GapCategory.TIMEOUT

    # 2. Import / module errors in handler
    if "ImportError" in error or "ModuleNotFoundError" in error:
        return GapCategory.HANDLER_ERROR

    # 3. Safety leak flagged by deterministic eval
    if eval_result.gap_hint == "safety_leak":
        return GapCategory.SAFETY_LEAK

    # 4. Query should have matched a skill but fell through to LLM
    if case.expected_action and result.pipeline_path == "conversational":
        return GapCategory.PATTERN_GAP

    # 5. Matched a different action than expected
    if case.expected_action and result.pipeline_path not in (
        None, "error", "conversational", case.expected_action,
    ):
        return GapCategory.ROUTING_WRONG

    # 6. Error during the correct handler
    if result.error and case.expected_action and result.pipeline_path == case.expected_action:
        return GapCategory.HANDLER_ERROR

    # 7. LLM quality low
    if eval_result.scores and eval_result.scores.get("overall", 0) < 3:
        return GapCategory.LLM_QUALITY_LOW

    # 8. Service / auth errors
    error_lower = error.lower()
    if any(tok in error_lower for tok in ("token", "oauth", "403")):
        return GapCategory.SERVICE_UNAVAIL

    # 9. Default
    return GapCategory.HANDLER_BAD_OUTPUT


def _infer_skill(case: TestCase, result: TestResult) -> str | None:
    """Best-effort extraction of affected skill name."""
    if case.expected_action:
        return case.expected_action
    if result.pipeline_path and result.pipeline_path not in ("error", "conversational"):
        return result.pipeline_path
    return None


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze(
    cases: list[TestCase],
    results: list[TestResult],
    evals: list[EvalResult],
) -> GapReport:
    """Produce a gap report from parallel lists of cases, results, and evals."""
    assert len(cases) == len(results) == len(evals), (
        f"Mismatched lengths: {len(cases)} cases, {len(results)} results, {len(evals)} evals"
    )

    gaps: list[Gap] = []
    passed = 0
    failed = 0
    category_counter: Counter[str] = Counter()
    skill_counter: Counter[str] = Counter()

    for case, result, eval_result in zip(cases, results, evals):
        if eval_result.passed:
            passed += 1
            continue

        failed += 1
        category = classify_failure(case, result, eval_result)
        skill = _infer_skill(case, result)

        gap = Gap(
            case_id=case.id,
            category=category,
            detail=_build_detail(case, result, eval_result, category),
            affected_skill=skill,
        )
        gaps.append(gap)
        category_counter[category.value] += 1
        if skill:
            skill_counter[skill] += 1

    total = len(cases)
    pass_rate = passed / total if total > 0 else 0.0

    top_gaps = sorted(category_counter.items(), key=lambda x: x[1], reverse=True)

    return GapReport(
        total_cases=total,
        passed=passed,
        failed=failed,
        pass_rate=pass_rate,
        gaps=gaps,
        by_category=dict(category_counter),
        by_skill=dict(skill_counter),
        top_gaps=top_gaps,
    )


def _build_detail(
    case: TestCase,
    result: TestResult,
    eval_result: EvalResult,
    category: GapCategory,
) -> str:
    """Build a human-readable detail string for a gap."""
    parts: list[str] = []

    if result.error:
        parts.append(f"error: {result.error[:200]}")

    failed_checks = [c for c in eval_result.checks if not c.passed]
    if failed_checks:
        names = ", ".join(c.name for c in failed_checks)
        parts.append(f"failed checks: {names}")

    if category == GapCategory.PATTERN_GAP:
        parts.append(f"expected action '{case.expected_action}' but got '{result.pipeline_path}'")
    elif category == GapCategory.ROUTING_WRONG:
        parts.append(f"expected '{case.expected_action}', routed to '{result.pipeline_path}'")
    elif category == GapCategory.LLM_QUALITY_LOW and eval_result.scores:
        parts.append(f"scores: {eval_result.scores}")

    return "; ".join(parts) if parts else "no additional detail"
