"""Proactive alert engine — detects things that need attention without being asked."""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DB_PATH, FINANCE_DIR, GOALS_DIR, TIMEZONE

log = logging.getLogger("khalil.scheduler.proactive")

# #89: Configurable alert thresholds — defaults can be overridden via settings table
_DEFAULT_THRESHOLDS = {
    "stale_goals_days": 90,
    "stale_projects_days": 60,
    "stale_portfolio_days": 60,
}


def _get_threshold(key: str) -> int:
    """Get a threshold from settings, falling back to default."""
    try:
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (f"threshold_{key}",)).fetchone()
        conn.close()
        if row:
            return max(1, int(row[0]))
    except Exception:
        pass
    return _DEFAULT_THRESHOLDS.get(key, 60)


def check_stale_goals() -> str | None:
    """No goals added/updated in 90+ days."""
    goals_file = GOALS_DIR / "2026.md"
    if not goals_file.exists():
        return "🎯 No 2026 goals file exists yet. Use /goals add to start."

    mtime = datetime.fromtimestamp(goals_file.stat().st_mtime, tz=ZoneInfo(TIMEZONE))
    days_old = (datetime.now(ZoneInfo(TIMEZONE)) - mtime).days
    threshold = _get_threshold("stale_goals_days")
    if days_old > threshold:
        return f"🎯 Goals file hasn't been updated in {days_old} days. Run /goals review."

    # Check if goals are actually empty
    from actions.goals import get_goal_summary
    summary = get_goal_summary()
    if "No goals set" in summary:
        return "🎯 No goals set for the current quarter. Use /goals add to set some."

    return None


def check_stale_projects() -> str | None:
    """Project files with open tasks and mtime > 60 days."""
    from actions.projects import KNOWN_PROJECTS, get_open_tasks

    stale = []
    now = datetime.now(ZoneInfo(TIMEZONE))

    for key, info in KNOWN_PROJECTS.items():
        filepath = info["file"]
        if not filepath.exists():
            continue

        tasks = get_open_tasks(key)
        if not tasks:
            continue

        mtime = datetime.fromtimestamp(filepath.stat().st_mtime, tz=ZoneInfo(TIMEZONE))
        days_old = (now - mtime).days
        project_threshold = _get_threshold("stale_projects_days")
        if days_old > project_threshold:
            stale.append(f"{info['name']}: {len(tasks)} open tasks, last touched {days_old}d ago")

    if stale:
        return "📁 Stale projects:\n" + "\n".join(f"  • {s}" for s in stale)
    return None


def check_passed_deadlines() -> str | None:
    """Financial deadlines with status 'PASSED'."""
    from actions.finance import get_deadlines

    deadlines = get_deadlines()
    passed = [d for d in deadlines if d["status"] == "PASSED"]

    if passed:
        items = "\n".join(f"  • {d['item']} (was {d['date']})" for d in passed)
        return f"⚠️ Passed financial deadlines:\n{items}"
    return None


def check_stale_portfolio() -> str | None:
    """Latest portfolio-*.md > 60 days old."""
    if not FINANCE_DIR.exists():
        return None

    portfolio_files = sorted(FINANCE_DIR.glob("portfolio-*.md"), reverse=True)
    if not portfolio_files:
        return "💰 No portfolio snapshots found. Consider creating one."

    latest = portfolio_files[0]
    mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=ZoneInfo(TIMEZONE))
    days_old = (datetime.now(ZoneInfo(TIMEZONE)) - mtime).days
    portfolio_threshold = _get_threshold("stale_portfolio_days")
    if days_old > portfolio_threshold:
        return f"💰 Portfolio snapshot ({latest.name}) is {days_old} days old. Time for an update."
    return None


def check_overdue_reminders() -> str | None:
    """Active reminders past due."""
    from actions.reminders import list_reminders

    reminders = list_reminders()
    now_iso = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    overdue = [r for r in reminders if r["due_at"] < now_iso]

    if overdue:
        items = "\n".join(f"  • #{r['id']}: {r['text']} (due {r['due_at'][:16]})" for r in overdue)
        return f"⏰ Overdue reminders ({len(overdue)}):\n{items}"
    return None


