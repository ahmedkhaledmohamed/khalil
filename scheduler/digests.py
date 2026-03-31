"""Digest generation — morning brief, weekly summary, financial alerts, career alerts."""

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

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


async def _get_weather() -> str:
    """Fetch current weather via actions.weather module."""
    from actions.weather import get_weather_summary
    return await get_weather_summary()


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

    # Gather context — sync calls
    personal = get_relevant_context("current work projects goals", max_chars=1500)

    from actions.reminders import list_reminders
    reminders = list_reminders()

    # --- Parallel async data gathering (with failure tracking) ---
    _failed_sources = []

    async def _fetch_recent():
        return await hybrid_search("recent important updates reminders deadlines", limit=5)

    async def _fetch_calendar():
        try:
            from actions.calendar import get_today_events, format_events_text
            events = await get_today_events()
            if events:
                return f"\n\nToday's calendar ({len(events)} events):\n{format_events_text(events)}"
        except Exception as e:
            log.warning("Calendar fetch for brief failed: %s", e)
            _failed_sources.append("Calendar")
        return ""

    async def _fetch_jobs():
        try:
            from actions.jobs import fetch_new_jobs
            jobs = await fetch_new_jobs()
            if jobs:
                return "\n\nNew job matches ({}):\n".format(len(jobs)) + "\n".join(
                    f"- {j['title']} @ {j['company']}" for j in jobs[:3]
                )
        except Exception:
            _failed_sources.append("Jobs")
        return ""

    async def _fetch_github_notifications():
        try:
            from actions.github_api import get_notifications
            notifications = await get_notifications(unread_only=True)
            if notifications:
                return f"\n\nGitHub: {len(notifications)} unread notification(s)"
        except Exception as e:
            log.warning("GitHub notifications fetch for brief failed: %s", e)
            _failed_sources.append("GitHub")
        return ""

    async def _fetch_appstore_summary():
        try:
            from actions.appstore import get_app_ratings, get_app_downloads
            from config import ZIA_APP_ID
            if not ZIA_APP_ID:
                return ""
            ratings, downloads = await asyncio.gather(
                get_app_ratings(ZIA_APP_ID),
                get_app_downloads(ZIA_APP_ID, days=7),
            )
            parts = []
            if ratings:
                parts.append(f"{ratings.get('average_rating', '?')}★")
            if downloads:
                parts.append(f"{downloads.get('total', '?')} downloads (7d)")
            if parts:
                return f"\n\nZia (App Store): {', '.join(parts)}"
        except Exception as e:
            log.warning("App Store fetch for brief failed: %s", e)
            _failed_sources.append("App Store")
        return ""

    async def _fetch_server_health():
        try:
            from actions.digitalocean import get_droplets, get_monthly_spend
            droplets, spend = await asyncio.gather(get_droplets(), get_monthly_spend())
            parts = []
            if droplets:
                active = [d for d in droplets if d.get("status") == "active"]
                parts.append(f"{len(active)}/{len(droplets)} droplets active")
            if spend:
                parts.append(f"${spend.get('month_to_date_usage', '?')} MTD")
            if parts:
                return f"\n\nServers: {', '.join(parts)}"
        except Exception as e:
            log.warning("DigitalOcean fetch for brief failed: %s", e)
            _failed_sources.append("Servers")
        return ""

    async def _fetch_spotify_recent():
        try:
            from actions.spotify import get_recently_played
            tracks = await get_recently_played(limit=3)
            if tracks:
                names = [f"{t.get('name', '?')} — {t.get('artist', '?')}" for t in tracks]
                return f"\n\nRecently played: {'; '.join(names)}"
        except Exception as e:
            log.warning("Spotify fetch for brief failed: %s", e)
            _failed_sources.append("Spotify")
        return ""

    async def _fetch_readwise_daily():
        try:
            from actions.readwise import get_daily_review
            highlights = await get_daily_review()
            if highlights:
                return f"\n\nReadwise: {len(highlights)} highlight(s) for review today"
        except Exception as e:
            log.warning("Readwise fetch for brief failed: %s", e)
            _failed_sources.append("Readwise")
        return ""

    (recent_raw, weather, calendar_text, job_text, github_text,
     appstore_text, server_text, spotify_text, readwise_text) = await asyncio.gather(
        _fetch_recent(),
        _get_weather(),
        _fetch_calendar(),
        _fetch_jobs(),
        _fetch_github_notifications(),
        _fetch_appstore_summary(),
        _fetch_server_health(),
        _fetch_spotify_recent(),
        _fetch_readwise_daily(),
    )

    recent_text = "\n".join(
        f"- [{r['category']}] {r['title']}: {r['content'][:150]}" for r in recent_raw
    )

    # Reminders (from sync call above)
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

    weather_text = f"\n\nToronto weather: {weather}" if weather else ""

    # Work priorities (sync, non-blocking)
    work_text = ""
    try:
        from actions.work import get_sprint_summary, get_p0_epics
        work_text = f"\n\nWork:\n{get_sprint_summary()}\n\nP0 Epics:\n{get_p0_epics()}"
    except Exception:
        pass

    # Goal progress (sync, non-blocking)
    goal_text = ""
    try:
        from actions.goals import get_goal_summary
        summary = get_goal_summary()
        goal_text = f"\n\nGoals: {summary}"
    except Exception:
        pass

    # Financial deadlines within 14 days (sync, non-blocking)
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

    # Note which data sources failed so the LLM can mention it
    failed_text = ""
    if _failed_sources:
        failed_text = f"\n\n[Data sources unavailable: {', '.join(_failed_sources)}]"
        log.warning("Morning brief: %d data sources failed: %s", len(_failed_sources), _failed_sources)

    context = (
        f"Personal Profile:\n{personal}\n\n"
        f"Recent Items:\n{recent_text}"
        f"{reminder_text}{weather_text}{calendar_text}{job_text}{github_text}"
        f"{appstore_text}{server_text}{spotify_text}{readwise_text}"
        f"{work_text}{goal_text}{deadline_text}{failed_text}"
    )

    brief = await ask_claude_fn(
        f"Generate a concise morning brief for the user. Today is {day_name}.\n"
        f"Day style: {day_style}\n\n"
        "Include:\n"
        "- Weather at the top (one line)\n"
        "- Today's calendar events (if any)\n"
        "- Reminders due today (if any, highlight them)\n"
        "- Work priorities: P0 epics or key in-progress items (1-2 lines)\n"
        "- Goal progress (one line if goals exist)\n"
        "- Financial deadlines if any are within 14 days or passed\n"
        "- GitHub unread notifications count (if any)\n"
        "- App Store stats for Zia if available (one line)\n"
        "- Server health if available (one line)\n"
        "- Readwise daily review count if available (one line)\n"
        "- Job matches if any new ones found\n"
        "- If any data sources were unavailable, note it briefly at the end (e.g. 'Note: Calendar was unreachable')\n"
        "- A closing line with suggested focus for the day\n"
        "- Keep it under 18 lines, be direct and actionable.",
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
        f"Based on the user's financial records below, identify any time-sensitive items for {month} {today.year}:\n"
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
        "Generate a concise weekly summary for the user. Include:\n"
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
        "Generate a Friday end-of-week reflection for the user. "
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


async def generate_meeting_prep(ask_claude_fn, event: dict) -> str:
    """#91: Generate a meeting prep brief for a calendar event.

    Args:
        ask_claude_fn: async callable(query, context, system_extra) -> str
        event: dict with keys like 'summary', 'start', 'end', 'attendees', 'description'

    Returns formatted prep brief text.
    """
    from knowledge.search import hybrid_search

    subject = event.get("summary", "Untitled Meeting")
    attendees = event.get("attendees", [])
    description = event.get("description", "")
    start = event.get("start", "")

    # Search knowledge base for relevant context
    attendee_info = ""
    if attendees:
        attendee_names = [a.get("displayName") or a.get("email", "") for a in attendees[:5]]
        for name in attendee_names:
            if name:
                results = await hybrid_search(name, limit=2)
                if results:
                    attendee_info += f"\n{name}:\n" + "\n".join(
                        f"  - {r['title']}: {r['content'][:100]}" for r in results
                    )

    # Search for related emails / notes by meeting subject
    related = await hybrid_search(subject, limit=5)
    related_text = "\n".join(
        f"- [{r['category']}] {r['title']}: {r['content'][:150]}" for r in related
    ) if related else "No related items found."

    # Search for previous meeting notes
    prev_notes = await hybrid_search(f"meeting notes {subject}", limit=3)
    notes_text = "\n".join(
        f"- {r['title']}: {r['content'][:150]}" for r in prev_notes
    ) if prev_notes else ""

    context = (
        f"Meeting: {subject}\n"
        f"Time: {start}\n"
        f"Description: {description}\n\n"
        f"Attendees:{attendee_info if attendee_info else ' (none listed)'}\n\n"
        f"Related Items:\n{related_text}\n"
        f"{f'Previous Meeting Notes:{chr(10)}{notes_text}' if notes_text else ''}"
    )

    brief = await ask_claude_fn(
        f"Generate a concise meeting prep brief for: {subject}\n\n"
        "Include:\n"
        "- Key context from related items\n"
        "- What you know about attendees (if anything)\n"
        "- Suggested talking points or questions\n"
        "- Any follow-ups from previous meetings\n"
        "Keep it under 10 lines, be direct and actionable.",
        context,
        system_extra=f"Meeting time: {start}",
    )

    return f"Meeting Prep: {subject}\n\n{brief}"


async def generate_weekly_synthesis(ask_claude_fn) -> str:
    """Generate weekly synthesis digest — replaces generic weekly summary.

    Sections: Capacity Score, Cross-domain risks, Recommendations, Wins.
    Delivered Sunday evening.
    """
    from synthesis.aggregator import aggregate_all_domains, snapshot_to_text
    from synthesis.capacity import detect_overcommitment, capacity_report_to_text

    today = date.today()

    # Aggregate all domains and detect overcommitment
    snapshot = await aggregate_all_domains()
    report = await detect_overcommitment(snapshot)

    snapshot_text = snapshot_to_text(snapshot)
    capacity_text = capacity_report_to_text(report)

    # Gather wins — completed goals, resolved items
    wins = []
    try:
        from actions.goals import GOALS_FILE, _parse_goals, _current_quarter
        if GOALS_FILE.exists():
            content = GOALS_FILE.read_text(encoding="utf-8")
            goals = _parse_goals(content)
            q_goals = goals.get(_current_quarter(), {})
            for cat, items in q_goals.items():
                for item in items:
                    if item["done"]:
                        wins.append(f"[{cat}] {item['text']}")
    except Exception:
        pass

    wins_text = "\n".join(f"  - {w}" for w in wins[:5]) if wins else "  (none tracked this period)"

    # M12: Goal progress bar for weekly digest
    goal_progress_text = ""
    try:
        from scheduler.planning import get_goal_progress_summary
        goal_progress_text = get_goal_progress_summary()
    except Exception:
        pass

    # M12: Weekly goal activity signals
    goal_signals_text = ""
    try:
        from learning import get_weekly_goal_progress
        signals = get_weekly_goal_progress(days=7)
        if signals:
            goal_signals_text = "Goal Activity This Week:\n" + "\n".join(
                f"  - {s['description']} (x{s['count']})" for s in signals[:5]
            )
    except Exception:
        pass

    context = (
        f"Domain Snapshot:\n{snapshot_text}\n\n"
        f"Capacity Report:\n{capacity_text}\n\n"
        f"Completed Goals:\n{wins_text}\n\n"
        f"{f'{goal_progress_text}{chr(10)}{chr(10)}' if goal_progress_text else ''}"
        f"{f'{goal_signals_text}{chr(10)}{chr(10)}' if goal_signals_text else ''}"
    )

    synthesis = await ask_claude_fn(
        "Generate a weekly synthesis digest for the user. Structure it exactly as:\n\n"
        "1. **Capacity Score** — state the score, label, and one-line interpretation\n"
        "2. **Cross-Domain Risks** — top 3 risks from the data, be specific\n"
        "3. **Recommendations** — top 3 actionable items, each starting with a verb\n"
        "4. **Wins** — acknowledge completed goals or resolved items\n\n"
        "Be direct, specific to the data. No filler. Under 20 lines.",
        context,
        system_extra=f"Today's date: {today.isoformat()}, Sunday evening",
    )

    return f"Weekly Synthesis — {today.isoformat()}\n\n{synthesis}"


async def generate_evening_digest(ask_claude_fn) -> str:
    """Generate evening digest — nutrition, health, focus recap for the day.

    Sent around 9 PM. Covers: calorie/protein, steps, fasting, pomodoro stats.
    """
    today = date.today()
    today_iso = today.isoformat()
    parts = []

    # Nutrition
    try:
        from actions.calorie_tracker import get_daily_summary
        cal = get_daily_summary()
        if cal.get("calories"):
            goal_str = f"/{cal['calorie_goal']}" if cal.get("calorie_goal") else ""
            parts.append(
                f"Nutrition: {cal['calories']}{goal_str} cal, "
                f"{cal.get('protein_g', 0)}g protein, {cal.get('meals', 0)} meals"
            )
    except Exception:
        pass

    # Steps
    try:
        from actions.apple_health import _get_health_data
        steps_data = await _get_health_data("steps")
        if steps_data:
            steps = steps_data.get("steps_today", 0)
            goal = steps_data.get("goal", 10000)
            pct = int(steps / goal * 100) if goal else 0
            parts.append(f"Steps: {steps:,}/{goal:,} ({pct}%)")
    except Exception:
        pass

    # Fasting
    try:
        from actions.fasting_tracker import get_status
        fast = get_status()
        if fast:
            parts.append(
                f"Fasting: {fast['elapsed_hours']:.1f}/{fast['target_hours']}h "
                f"({fast.get('protocol', '')})"
            )
    except Exception:
        pass

    # Focus / Pomodoro
    try:
        from actions.pomodoro import get_today_stats
        pomo = get_today_stats()
        if pomo.get("sessions"):
            parts.append(
                f"Focus: {pomo['sessions']} sessions, {pomo['total_min']}min, "
                f"{pomo['completed']} completed"
            )
    except Exception:
        pass

    if not parts:
        return f"🌙 Evening Digest — {today_iso}\n\nNo health/focus data logged today."

    data_text = "\n".join(f"- {p}" for p in parts)

    brief = await ask_claude_fn(
        "Generate a concise evening health/focus recap for the user. "
        "Include the data below and add a brief observation or encouragement. "
        "Keep it under 8 lines, be direct.",
        f"Today's data:\n{data_text}",
        system_extra=f"Today's date: {today_iso}, evening",
    )

    return f"\U0001f319 Evening Digest — {today_iso}\n\n{brief}"


async def generate_capability_health_digest() -> str:
    """Generate weekly capability health report from learning system."""
    from learning import get_capability_health, format_capability_health

    report = get_capability_health(days=7)
    return format_capability_health(report)


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
