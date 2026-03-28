"""Email state provider — unread count, flagged emails, needs-reply detection."""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import TOKEN_FILE, TIMEZONE

log = logging.getLogger("khalil.state.email")


def _get_gmail_service():
    """Get Gmail API service using existing OAuth tokens."""
    from googleapiclient.discovery import build
    from oauth_utils import load_credentials

    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds = load_credentials(TOKEN_FILE, scopes, allow_interactive=False)
    return build("gmail", "v1", credentials=creds)


def _fetch_unread_count_sync() -> int:
    """Fetch unread message count from INBOX (sync, runs in thread)."""
    service = _get_gmail_service()
    results = service.users().labels().get(userId="me", id="INBOX").execute()
    return results.get("messagesUnread", 0)


def _fetch_needs_reply_sync(max_results: int = 10) -> list[dict]:
    """Fetch recent emails that likely need a reply.

    Heuristic: unread emails in INBOX from the last 3 days where
    the user is in the To/Cc field (not just BCC or mailing list).
    """
    service = _get_gmail_service()
    tz = ZoneInfo(TIMEZONE)
    cutoff = datetime.now(tz) - timedelta(days=3)
    query = f"is:unread in:inbox after:{cutoff.strftime('%Y/%m/%d')}"

    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()

    messages = results.get("messages", [])
    needs_reply = []

    for msg_meta in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_meta["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_addr = headers.get("From", "")
        subject = headers.get("Subject", "(no subject)")

        # Skip automated / no-reply senders
        skip_patterns = ["noreply", "no-reply", "notifications", "mailer-daemon", "donotreply"]
        if any(p in from_addr.lower() for p in skip_patterns):
            continue

        needs_reply.append({
            "from": from_addr,
            "subject": subject,
            "date": headers.get("Date", ""),
            "snippet": msg.get("snippet", "")[:120],
        })

    return needs_reply


async def get_unread_count() -> int:
    """Get INBOX unread count."""
    return await asyncio.to_thread(_fetch_unread_count_sync)


async def get_needs_reply(max_results: int = 10) -> list[dict]:
    """Get emails that likely need a reply. Returns list of {from, subject, date, snippet}."""
    return await asyncio.to_thread(_fetch_needs_reply_sync, max_results)
