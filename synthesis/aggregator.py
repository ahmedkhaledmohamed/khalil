"""Domain status aggregator — single call returns structured snapshot of all life domains."""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import FINANCE_DIR, GOALS_DIR, PROJECTS_DIR, TIMEZONE, WORK_DIR

log = logging.getLogger("khalil.synthesis.aggregator")


# --- Domain status dataclasses ---

@dataclass
class DomainStatus:
    """Base fields shared by all domain statuses."""
    risk_level: str = "green"  # green / yellow / red
    items_at_risk: list[str] = field(default_factory=list)
    days_since_review: int = 0
    open_blockers: list[str] = field(default_factory=list)
    upcoming_deadlines: list[str] = field(default_factory=list)


@dataclass
class WorkStatus(DomainStatus):
    p0_count: int = 0
    in_progress_count: int = 0
    blocked_count: int = 0
    meeting_count_today: int = 0
    sprint_summary: str = ""


@dataclass
class ProjectStatus(DomainStatus):
    name: str = ""
    open_task_count: int = 0
    days_since_update: int = 0


@dataclass
class FinanceStatus(DomainStatus):
    passed_deadlines: list[str] = field(default_factory=list)
    upcoming_soon: list[str] = field(default_factory=list)
    portfolio_age_days: int = 0


@dataclass
class GoalStatus(DomainStatus):
    quarter: str = ""
    total_goals: int = 0
    completed_goals: int = 0
    completion_pct: int = 0


@dataclass
class HealthSignals(DomainStatus):
    """Inferred health signals — calendar gaps, overdue personal items."""
    deep_work_hours_available: float = 0.0
    calendar_gap_pct: float = 0.0  # % of work hours free
    overdue_personal_items: int = 0


@dataclass
class NutritionStatus(DomainStatus):
    """Daily nutrition from calorie tracker."""
    calories_today: int = 0
    calorie_goal: int = 0
    protein_g: int = 0
    protein_goal: int = 0
    meals_logged: int = 0


@dataclass
class FastingStatus(DomainStatus):
    """Current fasting state from fasting tracker."""
    active: bool = False
    elapsed_hours: float = 0.0
    target_hours: float = 0.0
    protocol: str = ""


@dataclass
class FocusStatus(DomainStatus):
    """Daily focus/productivity from pomodoro tracker."""
    sessions_today: int = 0
    total_minutes: int = 0
    completed: int = 0


@dataclass
class DomainSnapshot:
    """Full cross-domain snapshot."""
    timestamp: str = ""
    work: WorkStatus = field(default_factory=WorkStatus)
    projects: dict[str, ProjectStatus] = field(default_factory=dict)
    finance: FinanceStatus = field(default_factory=FinanceStatus)
    goals: GoalStatus = field(default_factory=GoalStatus)
    health: HealthSignals = field(default_factory=HealthSignals)
    nutrition: NutritionStatus = field(default_factory=NutritionStatus)
    fasting: FastingStatus = field(default_factory=FastingStatus)
    focus: FocusStatus = field(default_factory=FocusStatus)


# --- Aggregation helpers ---

def _aggregate_work() -> WorkStatus:
    """Pull work status from sprint planning data and calendar."""
    status = WorkStatus()
    try:
        from actions.work import _load_epics, get_p0_epics, get_in_progress

        epics = _load_epics()
        p0s = [e for e in epics if "P0" in (e["priority"] or "").upper()]
        in_progress = [e for e in epics if "in progress" in (e["status"] or "").lower()]
        blocked = [e for e in epics if "blocked" in (e["status"] or "").lower()]

        status.p0_count = len(p0s)
        status.in_progress_count = len(in_progress)
        status.blocked_count = len(blocked)

        for e in blocked:
            status.open_blockers.append(e["description"][:80])
        for e in p0s:
            if "in progress" not in (e["status"] or "").lower():
                status.items_at_risk.append(f"P0 not started: {e['description'][:60]}")

        # Planning CSV mtime as "last review"
        from actions.work import PLANNING_CSV
        if PLANNING_CSV.exists():
            mtime = datetime.fromtimestamp(PLANNING_CSV.stat().st_mtime, tz=ZoneInfo(TIMEZONE))
            status.days_since_review = (datetime.now(ZoneInfo(TIMEZONE)) - mtime).days

        # Risk level
        if status.blocked_count > 0 or status.p0_count > 3:
            status.risk_level = "red"
        elif status.p0_count > 1 or status.days_since_review > 14:
            status.risk_level = "yellow"

    except Exception as e:
        log.debug("Work aggregation failed: %s", e)

    return status


