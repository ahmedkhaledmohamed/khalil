"""Google Calendar integration — read events and create/update events.

Uses separate OAuth tokens:
- calendar.readonly scope for reading (TOKEN_FILE_CALENDAR)
- calendar.events scope for writing (TOKEN_FILE_CALENDAR_WRITE)

Write operations require re-authorization:
    python3 -c "
    from actions.calendar import _authorize_write
    _authorize_write()
    "

All public functions are async — sync Google API calls run in asyncio.to_thread().
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import TOKEN_FILE_CALENDAR, TIMEZONE

log = logging.getLogger("khalil.actions.calendar")

SKILL = {
    "name": "calendar",
    "description": "Google Calendar — read events, check schedule, create events",
    "category": "productivity",
    "patterns": [
        (r"\bcalendar\b", "calendar"),
        (r"\bwhat'?s\s+on\s+(?:my\s+)?(?:schedule|calendar)\b", "calendar"),
        (r"\bmeeting(?:s)?\s+today\b", "calendar"),
        (r"\b(?:check|show|view|list)\s+(?:my\s+)?(?:schedule|events?|meetings?)\b", "calendar"),
        (r"\b(?:what'?s|what\s+are)\s+my\s+(?:meetings?|events?)\b", "calendar"),
        (r"\bevents?\s+(?:today|tomorrow|this\s+week|status)\b", "calendar"),
        (r"\b(?:am\s+i|are\s+we)\s+free\b", "calendar"),
        (r"\blist\s+(?:today|tomorrow)\b", "calendar"),
        (r"\bdo\s+i\s+have\s+(?:meetings?|events?)\s+today\b", "calendar"),
        (r"\bcalendar\s+for\s+tomorrow\b", "calendar"),
    ],
    "actions": [
        {"type": "calendar", "handler": "handle_intent", "keywords": "calendar schedule meetings events today", "description": "Check calendar and schedule"},
        {"type": "calendar_create", "handler": "handle_intent", "keywords": "schedule book block create event meeting",
         "description": "Create a calendar event",
         "parameters": {
             "summary": {"type": "string", "description": "Event title/summary"},
             "start_time": {"type": "string", "description": "Start time in ISO 8601 format (e.g., 2026-04-07T14:00:00)"},
             "end_time": {"type": "string", "description": "End time in ISO 8601 (defaults to 1 hour after start)"},
             "description": {"type": "string", "description": "Event description/notes"},
             "location": {"type": "string", "description": "Event location"},
         }},
        {"type": "calendar_upcoming", "handler": "handle_intent", "keywords": "calendar week upcoming next days schedule",
         "description": "Check upcoming events for next N days",
         "parameters": {
             "days": {"type": "integer", "description": "Number of days to look ahead (default 7)"},
         }},
    ],
    "examples": ["What's on my calendar today?", "Schedule a meeting tomorrow at 2pm", "Any meetings this week?"],
    "sensor": {"function": "sense_calendar", "interval_min": 5, "identify_opportunities": "identify_calendar_opportunities"},
}

SCOPES_READ = ["https://www.googleapis.com/auth/calendar.readonly"]
SCOPES_WRITE = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

# Backward compatibility
SCOPES = SCOPES_READ

# Write token stored alongside the read token
TOKEN_FILE_CALENDAR_WRITE = TOKEN_FILE_CALENDAR.parent / "token_calendar_write.json"


def _get_credentials(write: bool = False):
    """Get or refresh OAuth credentials for Calendar."""
    from oauth_utils import load_credentials
    scopes = SCOPES_WRITE if write else SCOPES_READ
    token_file = TOKEN_FILE_CALENDAR_WRITE if write else TOKEN_FILE_CALENDAR
    return load_credentials(token_file, scopes)


def _authorize_write():
    """Interactive: authorize write access to Calendar. Run manually once."""
    _get_credentials(write=True)
    print(f"Calendar write token saved to {TOKEN_FILE_CALENDAR_WRITE}")


def _get_calendar_service(write: bool = False):
    """Get Calendar API service. Use write=True for create/update/delete."""
    try:
        creds = _get_credentials(write=write)
        return build("calendar", "v3", credentials=creds)
    except RuntimeError as e:
        raise RuntimeError(f"Calendar auth failed: {e}. Re-authorize with: python3 -c \"from actions.calendar import _get_credentials; _get_credentials()\"") from e


def _handle_http_error(e: HttpError, write: bool = False):
    """Handle Google API HTTP errors — refresh token on auth failures.

    On 401/403: try refreshing the token. Only delete if refresh also fails.
    This avoids nuking valid tokens on transient Google API errors.
    """
    status = e.resp.status if hasattr(e, "resp") else 0
    if status in (401, 403):
        token_file = TOKEN_FILE_CALENDAR_WRITE if write else TOKEN_FILE_CALENDAR
        log.warning("Calendar API returned %d — attempting token refresh for %s", status, token_file.name)
        try:
            from oauth_utils import load_credentials
            scopes = SCOPES_WRITE if write else SCOPES_READ
            load_credentials(token_file, scopes, allow_interactive=False)
            # Refresh succeeded — the 403 was likely transient. Re-raise the
            # original error so the caller can retry on next request.
        except Exception:
            # Refresh failed — token is truly dead. Delete it.
            log.error("Token refresh failed for %s — deleting", token_file.name)
            if token_file.exists():
                token_file.unlink()
        raise RuntimeError(
            f"Calendar access denied (HTTP {status}). "
            "If this persists, re-authorize with: python3 -c "
            "\"from actions.calendar import _get_credentials; _get_credentials()\""
        ) from e
    raise


def _get_events_sync(days: int = 1, max_results: int = 20) -> list[dict]:
    """Fetch upcoming events from primary + family calendar. Runs in thread."""
    from config import FAMILY_CALENDAR_ID
    service = _get_calendar_service()

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()

    calendar_ids = [("primary", "")]
    if FAMILY_CALENDAR_ID:
        calendar_ids.append((FAMILY_CALENDAR_ID, "👨‍👩‍👧‍👦 "))

    result = []
    for cal_id, prefix in calendar_ids:
        try:
            events_result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
                timeZone=TIMEZONE,
            ).execute()
        except HttpError as e:
            if cal_id != "primary":
                log.debug("Family calendar fetch failed: %s", e)
                continue
            _handle_http_error(e, write=False)

        for event in events_result.get("items", []):
            start = event["start"].get("dateTime", event["start"].get("date", ""))
            end = event["end"].get("dateTime", event["end"].get("date", ""))
            result.append({
                "summary": prefix + event.get("summary", "(no title)"),
                "start": start,
                "end": end,
                "location": event.get("location", ""),
                "description": (event.get("description") or "")[:200],
                "all_day": "date" in event["start"],
            })

    # Sort merged events by start time
    result.sort(key=lambda e: e["start"])
    return result


async def get_today_events() -> list[dict]:
    """Get today's calendar events (with retry for transient failures)."""
    from resilience import retry

    @retry(max_attempts=2, backoff_factor=1.0)
    async def _fetch():
        return await asyncio.to_thread(_get_events_sync, days=1)

    return await _fetch()


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


