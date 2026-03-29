"""Categorize and label Gmail inbox emails based on content.

Uses the Gmail API with OAuth credentials:
- READ: Reuses existing readonly token (scripts/token.json) for fetching emails
- MODIFY: Requires a separate token with gmail.modify scope for applying labels

Setup for modify token:
    1. Ensure scripts/credentials.json exists (same OAuth client)
    2. Run PharoClaw and use /label run — it will trigger the OAuth flow
       for gmail.modify scope and save to scripts/token_label.json
    3. Grant the gmail.modify permission when prompted in browser
"""

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from config import (
    DB_PATH,
    SCRIPTS_DIR,
    TIMEZONE,
    TOKEN_FILE,
)

log = logging.getLogger("pharoclaw.actions.email_categorizer")

_tables_ensured = False

SKILL = {
    "name": "email_categorizer",
    "description": "Categorize and label Gmail inbox emails using AI classification",
    "category": "productivity",
    "patterns": [
        (r"\b(?:categoriz|label|organiz|sort)\w*\s+(?:my\s+)?(?:email|inbox|mail)\b", "label"),
        (r"\b(?:email|inbox|mail)\w*\s+.*\b(?:categoriz|label|organiz|sort)\b", "label"),
    ],
    "actions": [
        {"type": "label", "handler": None, "keywords": "categorize label organize sort email inbox mail", "description": "Categorize inbox emails"},
    ],
    "examples": ["Categorize my inbox", "Label my emails"],
}

TOKEN_FILE_MODIFY = SCRIPTS_DIR / "token_label.json"
SCOPES_MODIFY = [
    "https://www.googleapis.com/auth/gmail.modify",
]

# Default categorization rules: label → list of keywords/patterns
DEFAULT_RULES = {
    "Finance": ["invoice", "receipt", "payment", "bank", "transaction", "statement", "tax"],
    "Shopping": ["order", "shipping", "delivery", "tracking", "purchase", "cart"],
    "Travel": ["flight", "booking", "hotel", "itinerary", "airline", "reservation"],
    "Newsletters": ["unsubscribe", "newsletter", "digest", "weekly update"],
    "Social": ["linkedin", "facebook", "twitter", "instagram", "mentioned you"],
    "Work": ["meeting", "standup", "sprint", "jira", "confluence", "slack"],
    "Promotions": ["sale", "discount", "offer", "deal", "coupon", "promo"],
}


def ensure_tables(conn: sqlite3.Connection):
    """Create tables. Called once at startup."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_label_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            keywords TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_label_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            subject TEXT,
            label_applied TEXT NOT NULL,
            categorized_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_label_rules_label
        ON email_label_rules(label)
    """)
    conn.commit()


# --- Helpers ---

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _get_credentials(scopes: list[str], token_file: Path):
    """Get or refresh OAuth credentials."""
    from oauth_utils import load_credentials
    return load_credentials(token_file, scopes)


def _get_gmail_read():
    """Gmail service with readonly scope."""
    creds = _get_credentials(
        ["https://www.googleapis.com/auth/gmail.readonly"], TOKEN_FILE
    )
    return build("gmail", "v1", credentials=creds)


def _get_gmail_modify():
    """Gmail service with modify scope (for applying labels)."""
    creds = _get_credentials(SCOPES_MODIFY, TOKEN_FILE_MODIFY)
    return build("gmail", "v1", credentials=creds)


def _get_rules() -> dict[str, list[str]]:
    """Load rules from DB, falling back to defaults."""
    conn = _get_conn()
    rows = conn.execute("SELECT label, keywords FROM email_label_rules").fetchall()
    conn.close()
    if rows:
        return {r["label"]: json.loads(r["keywords"]) for r in rows}
    return dict(DEFAULT_RULES)


