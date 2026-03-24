"""Overcommitment detector — scores current load across all dimensions."""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import FINANCE_DIR, GOALS_DIR, TIMEZONE
from synthesis.aggregator import DomainSnapshot

log = logging.getLogger("khalil.synthesis.capacity")


@dataclass
class CapacityReport:
    """Output of overcommitment detection."""
    capacity_score: int = 0  # 0-100, higher = more loaded. >80 = overcommitted
    risk_areas: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    breakdown: dict[str, int] = field(default_factory=dict)  # dimension -> sub-score


# --- Scoring constants ---
# Each dimension contributes up to its max to the total score (sum of maxes = 100)

_WORK_MAX = 35
_PROJECTS_MAX = 20
_PERSONAL_MAX = 25
_ADMIN_MAX = 20


def _score_work(snapshot: DomainSnapshot) -> tuple[int, list[str], list[str]]:
    """Score work load: meetings, P0s, blocked items.

    Returns (score, risks, recommendations).
    """
    w = snapshot.work
    score = 0
    risks = []
    recs = []

    # Meeting density: >6 meetings = high load
    if w.meeting_count_today >= 8:
        score += 15
        risks.append(f"{w.meeting_count_today} meetings today — no room to think")
        recs.append("Decline or reschedule at least 2 meetings to protect deep work time")
    elif w.meeting_count_today >= 5:
        score += 10
        risks.append(f"{w.meeting_count_today} meetings today — tight schedule")
    elif w.meeting_count_today >= 3:
        score += 5

    # P0 count
    if w.p0_count >= 4:
        score += 12
        risks.append(f"{w.p0_count} active P0s — everything is a priority means nothing is")
        recs.append("Force-rank P0s: pick the top 2 and explicitly deprioritize the rest")
    elif w.p0_count >= 2:
        score += 6
    elif w.p0_count >= 1:
        score += 3

    # Blocked items
    if w.blocked_count >= 3:
        score += 8
        risks.append(f"{w.blocked_count} blocked epics — unresolved dependencies")
        recs.append("Schedule a 15-min unblock session for each blocked epic this week")
    elif w.blocked_count >= 1:
        score += 4
        risks.append(f"{w.blocked_count} blocked epic(s)")

    return min(score, _WORK_MAX), risks, recs


def _score_projects(snapshot: DomainSnapshot) -> tuple[int, list[str], list[str]]:
    """Score project load: active milestones and staleness.

    Returns (score, risks, recommendations).
    """
    score = 0
    risks = []
    recs = []

    active_projects = 0
    stale_projects = []

    for key, p in snapshot.projects.items():
        if p.open_task_count > 0:
            active_projects += 1

        if p.risk_level == "red":
            score += 6
            stale_projects.append(p.name)
        elif p.risk_level == "yellow":
            score += 3

    # More than 2 active side projects = spreading thin
    if active_projects >= 3:
        score += 5
        risks.append(f"{active_projects} active side projects — spreading too thin")
        recs.append("Pick 1 project to focus on this week, pause the others")
    elif active_projects >= 2:
        score += 2

    if stale_projects:
        risks.append(f"Stale projects: {', '.join(stale_projects)}")
        recs.append(f"Either make progress on {stale_projects[0]} or consciously shelve it")

    return min(score, _PROJECTS_MAX), risks, recs


def _score_personal(snapshot: DomainSnapshot) -> tuple[int, list[str], list[str]]:
    """Score personal capacity: calendar gaps and deep work time.

    Returns (score, risks, recommendations).
    """
    h = snapshot.health
    score = 0
    risks = []
    recs = []

    # Deep work time
    if h.deep_work_hours_available < 1.0:
        score += 15
        risks.append(f"Only {h.deep_work_hours_available}h deep work time today")
        recs.append("Block 2 hours tomorrow morning for uninterrupted work")
    elif h.deep_work_hours_available < 2.0:
        score += 8
        risks.append(f"Low deep work: {h.deep_work_hours_available}h")
    elif h.deep_work_hours_available < 3.0:
        score += 4

    # Overdue personal items
    if h.overdue_personal_items >= 5:
        score += 10
        risks.append(f"{h.overdue_personal_items} overdue personal items piling up")
        recs.append("Spend 30 minutes clearing overdue reminders — triage or delete")
    elif h.overdue_personal_items >= 2:
        score += 5
        risks.append(f"{h.overdue_personal_items} overdue items")

    return min(score, _PERSONAL_MAX), risks, recs


