"""Quarterly planning automation — goal setting, alignment, mid-quarter reviews.

Triggered on specific dates:
- Planning prompt: 2 weeks before quarter end (Mar 15, Jun 15, Sep 15, Dec 15)
- Mid-quarter review: quarter midpoint (Feb 15, May 15, Aug 15, Nov 15)
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DB_PATH, GOALS_DIR, OWNER_NAME, TIMEZONE

log = logging.getLogger("pharoclaw.scheduler.planning")

# --- Quarter utilities ---

# Planning trigger dates: 2 weeks before quarter end
PLANNING_TRIGGER_DATES = {
    (3, 15): ("Q1", "Q2"),   # Q1 ends Mar 31, plan Q2
    (6, 15): ("Q2", "Q3"),
    (9, 15): ("Q3", "Q4"),
    (12, 15): ("Q4", "Q1"),
}

# Mid-quarter review dates
MID_QUARTER_DATES = {
    (2, 15): "Q1",
    (5, 15): "Q2",
    (8, 15): "Q3",
    (11, 15): "Q4",
}

# Domain mapping for goal alignment
DOMAIN_KEYWORDS = {
    "work": ["spotify", "sprint", "epic", "P0", "team", "roadmap", "promotion", "role",
             "leadership", "stakeholder", "meeting", "1:1"],
    "project": ["zia", "bezier", "bézier", "tiny grounds", "side project", "app", "ship",
                "launch", "build", "code", "prototype"],
    "finance": ["invest", "rrsp", "tfsa", "rsu", "tax", "portfolio", "savings", "budget",
                "money", "financial", "income"],
    "personal": ["health", "fitness", "family", "kids", "read", "learn", "course",
                 "travel", "habit", "meditation", "sleep", "weight"],
}


def _current_quarter() -> str:
    """Return current quarter label like 'Q1'."""
    tz = ZoneInfo(TIMEZONE)
    month = datetime.now(tz).month
    return f"Q{(month - 1) // 3 + 1}"


def _next_quarter(quarter: str) -> str:
    """Return the next quarter label."""
    q_num = int(quarter[1])
    return f"Q{q_num % 4 + 1}"


def _quarter_label(quarter: str) -> str:
    """Human-readable quarter label."""
    labels = {"Q1": "Q1 (Jan-Mar)", "Q2": "Q2 (Apr-Jun)",
              "Q3": "Q3 (Jul-Sep)", "Q4": "Q4 (Oct-Dec)"}
    return labels.get(quarter, quarter)


def is_planning_trigger_date(today: date | None = None) -> tuple[str, str] | None:
    """Check if today is a planning trigger date.

    Returns (ending_quarter, next_quarter) or None.
    """
    if today is None:
        today = date.today()
    key = (today.month, today.day)
    return PLANNING_TRIGGER_DATES.get(key)


def is_mid_quarter_date(today: date | None = None) -> str | None:
    """Check if today is a mid-quarter review date.

    Returns the current quarter string or None.
    """
    if today is None:
        today = date.today()
    key = (today.month, today.day)
    return MID_QUARTER_DATES.get(key)


# --- Goal domain alignment ---

def map_goal_to_domain(goal_text: str) -> str:
    """Map a goal to a domain based on keyword matching.

    Returns one of: work, project, finance, personal.
    """
    text_lower = goal_text.lower()
    scores = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw.lower() in text_lower)
        if score > 0:
            scores[domain] = score

    if scores:
        return max(scores, key=scores.get)
    return "personal"  # default


def _estimate_weekly_hours(goal_text: str) -> float:
    """Rough estimate of weekly hours a goal requires.

    Simple heuristic: goals mentioning building/shipping need more time.
    """
    text_lower = goal_text.lower()
    if any(w in text_lower for w in ["build", "ship", "launch", "prototype", "create"]):
        return 8.0
    if any(w in text_lower for w in ["learn", "course", "study", "read"]):
        return 3.0
    if any(w in text_lower for w in ["habit", "daily", "exercise", "meditation"]):
        return 5.0
    if any(w in text_lower for w in ["review", "track", "monitor"]):
        return 1.0
    return 2.0  # default


async def detect_goal_conflicts(goals: list[dict]) -> list[str]:
    """Detect conflicts between goals using capacity data.

    Each goal dict: {text, domain, estimated_hours}.
    Returns list of conflict descriptions.
    """
    from synthesis.aggregator import aggregate_all_domains
    from synthesis.capacity import detect_overcommitment

    snapshot = await aggregate_all_domains()
    report = await detect_overcommitment(snapshot)

    conflicts = []

    # Sum estimated hours by domain
    domain_hours: dict[str, float] = {}
    for g in goals:
        domain = g.get("domain", "personal")
        domain_hours[domain] = domain_hours.get(domain, 0) + g.get("estimated_hours", 2.0)

    total_weekly_hours = sum(domain_hours.values())

    # Available weekly hours heuristic: ~40h work + ~20h personal = ~60h max capacity
    # Subtract current load
    available_hours = 60.0
    if report.capacity_score > 60:
        available_hours = 30.0  # heavy load
    elif report.capacity_score > 40:
        available_hours = 45.0  # busy

    if total_weekly_hours > available_hours:
        conflicts.append(
            f"Goals require ~{total_weekly_hours:.0f}h/week but capacity analysis "
            f"shows ~{available_hours:.0f}h available (capacity score: {report.capacity_score}/100)"
        )

    # Check per-domain overload
    if domain_hours.get("work", 0) > 25:
        conflicts.append(
            f"Work goals alone require ~{domain_hours['work']:.0f}h/week — "
            "leaves little room for side projects"
        )

    if domain_hours.get("project", 0) > 15:
        conflicts.append(
            f"Side project goals require ~{domain_hours['project']:.0f}h/week — "
            "that's aggressive on top of a full-time role"
        )

    # Check if current capacity is already strained
    if report.capacity_score > 80:
        conflicts.append(
            "Current capacity score is OVERCOMMITTED — "
            "consider deferring or reducing scope before adding new goals"
        )

    return conflicts


# --- Planning prompt generation ---

async def generate_planning_prompt(ask_claude_fn) -> str:
    """Generate the quarterly planning prompt.

    Triggered 2 weeks before quarter end. Gathers achievements,
    unfinished goals, and domain snapshot to prompt next-quarter planning.

    Args:
        ask_claude_fn: async callable(query, context, system_extra) -> str
    """
    from synthesis.aggregator import aggregate_all_domains, snapshot_to_text
    from synthesis.capacity import detect_overcommitment, capacity_report_to_text
    from actions.goals import GOALS_FILE, _parse_goals, _current_quarter as goals_current_quarter

    today = date.today()
    trigger = is_planning_trigger_date(today)
    if not trigger:
        # Fallback: use current/next quarter
        ending_q = _current_quarter()
        next_q = _next_quarter(ending_q)
    else:
        ending_q, next_q = trigger

    # Gather current quarter achievements
    achievements = []
    unfinished = []
    try:
        if GOALS_FILE.exists():
            content = GOALS_FILE.read_text(encoding="utf-8")
            goals = _parse_goals(content)
            q_goals = goals.get(ending_q, {})
            for category, items in q_goals.items():
                for item in items:
                    entry = f"[{category}] {item['text']}"
                    if item["done"]:
                        achievements.append(entry)
                    else:
                        unfinished.append(entry)
    except Exception as e:
        log.debug("Failed to read goals: %s", e)

    # Gather signals from learning module (recent accomplishments)
    signal_achievements = []
    try:
        from learning import _get_conn
        conn = _get_conn()
        rows = conn.execute(
            "SELECT context FROM interaction_signals "
            "WHERE signal_type = 'goal_progress' AND created_at > date('now', '-90 days') "
            "ORDER BY created_at DESC LIMIT 20",
        ).fetchall()
        for row in rows:
            if row[0]:
                ctx = json.loads(row[0])
                signal_achievements.append(ctx.get("description", ""))
    except Exception:
        pass

    # Domain snapshot
    snapshot = await aggregate_all_domains()
    report = await detect_overcommitment(snapshot)

    snapshot_text = snapshot_to_text(snapshot)
    capacity_text = capacity_report_to_text(report)

    achievements_text = "\n".join(f"  - {a}" for a in achievements) if achievements else "  (none tracked)"
    unfinished_text = "\n".join(f"  - {u}" for u in unfinished) if unfinished else "  (none)"
    signals_text = "\n".join(f"  - {s}" for s in signal_achievements if s) if signal_achievements else ""

    context = (
        f"Quarter Ending: {_quarter_label(ending_q)}\n"
        f"Next Quarter: {_quarter_label(next_q)}\n\n"
        f"Completed Goals:\n{achievements_text}\n\n"
        f"Unfinished Goals:\n{unfinished_text}\n\n"
        f"{f'Progress Signals:{chr(10)}{signals_text}{chr(10)}{chr(10)}' if signals_text else ''}"
        f"Domain Snapshot:\n{snapshot_text}\n\n"
        f"Capacity Report:\n{capacity_text}"
    )

    prompt = await ask_claude_fn(
        f"{_quarter_label(ending_q)} ends in 2 weeks. Generate a quarterly planning prompt for {OWNER_NAME}.\n\n"
        "Structure:\n"
        f"1. **{ending_q} Recap** — What was accomplished, what fell short (be specific)\n"
        f"2. **Unfinished Business** — What's still open and whether to carry forward or drop\n"
        f"3. **{next_q} Planning** — Based on current capacity and domain snapshot, suggest:\n"
        "   - 2-3 goals per domain (work, projects, personal, finance)\n"
        "   - Flag any capacity conflicts\n"
        f"4. **Question** — Ask {OWNER_NAME} one sharp question about their priorities\n\n"
        "Be direct and specific to the data. No filler. Under 25 lines.",
        context,
        system_extra=f"Today's date: {today.isoformat()}",
    )

    return (
        f"Quarterly Planning — {_quarter_label(ending_q)} -> {_quarter_label(next_q)}\n\n"
        f"{prompt}\n\n"
        f"To set goals: /goals add <category> <goal>\n"
        f"To review: /goals review"
    )


async def generate_mid_quarter_review(ask_claude_fn) -> str:
    """Generate mid-quarter progress review.

    For each goal: assess on track / at risk / behind.
    Provide recommendations: accelerate, adjust scope, defer, or celebrate.

    Args:
        ask_claude_fn: async callable(query, context, system_extra) -> str
    """
    from synthesis.aggregator import aggregate_all_domains, snapshot_to_text
    from synthesis.capacity import detect_overcommitment, capacity_report_to_text
    from actions.goals import GOALS_FILE, _parse_goals

    today = date.today()
    quarter = _current_quarter()

    # Load current goals
    goals_by_status = {"done": [], "in_progress": []}
    try:
        if GOALS_FILE.exists():
            content = GOALS_FILE.read_text(encoding="utf-8")
            goals = _parse_goals(content)
            q_goals = goals.get(quarter, {})
            for category, items in q_goals.items():
                for item in items:
                    entry = {"category": category, "text": item["text"], "done": item["done"]}
                    if item["done"]:
                        goals_by_status["done"].append(entry)
                    else:
                        goals_by_status["in_progress"].append(entry)
    except Exception as e:
        log.debug("Failed to read goals for mid-quarter review: %s", e)

    total_goals = len(goals_by_status["done"]) + len(goals_by_status["in_progress"])
    done_count = len(goals_by_status["done"])

    if total_goals == 0:
        return (
            f"Mid-Quarter Review — {_quarter_label(quarter)}\n\n"
            "No goals set for this quarter. Use /goals add <category> <goal> to set some."
        )

    # Gather recent progress signals
    progress_signals = []
    try:
        from learning import _get_conn
        conn = _get_conn()
        rows = conn.execute(
            "SELECT context, created_at FROM interaction_signals "
            "WHERE signal_type = 'goal_progress' AND created_at > date('now', '-45 days') "
            "ORDER BY created_at DESC LIMIT 15",
        ).fetchall()
        for row in rows:
            if row[0]:
                ctx = json.loads(row[0])
                progress_signals.append(f"{ctx.get('description', '')} ({row[1][:10]})")
    except Exception:
        pass

    # Domain snapshot for context
    snapshot = await aggregate_all_domains()
    report = await detect_overcommitment(snapshot)

    snapshot_text = snapshot_to_text(snapshot)
    capacity_text = capacity_report_to_text(report)

    # Build goals text
    done_text = "\n".join(
        f"  - [{g['category']}] {g['text']}" for g in goals_by_status["done"]
    ) if goals_by_status["done"] else "  (none yet)"

    in_progress_text = "\n".join(
        f"  - [{g['category']}] {g['text']}" for g in goals_by_status["in_progress"]
    ) if goals_by_status["in_progress"] else "  (none)"

    signals_text = "\n".join(f"  - {s}" for s in progress_signals) if progress_signals else "  (no signals)"

    pct = int(done_count / total_goals * 100) if total_goals > 0 else 0

    context = (
        f"Quarter: {_quarter_label(quarter)} (midpoint)\n"
        f"Progress: {done_count}/{total_goals} ({pct}%)\n\n"
        f"Completed:\n{done_text}\n\n"
        f"In Progress (unfinished):\n{in_progress_text}\n\n"
        f"Recent Progress Signals:\n{signals_text}\n\n"
        f"Domain Snapshot:\n{snapshot_text}\n\n"
        f"Capacity Report:\n{capacity_text}"
    )

    review = await ask_claude_fn(
        f"Generate a mid-quarter review for {_quarter_label(quarter)}.\n\n"
        "For each unfinished goal, assess:\n"
        "- ON TRACK / AT RISK / BEHIND\n"
        "- One-line rationale based on the data\n\n"
        "Then provide recommendations:\n"
        "- Which goals to accelerate (close to done)\n"
        "- Which to adjust scope (too ambitious for remaining time)\n"
        "- Which to defer to next quarter\n"
        "- Which to celebrate (already done)\n\n"
        "End with a single action item for this week.\n"
        "Be direct and specific. Under 20 lines.",
        context,
        system_extra=f"Today's date: {today.isoformat()}",
    )

    return (
        f"Mid-Quarter Review — {_quarter_label(quarter)}\n"
        f"Progress: {done_count}/{total_goals} ({pct}%)\n\n"
        f"{review}"
    )


def get_goal_progress_summary() -> str:
    """Generate a compact goal progress summary for the weekly synthesis digest.

    Returns formatted text with progress bars per category.
    """
    from actions.goals import GOALS_FILE, _parse_goals

    quarter = _current_quarter()

    try:
        if not GOALS_FILE.exists():
            return f"{quarter} Goals: No goals file"

        content = GOALS_FILE.read_text(encoding="utf-8")
        goals = _parse_goals(content)
        q_goals = goals.get(quarter, {})

        if not q_goals:
            return f"{quarter} Goals: None set"

        lines = [f"{quarter} Goal Progress:"]
        total_all = 0
        done_all = 0

        for category, items in q_goals.items():
            if not items:
                continue
            total = len(items)
            done = sum(1 for item in items if item["done"])
            total_all += total
            done_all += done

            # Progress bar: filled blocks for done, empty for remaining
            bar_filled = "█" * done
            bar_empty = "░" * (total - done)
            pct = int(done / total * 100) if total > 0 else 0
            lines.append(f"  {category.capitalize():10s} {bar_filled}{bar_empty} {done}/{total} ({pct}%)")

        if total_all > 0:
            overall_pct = int(done_all / total_all * 100)
            lines.append(f"  Overall: {done_all}/{total_all} ({overall_pct}%)")

        return "\n".join(lines)
    except Exception as e:
        log.debug("Goal progress summary failed: %s", e)
        return f"{quarter} Goals: Error reading goals"