def _save_rule(label: str, keywords: list[str]):
    """Upsert a categorization rule."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO email_label_rules (label, keywords) VALUES (?, ?) "
        "ON CONFLICT(label) DO UPDATE SET keywords = excluded.keywords",
        (label, json.dumps(keywords)),
    )
    conn.commit()
    conn.close()


def _delete_rule(label: str) -> bool:
    """Delete a categorization rule. Returns True if deleted."""
    conn = _get_conn()
    result = conn.execute(
        "DELETE FROM email_label_rules WHERE label = ?", (label,)
    )
    conn.commit()
    conn.close()
    return result.rowcount > 0


# --- Core sync functions (called via asyncio.to_thread) ---

def _fetch_inbox_sync(max_results: int = 50) -> list[dict]:
    """Fetch inbox emails that haven't been categorized yet."""
    service = _get_gmail_read()
    results = service.users().messages().list(
        userId="me", q="in:inbox", maxResults=max_results
    ).execute()

    messages = results.get("messages", [])
    if not messages:
        return []

    # Filter out already-categorized messages
    conn = _get_conn()
    existing = {
        r["message_id"]
        for r in conn.execute(
            "SELECT message_id FROM email_label_history"
        ).fetchall()
    }
    conn.close()

    emails = []
    for msg in messages:
        if msg["id"] in existing:
            continue
        full = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["Subject", "From"],
        ).execute()
        headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
        emails.append({
            "id": msg["id"],
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "snippet": full.get("snippet", ""),
            "label_ids": full.get("labelIds", []),
        })

    return emails


def _categorize_email(email: dict, rules: dict[str, list[str]]) -> str | None:
    """Match an email against rules using word-boundary matching. Returns the best label or None."""
    text = f"{email['subject']} {email['snippet']} {email['from']}"
    best_label = None
    best_score = 0
    for label, keywords in rules.items():
        score = sum(
            1 for kw in keywords
            if re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE)
        )
        if score > best_score:
            best_score = score
            best_label = label
    return best_label if best_score > 0 else None


def _ensure_gmail_label_sync(service, label_name: str) -> str:
    """Get or create a Gmail label. Returns the label ID."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"].lower() == label_name.lower():
            return lbl["id"]

    created = service.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    log.info(f"Created Gmail label: {label_name} ({created['id']})")
    return created["id"]


def _apply_labels_sync(categorized: list[tuple[dict, str]]) -> int:
    """Apply Gmail labels to categorized emails. Returns count of labeled emails."""
    if not categorized:
        return 0

    service = _get_gmail_modify()
    label_cache: dict[str, str] = {}
    applied = 0

    conn = _get_conn()
    try:
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz).isoformat()

        for email, label_name in categorized:
            if label_name not in label_cache:
                label_cache[label_name] = _ensure_gmail_label_sync(service, label_name)

            label_id = label_cache[label_name]
            service.users().messages().modify(
                userId="me", id=email["id"],
                body={"addLabelIds": [label_id]},
            ).execute()

            conn.execute(
                "INSERT INTO email_label_history (message_id, subject, label_applied, categorized_at) "
                "VALUES (?, ?, ?, ?)",
                (email["id"], email["subject"], label_name, now),
            )
            applied += 1
            log.info(f"Labeled '{email['subject'][:50]}' → {label_name}")

        conn.commit()
    finally:
        conn.close()
    return applied


def _get_history_sync(limit: int = 20) -> list[dict]:
    """Fetch recent categorization history."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT subject, label_applied, categorized_at FROM email_label_history "
        "ORDER BY categorized_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Async wrappers ---