# --- Write Operations (require calendar.events scope) ---


def _create_event_sync(
    summary: str,
    start_time: datetime,
    end_time: datetime | None = None,
    description: str = "",
    location: str = "",
    all_day: bool = False,
) -> dict:
    """Create a calendar event. Runs in thread."""
    if not TOKEN_FILE_CALENDAR_WRITE.exists():
        raise RuntimeError(
            "Calendar write access not authorized. Run:\n"
            "  python3 -c \"from actions.calendar import _authorize_write; _authorize_write()\""
        )

    service = _get_calendar_service(write=True)
    tz = ZoneInfo(TIMEZONE)

    if end_time is None:
        end_time = start_time + timedelta(hours=1)

    if all_day:
        event_body = {
            "summary": summary,
            "start": {"date": start_time.strftime("%Y-%m-%d")},
            "end": {"date": end_time.strftime("%Y-%m-%d")},
        }
    else:
        event_body = {
            "summary": summary,
            "start": {"dateTime": start_time.astimezone(tz).isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end_time.astimezone(tz).isoformat(), "timeZone": TIMEZONE},
        }

    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location

    try:
        event = service.events().insert(calendarId="primary", body=event_body).execute()
    except HttpError as e:
        _handle_http_error(e, write=True)
    return {
        "id": event["id"],
        "summary": event.get("summary", ""),
        "start": event["start"].get("dateTime", event["start"].get("date", "")),
        "link": event.get("htmlLink", ""),
    }


async def create_event(
    summary: str,
    start_time: datetime,
    end_time: datetime | None = None,
    description: str = "",
    location: str = "",
    all_day: bool = False,
) -> dict:
    """Create a calendar event. Returns event dict with id, summary, start, link."""
    return await asyncio.to_thread(
        _create_event_sync, summary, start_time, end_time, description, location, all_day
    )


