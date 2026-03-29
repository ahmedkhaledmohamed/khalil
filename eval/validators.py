"""Skill-specific content validators for Khalil's eval pipeline.

Each validator returns a list of Check objects that verify domain-specific
response quality beyond generic heuristics.
"""

from __future__ import annotations

import re
from typing import Callable


# ---------------------------------------------------------------------------
# Validator registry
# ---------------------------------------------------------------------------

# Map of action_type -> validator function
# Each validator: (query: str, response: str) -> list[tuple[str, bool, str]]
# Returns list of (check_name, passed, detail)
_VALIDATORS: dict[str, Callable] = {}


def register_validator(action_type: str):
    """Decorator to register a validator for an action type."""
    def decorator(fn):
        _VALIDATORS[action_type] = fn
        return fn
    return decorator


def get_validator(action_type: str | None) -> Callable | None:
    """Get validator for an action type, or None."""
    if action_type is None:
        return None
    return _VALIDATORS.get(action_type)


def validate(action_type: str | None, query: str, response: str) -> list[tuple[str, bool, str]]:
    """Run skill-specific + generic validators. Returns [(name, passed, detail), ...]."""
    checks = []

    # Skill-specific validator
    validator = get_validator(action_type)
    if validator:
        checks.extend(validator(query, response))

    # Generic validators (always run)
    checks.extend(_generic_validators(query, response))

    return checks


# ---------------------------------------------------------------------------
# Generic validators (apply to all responses)
# ---------------------------------------------------------------------------

_TRACEBACK_PATTERNS = [
    "Traceback (most recent call last)",
    "File \"",
    "raise ",
    "Error:",
]

def _generic_validators(query: str, response: str) -> list[tuple[str, bool, str]]:
    """Validators that apply to every response."""
    checks = []

    # No raw Python error leak
    has_traceback = any(p in response for p in _TRACEBACK_PATTERNS)
    # Allow "Error:" only if it's part of normal text, not a traceback
    if has_traceback and "Traceback" not in response:
        has_traceback = False
    checks.append((
        "no_error_leak",
        not has_traceback,
        "Python traceback leaked into response" if has_traceback else "",
    ))

    return checks


# ---------------------------------------------------------------------------
# Skill-specific validators
# ---------------------------------------------------------------------------

@register_validator("weather")
@register_validator("weather_forecast")
def _validate_weather(query: str, response: str) -> list[tuple[str, bool, str]]:
    """Weather responses should contain temperature or condition info."""
    lower = response.lower()
    has_temp = bool(re.search(r'\d+\s*\u00b0', response)) or bool(re.search(r'\d+\s*(celsius|fahrenheit|degrees)', lower))
    has_condition = any(w in lower for w in ("sunny", "cloudy", "rain", "snow", "clear", "overcast", "wind", "humid", "fog", "storm", "warm", "cold", "hot", "cool", "forecast"))

    passed = has_temp or has_condition
    return [("weather_content", passed, "" if passed else "No temperature or weather condition found in response")]


@register_validator("calendar")
def _validate_calendar(query: str, response: str) -> list[tuple[str, bool, str]]:
    """Calendar responses should contain times, dates, or 'no events'."""
    lower = response.lower()
    has_time = bool(re.search(r'\d{1,2}:\d{2}', response))
    has_date = bool(re.search(r'(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)', lower))
    has_no_events = any(phrase in lower for phrase in ("no events", "no meetings", "nothing scheduled", "calendar is clear", "free"))

    passed = has_time or has_date or has_no_events
    return [("calendar_content", passed, "" if passed else "No time, date, or 'no events' in response")]


@register_validator("email")
@register_validator("email_work")
@register_validator("email_personal")
@register_validator("gmail")
def _validate_email(query: str, response: str) -> list[tuple[str, bool, str]]:
    """Email responses should contain email-related terms."""
    lower = response.lower()
    email_terms = ("subject", "from", "to", "unread", "inbox", "draft", "email", "message", "sent", "starred", "no new")
    has_terms = any(t in lower for t in email_terms)

    return [("email_content", has_terms, "" if has_terms else "No email-related terms in response")]


@register_validator("reminder")
@register_validator("icloud_reminder")
def _validate_reminder(query: str, response: str) -> list[tuple[str, bool, str]]:
    """Reminder responses should confirm the action or list reminders."""
    lower = response.lower()
    confirmation = any(w in lower for w in ("reminder", "remind", "set", "created", "scheduled", "no reminders", "list"))

    return [("reminder_content", confirmation, "" if confirmation else "No reminder confirmation in response")]


@register_validator("shell")
def _validate_shell(query: str, response: str) -> list[tuple[str, bool, str]]:
    """Shell responses should be substantive, not refusals."""
    lower = response.lower().strip()
    is_refusal = any(lower.startswith(p) for p in ("i can't", "i don't", "i'm unable", "i cannot"))

    return [("shell_not_refusal", not is_refusal, "" if not is_refusal else "Shell command produced a refusal")]


