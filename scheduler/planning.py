"""Quarterly planning automation — goal setting, alignment, mid-quarter reviews.

Triggered on specific dates:
- Planning prompt: 2 weeks before quarter end (Mar 15, Jun 15, Sep 15, Dec 15)
- Mid-quarter review: quarter midpoint (Feb 15, May 15, Aug 15, Nov 15)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DB_PATH, GOALS_DIR, TIMEZONE

log = logging.getLogger("khalil.scheduler.planning")

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
        f"{_quarter_label(ending_q)} ends in 2 weeks. Generate a quarterly planning prompt for the user.\n\n"
        "Structure:\n"
        f"1. **{ending_q} Recap** — What was accomplished, what fell short (be specific)\n"
        f"2. **Unfinished Business** — What's still open and whether to carry forward or drop\n"
        f"3. **{next_q} Planning** — Based on current capacity and domain snapshot, suggest:\n"
        "   - 2-3 goals per domain (work, projects, personal, finance)\n"
        "   - Flag any capacity conflicts\n"
        "4. **Question** — Ask the user one sharp question about his priorities\n\n"
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


# --- Goal-Driven Daily Planner ---

@dataclass
class DailyAction:
    """A single recommended action for the day."""
    description: str
    time_estimate: str  # "30min", "1h", "2h"
    linked_goal: str    # which goal this advances
    priority: int       # 1 = highest


async def generate_daily_plan(ask_claude_fn) -> list[DailyAction]:
    """Generate 3 prioritized actions for today based on goals, calendar, and capacity.

    Returns a list of DailyAction objects (usually 3).
    """
    # Gather context from multiple sources
    parts = []

    # 1. Goals summary
    goal_summary = get_goal_progress_summary()
    parts.append(f"GOALS:\n{goal_summary}")

    # 2. Domain snapshot (calendar, work, finance, health)
    try:
        from synthesis.aggregator import aggregate_all_domains, snapshot_to_text
        snapshot = await aggregate_all_domains()
        parts.append(f"DOMAIN STATUS:\n{snapshot_to_text(snapshot)}")
    except Exception as e:
        log.debug("Daily plan: snapshot failed: %s", e)

    # 3. Active reminders
    try:
        from actions.reminders import list_reminders
        reminders = list_reminders(status="active")
        if reminders:
            r_text = "\n".join(f"  - {r['text']} (due: {r['due_at']})" for r in reminders[:10])
            parts.append(f"ACTIVE REMINDERS:\n{r_text}")
    except Exception:
        pass

    # 4. Overdue commitments
    try:
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT action_text, person, due_date FROM meeting_commitments "
            "WHERE status = 'open' AND due_date <= date('now') ORDER BY due_date LIMIT 5"
        ).fetchall()
        if rows:
            c_text = "\n".join(f"  - {r['action_text']} (for {r['person']}, due {r['due_date']})" for r in rows)
            parts.append(f"OVERDUE COMMITMENTS:\n{c_text}")
        conn.close()
    except Exception:
        pass

    context = "\n\n".join(parts)

    prompt = (
        "Based on the context below, suggest exactly 3 prioritized actions for today.\n\n"
        "Rules:\n"
        "- Each action should be concrete and completable in one sitting\n"
        "- Link each action to a specific goal or commitment\n"
        "- Include a realistic time estimate\n"
        "- Priority 1 = most important/urgent\n"
        "- Focus on what moves the needle, not busywork\n\n"
        f"Context:\n{context}\n\n"
        "Respond with ONLY a JSON array (no markdown fences):\n"
        '[{"description": "...", "time_estimate": "30min", "linked_goal": "...", "priority": 1}, ...]'
    )

    response = await ask_claude_fn(
        prompt, "",
        system_extra="Respond with ONLY a JSON array of 3 actions. No explanation.",
    )
    response = response.strip()

    # Parse response
    try:
        if "```" in response:
            response = response.split("```")[1]
            if response.startswith("json"):
                response = response[4:]
        actions_data = json.loads(response.strip())
        if not isinstance(actions_data, list):
            return []
        return [
            DailyAction(
                description=a.get("description", ""),
                time_estimate=a.get("time_estimate", ""),
                linked_goal=a.get("linked_goal", ""),
                priority=a.get("priority", i + 1),
            )
            for i, a in enumerate(actions_data[:3])
        ]
    except (json.JSONDecodeError, KeyError) as e:
        log.warning("Daily plan parse failed: %s", e)
        return []


def format_daily_plan(actions: list[DailyAction]) -> str:
    """Format daily plan for Telegram display."""
    if not actions:
        return "📋 Couldn't generate today's plan — try again later."
    lines = ["📋 **Today's Plan**\n"]
    for a in sorted(actions, key=lambda x: x.priority):
        lines.append(
            f"{a.priority}. **{a.description}** (~{a.time_estimate})\n"
            f"   → _{a.linked_goal}_"
        )
    lines.append("\nReply **approve** to set these as reminders, or **dismiss** to skip.")
    return "\n".join(lines)
