"""Live state collector — parallel collection with TTL cache.

Gathers real-time context (calendar, email, etc.) and formats it
for injection into the LLM prompt. Each provider runs in parallel
with independent error handling so one failure doesn't block others.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from config import TIMEZONE

log = logging.getLogger("khalil.state.collector")

# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------

@dataclass
class _CacheEntry:
    """Single cached state value with TTL."""
    data: object
    expires_at: float

    @property
    def is_valid(self) -> bool:
        return time.monotonic() < self.expires_at


_cache: dict[str, _CacheEntry] = {}

# Default TTLs per provider (seconds)
_TTL = {
    "calendar_events": 300,   # 5 min — events don't change often
    "next_meeting": 120,      # 2 min — need fresher for "meeting in 5 min" alerts
    "unread_count": 180,      # 3 min
    "needs_reply": 300,       # 5 min
    "frontmost_app": 30,      # 30s — app focus changes frequently
}


# ---------------------------------------------------------------------------
# LiveState dataclass
# ---------------------------------------------------------------------------

@dataclass
class LiveState:
    """Structured live state snapshot."""
    collected_at: str = ""
    calendar: dict = field(default_factory=dict)
    email: dict = field(default_factory=dict)
    frontmost_app: str | None = None


def _get_cached(key: str) -> object | None:
    entry = _cache.get(key)
    if entry and entry.is_valid:
        return entry.data
    return None


def _set_cached(key: str, data: object, ttl: float | None = None):
    if ttl is None:
        ttl = _TTL.get(key, 180)
    _cache[key] = _CacheEntry(data=data, expires_at=time.monotonic() + ttl)


def invalidate_cache(key: str | None = None):
    """Invalidate a specific cache key or all cached state."""
    if key:
        _cache.pop(key, None)
    else:
        _cache.clear()


# ---------------------------------------------------------------------------
# Provider wrappers (with caching + error isolation)
# ---------------------------------------------------------------------------

async def _collect_calendar() -> dict:
    """Collect calendar state: today's events + next meeting."""
    result = {}

    cached_events = _get_cached("calendar_events")
    if cached_events is not None:
        result["events"] = cached_events
    else:
        try:
            from state.calendar_provider import get_today_events
            events = await get_today_events()
            _set_cached("calendar_events", events)
            result["events"] = events
        except Exception as e:
            log.warning("Calendar events collection failed: %s", e)
            result["events"] = []

    cached_next = _get_cached("next_meeting")
    if cached_next is not None:
        result["next_meeting"] = cached_next
    else:
        try:
            from state.calendar_provider import get_next_meeting
            nxt = await get_next_meeting(within_minutes=60)
            _set_cached("next_meeting", nxt)
            result["next_meeting"] = nxt
        except Exception as e:
            log.warning("Next meeting collection failed: %s", e)
            result["next_meeting"] = None

    return result


async def _collect_email() -> dict:
    """Collect email state: unread count + needs-reply."""
    result = {}

    cached_unread = _get_cached("unread_count")
    if cached_unread is not None:
        result["unread_count"] = cached_unread
    else:
        try:
            from state.email_provider import get_unread_count
            count = await get_unread_count()
            _set_cached("unread_count", count)
            result["unread_count"] = count
        except Exception as e:
            log.warning("Unread count collection failed: %s", e)
            result["unread_count"] = None

    cached_reply = _get_cached("needs_reply")
    if cached_reply is not None:
        result["needs_reply"] = cached_reply
    else:
        try:
            from state.email_provider import get_needs_reply
            emails = await get_needs_reply(max_results=5)
            _set_cached("needs_reply", emails)
            result["needs_reply"] = emails
        except Exception as e:
            log.warning("Needs-reply collection failed: %s", e)
            result["needs_reply"] = []

    return result


async def _collect_macos() -> dict:
    """Collect macOS state: frontmost app."""
    result = {}

    cached_app = _get_cached("frontmost_app")
    if cached_app is not None:
        result["frontmost_app"] = cached_app
    else:
        try:
            from actions.macos import get_frontmost_app
            app = await get_frontmost_app()
            _set_cached("frontmost_app", app)
            result["frontmost_app"] = app or None
        except Exception as e:
            log.warning("Frontmost app collection failed: %s", e)
            result["frontmost_app"] = None

    return result


# ---------------------------------------------------------------------------
# Main collector
# ---------------------------------------------------------------------------

async def collect_live_state() -> dict:
    """Collect all live state in parallel. Returns combined state dict.

    Each provider is independent — failures are logged but don't
    block other providers.
    """
    calendar_task = asyncio.create_task(_collect_calendar())
    email_task = asyncio.create_task(_collect_email())
    macos_task = asyncio.create_task(_collect_macos())

    calendar_state, email_state, macos_state = await asyncio.gather(
        calendar_task, email_task, macos_task, return_exceptions=True
    )

    state = {"collected_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat()}

    if isinstance(calendar_state, dict):
        state["calendar"] = calendar_state
    else:
        log.warning("Calendar collection returned exception: %s", calendar_state)
        state["calendar"] = {"events": [], "next_meeting": None}

    if isinstance(email_state, dict):
        state["email"] = email_state
    else:
        log.warning("Email collection returned exception: %s", email_state)
        state["email"] = {"unread_count": None, "needs_reply": []}

    if isinstance(macos_state, dict):
        state["frontmost_app"] = macos_state.get("frontmost_app")
    else:
        log.warning("macOS collection returned exception: %s", macos_state)
        state["frontmost_app"] = None

    return state


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

def format_for_prompt(state: dict) -> str:
    """Format collected state into a concise text block for LLM injection."""
    lines = []
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    lines.append(f"Current time: {now.strftime('%A, %B %d %Y at %I:%M %p %Z')}")

    # Calendar
    cal = state.get("calendar", {})
    events = cal.get("events", [])
    if events:
        lines.append(f"\nCalendar ({len(events)} events today):")
        for ev in events[:8]:  # cap at 8 to save tokens
            time_str = ev.get("start", "")
            if not ev.get("all_day") and "T" in time_str:
                try:
                    dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                    time_str = dt.astimezone(tz).strftime("%I:%M %p")
                except (ValueError, TypeError):
                    pass
            elif ev.get("all_day"):
                time_str = "all day"
            attendee_count = len(ev.get("attendees", []))
            att_str = f" ({attendee_count} attendees)" if attendee_count else ""
            lines.append(f"  - {time_str}: {ev['title']}{att_str}")

    next_mtg = cal.get("next_meeting")
    if next_mtg:
        lines.append(f"\nNext meeting in {next_mtg['minutes_until']} min: {next_mtg['title']}")

    # Email
    email = state.get("email", {})
    unread = email.get("unread_count")
    if unread is not None:
        lines.append(f"\nEmail: {unread} unread in inbox")

    needs_reply = email.get("needs_reply", [])
    if needs_reply:
        lines.append(f"Needs reply ({len(needs_reply)}):")
        for em in needs_reply[:5]:
            lines.append(f"  - From {em['from']}: {em['subject']}")

    # macOS
    frontmost = state.get("frontmost_app")
    if frontmost:
        lines.append(f"\nActive app: {frontmost}")

    return "\n".join(lines)
