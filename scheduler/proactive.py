"""Proactive alert engine — detects things that need attention without being asked."""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import FINANCE_DIR, GOALS_DIR, TIMEZONE

log = logging.getLogger("khalil.scheduler.proactive")


def check_stale_goals() -> str | None:
    """No goals added/updated in 90+ days."""
    goals_file = GOALS_DIR / "2026.md"
    if not goals_file.exists():
        return "🎯 No 2026 goals file exists yet. Use /goals add to start."

    mtime = datetime.fromtimestamp(goals_file.stat().st_mtime, tz=ZoneInfo(TIMEZONE))
    days_old = (datetime.now(ZoneInfo(TIMEZONE)) - mtime).days
    if days_old > 90:
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
        if days_old > 60:
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
    if days_old > 60:
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


def run_proactive_checks() -> list[str]:
    """Run all proactive detectors. Returns list of findings (strings)."""
    checks = [
        check_stale_goals,
        check_stale_projects,
        check_passed_deadlines,
        check_stale_portfolio,
        check_overdue_reminders,
    ]

    findings = []
    for check in checks:
        try:
            result = check()
            if result:
                findings.append(result)
        except Exception as e:
            log.error("Proactive check %s failed: %s", check.__name__, e)

    return findings
