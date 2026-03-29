"""State-aware proactive alerts — scheduled checks using live state data (M8.5)."""

import logging
import re
from datetime import datetime, timedelta
import zoneinfo

from config import TIMEZONE

log = logging.getLogger("pharoclaw.scheduler.state_alerts")

# Patterns that suggest important senders
_IMPORTANT_SENDER_PATTERNS = re.compile(
    r"\b(director|vp|vice\s*president|cto|ceo|cfo|coo|svp|evp|head\s+of)\b",
    re.IGNORECASE,
)


async def check_meeting_prep(channel, chat_id: int):
    """Check for meetings with 3+ attendees within 30 minutes and send a brief."""
    try:
        from state.calendar_provider import get_today_events
        from actions.meetings import build_meeting_context

        events = await get_today_events()
        if not events:
            return

        tz = zoneinfo.ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        window = now + timedelta(minutes=30)

        for event in events:
            if event.get("all_day"):
                continue
            attendees = event.get("attendees", [])
            if len(attendees) < 3:
                continue

            start_str = event.get("start", "")
            try:
                start = datetime.fromisoformat(start_str)
                if start.tzinfo is None:
                    start = start.replace(tzinfo=tz)
            except (ValueError, TypeError):
                continue

            if now < start <= window:
                minutes_until = int((start - now).total_seconds() / 60)
                try:
                    brief = await build_meeting_context(event)
                    await channel.send_message(
                        chat_id,
                        f"📋 Meeting prep — \"{event.get('title', '(no title)')}\" "
                        f"in {minutes_until} min ({len(attendees)} attendees):\n\n{brief}",
                    )
                except Exception as e:
                    log.error("Failed to build meeting context: %s", e)
    except Exception as e:
        log.error("check_meeting_prep failed: %s", e)


async def check_email_urgency(channel, chat_id: int):
    """Flag emails from important senders that are unread 24h+."""
    try:
        from state.email_provider import get_needs_reply

        emails = await get_needs_reply(max_results=20)
        if not emails:
            return

        tz = zoneinfo.ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        cutoff = now - timedelta(hours=24)

        urgent = []
        for email in emails:
            sender = email.get("from", "")
            date_str = email.get("date", "")

            # Check if sender matches important patterns
            if not _IMPORTANT_SENDER_PATTERNS.search(sender):
                continue

            # Check if older than 24 hours
            try:
                email_date = datetime.fromisoformat(date_str)
                if email_date.tzinfo is None:
                    email_date = email_date.replace(tzinfo=tz)
                if email_date > cutoff:
                    continue  # Not old enough
            except (ValueError, TypeError):
                continue  # Can't parse date, skip

            urgent.append(email)

        if urgent:
            lines = ["🚨 Urgent emails needing reply (24h+ from important senders):"]
            for e in urgent[:5]:
                lines.append(
                    f"  • From: {e.get('from', '?')} — {e.get('subject', '(no subject)')}"
                )
            await channel.send_message(chat_id, "\n".join(lines))
    except Exception as e:
        log.error("check_email_urgency failed: %s", e)


async def check_deep_work_window(channel, chat_id: int):
    """Detect 2+ hour calendar gaps and suggest focus time."""
    try:
        from state.calendar_provider import get_today_events

        events = await get_today_events()
        tz = zoneinfo.ZoneInfo(TIMEZONE)
        now = datetime.now(tz)

        # Only look at future events today
        future_events = []
        for event in (events or []):
            if event.get("all_day"):
                continue
            start_str = event.get("start", "")
            end_str = event.get("end", "")
            try:
                start = datetime.fromisoformat(start_str)
                end = datetime.fromisoformat(end_str)
                if start.tzinfo is None:
                    start = start.replace(tzinfo=tz)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=tz)
                if end > now:
                    future_events.append({"start": start, "end": end, "title": event.get("title", "")})
            except (ValueError, TypeError):
                continue

        future_events.sort(key=lambda e: e["start"])

        # Find gaps of 2+ hours between now and remaining events
        # Start from now (or end of current meeting if in one)
        cursor = now
        for event in future_events:
            if event["start"] <= now <= event["end"]:
                cursor = event["end"]
                continue
            if event["start"] > cursor:
                gap = (event["start"] - cursor).total_seconds() / 3600
                if gap >= 2:
                    gap_start = cursor.strftime("%I:%M %p")
                    gap_end = event["start"].strftime("%I:%M %p")
                    await channel.send_message(
                        chat_id,
                        f"🧠 Deep work window: {gap_start} - {gap_end} ({gap:.1f}h free). "
                        f"Good time to focus on high-priority work.",
                    )
                    return  # Only notify about the first gap
            cursor = max(cursor, event["end"])

        # Check gap between last event and end of day (6 PM)
        end_of_day = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if cursor < end_of_day:
            gap = (end_of_day - cursor).total_seconds() / 3600
            if gap >= 2:
                gap_start = cursor.strftime("%I:%M %p")
                await channel.send_message(
                    chat_id,
                    f"🧠 Deep work window: {gap_start} - 6:00 PM ({gap:.1f}h free). "
                    f"Good time to focus on high-priority work.",
                )
    except Exception as e:
        log.error("check_deep_work_window failed: %s", e)


async def run_state_aware_checks(channel, chat_id: int):
    """Run all state-aware checks. Only during weekday work hours (8-18)."""
    tz = zoneinfo.ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    # Skip weekends (Mon=0, Sun=6)
    if now.weekday() >= 5:
        return

    # Skip outside work hours
    if not (8 <= now.hour < 18):
        return

    log.info("Running state-aware checks")
    await check_meeting_prep(channel, chat_id)
    await check_email_urgency(channel, chat_id)
    await check_deep_work_window(channel, chat_id)
