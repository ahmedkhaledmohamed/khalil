"""Calendar state provider — fetch today's events and next meeting."""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import TOKEN_FILE_CALENDAR, TIMEZONE

log = logging.getLogger("khalil.state.calendar")


def _get_calendar_service():
    """Get Calendar API service using existing OAuth tokens."""
    from googleapiclient.discovery import build
    from oauth_utils import load_credentials

    scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    creds = load_credentials(TOKEN_FILE_CALENDAR, scopes, allow_interactive=False)
    return build("calendar", "v3", credentials=creds)


def _fetch_today_events_sync() -> list[dict]:
    """Fetch today's events (sync, runs in thread)."""
    service = _get_calendar_service()
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)

    events_result = service.events().list(
        calendarId="primary",
        timeMin=start_of_day.isoformat(),
        timeMax=end_of_day.isoformat(),
        maxResults=20,
        singleEvents=True,
        orderBy="startTime",
        timeZone=TIMEZONE,
    ).execute()

    events = events_result.get("items", [])
    result = []
    for event in events:
        start_raw = event["start"].get("dateTime", event["start"].get("date", ""))
        end_raw = event["end"].get("dateTime", event["end"].get("date", ""))

        attendees = []
        for a in event.get("attendees", []):
            attendees.append(a.get("email", ""))

        result.append({
            "title": event.get("summary", "(no title)"),
            "start": start_raw,
            "end": end_raw,
            "attendees": attendees,
            "all_day": "date" in event["start"],
        })

    return result


async def get_today_events() -> list[dict]:
    """Get today's calendar events. Returns list of {title, start, end, attendees, all_day}."""
    return await asyncio.to_thread(_fetch_today_events_sync)


async def get_next_meeting(within_minutes: int = 60) -> dict | None:
    """Get the next upcoming meeting within N minutes. Returns dict or None."""
    events = await get_today_events()
    if not events:
        return None

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    cutoff = now + timedelta(minutes=within_minutes)

    for event in events:
        if event["all_day"]:
            continue
        try:
            start_dt = datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
            start_dt = start_dt.astimezone(tz)
        except (ValueError, TypeError):
            continue

        if now <= start_dt <= cutoff:
            minutes_until = int((start_dt - now).total_seconds() / 60)
            return {
                **event,
                "minutes_until": minutes_until,
            }

    return None