def _aggregate_work_calendar(status: WorkStatus) -> WorkStatus:
    """Enrich work status with today's calendar data (async helper called separately)."""
    # This is called from the async aggregate function
    return status


async def _get_meeting_count() -> int:
    """Get today's meeting count from calendar."""
    try:
        from actions.calendar import get_today_events
        events = await get_today_events()
        return len(events) if events else 0
    except Exception:
        return 0


async def _get_calendar_gaps() -> tuple[float, float]:
    """Calculate deep work hours and calendar gap percentage for today.

    Returns (deep_work_hours, gap_pct) where gap_pct is 0.0-1.0.
    """
    try:
        from actions.calendar import get_today_events
        events = await get_today_events()
        if not events:
            return 8.0, 1.0  # Full day free

        work_hours = 8.0  # Assume 9-5
        meeting_minutes = 0
        for event in events:
            start = event.get("start", {})
            end = event.get("end", {})
            start_dt = start.get("dateTime", "")
            end_dt = end.get("dateTime", "")
            if start_dt and end_dt:
                try:
                    s = datetime.fromisoformat(start_dt)
                    e = datetime.fromisoformat(end_dt)
                    meeting_minutes += (e - s).total_seconds() / 60
                except (ValueError, TypeError):
                    meeting_minutes += 30  # assume 30 min default
            else:
                meeting_minutes += 30

        meeting_hours = meeting_minutes / 60
        deep_work = max(0.0, work_hours - meeting_hours)
        gap_pct = deep_work / work_hours if work_hours > 0 else 0.0
        return round(deep_work, 1), round(gap_pct, 2)
    except Exception:
        return 0.0, 0.0


def _aggregate_projects() -> dict[str, ProjectStatus]:
    """Pull status for all known projects."""
    projects: dict[str, ProjectStatus] = {}
    try:
        from actions.projects import KNOWN_PROJECTS, get_open_tasks

        now = datetime.now(ZoneInfo(TIMEZONE))
        for key, info in KNOWN_PROJECTS.items():
            ps = ProjectStatus(name=info["name"])
            filepath = info["file"]

            if not filepath.exists():
                ps.risk_level = "yellow"
                ps.items_at_risk.append("Project file missing")
                projects[key] = ps
                continue

            tasks = get_open_tasks(key)
            ps.open_task_count = len(tasks)

            mtime = datetime.fromtimestamp(filepath.stat().st_mtime, tz=ZoneInfo(TIMEZONE))
            ps.days_since_update = (now - mtime).days
            ps.days_since_review = ps.days_since_update

            # Risk: open tasks + stale
            if ps.open_task_count > 0 and ps.days_since_update > 30:
                ps.risk_level = "red"
                ps.items_at_risk.append(f"{ps.open_task_count} open tasks, untouched {ps.days_since_update}d")
            elif ps.open_task_count > 0 and ps.days_since_update > 14:
                ps.risk_level = "yellow"
                ps.items_at_risk.append(f"{ps.open_task_count} tasks, {ps.days_since_update}d stale")

            projects[key] = ps

    except Exception as e:
        log.debug("Project aggregation failed: %s", e)

    return projects


