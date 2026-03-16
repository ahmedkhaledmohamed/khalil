"""Shared OAuth token management — proactive refresh, health checks, notifications.

Centralizes Google OAuth token handling across gmail.py, calendar.py, drive.py.
Detects expiring tokens before API calls fail and refreshes them proactively.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from config import (
    CREDENTIALS_FILE,
    TOKEN_FILE,
    TOKEN_FILE_COMPOSE,
    TOKEN_FILE_CALENDAR,
)

log = logging.getLogger("khalil.oauth")

# All managed token files with their purpose
TOKEN_FILES = {
    "gmail_readonly": TOKEN_FILE,
    "gmail_compose": TOKEN_FILE_COMPOSE,
    "calendar": TOKEN_FILE_CALENDAR,
}


def check_token_health(token_file: Path) -> dict:
    """Check a token file's health. Returns status dict."""
    if not token_file.exists():
        return {"status": "missing", "path": str(token_file)}

    try:
        creds = Credentials.from_authorized_user_file(str(token_file))
    except Exception as e:
        return {"status": "corrupt", "path": str(token_file), "error": str(e)}

    if not creds.refresh_token:
        return {"status": "no_refresh_token", "path": str(token_file)}

    if creds.valid and not creds.expired:
        return {"status": "healthy", "path": str(token_file)}

    # Token expired but refresh token exists — try to refresh
    try:
        creds.refresh(Request())
        # Persist refreshed token
        with open(token_file, "w") as f:
            f.write(creds.to_json())
        log.info("Proactively refreshed token: %s", token_file.name)
        return {"status": "refreshed", "path": str(token_file)}
    except Exception as e:
        return {"status": "refresh_failed", "path": str(token_file), "error": str(e)}


def check_all_tokens() -> list[dict]:
    """Check health of all managed Google OAuth tokens. Returns list of statuses."""
    results = []
    for name, token_file in TOKEN_FILES.items():
        result = check_token_health(token_file)
        result["name"] = name
        results.append(result)
    return results


async def proactive_token_refresh(notify_fn=None):
    """Proactively refresh all tokens that are expired or close to expiring.

    Args:
        notify_fn: Optional async callable(message: str) to notify on failures.
    """
    problems = []
    for name, token_file in TOKEN_FILES.items():
        result = check_token_health(token_file)
        status = result["status"]

        if status in ("missing", "no_refresh_token", "corrupt", "refresh_failed"):
            msg = f"OAuth token '{name}' ({token_file.name}): {status}"
            if "error" in result:
                msg += f" — {result['error']}"
            problems.append(msg)
            log.warning(msg)

    if problems and notify_fn:
        await notify_fn(
            "⚠️ OAuth Token Issues\n\n"
            + "\n".join(f"• {p}" for p in problems)
            + "\n\nRe-authorize with: python3 actions/gmail.py --reauth"
        )

    return problems
