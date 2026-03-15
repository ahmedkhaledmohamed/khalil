"""Digest generation — morning brief, weekly summary, financial alerts, career alerts."""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from config import TIMEZONE

log = logging.getLogger("khalil.scheduler")

# Day-of-week brief style
DAY_STYLE = {
    0: "Monday — start of the work week. Focus on priorities and planning.",
    1: "Tuesday — deep work day. What needs your focused attention?",
    2: "Wednesday — midweek checkpoint. How are things tracking?",
    3: "Thursday — push to finish. What can you close out this week?",
    4: "Friday — wrap-up day. Tie loose ends, plan for next week.",
    5: "Saturday — personal day. Side projects, family, rest.",
    6: "Sunday — prep day. Review the week ahead.",
}


async def _get_weather_toronto() -> str:
    """Fetch current Toronto weather from Open-Meteo (free, no API key)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": 43.65,
                    "longitude": -79.38,
                    "current": "temperature_2m,weather_code",
                    "timezone": "America/Toronto",
                },
            )
            resp.raise_for_status()
            data = resp.json()["current"]
            temp = data["temperature_2m"]
            code = data["weather_code"]
            # Simple weather code interpretation
            conditions = {
                0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
                45: "Foggy", 48: "Foggy", 51: "Light drizzle", 53: "Drizzle",
                55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
                71: "Light snow", 73: "Snow", 75: "Heavy snow", 80: "Rain showers",
                95: "Thunderstorm",
            }
            desc = conditions.get(code, f"Code {code}")
            return f"{temp}°C, {desc}"
    except Exception as e:
        log.debug("Weather fetch failed: %s", e)
        return ""


async def generate_morning_brief(ask_claude_fn) -> str:
    """Generate the morning brief sent to Telegram at 7 AM.

    Args:
        ask_claude_fn: async callable(query, context, system_extra) -> str
    """
    from knowledge.context import get_relevant_context
    from knowledge.search import hybrid_search

    today = date.today()
    today_iso = today.isoformat()
    day_name = today.strftime("%A")
    day_style = DAY_STYLE.get(today.weekday(), "")

    # Gather context
    personal = get_relevant_context("current work projects goals", max_chars=1500)

    # Search for recent / relevant items
    recent = await hybrid_search("recent important updates reminders deadlines", limit=5)
    recent_text = "\n".join(
        f"- [{r['category']}] {r['title']}: {r['content'][:150]}" for r in recent
    )

    # Check active reminders — due today or upcoming
    from actions.reminders import list_reminders
    reminders = list_reminders()
    reminder_text = ""
    if reminders:
        due_today = [r for r in reminders if r["due_at"][:10] == today_iso]
        upcoming = [r for r in reminders if r["due_at"] > today_iso][:5]
        parts = []
        if due_today:
            parts.append("Due TODAY:\n" + "\n".join(f"  ⚡ {r['text']}" for r in due_today))
        if upcoming:
            parts.append("Upcoming:\n" + "\n".join(
                f"  - {r['text']} ({r['due_at'][:16]})" for r in upcoming
            ))
        if parts:
            reminder_text = "\n\nReminders:\n" + "\n".join(parts)

    # Weather
    weather = await _get_weather_toronto()
    weather_text = f"\n\nToronto weather: {weather}" if weather else ""

    # Calendar events (non-blocking, best-effort)
    calendar_text = ""
    try:
        from actions.calendar import get_today_events, format_events_text
        events = await get_today_events()
        if events:
            calendar_text = f"\n\nToday's calendar ({len(events)} events):\n{format_events_text(events)}"
    except Exception as e:
        log.debug("Calendar fetch for brief failed: %s", e)

    # Job matches (non-blocking, best-effort)
    job_text = ""
    try:
        from actions.jobs import fetch_new_jobs
        jobs = await fetch_new_jobs()
        if jobs:
            job_text = f"\n\nNew job matches ({len(jobs)}):\n" + "\n".join(
                f"- {j['title']} @ {j['company']}" for j in jobs[:3]
            )
    except Exception:
        pass

    # Work priorities (non-blocking)
    work_text = ""
    try:
        from actions.work import get_sprint_summary, get_p0_epics
        work_text = f"\n\nWork:\n{get_sprint_summary()}\n\nP0 Epics:\n{get_p0_epics()}"
    except Exception:
        pass

    # Goal progress (non-blocking)
    goal_text = ""
    try:
        from actions.goals import get_goal_summary
        summary = get_goal_summary()
        goal_text = f"\n\nGoals: {summary}"
    except Exception:
        pass

    # Financial deadlines within 14 days (non-blocking)
    deadline_text = ""
    try:
        from actions.finance import get_deadlines
        deadlines = get_deadlines()
        urgent = [d for d in deadlines if -7 <= d["days_away"] <= 14]
        if urgent:
            deadline_text = "\n\nFinancial deadlines:\n" + "\n".join(
                f"- {'⚠️ PASSED: ' if d['status'] == 'PASSED' else ''}{d['item']} ({d['date']})"
                for d in urgent
            )
    except Exception:
        pass

    context = (
        f"Personal Profile:\n{personal}\n\n"
        f"Recent Items:\n{recent_text}"
        f"{reminder_text}{weather_text}{calendar_text}{job_text}"
        f"{work_text}{goal_text}{deadline_text}"
    )

    brief = await ask_claude_fn(
        f"Generate a concise morning brief for Ahmed. Today is {day_name}.\n"
        f"Day style: {day_style}\n\n"
        "Include:\n"
        "- Weather at the top (one line)\n"
        "- Today's calendar events (if any)\n"
        "- Reminders due today (if any, highlight them)\n"
        "- Work priorities: P0 epics or key in-progress items (1-2 lines)\n"
        "- Goal progress (one line if goals exist)\n"
        "- Financial deadlines if any are within 14 days or passed\n"
        "- Job matches if any new ones found\n"
        "- A closing line with suggested focus for the day\n"
        "- Keep it under 15 lines, be direct and actionable.",
        context,
        system_extra=f"Today's date: {today_iso}, {day_name}",
    )

    return f"☀️ Morning Brief — {day_name}, {today_iso}\n\n{brief}"


async def generate_financial_alert(ask_claude_fn) -> str | None:
    """Generate financial alerts from knowledge base context.

    Returns alert text, or None if nothing notable.
    """
    from knowledge.search import hybrid_search, keyword_search

    today = date.today()
    month = today.strftime("%B")

    # Search for financial context
    queries = [
        "RRSP TFSA contribution limit",
        "RSU vesting schedule stock",
        "tax deadline CRA filing",
        "bill payment subscription renewal",
    ]

    all_results = []
    for q in queries:
        results = await hybrid_search(q, limit=3, category="email:finance")
        all_results.extend(results)
        # Also check keyword in broader categories
        kw = keyword_search(q, limit=2)
        all_results.extend(kw)

    if not all_results:
        return None

    # Deduplicate
    seen = set()
    unique = []
    for r in all_results:
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)

    context = "\n\n".join(
        f"[{r['category']}] {r['title']}: {r['content'][:200]}" for r in unique[:10]
    )

    alert = await ask_claude_fn(
        f"Based on Ahmed's financial records below, identify any time-sensitive items for {month} {today.year}:\n"
        "- RRSP/TFSA contribution deadlines or room\n"
        "- RSU vesting dates coming up\n"
        "- Tax filing deadlines\n"
        "- Subscription renewals\n\n"
        "Only mention items that are actually relevant NOW. If nothing is urgent, respond with just 'Nothing urgent.'",
        context,
        system_extra=f"Today's date: {today.isoformat()}",
    )

    if "nothing urgent" in alert.lower():
        return None

    return f"💰 Financial Alert — {month} {today.year}\n\n{alert}"


async def generate_weekly_summary(ask_claude_fn) -> str:
    """Generate weekly summary from knowledge base."""
    from knowledge.context import get_relevant_context
    from knowledge.search import hybrid_search
    from actions.reminders import list_reminders

    personal = get_relevant_context("current work projects goals", max_chars=1000)

    # Check for stale reminders
    reminders = list_reminders()
    stale = [r for r in reminders if r["due_at"] < datetime.now(ZoneInfo(TIMEZONE)).isoformat()]
    stale_text = ""
    if stale:
        stale_text = "\n\nStale reminders (past due):\n" + "\n".join(
            f"- #{r['id']}: {r['text']} (was due: {r['due_at'][:16]})" for r in stale
        )

    recent = await hybrid_search("work projects updates this week", limit=5)
    recent_text = "\n".join(
        f"- {r['title']}: {r['content'][:100]}" for r in recent
    )

    # Check for projects with open tasks
    project_text = ""
    try:
        from actions.projects import get_stale_projects
        stale_projects = get_stale_projects()
        if stale_projects:
            project_text = "\n\nProjects with open tasks:\n" + "\n".join(
                f"- {p}" for p in stale_projects
            )
    except Exception:
        pass

    # What I Learned — recent insights from self-improvement
    learned_text = ""
    try:
        from learning import get_insights
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        recent_insights = get_insights(limit=5)
        this_week = [i for i in recent_insights if i["created_at"] and i["created_at"] > cutoff]
        if this_week:
            learned_text = "\n\nKhalil's insights this week:\n" + "\n".join(
                f"- [{i['category']}] {i['summary']} (status: {i['status']})" for i in this_week
            )
    except Exception:
        pass

    context = f"Profile:\n{personal}\n\nRecent:\n{recent_text}{stale_text}{project_text}{learned_text}"

    summary = await ask_claude_fn(
        "Generate a concise weekly summary for Ahmed. Include:\n"
        "- Key themes from the past week\n"
        "- Any stale reminders that need attention\n"
        "- Projects with open tasks that may need attention\n"
        "- If there are Khalil insights, include a brief 'What I Learned' section\n"
        "- Suggested focus areas for next week\n"
        "Keep it under 15 lines.",
        context,
        system_extra=f"Today's date: {date.today().isoformat()}",
    )

    return f"📊 Weekly Summary\n\n{summary}"


async def generate_friday_reflection(ask_claude_fn) -> str:
    """Friday 5pm reflection — what moved, what's stuck, what matters next week.

    Different from Sunday weekly summary: this is forward-looking and provocative.
    """
    from actions.reminders import list_reminders
    from actions.work import get_p0_epics, get_in_progress
    from actions.goals import get_goal_summary
    from scheduler.proactive import run_proactive_checks

    today = date.today()

    # Gather week's reminders (fired this week)
    reminder_text = ""
    try:
        reminders = list_reminders()
        if reminders:
            reminder_text = f"Active reminders: {len(reminders)}"
    except Exception:
        pass

    # Proactive findings
    findings_text = ""
    try:
        findings = run_proactive_checks()
        if findings:
            findings_text = "\n\nProactive findings:\n" + "\n".join(f"- {f}" for f in findings)
    except Exception:
        pass

    # Work P0 status
    work_text = ""
    try:
        work_text = f"\n\nP0 Epics:\n{get_p0_epics()}\n\nIn Progress:\n{get_in_progress()}"
    except Exception:
        pass

    # Goal progress
    goal_text = ""
    try:
        goal_text = f"\n\nGoals: {get_goal_summary()}"
    except Exception:
        pass

    context = f"{reminder_text}{findings_text}{work_text}{goal_text}"

    reflection = await ask_claude_fn(
        "Generate a Friday end-of-week reflection for Ahmed. "
        "Based on the data below, ask exactly 3 sharp questions:\n"
        "1. What moved forward this week? (acknowledge progress)\n"
        "2. What's stuck or being avoided? (call it out directly)\n"
        "3. What's the single most important thing for next week?\n\n"
        "Be specific to his actual data — reference real projects, epics, or deadlines. "
        "Be provocative, not comforting. 5-7 lines max. No fluff.",
        context,
        system_extra=f"Today's date: {today.isoformat()}, Friday",
    )

    return f"🪞 Friday Reflection — {today.isoformat()}\n\n{reflection}"


async def generate_career_alert() -> str | None:
    """Run job scraper and return formatted results, or None if no new matches."""
    from actions.jobs import fetch_new_jobs, format_jobs_text

    try:
        jobs = await fetch_new_jobs()
        if not jobs:
            log.info("Career alert: no new job matches")
            return None
        return f"💼 Career Alert — {date.today().isoformat()}\n\n{format_jobs_text(jobs)}"
    except Exception as e:
        log.error("Career alert failed: %s", e)
        return None