def _aggregate_finance() -> FinanceStatus:
    """Pull financial status from deadlines and portfolio age."""
    status = FinanceStatus()
    try:
        from actions.finance import get_deadlines

        deadlines = get_deadlines()
        for d in deadlines:
            if d["status"] == "PASSED":
                status.passed_deadlines.append(d["item"])
                status.items_at_risk.append(f"Passed: {d['item']}")
            elif d["status"] == "SOON":
                status.upcoming_soon.append(f"{d['item']} ({d['date']})")
                status.upcoming_deadlines.append(f"{d['item']} in {d['days_away']}d")

        # Portfolio age
        if FINANCE_DIR.exists():
            portfolio_files = sorted(FINANCE_DIR.glob("portfolio-*.md"), reverse=True)
            if portfolio_files:
                mtime = datetime.fromtimestamp(
                    portfolio_files[0].stat().st_mtime, tz=ZoneInfo(TIMEZONE)
                )
                status.portfolio_age_days = (datetime.now(ZoneInfo(TIMEZONE)) - mtime).days
                status.days_since_review = status.portfolio_age_days
                if status.portfolio_age_days > 60:
                    status.items_at_risk.append(f"Portfolio snapshot {status.portfolio_age_days}d old")

        # Risk level
        if status.passed_deadlines:
            status.risk_level = "red"
        elif status.upcoming_soon or status.portfolio_age_days > 60:
            status.risk_level = "yellow"

    except Exception as e:
        log.debug("Finance aggregation failed: %s", e)

    # Enrich with expense tracker data
    try:
        from actions.expense_tracker import get_monthly_summary
        expenses = get_monthly_summary()
        total = expenses.get("total", 0)
        budgets = expenses.get("budgets", {})
        over_budget = []
        for cat, spent in expenses.get("by_category", {}).items():
            budget = budgets.get(cat, 0)
            if budget and spent > budget:
                over_budget.append(f"{cat}: ${spent:.0f}/${budget:.0f}")
        if over_budget:
            if status.risk_level == "green":
                status.risk_level = "yellow"
            status.items_at_risk.extend(f"Over budget: {item}" for item in over_budget[:3])
    except Exception as e:
        log.debug("Expense enrichment failed: %s", e)

    return status


def _aggregate_goals() -> GoalStatus:
    """Pull goal progress for the current quarter."""
    status = GoalStatus()
    try:
        from actions.goals import GOALS_FILE, _parse_goals, _current_quarter

        status.quarter = _current_quarter()

        if not GOALS_FILE.exists():
            status.risk_level = "red"
            status.items_at_risk.append("No goals file exists")
            return status

        content = GOALS_FILE.read_text(encoding="utf-8")
        goals = _parse_goals(content)
        q_goals = goals.get(status.quarter, {})

        status.total_goals = sum(len(items) for items in q_goals.values())
        status.completed_goals = sum(
            1 for items in q_goals.values() for item in items if item["done"]
        )
        status.completion_pct = (
            int(status.completed_goals / status.total_goals * 100)
            if status.total_goals > 0
            else 0
        )

        # Days since goals file was updated
        mtime = datetime.fromtimestamp(GOALS_FILE.stat().st_mtime, tz=ZoneInfo(TIMEZONE))
        status.days_since_review = (datetime.now(ZoneInfo(TIMEZONE)) - mtime).days

        # Risk
        if status.total_goals == 0:
            status.risk_level = "red"
            status.items_at_risk.append("No goals set for current quarter")
        elif status.days_since_review > 30:
            status.risk_level = "yellow"
            status.items_at_risk.append(f"Goals not reviewed in {status.days_since_review}d")
        elif status.completion_pct < 25 and status.quarter in ("Q1",) and date.today().month >= 3:
            status.risk_level = "yellow"
            status.items_at_risk.append(f"Only {status.completion_pct}% done, quarter ending soon")

    except Exception as e:
        log.debug("Goals aggregation failed: %s", e)

    return status


