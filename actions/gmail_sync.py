"""Incremental email sync — fetch new emails from Gmail and index into knowledge base.

Uses the same OAuth credentials as gmail.py (readonly scope).
Tracks last sync timestamp in the settings table.
"""

import asyncio
import logging
from datetime import datetime, timezone

from config import DB_PATH, DATA_DIR

log = logging.getLogger("khalil.actions.gmail_sync")


def _fetch_new_emails_sync(after_timestamp: str | None, max_results: int = 50) -> list[dict]:
    """Fetch emails from Gmail newer than after_timestamp. Runs in thread."""
    from actions.gmail import _get_gmail_service

    service = _get_gmail_service(write=False)

    # Build Gmail query — 'after:' uses epoch seconds
    query = ""
    if after_timestamp:
        try:
            dt = datetime.fromisoformat(after_timestamp)
            epoch = int(dt.timestamp())
            query = f"after:{epoch}"
        except (ValueError, TypeError):
            pass

    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()

    messages = results.get("messages", [])
    if not messages:
        return []

    emails = []
    for msg in messages:
        full = service.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()
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


async def sync_new_emails() -> dict:
    """Fetch new emails since last sync and index them into the knowledge base.

    Returns dict with counts: {"fetched": N, "indexed": N}.
    """
    from knowledge.indexer import init_db, index_source

    conn = init_db()

    # Get last sync timestamp
    row = conn.execute("SELECT value FROM settings WHERE key = 'last_email_sync'").fetchone()
    last_sync = row[0] if row else None

    log.info("Email sync starting (last sync: %s)", last_sync or "never")

    # Fetch new emails
    emails = await asyncio.to_thread(_fetch_new_emails_sync, last_sync)

    if not emails:
        log.info("No new emails to sync")
        # Still update timestamp
        _update_sync_timestamp(conn)
        return {"fetched": 0, "indexed": 0}

    log.info("Fetched %d new emails", len(emails))

    # Convert to indexer entry format (same as parse_email_file output)
    # #47: Auto-categorize each email during sync
    entries = []
    for e in emails:
        category = categorize_email(e)
        content = f"From: {e['from']}\nDate: {e['date']}\n{e['subject']}\n{e['body'][:1000] or e['snippet']}"
        entries.append({
            "title": e["subject"],
            "content": content,
            "metadata": f"from={e['from']}; date={e['date']}; gmail_id={e['id']}",
            "category": f"email:{category}",
        })

    # Index into knowledge base (use per-email category)
    indexed = 0
    for entry in entries:
        cat = entry.pop("category")
        indexed += await index_source(conn, "gmail_sync", cat, [entry])

    # Update sync timestamp
    _update_sync_timestamp(conn)

    log.info("Email sync complete: %d fetched, %d indexed", len(emails), indexed)
    return {"fetched": len(emails), "indexed": indexed}


def _update_sync_timestamp(conn):
    """Update the last email sync timestamp in settings."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('last_email_sync', ?)",
        (now,),
    )
    conn.commit()