async def run_categorization(max_results: int = 50) -> str:
    """Fetch inbox, categorize, and apply labels. Returns summary text."""
    emails = await asyncio.to_thread(_fetch_inbox_sync, max_results)
    if not emails:
        return "No new inbox emails to categorize."

    rules = _get_rules()
    categorized = []
    skipped = 0
    for email in emails:
        label = _categorize_email(email, rules)
        if label:
            categorized.append((email, label))
        else:
            skipped += 1

    applied = await asyncio.to_thread(_apply_labels_sync, categorized)

    lines = [f"Processed {len(emails)} emails: {applied} labeled, {skipped} unmatched."]
    if categorized:
        lines.append("")
        label_counts: dict[str, int] = {}
        for _, lbl in categorized:
            label_counts[lbl] = label_counts.get(lbl, 0) + 1
        for lbl, cnt in sorted(label_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {lbl}: {cnt}")

    return "\n".join(lines)


async def get_history(limit: int = 20) -> str:
    """Format recent categorization history."""
    rows = await asyncio.to_thread(_get_history_sync, limit)
    if not rows:
        return "No categorization history yet."
    lines = ["Recent categorizations:"]
    for r in rows:
        lines.append(f"  [{r['label_applied']}] {r['subject'][:60]}")
    return "\n".join(lines[:50])  # cap output


async def handle_label(update, context):
    """Handle /label command.

    Subcommands:
      /label           — categorize inbox emails
      /label run [N]   — categorize up to N emails (default 50)
      /label preview [N] — preview categorization without applying (default 20)
      /label rules     — show current rules
      /label add <Label> kw1,kw2,...  — add/update a rule
      /label remove <Label>  — remove a custom rule
      /label history   — show recent categorizations
    """
    global _tables_ensured
    args = context.args or []
    sub = args[0].lower() if args else "run"

    if not _tables_ensured:
        conn = _get_conn()
        ensure_tables(conn)
        conn.close()
        _tables_ensured = True

    if sub == "preview":
        max_results = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
        await update.message.reply_text("Previewing categorization (read-only)...")
        emails = await asyncio.to_thread(_fetch_inbox_sync, max_results)
        if not emails:
            await update.message.reply_text("No new inbox emails to categorize.")
            return
        rules = _get_rules()
        lines = []
        unmatched = 0
        for email in emails:
            label = _categorize_email(email, rules)
            if label:
                lines.append(f"[{label}] {email['subject'][:60]} — \"{email['snippet'][:40]}...\"")
            else:
                unmatched += 1
        if lines:
            lines.append(f"\n{len(lines)} matched, {unmatched} unmatched.")
        else:
            lines.append(f"No matches. {unmatched} unmatched emails.")
        await update.message.reply_text("\n".join(lines[:50]))

    elif sub == "run" or sub.isdigit():
        max_results = int(args[1]) if len(args) > 1 and args[1].isdigit() else 50
        if sub.isdigit():
            max_results = int(sub)
        await update.message.reply_text("Categorizing inbox emails...")
        result = await run_categorization(max_results)
        await update.message.reply_text(result)

    elif sub == "rules":
        rules = _get_rules()
        lines = ["Categorization rules:"]
        for label, keywords in sorted(rules.items()):
            lines.append(f"  {label}: {', '.join(keywords[:5])}{'...' if len(keywords) > 5 else ''}")
        await update.message.reply_text("\n".join(lines))

    elif sub == "add":
        if len(args) < 3:
            await update.message.reply_text("Usage: /label add <Label> kw1,kw2,kw3")
            return
        label = args[1]
        keywords = [kw.strip() for kw in " ".join(args[2:]).split(",") if kw.strip()]
        _save_rule(label, keywords)
        await update.message.reply_text(f"Rule saved: {label} → {', '.join(keywords)}")

    elif sub == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /label remove <Label>")
            return
        label = args[1]
        if _delete_rule(label):
            await update.message.reply_text(f"Rule '{label}' removed.")
        else:
            await update.message.reply_text(f"No custom rule found for '{label}'.")

    elif sub == "history":
        result = await get_history()
        await update.message.reply_text(result)

    else:
        await update.message.reply_text(
            "Usage: /label [run|preview|rules|add|remove|history]\n"
            "  /label — categorize inbox emails\n"
            "  /label preview [N] — preview without applying\n"
            "  /label rules — show rules\n"
            "  /label add Finance invoice,receipt\n"
            "  /label remove Finance\n"
            "  /label history — recent labels applied"
        )