def generate_weekend_nudge() -> str | None:
    """#92: Generate a weekend project nudge if today is Saturday.

    Checks for stale project-related items (goals, reminders older than 14 days).
    Returns a nudge message with suggested first step, or None if nothing stale or not Saturday.
    """
    today = date.today()
    if today.weekday() != 5:  # 5 = Saturday
        return None

    import sqlite3
    stale_items = []

    # Check for stale goals (goals file older than 14 days)
    goals_file = GOALS_DIR / "2026.md"
    if goals_file.exists():
        mtime = datetime.fromtimestamp(goals_file.stat().st_mtime, tz=ZoneInfo(TIMEZONE))
        days_old = (datetime.now(ZoneInfo(TIMEZONE)) - mtime).days
        if days_old > 14:
            stale_items.append(f"Goals file last updated {days_old} days ago")

    # Check for stale reminders (older than 14 days)
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cutoff = (datetime.now(ZoneInfo(TIMEZONE)) - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
        old_reminders = conn.execute(
            "SELECT text, due_at FROM reminders WHERE status = 'active' AND due_at < ?",
            (cutoff,),
        ).fetchall()
        conn.close()
        for r in old_reminders:
            stale_items.append(f"Overdue reminder: {r[0]} (due {r[1][:10]})")
    except Exception as e:
        log.debug("Weekend nudge reminder check failed: %s", e)

    # Check for stale projects
    try:
        from actions.projects import KNOWN_PROJECTS, get_open_tasks
        now = datetime.now(ZoneInfo(TIMEZONE))
        for key, info in KNOWN_PROJECTS.items():
            filepath = info["file"]
            if not filepath.exists():
                continue
            tasks = get_open_tasks(key)
            if not tasks:
                continue
            mtime = datetime.fromtimestamp(filepath.stat().st_mtime, tz=ZoneInfo(TIMEZONE))
            days_old = (now - mtime).days
            if days_old > 14:
                stale_items.append(f"Project '{info['name']}': {len(tasks)} open tasks, untouched for {days_old}d")
    except Exception as e:
        log.debug("Weekend nudge project check failed: %s", e)

    if not stale_items:
        return None

    nudge = "🛠 Weekend Project Nudge\n\nSome things have been sitting idle:\n"
    for item in stale_items[:5]:
        nudge += f"  • {item}\n"
    nudge += "\nSuggested first step: pick one item and spend 30 minutes on it today."
    return nudge


def check_overdue_commitments() -> str | None:
    """M11: Commitments past due by 2+ days."""
    try:
        from actions.meetings import get_overdue_commitments, format_commitments
        overdue = get_overdue_commitments(days_past=2)
        if overdue:
            formatted = format_commitments(overdue)
            return f"📋 Overdue commitments ({len(overdue)}):\n{formatted}"
    except Exception as e:
        log.debug("Overdue commitments check failed: %s", e)
    return None


def run_proactive_checks() -> list[str]:
    """Run all proactive detectors. Returns list of findings (strings).

    M9: Applies smart timing filter — suppresses non-urgent alerts during
    inactive hours based on learned activity patterns.
    M10: Priority filtering — suppresses low-value alerts during busy periods.
    """
    checks = [
        check_stale_goals,
        check_stale_projects,
        check_passed_deadlines,
        check_stale_portfolio,
        check_overdue_reminders,
        check_overdue_commitments,
    ]

    findings = []
    for check in checks:
        try:
            result = check()
            if result:
                findings.append(result)
        except Exception as e:
            log.error("Proactive check %s failed: %s", check.__name__, e)

    # M10: Priority filtering — suppress low-value alerts during busy weeks
    findings = _priority_filter(findings)

    # M9: Filter by smart timing
    return filter_alerts_by_timing(findings)


def _priority_filter(findings: list[str]) -> list[str]:
    """M10: Suppress stale-portfolio and stale-project alerts during busy weeks.

    If the user has many meetings or active goals, non-critical alerts add noise.
    """
    if len(findings) <= 2:
        return findings  # Few alerts, show all

    # Check if it's a busy period
    try:
        from synthesis.aggregator import aggregate_all_domains
        import asyncio
        # Sync check: see if work domain is stressed
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        # Count recent activity signals as proxy for busyness
        cutoff = (datetime.now(ZoneInfo(TIMEZONE)) - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        activity = conn.execute(
            "SELECT COUNT(*) FROM interaction_signals WHERE created_at > ?", (cutoff,)
        ).fetchone()[0]
        conn.close()

        if activity > 50:  # Very active period
            # Keep only deadline-related and overdue alerts
            return [f for f in findings if "deadline" in f.lower() or "overdue" in f.lower() or "⚠" in f]
    except Exception:
        pass

    return findings


# --- M9: Smart Proactive Timing (Task 9.3) ---

def record_activity_timing(signal_type: str = "user_active"):
    """Record when the user actually reads/responds to alerts.

    Called when a user message is received, indicating active engagement.
    """
    import sqlite3
    now = datetime.now(ZoneInfo(TIMEZONE))
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT INTO activity_timing (signal_type, hour, day_of_week, created_at) VALUES (?, ?, ?, ?)",
            (signal_type, now.hour, now.weekday(), now.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug("Failed to record activity timing: %s", e)


def get_active_hours(day_of_week: int | None = None, min_signals: int = 1) -> list[int]:
    """Get hours when the user is typically active, based on observed signals.

    Args:
        day_of_week: 0=Monday, 6=Sunday. None means current day.
        min_signals: minimum signal count to consider an hour "active".

    Returns sorted list of active hours (0-23).
    """
    import sqlite3
    if day_of_week is None:
        day_of_week = datetime.now(ZoneInfo(TIMEZONE)).weekday()

    # Default work hours — used when learned data is too sparse
    if day_of_week >= 5:  # weekend
        default_hours = list(range(9, 23))  # 9 AM - 10 PM
    else:
        default_hours = list(range(8, 22))  # 8 AM - 9 PM

    try:
        conn = sqlite3.connect(str(DB_PATH))
        # Look at last 30 days of data for this day of week
        cutoff = (datetime.now(ZoneInfo(TIMEZONE)) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            "SELECT hour, COUNT(*) as cnt FROM activity_timing "
            "WHERE day_of_week = ? AND created_at > ? "
            "GROUP BY hour HAVING cnt >= ? ORDER BY hour",
            (day_of_week, cutoff, min_signals),
        ).fetchall()
        conn.close()
        learned = [r[0] for r in rows]
        # If learned window is too narrow (<4 hours), merge with defaults
        if len(learned) < 4:
            return sorted(set(default_hours) | set(learned))
        return learned
    except Exception as e:
        log.debug("Failed to get active hours: %s", e)
        return default_hours


def is_good_time_for_alert(alert_type: str = "general") -> bool:
    """Check if now is a good time to send an alert based on learned activity patterns.

    Returns True if:
    - Not enough data yet (defaults to permissive)
    - Current hour is in the learned active hours for this day of week
    - Calendar is not in deep work mode (if calendar integration available)
    """
    now = datetime.now(ZoneInfo(TIMEZONE))
    active_hours = get_active_hours(now.weekday())

    # If we don't have enough data, fall back to default work hours
    if not active_hours:
        # Default: 7 AM - 10 PM on weekdays, 9 AM - 10 PM on weekends
        if now.weekday() >= 5:  # weekend
            return 9 <= now.hour <= 22
        return 7 <= now.hour <= 22

    # Check if current hour is in learned active hours
    if now.hour not in active_hours:
        log.info("Suppressing alert: hour %d not in active hours %s for day %d",
                 now.hour, active_hours, now.weekday())
        return False

    # Check for calendar-blocked deep work
    try:
        _check_deep_work_block(now)
    except Exception:
        pass  # Calendar check is best-effort

    return True


def _check_deep_work_block(now: datetime) -> bool:
    """Check if there's a calendar event blocking alerts (deep work, focus time).

    Returns True if alerts should be suppressed.
    """
    try:
        from actions.calendar_reader import get_events_for_range
        events = get_events_for_range(now, now + timedelta(minutes=1))
        for event in events:
            summary = (event.get("summary") or "").lower()
            if any(keyword in summary for keyword in ("deep work", "focus", "do not disturb", "dnd", "heads down")):
                log.info("Suppressing alert: calendar blocked (%s)", event.get("summary"))
                return True
    except Exception:
        pass  # Calendar integration may not be available
    return False


async def run_synthesis_nudge_check() -> str | None:
    """M10: Check if capacity score crosses threshold and return nudge text.

    Triggers when capacity score > 70. Returns formatted nudge or None.
    Called by the scheduler to proactively alert the user.
    """
    try:
        from synthesis.aggregator import aggregate_all_domains
        from synthesis.capacity import detect_overcommitment

        snapshot = await aggregate_all_domains()
        report = await detect_overcommitment(snapshot)

        if report.capacity_score <= 70:
            log.debug("Synthesis nudge: score %d, below threshold", report.capacity_score)
            return None

        score = report.capacity_score
        if score > 80:
            label = "OVERCOMMITTED"
        else:
            label = "Heavy Load"

        lines = [f"Capacity Alert: {score}/100 ({label})\n"]

        for risk in report.risk_areas[:3]:
            lines.append(f"  - {risk}")

        if report.recommendations:
            lines.append("")
            for rec in report.recommendations[:3]:
                lines.append(f"  > {rec}")

        return "\n".join(lines)

    except Exception as e:
        log.debug("Synthesis nudge check failed: %s", e)
        return None


def filter_alerts_by_timing(findings: list[str]) -> list[str]:
    """Filter proactive check findings based on smart timing.

    Suppresses alerts if now is not a good time for the user.
    """
    if not findings:
        return findings

    if is_good_time_for_alert():
        return findings

    # Suppress non-urgent alerts during inactive hours
    # Overdue reminders and passed deadlines are always delivered
    urgent_keywords = ("overdue", "passed", "expired", "urgent")
    return [f for f in findings if any(k in f.lower() for k in urgent_keywords)]


# --- #25: Daily Anticipation Pass ---

async def daily_anticipation(ask_llm_fn=None) -> list[str]:
    """Run a daily anticipation check — surfaces things that need proactive attention.

    Called at 7 AM by scheduler. Checks:
    1. Unusual calendar events (first meeting with new person, all-day events)
    2. High-priority unread emails (from manager, with urgent keywords)
    3. Weather alerts (if severe conditions)
    """
    findings = []

    # 1. Calendar anticipation — detect unusual events
    try:
        from state.calendar_provider import get_today_events
        events = await get_today_events()
        if events:
            for event in events:
                title = event.get("summary", "").lower()
                # Flag all-day events (often important deadlines)
                if event.get("all_day"):
                    findings.append(f"\U0001f4c5 All-day event today: {event.get('summary', 'Untitled')}")
                # Flag events with external attendees (potential prep needed)
                attendees = event.get("attendees", [])
                if len(attendees) > 5:
                    findings.append(
                        f"\U0001f465 Large meeting today: {event.get('summary', 'Untitled')} "
                        f"({len(attendees)} attendees) — may need prep"
                    )
    except Exception as e:
        log.debug("Calendar anticipation failed: %s", e)

    # 2. Unread email priority check
    try:
        from state.email_provider import get_unread_count, get_urgent_unread
        urgent = await get_urgent_unread()
        if urgent and len(urgent) > 0:
            findings.append(
                f"\U0001f4e8 {len(urgent)} high-priority unread email(s) — "
                f"from: {', '.join(u.get('from', '?')[:30] for u in urgent[:3])}"
            )
    except Exception as e:
        log.debug("Email anticipation failed: %s", e)

    # 3. Weather alert (severe conditions)
    try:
        import os
        lat = os.environ.get("KHALIL_WEATHER_LAT")
        lon = os.environ.get("KHALIL_WEATHER_LON")
        if lat and lon:
            from actions.weather import get_weather_data
            data = await get_weather_data()
            if data:
                # Check for severe weather codes (thunderstorm, heavy rain, snow)
                code = data.get("current", {}).get("weather_code", 0)
                if code >= 65:  # WMO codes: 65+ = heavy rain/snow/thunderstorm
                    desc = data.get("current", {}).get("description", "severe weather")
                    findings.append(f"\u26a0\ufe0f Weather alert: {desc}")
    except Exception as e:
        log.debug("Weather anticipation failed: %s", e)

    if findings:
        log.info("Daily anticipation: %d findings", len(findings))

    return findings


# --- #25: Weekly Pattern Analyzer ---

def analyze_weekly_patterns() -> dict:
    """Analyze user interaction patterns from the last 7 days.

    Returns summary of: peak activity hours, most-used tools, common queries.
    Runs weekly (Sunday evening) to inform proactive suggestions.
    """
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    cutoff = (datetime.now(ZoneInfo(TIMEZONE)) - timedelta(days=7)).isoformat()
    patterns = {}

    # Peak activity hours
    try:
        rows = conn.execute(
            "SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour, COUNT(*) as cnt "
            "FROM conversations WHERE role = 'user' AND timestamp > ? "
            "GROUP BY hour ORDER BY cnt DESC LIMIT 5",
            (cutoff,),
        ).fetchall()
        if rows:
            patterns["peak_hours"] = [{"hour": r["hour"], "messages": r["cnt"]} for r in rows]
    except Exception:
        pass

    # Most-used capabilities
    try:
        rows = conn.execute(
            "SELECT json_extract(context, '$.action') as action, COUNT(*) as cnt "
            "FROM interaction_signals "
            "WHERE signal_type = 'capability_usage' AND created_at > ? "
            "GROUP BY action ORDER BY cnt DESC LIMIT 10",
            (cutoff,),
        ).fetchall()
        if rows:
            patterns["top_capabilities"] = [{"action": r["action"], "count": r["cnt"]} for r in rows]
    except Exception:
        pass

    # Failure hotspots
    try:
        rows = conn.execute(
            "SELECT json_extract(context, '$.action') as action, COUNT(*) as cnt "
            "FROM interaction_signals "
            "WHERE signal_type IN ('tool_failure', 'action_execution_failure') AND created_at > ? "
            "GROUP BY action ORDER BY cnt DESC LIMIT 5",
            (cutoff,),
        ).fetchall()
        if rows:
            patterns["failure_hotspots"] = [{"action": r["action"], "failures": r["cnt"]} for r in rows]
    except Exception:
        pass

    conn.close()
    return patterns