def _aggregate_health_sync() -> HealthSignals:
    """Sync portion of health signal aggregation."""
    signals = HealthSignals()
    try:
        from actions.reminders import list_reminders

        reminders = list_reminders()
        now_iso = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        overdue = [r for r in reminders if r["due_at"] < now_iso]
        signals.overdue_personal_items = len(overdue)
        if overdue:
            for r in overdue[:3]:
                signals.items_at_risk.append(f"Overdue: {r['text']}")

        if signals.overdue_personal_items > 5:
            signals.risk_level = "red"
        elif signals.overdue_personal_items > 2:
            signals.risk_level = "yellow"

    except Exception as e:
        log.debug("Health signal aggregation failed: %s", e)

    return signals


def _aggregate_nutrition() -> NutritionStatus:
    """Pull today's nutrition data from calorie tracker."""
    status = NutritionStatus()
    try:
        from actions.calorie_tracker import get_daily_summary
        data = get_daily_summary()
        status.calories_today = data.get("calories", 0)
        status.calorie_goal = data.get("calorie_goal", 0)
        status.protein_g = data.get("protein_g", 0)
        status.protein_goal = data.get("protein_goal", 0)
        status.meals_logged = data.get("meals", 0)

        if status.calorie_goal and status.calories_today > status.calorie_goal * 1.2:
            status.risk_level = "yellow"
            status.items_at_risk.append(f"Over calorie goal: {status.calories_today}/{status.calorie_goal}")
    except Exception as e:
        log.debug("Nutrition aggregation failed: %s", e)
    return status


def _aggregate_fasting() -> FastingStatus:
    """Pull current fasting state from fasting tracker."""
    status = FastingStatus()
    try:
        from actions.fasting_tracker import get_status
        data = get_status()
        if data:
            status.active = True
            status.elapsed_hours = data.get("elapsed_hours", 0)
            status.target_hours = data.get("target_hours", 0)
            status.protocol = data.get("protocol", "")
            remaining = data.get("remaining_hours", 0)
            if remaining < 0:
                status.risk_level = "yellow"
                status.items_at_risk.append(f"Fast exceeded target by {abs(remaining):.1f}h")
    except Exception as e:
        log.debug("Fasting aggregation failed: %s", e)
    return status


def _aggregate_focus() -> FocusStatus:
    """Pull today's focus/pomodoro stats."""
    status = FocusStatus()
    try:
        from actions.pomodoro import get_today_stats
        data = get_today_stats()
        status.sessions_today = data.get("sessions", 0)
        status.total_minutes = data.get("total_min", 0)
        status.completed = data.get("completed", 0)
    except Exception as e:
        log.debug("Focus aggregation failed: %s", e)
    return status


async def aggregate_all_domains() -> DomainSnapshot:
    """Single call to aggregate status across all life domains.

    Returns a DomainSnapshot with risk levels (green/yellow/red) for each domain.
    """
    import asyncio

    snapshot = DomainSnapshot(
        timestamp=datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
    )

    # Sync aggregations (file-based, fast)
    snapshot.work = _aggregate_work()
    snapshot.projects = _aggregate_projects()
    snapshot.finance = _aggregate_finance()
    snapshot.goals = _aggregate_goals()
    snapshot.health = _aggregate_health_sync()
    snapshot.nutrition = _aggregate_nutrition()
    snapshot.fasting = _aggregate_fasting()
    snapshot.focus = _aggregate_focus()

    # Async aggregations (calendar API calls)
    try:
        meeting_count, (deep_work, gap_pct) = await asyncio.gather(
            _get_meeting_count(),
            _get_calendar_gaps(),
        )
        snapshot.work.meeting_count_today = meeting_count
        snapshot.health.deep_work_hours_available = deep_work
        snapshot.health.calendar_gap_pct = gap_pct

        # Update health risk based on calendar
        if deep_work < 1.0 and snapshot.work.meeting_count_today > 6:
            snapshot.health.risk_level = "red"
            snapshot.health.items_at_risk.append(
                f"Only {deep_work}h deep work, {meeting_count} meetings today"
            )
        elif deep_work < 2.0:
            if snapshot.health.risk_level == "green":
                snapshot.health.risk_level = "yellow"
            snapshot.health.items_at_risk.append(f"Low deep work time: {deep_work}h")

    except Exception as e:
        log.debug("Async aggregation failed: %s", e)

    return snapshot