def _score_admin(snapshot: DomainSnapshot) -> tuple[int, list[str], list[str]]:
    """Score admin overhead: overdue reviews (portfolio, goals, tax deadlines).

    Returns (score, risks, recommendations).
    """
    score = 0
    risks = []
    recs = []

    # Goals review staleness
    g = snapshot.goals
    if g.total_goals == 0:
        score += 5
        risks.append("No goals set for the quarter")
        recs.append("Spend 15 minutes setting 3-5 goals for the quarter")
    elif g.days_since_review > 30:
        score += 4
        risks.append(f"Goals not reviewed in {g.days_since_review} days")
        recs.append("Run /goals review to check progress")

    # Finance review
    f = snapshot.finance
    if f.passed_deadlines:
        score += 6
        risks.append(f"{len(f.passed_deadlines)} passed financial deadlines")
        recs.append(f"Address passed deadline: {f.passed_deadlines[0]}")
    if f.portfolio_age_days > 60:
        score += 3
        risks.append(f"Portfolio snapshot is {f.portfolio_age_days} days old")
        recs.append("Update portfolio snapshot this weekend")

    # Work planning staleness
    w = snapshot.work
    if w.days_since_review > 14:
        score += 3
        risks.append(f"Sprint planning data {w.days_since_review}d stale")

    return min(score, _ADMIN_MAX), risks, recs


async def detect_overcommitment(snapshot: DomainSnapshot) -> CapacityReport:
    """Analyze a DomainSnapshot and produce a capacity report.

    Score 0-100:
      0-40: comfortable
      41-60: busy but manageable
      61-80: heavy load, watch out
      81-100: overcommitted, action needed
    """
    report = CapacityReport()

    work_score, work_risks, work_recs = _score_work(snapshot)
    proj_score, proj_risks, proj_recs = _score_projects(snapshot)
    personal_score, personal_risks, personal_recs = _score_personal(snapshot)
    admin_score, admin_risks, admin_recs = _score_admin(snapshot)

    report.capacity_score = work_score + proj_score + personal_score + admin_score
    report.breakdown = {
        "work": work_score,
        "projects": proj_score,
        "personal": personal_score,
        "admin": admin_score,
    }

    report.risk_areas = work_risks + proj_risks + personal_risks + admin_risks
    report.recommendations = work_recs + proj_recs + personal_recs + admin_recs

    return report


def capacity_report_to_text(report: CapacityReport) -> str:
    """Format a CapacityReport as human-readable text."""
    # Score label
    score = report.capacity_score
    if score > 80:
        label = "OVERCOMMITTED"
    elif score > 60:
        label = "Heavy Load"
    elif score > 40:
        label = "Busy"
    else:
        label = "Comfortable"

    lines = [f"Capacity: {score}/100 ({label})\n"]

    # Breakdown
    lines.append("Breakdown:")
    for dim, sub_score in report.breakdown.items():
        bar_len = sub_score // 3  # rough visual bar
        bar = "#" * bar_len
        lines.append(f"  {dim:>10}: {sub_score:>2} {bar}")

    # Top risks
    if report.risk_areas:
        lines.append(f"\nRisks ({len(report.risk_areas)}):")
        for risk in report.risk_areas[:5]:
            lines.append(f"  - {risk}")

    # Top recommendations
    if report.recommendations:
        lines.append(f"\nRecommendations:")
        for rec in report.recommendations[:5]:
            lines.append(f"  > {rec}")

    return "\n".join(lines)
