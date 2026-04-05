"""Incremental email sync — fetch new emails from Gmail and index into knowledge base.

Uses the same OAuth credentials as gmail.py (readonly scope).
Tracks last sync timestamp in the settings table.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from config import DB_PATH, DATA_DIR

log = logging.getLogger("khalil.actions.gmail_sync")


def _get_gmail_service_for_token(token_file=None):
    """Get Gmail readonly service for a specific token file."""
    if token_file is None:
        from actions.gmail import _get_gmail_service
        return _get_gmail_service(write=False)
    from googleapiclient.discovery import build
    from oauth_utils import load_credentials
    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds = load_credentials(token_file, scopes, allow_interactive=False)
    return build("gmail", "v1", credentials=creds)


def _fetch_new_emails_sync(after_timestamp: str | None, max_results: int = 50, token_file=None) -> list[dict]:
    """Fetch emails from Gmail newer than after_timestamp. Runs in thread."""
    service = _get_gmail_service_for_token(token_file)

    # Build Gmail query — 'after:' uses epoch seconds
    query = ""
    if after_timestamp:
        try:
            dt = datetime.fromisoformat(after_timestamp)
            epoch = int(dt.timestamp())
            query = f"after:{epoch}"
        except (ValueError, TypeError):
            pass

    def _api_call_with_retry(request, retries=2):
        """Execute a Google API request, retrying on empty/transient responses."""
        for attempt in range(retries):
            try:
                return request.execute()
            except (json.JSONDecodeError, Exception) as e:
                if "Expecting value" in str(e) or "Empty" in str(e):
                    if attempt < retries - 1:
                        log.warning("Gmail API empty response (attempt %d/%d), retrying", attempt + 1, retries)
                        time.sleep(1)
                        continue
                raise

    results = _api_call_with_retry(
        service.users().messages().list(userId="me", q=query, maxResults=max_results)
    )

    messages = results.get("messages", [])
    if not messages:
        return []

    emails = []
    for msg in messages:
        full = _api_call_with_retry(
            service.users().messages().get(userId="me", id=msg["id"], format="full")
        )
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}

        # Extract plain text body
        from actions.gmail import extract_body
        body = extract_body(full.get("payload", {}))

        emails.append({
            "id": msg["id"],
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "date": headers.get("Date", ""),
            "snippet": full.get("snippet", ""),
            "body": body,
        })

    return emails


# --- #47: Email Auto-Categorization ---

_CATEGORY_KEYWORDS = {
    "finance": [
        "invoice", "receipt", "payment", "bank", "transaction", "statement",
        "credit card", "debit", "refund", "tax", "rrsp", "tfsa", "investment",
        "dividend", "portfolio", "mortgage", "salary", "payroll", "expense",
    ],
    "work": [
        "sprint", "standup", "jira", "confluence", "slack", "meeting",
        "deadline", "project", "deploy", "release", "review", "okr",
        "roadmap", "backlog", "scrum", "agile", "manager", "team",
    ],
    "shopping": [
        "order", "shipped", "delivery", "tracking", "purchase", "cart",
        "amazon", "shopify", "store", "return", "refund", "coupon",
    ],
    "travel": [
        "flight", "hotel", "booking", "airline", "boarding", "itinerary",
        "reservation", "check-in", "airbnb", "expedia", "travel",
    ],
    "newsletters": [
        "unsubscribe", "newsletter", "digest", "weekly", "roundup",
        "subscribe", "mailing list",
    ],
    "notifications": [
        "notification", "alert", "reminder", "automated", "noreply",
        "no-reply", "do-not-reply", "donotreply",
    ],
    "personal": [
        "family", "kids", "birthday", "party", "dinner", "weekend",
        "vacation", "photo", "wedding",
    ],
}


def categorize_email(email_dict: dict) -> str:
    """Categorize an email using keyword matching.

    Args:
        email_dict: dict with keys like 'subject', 'from', 'body', 'snippet'.

    Returns:
        Category string, e.g. "finance", "work", "personal", etc.
    """
    # Build searchable text from available fields
    parts = [
        email_dict.get("subject", ""),
        email_dict.get("from", ""),
        email_dict.get("snippet", ""),
        (email_dict.get("body", "") or "")[:500],
    ]
    text_lower = " ".join(parts).lower()

    best_category = "personal"  # default
    best_score = 0

    for category, keywords in _CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > best_score:
            best_score = score
            best_category = category

    return best_category


async def sync_new_emails(
    conn=None,
    after_timestamp: str | None = None,
    token_file=None,
    source_prefix: str = "gmail_sync",
    category_prefix: str = "email",
) -> int | dict:
    """Fetch new emails since last sync and index them into the knowledge base.

    Args:
        conn: Database connection. If None, creates one (legacy behavior).
        after_timestamp: Override for last sync timestamp (ISO format).
        token_file: OAuth token file for the Gmail account. None = personal default.
        source_prefix: Source name for knowledge base entries.
        category_prefix: Category prefix (e.g. "email" or "email:work").

    Returns:
        int (count) when called with conn, or dict with counts for legacy callers.
    """
    from knowledge.indexer import init_db, index_source

    legacy_mode = conn is None
    if legacy_mode:
        conn = init_db()

    # Get last sync timestamp (use provided or look up from settings)
    if after_timestamp is None:
        setting_key = f"last_{source_prefix.replace('gmail_sync', 'email')}_sync"
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (setting_key,)).fetchone()
        after_timestamp = row[0] if row else None

    log.info("%s sync starting (last sync: %s)", source_prefix, after_timestamp or "never")

    # Fetch new emails
    emails = await asyncio.to_thread(_fetch_new_emails_sync, after_timestamp, token_file=token_file)

    if not emails:
        log.info("No new emails to sync for %s", source_prefix)
        if legacy_mode:
            _update_sync_timestamp(conn)
            return {"fetched": 0, "indexed": 0}
        return 0

    log.info("Fetched %d new emails for %s", len(emails), source_prefix)

    # Convert to indexer entry format + auto-categorize
    indexed = 0
    for e in emails:
        category = categorize_email(e)
        content = f"From: {e['from']}\nDate: {e['date']}\n{e['subject']}\n{e['body'][:1000] or e['snippet']}"
        entry = {
            "title": e["subject"],
            "content": content,
            "metadata": f"from={e['from']}; date={e['date']}; gmail_id={e['id']}",
        }
        indexed += await index_source(conn, source_prefix, f"{category_prefix}:{category}", [entry])

    if legacy_mode:
        _update_sync_timestamp(conn)
        log.info("Email sync complete: %d fetched, %d indexed", len(emails), indexed)
        return {"fetched": len(emails), "indexed": indexed}

    return indexed


def _update_sync_timestamp(conn):
    """Update the last email sync timestamp in settings."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('last_email_sync', ?)",
        (now,),
    )
    conn.commit()