@register_validator("spotify_now")
@register_validator("spotify_recent")
@register_validator("spotify_top")
def _validate_spotify(query: str, response: str) -> list[tuple[str, bool, str]]:
    """Spotify responses should contain music-related terms."""
    lower = response.lower()
    music_terms = ("track", "song", "artist", "album", "playlist", "playing", "listen", "music", "spotify", "no track", "nothing playing")
    has_terms = any(t in lower for t in music_terms)

    return [("spotify_content", has_terms, "" if has_terms else "No music-related terms in response")]


@register_validator("web_search")
def _validate_web_search(query: str, response: str) -> list[tuple[str, bool, str]]:
    """Web search responses should contain results or search-related content."""
    lower = response.lower()
    has_results = len(response) > 100 or any(t in lower for t in ("result", "found", "search", "http", "www", "link"))

    return [("search_content", has_results, "" if has_results else "No search results in response")]


@register_validator("github_notifications")
@register_validator("github_prs")
@register_validator("github_create_issue")
def _validate_github(query: str, response: str) -> list[tuple[str, bool, str]]:
    """GitHub responses should contain repo/PR/issue terms."""
    lower = response.lower()
    gh_terms = ("pull request", "pr", "issue", "commit", "repository", "repo", "notification", "merge", "branch", "no notification", "no pr")
    has_terms = any(t in lower for t in gh_terms)

    return [("github_content", has_terms, "" if has_terms else "No GitHub-related terms in response")]


@register_validator("macos_apps")
def _validate_macos(query: str, response: str) -> list[tuple[str, bool, str]]:
    """macOS responses should contain app/system info."""
    lower = response.lower()
    has_content = len(response) > 20 and any(t in lower for t in ("app", "running", "process", "window", "system", "battery", "cpu", "memory", "disk", "screenshot"))

    return [("macos_content", has_content, "" if has_content else "No macOS system content in response")]


@register_validator("notion_search")
@register_validator("notion_create")
def _validate_notion(query: str, response: str) -> list[tuple[str, bool, str]]:
    """Notion responses should reference pages or documents."""
    lower = response.lower()
    has_terms = any(t in lower for t in ("page", "notion", "document", "created", "found", "no results"))

    return [("notion_content", has_terms, "" if has_terms else "No Notion-related terms in response")]


@register_validator("readwise_highlights")
@register_validator("readwise_review")
def _validate_readwise(query: str, response: str) -> list[tuple[str, bool, str]]:
    """Readwise responses should reference highlights or books."""
    lower = response.lower()
    has_terms = any(t in lower for t in ("highlight", "book", "readwise", "note", "passage", "no highlights"))

    return [("readwise_content", has_terms, "" if has_terms else "No Readwise-related terms in response")]


@register_validator("appstore_ratings")
@register_validator("appstore_downloads")
def _validate_appstore(query: str, response: str) -> list[tuple[str, bool, str]]:
    """App Store responses should contain ratings, downloads, or review info."""
    lower = response.lower()
    has_terms = any(t in lower for t in ("rating", "download", "review", "star", "app store", "version", "zia"))

    return [("appstore_content", has_terms, "" if has_terms else "No App Store metrics in response")]


@register_validator("digitalocean_status")
@register_validator("digitalocean_spend")
def _validate_digitalocean(query: str, response: str) -> list[tuple[str, bool, str]]:
    """DigitalOcean responses should contain server/billing info."""
    lower = response.lower()
    has_terms = any(t in lower for t in ("droplet", "server", "status", "running", "spend", "cost", "$", "digitalocean", "active"))

    return [("digitalocean_content", has_terms, "" if has_terms else "No DigitalOcean metrics in response")]


@register_validator("youtube_search")
@register_validator("youtube_liked")
def _validate_youtube(query: str, response: str) -> list[tuple[str, bool, str]]:
    """YouTube responses should reference videos or channels."""
    lower = response.lower()
    has_terms = any(t in lower for t in ("video", "youtube", "channel", "watch", "liked", "search"))

    return [("youtube_content", has_terms, "" if has_terms else "No YouTube content in response")]


@register_validator("linkedin_messages")
@register_validator("linkedin_jobs")
@register_validator("linkedin_profile")
def _validate_linkedin(query: str, response: str) -> list[tuple[str, bool, str]]:
    """LinkedIn responses should reference professional content."""
    lower = response.lower()
    has_terms = any(t in lower for t in ("linkedin", "message", "job", "connection", "profile", "view", "application"))

    return [("linkedin_content", has_terms, "" if has_terms else "No LinkedIn content in response")]


@register_validator("imessage_read")
def _validate_imessage(query: str, response: str) -> list[tuple[str, bool, str]]:
    """iMessage responses should reference messages or contacts."""
    lower = response.lower()
    has_terms = any(t in lower for t in ("message", "imessage", "text", "sent", "received", "from", "no message", "unread"))

    return [("imessage_content", has_terms, "" if has_terms else "No iMessage content in response")]
