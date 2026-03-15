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
    entries = []
    for e in emails:
        content = f"From: {e['from']}\nDate: {e['date']}\n{e['subject']}\n{e['body'][:1000] or e['snippet']}"
        entries.append({
            "title": e["subject"],
            "content": content,
            "metadata": f"from={e['from']}; date={e['date']}; gmail_id={e['id']}",
        })

    # Index into knowledge base
    indexed = await index_source(conn, "gmail_sync", "email:synced", entries)

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
