"""Google Calendar integration — fetch upcoming events (read-only).

Uses a separate OAuth token (calendar.readonly scope).
All public functions are async — sync Google API calls run in asyncio.to_thread().
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from config import CREDENTIALS_FILE, TOKEN_FILE_CALENDAR, TIMEZONE

log = logging.getLogger("khalil.actions.calendar")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _get_credentials():
    """Get or refresh OAuth credentials for Calendar readonly."""
    creds = None
    if TOKEN_FILE_CALENDAR.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE_CALENDAR), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Missing {CREDENTIALS_FILE}. "
                    "Download from Google Cloud Console → APIs → Credentials → OAuth 2.0"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE_CALENDAR, "w") as f:
            f.write(creds.to_json())

    return creds


def _get_calendar_service():
    """Get Calendar API service."""
    creds = _get_credentials()
    return build("calendar", "v3", credentials=creds)


def _get_events_sync(days: int = 1, max_results: int = 20) -> list[dict]:
    """Fetch upcoming events. Runs in thread."""
    service = _get_calendar_service()

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()

    events_result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        maxResults=max_results,
        singleEvents=True,
        orderBy="startTime",
        timeZone=TIMEZONE,
    ).execute()

    events = events_result.get("items", [])
    result = []
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date", ""))
        end = event["end"].get("dateTime", event["end"].get("date", ""))
        result.append({
            "summary": event.get("summary", "(no title)"),
            "start": start,
            "end": end,
            "location": event.get("location", ""),
            "description": (event.get("description") or "")[:200],
            "all_day": "date" in event["start"],
        })

    return result


async def get_today_events() -> list[dict]:
    """Get today's calendar events."""
    return await asyncio.to_thread(_get_events_sync, days=1)


async def get_upcoming_events(days: int = 7) -> list[dict]:
    """Get events for the next N days."""
    return await asyncio.to_thread(_get_events_sync, days=days)


def format_events_text(events: list[dict]) -> str:
    """Format events for Telegram display."""
    if not events:
        return "No upcoming events."

    lines = []
    for e in events:
        if e["all_day"]:
            time_str = "All day"
        else:
            # Extract just the time portion
            try:
                dt = datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
                dt = dt.astimezone(ZoneInfo(TIMEZONE))
                time_str = dt.strftime("%I:%M %p")
            except (ValueError, TypeError):
                time_str = e["start"][:16]

        line = f"📅 {time_str} — {e['summary']}"
        if e["location"]:
            line += f"\n   📍 {e['location']}"
        lines.append(line)

    return "\n\n".join(lines)