def snapshot_to_text(snapshot: DomainSnapshot) -> str:
    """Format a DomainSnapshot as human-readable text for digests and nudges."""
    lines = [f"Domain Snapshot ({snapshot.timestamp[:16]})\n"]

    # Work
    w = snapshot.work
    risk_icon = {"green": "G", "yellow": "Y", "red": "R"}.get(w.risk_level, "?")
    lines.append(f"[{risk_icon}] Work: {w.p0_count} P0s, {w.in_progress_count} in-progress, "
                 f"{w.blocked_count} blocked, {w.meeting_count_today} meetings today")
    if w.items_at_risk:
        lines.extend(f"  - {item}" for item in w.items_at_risk[:3])

    # Projects
    for key, p in snapshot.projects.items():
        if p.risk_level != "green" or p.open_task_count > 0:
            risk_icon = {"green": "G", "yellow": "Y", "red": "R"}.get(p.risk_level, "?")
            lines.append(f"[{risk_icon}] {p.name}: {p.open_task_count} tasks, "
                         f"updated {p.days_since_update}d ago")

    # Finance
    f = snapshot.finance
    risk_icon = {"green": "G", "yellow": "Y", "red": "R"}.get(f.risk_level, "?")
    parts = []
    if f.passed_deadlines:
        parts.append(f"{len(f.passed_deadlines)} passed deadlines")
    if f.upcoming_soon:
        parts.append(f"{len(f.upcoming_soon)} upcoming")
    if f.portfolio_age_days > 0:
        parts.append(f"portfolio {f.portfolio_age_days}d old")
    lines.append(f"[{risk_icon}] Finance: {', '.join(parts) if parts else 'OK'}")

    # Goals
    g = snapshot.goals
    risk_icon = {"green": "G", "yellow": "Y", "red": "R"}.get(g.risk_level, "?")
    lines.append(f"[{risk_icon}] Goals ({g.quarter}): {g.completed_goals}/{g.total_goals} "
                 f"({g.completion_pct}%), reviewed {g.days_since_review}d ago")

    # Health
    h = snapshot.health
    risk_icon = {"green": "G", "yellow": "Y", "red": "R"}.get(h.risk_level, "?")
    lines.append(f"[{risk_icon}] Health: {h.deep_work_hours_available}h deep work, "
                 f"{h.overdue_personal_items} overdue items")
    if h.items_at_risk:
        lines.extend(f"  - {item}" for item in h.items_at_risk[:3])

    # Nutrition
    n = snapshot.nutrition
    if n.calories_today or n.meals_logged:
        risk_icon = {"green": "G", "yellow": "Y", "red": "R"}.get(n.risk_level, "?")
        cal_str = f"{n.calories_today}/{n.calorie_goal}" if n.calorie_goal else str(n.calories_today)
        lines.append(f"[{risk_icon}] Nutrition: {cal_str} cal, {n.protein_g}g protein, {n.meals_logged} meals")

    # Fasting
    fs = snapshot.fasting
    if fs.active:
        risk_icon = {"green": "G", "yellow": "Y", "red": "R"}.get(fs.risk_level, "?")
        lines.append(f"[{risk_icon}] Fasting: {fs.elapsed_hours:.1f}/{fs.target_hours}h ({fs.protocol})")

    # Focus
    fc = snapshot.focus
    if fc.sessions_today:
        lines.append(f"[G] Focus: {fc.sessions_today} sessions, {fc.total_minutes}min, {fc.completed} completed")

    return "\n".join(lines)
