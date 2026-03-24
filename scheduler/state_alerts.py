"""State-aware proactive alerts — Milestone 8, Task 8.5.

Uses live state data to generate context-aware alerts:
- Meeting prep (3+ attendees within 30 min)
- Email urgency (important senders unread 24h+)
- Deep work windows (2+ hour calendar gaps)

Run every 30 min during work hours (8 AM - 6 PM weekdays).
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import TIMEZONE

log = logging.getLogger("khalil.scheduler.state_alerts")


async def check_meeting_prep(state: dict) -> str | None:
    """If meeting with 3+ attendees starts within 30 min, send prep brief."""
    cal = state.get("calendar", {})
    mtg = cal.get("next_meeting")
    if mtg is None:
        return None

    minutes_until = mtg.get("minutes_until", 999)
    attendees = mtg.get("attendees", [])

    if minutes_until <= 30 and len(attendees) >= 3:
        title = mtg.get("title", "Meeting")
        attendee_list = ", ".join(a for a in attendees[:5])
        if len(attendees) > 5:
            attendee_list += f" (+{len(attendees) - 5} more)"
        return (
            f"Meeting Prep: \"{title}\" in {minutes_until} min\n"
            f"  Attendees ({len(attendees)}): {attendee_list}\n"
            f"  Consider reviewing context before joining."
        )
    return None


async def check_email_urgency(state: dict) -> str | None:
    """Flag emails from important senders unread 2+ days."""
    email_state = state.get("email", {})
    needs_reply = email_state.get("needs_reply", [])
    if not needs_reply:
        return None

    important_keywords = ["director", "vp", "head of", "skip-level", "cto", "ceo"]
    urgent = []
    for email in needs_reply:
        sender = email.get("from", "").lower()
        if any(kw in sender for kw in important_keywords):
            urgent.append(email.get("from", "unknown"))

    if urgent:
        names = ", ".join(urgent[:3])
        return (
            f"Important emails need reply: {names}\n"
            f"  These have been unread for 24h+. Consider responding today."
        )
    return None


async def check_deep_work_window(state: dict) -> str | None:
    """If calendar is clear for 2+ hours, suggest deep work."""
    cal = state.get("calendar", {})
    events = cal.get("events")
    if events is None:
        return None

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    if now.hour < 8 or now.hour >= 18:
        return None

    next_event_start = None
    for event in events:
        if event.get("all_day"):
            continue
        try:
            start_dt = datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
            start_dt = start_dt.astimezone(tz)
            if start_dt > now:
                next_event_start = start_dt
                break
        except (ValueError, TypeError):
            continue

    if next_event_start is None:
        end_of_work = now.replace(hour=18, minute=0, second=0, microsecond=0)
        hours_free = (end_of_work - now).total_seconds() / 3600
        if hours_free >= 2:
            return (
                f"Deep work window: {hours_free:.0f}h free until end of day.\n"
                f"  No more meetings -- good time for focused work."
            )
    else:
        hours_until = (next_event_start - now).total_seconds() / 3600
        if hours_until >= 2:
            return (
                f"Deep work window: {hours_until:.0f}h until next meeting.\n"
                f"  Good time for focused work."
            )

    return None


async def run_state_aware_checks() -> list[str]:
    """Run state-aware proactive checks using live state data.

    Called every 30 min during work hours (8 AM - 6 PM weekdays).
    """
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    if now.weekday() >= 5:
        return []
    if now.hour < 8 or now.hour >= 18:
        return []

    try:
        from state.collector import collect_live_state
        state = await collect_live_state()
    except Exception as e:
        log.error("State-aware checks failed to collect state: %s", e)
        return []

    checks = [
        check_meeting_prep(state),
        check_email_urgency(state),
        check_deep_work_window(state),
    ]

    results = await asyncio.gather(*checks, return_exceptions=True)

    findings = []
    for result in results:
        if isinstance(result, Exception):
            log.error("State-aware check failed: %s", result)
        elif result:
            findings.append(result)

    return findings