def _delete_event_sync(event_id: str) -> bool:
    """Delete a calendar event by ID. Runs in thread."""
    service = _get_calendar_service(write=True)
    try:
        service.events().delete(calendarId="primary", eventId=event_id).execute()
    except HttpError as e:
        _handle_http_error(e, write=True)
    return True


async def delete_event(event_id: str) -> bool:
    """Delete a calendar event by ID."""
    return await asyncio.to_thread(_delete_event_sync, event_id)


# ---------------------------------------------------------------------------
# Agent loop sensor
# ---------------------------------------------------------------------------

async def sense_calendar() -> dict:
    """Sensor: check for upcoming meetings in next 2 hours."""
    try:
        events = await get_today_events()
        now = datetime.now(ZoneInfo(TIMEZONE))
        upcoming = []
        for ev in (events or []):
            start_str = ev.get("start", "")
            if isinstance(start_str, dict):
                start_str = start_str.get("dateTime") or start_str.get("date", "")
            if not start_str:
                continue
            try:
                start = datetime.fromisoformat(start_str)
                if not start.tzinfo:
                    start = start.replace(tzinfo=ZoneInfo(TIMEZONE))
                delta = (start - now).total_seconds() / 60
                if 0 < delta <= 120:
                    ev["_minutes_until"] = int(delta)
                    upcoming.append(ev)
            except (ValueError, TypeError):
                pass
        return {"upcoming_events": upcoming}
    except Exception as e:
        log.debug("Calendar sensor failed: %s", e)
        return {"upcoming_events": []}


def identify_calendar_opportunities(state: dict, last_state: dict, cooldowns: dict):
    """Identify meeting prep opportunities from calendar sensor data."""
    import time as _time
    from agent_loop import Opportunity, Urgency, _on_cooldown

    opps = []
    now = _time.monotonic()

    for ev in state.get("calendar", {}).get("upcoming_events", []):
        mins = ev.get("_minutes_until", 999)
        if mins <= 35:
            title = ev.get("summary", "meeting")
            start_key = ev.get("start", "")
            if isinstance(start_key, dict):
                start_key = start_key.get("dateTime", "")[:10]
            opp_id = f"meeting_prep_{title[:30]}_{start_key}"
            if _on_cooldown(opp_id, cooldowns, now, hours=12):
                continue
            opps.append(Opportunity(
                id=opp_id, source="calendar",
                summary=f"\U0001f4c5 Meeting in {mins}min: {title}",
                urgency=Urgency.MEDIUM, action_type="meeting_prep",
                payload={"event": ev}, requires_llm=True,
            ))

    return opps


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "calendar":
        try:
            events = await get_today_events()
            await ctx.reply(format_events_text(events))
        except Exception as e:
            from resilience import format_user_error
            await ctx.reply(format_user_error(e, skill_name="Calendar"))
        return True

    elif action == "calendar_upcoming":
        try:
            days = int(intent.get("days", 7))
            events = await get_upcoming_events(days=days)
            if not events:
                await ctx.reply(f"No events in the next {days} days.")
            else:
                await ctx.reply(f"📅 Next {days} days:\n\n{format_events_text(events)}")
        except Exception as e:
            from resilience import format_user_error
            await ctx.reply(format_user_error(e, skill_name="Calendar"))
        return True

    elif action == "calendar_create":
        try:
            summary = intent.get("summary", "")
            start_str = intent.get("start_time", "")
            end_str = intent.get("end_time", "")

            if not summary or not start_str:
                await ctx.reply("I need at least a title and start time to create an event.")
                return True

            tz = ZoneInfo(TIMEZONE)
            start_time = datetime.fromisoformat(start_str)
            if not start_time.tzinfo:
                start_time = start_time.replace(tzinfo=tz)

            end_time = None
            if end_str:
                end_time = datetime.fromisoformat(end_str)
                if not end_time.tzinfo:
                    end_time = end_time.replace(tzinfo=tz)

            result = await create_event(
                summary=summary,
                start_time=start_time,
                end_time=end_time,
                description=intent.get("description", ""),
                location=intent.get("location", ""),
            )
            start_display = start_time.strftime("%A %b %d at %I:%M %p")
            await ctx.reply(
                f"✅ Created: **{result['summary']}**\n"
                f"📅 {start_display}\n"
                f"🔗 {result.get('link', '')}"
            )
        except RuntimeError as e:
            await ctx.reply(f"⚠️ {e}")
        except (ValueError, TypeError) as e:
            await ctx.reply(f"⚠️ Couldn't parse the time: {e}")
        except Exception as e:
            from resilience import format_user_error
            await ctx.reply(format_user_error(e, skill_name="Calendar"))
        return True

    return False
