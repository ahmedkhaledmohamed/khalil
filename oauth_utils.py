"""Shared OAuth token management — proactive refresh, health checks, notifications.

Centralizes Google OAuth token handling across gmail.py, calendar.py, drive.py.
Detects expiring tokens before API calls fail and refreshes them proactively.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from config import (
    CREDENTIALS_FILE,
    TOKEN_FILE,
    TOKEN_FILE_COMPOSE,
    TOKEN_FILE_CALENDAR,
    TOKEN_FILE_MODIFY,
    TOKEN_FILE_CONTACTS,
    TOKEN_FILE_TASKS,
    TOKEN_FILE_DRIVE_WRITE,
    TOKEN_FILE_WORK,
    TOKEN_FILE_YOUTUBE,
)

log = logging.getLogger("pharoclaw.oauth")

# All managed token files with their purpose
TOKEN_FILES = {
    "gmail_readonly": TOKEN_FILE,
    "gmail_compose": TOKEN_FILE_COMPOSE,
    "calendar": TOKEN_FILE_CALENDAR,
    "gmail_modify": TOKEN_FILE_MODIFY,
    "contacts": TOKEN_FILE_CONTACTS,
    "tasks": TOKEN_FILE_TASKS,
    "drive_write": TOKEN_FILE_DRIVE_WRITE,
    "work_gmail": TOKEN_FILE_WORK,
    "youtube": TOKEN_FILE_YOUTUBE,
}


def _safe_load_token(token_file: Path, scopes: list[str] | None = None) -> Credentials | None:
    """Load credentials from a token file, handling corrupt/empty files.

    Returns None (and deletes the file) if the file is empty or unparseable.
    """
    if not token_file.exists():
        return None

    if token_file.stat().st_size == 0:
        log.warning("Empty token file, deleting: %s", token_file.name)
        token_file.unlink()
        return None

    try:
        if scopes:
            return Credentials.from_authorized_user_file(str(token_file), scopes)
        return Credentials.from_authorized_user_file(str(token_file))
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        log.warning("Corrupt token file (%s), deleting: %s", e, token_file.name)
        token_file.unlink()
        return None


def _atomic_write_token(token_file: Path, creds: Credentials) -> None:
    """Write credentials to a token file atomically (write tmp + rename)."""
    tmp_path = token_file.with_suffix(".tmp")
    try:
        with open(tmp_path, "w") as f:
            f.write(creds.to_json())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, token_file)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def load_credentials(
    token_file: Path,
    scopes: list[str],
    allow_interactive: bool = True,
) -> Credentials:
    """Load, refresh, or create OAuth credentials.

    Args:
        token_file: Path to the token JSON file.
        scopes: OAuth scopes required.
        allow_interactive: If True, can open browser for re-auth. Set False for daemon context.

    Returns:
        Valid Credentials object.

    Raises:
        RuntimeError: If credentials are unavailable and interactive auth is disabled.
        FileNotFoundError: If credentials.json is missing and re-auth is needed.
    """
    creds = _safe_load_token(token_file, scopes)

    if creds and creds.valid:
        return creds

    # Try refresh if we have a refresh token
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _atomic_write_token(token_file, creds)
            return creds
        except RefreshError as e:
            log.warning("Refresh failed for %s (%s), need re-auth", token_file.name, e)
            if token_file.exists():
                token_file.unlink()

    # Need fresh auth
    if not allow_interactive:
        raise RuntimeError(
            f"OAuth token missing or invalid: {token_file.name}. "
            "Run interactive auth first (e.g. python3 actions/gmail.py --reauth)"
        )

    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Missing {CREDENTIALS_FILE}. "
            "Download from Google Cloud Console → APIs → Credentials → OAuth 2.0"
        )

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), scopes)
    creds = flow.run_local_server(port=0)
    _atomic_write_token(token_file, creds)
    return creds


def check_token_health(token_file: Path) -> dict:
    """Check a token file's health. Returns status dict."""
    if not token_file.exists():
        return {"status": "missing", "path": str(token_file)}

    creds = _safe_load_token(token_file)
    if creds is None:
        # _safe_load_token already deleted the corrupt file
        return {"status": "corrupt", "path": str(token_file)}

    if not creds.refresh_token:
        return {"status": "no_refresh_token", "path": str(token_file)}

    if creds.valid and not creds.expired:
        return {"status": "healthy", "path": str(token_file)}

    # Token expired but refresh token exists — try to refresh
    try:
        creds.refresh(Request())
        _atomic_write_token(token_file, creds)
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
