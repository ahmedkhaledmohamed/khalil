"""Manage Gmail labels and bulk message actions via Telegram.

Requires gmail.modify token (TOKEN_FILE_MODIFY): python scripts/google_sync.py --scope modify
Filter management needs gmail.settings.basic scope and is NOT supported here.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from config import DB_PATH, TOKEN_FILE_MODIFY, TIMEZONE

log = logging.getLogger("khalil.actions.gmail_manager")
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
_tables_ready = False

def ensure_tables(conn: sqlite3.Connection):
    """Create tables. Called once at startup."""
    global _tables_ready
    conn.execute(
        "CREATE TABLE IF NOT EXISTS gmail_bulk_ops (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "operation TEXT NOT NULL, query TEXT NOT NULL, label TEXT, "
        "message_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
    conn.commit()
    _tables_ready = True


def _maybe_ensure_tables():
    global _tables_ready
    if not _tables_ready:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            ensure_tables(conn)
        finally:
            conn.close()


def _svc():
    from oauth_utils import load_credentials
    return build("gmail", "v1", credentials=load_credentials(TOKEN_FILE_MODIFY, _SCOPES))


# --- Core sync functions (called via asyncio.to_thread) ---

def _list_labels_sync() -> list[dict]:
    svc = _svc()
    out = []
    for lbl in svc.users().labels().list(userId="me").execute().get("labels", []):
        d = svc.users().labels().get(userId="me", id=lbl["id"]).execute()
        out.append({"id": d["id"], "name": d["name"], "type": d.get("type", ""),
                     "total": d.get("messagesTotal", 0), "unread": d.get("messagesUnread", 0)})
    return out


def _create_label_sync(name: str) -> dict:
    svc = _svc()
    lbl = svc.users().labels().create(userId="me", body={
        "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show",
    }).execute()
    log.info("Created label: %s (%s)", lbl["name"], lbl["id"])
    return {"id": lbl["id"], "name": lbl["name"]}


def _delete_label_sync(name: str) -> dict:
    svc = _svc()
    for lbl in svc.users().labels().list(userId="me").execute().get("labels", []):
        if lbl["name"].lower() == name.lower():
            if lbl.get("type") == "system":
                raise ValueError(f"Cannot delete system label: {name}")
            svc.users().labels().delete(userId="me", id=lbl["id"]).execute()
            log.info("Deleted label: %s (%s)", lbl["name"], lbl["id"])
            return {"id": lbl["id"], "name": lbl["name"]}
    raise ValueError(f"Label not found: {name}")


def _resolve_label_id(svc, name: str) -> str:
    for lbl in svc.users().labels().list(userId="me").execute().get("labels", []):
        if lbl["name"].lower() == name.lower():
            return lbl["id"]
    raise ValueError(f"Label not found: {name}")


def _search_ids(svc, query: str, limit: int = 50) -> list[str]:
    ids, token = [], None
    while len(ids) < limit:
        resp = svc.users().messages().list(
            userId="me", q=query, maxResults=min(limit - len(ids), 50), pageToken=token,
        ).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids


def _preview_sync(query: str, limit: int = 10) -> list[dict]:
    svc = _svc()
    out = []
    for mid in _search_ids(svc, query, limit):
        msg = svc.users().messages().get(
            userId="me", id=mid, format="metadata", metadataHeaders=["Subject", "From", "Date"],
        ).execute()
        h = {x["name"]: x["value"] for x in msg.get("payload", {}).get("headers", [])}
        out.append({"id": mid, "subject": h.get("Subject", "(no subject)"),
                     "from": h.get("From", ""), "date": h.get("Date", "")})
    return out


def _batch_modify(svc, ids: list[str], add: list[str] | None = None, remove: list[str] | None = None):
    body = {"ids": [], **({"addLabelIds": add} if add else {}), **({"removeLabelIds": remove} if remove else {})}
    for i in range(0, len(ids), 50):
        body["ids"] = ids[i:i + 50]
        svc.users().messages().batchModify(userId="me", body=body).execute()


def _bulk_label_sync(query: str, label: str, limit: int = 50) -> dict:
    svc = _svc()
    lid = _resolve_label_id(svc, label)
    ids = _search_ids(svc, query, limit)
    if ids:
        _batch_modify(svc, ids, add=[lid])
        log.info("Bulk labeled %d msgs with '%s'", len(ids), label)
    return {"count": len(ids), "label": label, "query": query}


def _bulk_archive_sync(query: str, limit: int = 50) -> dict:
    svc = _svc()
    ids = _search_ids(svc, query, limit)
    if ids:
        _batch_modify(svc, ids, remove=["INBOX"])
        log.info("Bulk archived %d msgs", len(ids))
    return {"count": len(ids), "query": query}


def _bulk_read_sync(query: str, limit: int = 50) -> dict:
    svc = _svc()
    ids = _search_ids(svc, query, limit)
    if ids:
        _batch_modify(svc, ids, remove=["UNREAD"])
        log.info("Bulk marked %d msgs read", len(ids))
    return {"count": len(ids), "query": query}


def _log_op(op: str, query: str, label: str | None, count: int):
    _maybe_ensure_tables()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO gmail_bulk_ops (operation, query, label, message_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (op, query, label, count, datetime.now(ZoneInfo(TIMEZONE)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# --- Async wrappers ---

async def list_labels(): return await asyncio.to_thread(_list_labels_sync)
async def create_label(n): return await asyncio.to_thread(_create_label_sync, n)
async def delete_label(n): return await asyncio.to_thread(_delete_label_sync, n)
async def preview_messages(q, n=10): return await asyncio.to_thread(_preview_sync, q, n)


async def bulk_label(q, label, n=50):
    r = await asyncio.to_thread(_bulk_label_sync, q, label, n)
    _log_op("label", q, label, r["count"])
    return r

async def bulk_archive(q, n=50):
    r = await asyncio.to_thread(_bulk_archive_sync, q, n)
    _log_op("archive", q, None, r["count"])
    return r


async def bulk_read(q, n=50):
    r = await asyncio.to_thread(_bulk_read_sync, q, n)
    _log_op("mark_read", q, None, r["count"])
    return r

# --- Telegram command handler ---

HELP_TEXT = (
    "Gmail Manager — /gmail <subcommand>\n\n"
    "  labels — list all labels with counts\n"
    "  label create|delete <name>\n"
    "  preview <query> — dry-run, show matches\n"
    "  bulk label <query> | <label>\n"
    "  bulk archive <query>\n"
    "  bulk read <query>\n"
    "  history — recent bulk operations\n\n"
    "Queries use Gmail syntax (from:, subject:, is:unread, etc.)."
)


async def handle_gmail(update, context):
    """Handle /gmail command."""
    args = context.args or []
    if not args:
        await update.message.reply_text(HELP_TEXT)
        return
    sub = args[0].lower()
    try:
        if sub == "labels":
            labels = await list_labels()
            if not labels:
                await update.message.reply_text("No labels found.")
                return
            user = sorted([l for l in labels if l["type"] != "system"], key=lambda x: x["name"])
            sys = sorted([l for l in labels if l["type"] == "system" and l["total"] > 0], key=lambda x: x["name"])
            lines = ["User labels:"]
            lines.extend(f"  {l['name']} — {l['total']} msgs ({l['unread']} unread)" for l in user)
            lines.append("\nSystem labels:")
            lines.extend(f"  {l['name']} — {l['total']} msgs ({l['unread']} unread)" for l in sys)
            text = "\n".join(lines)
            await update.message.reply_text(text[:4000] + ("\n..." if len(text) > 4000 else ""))

        elif sub == "label" and len(args) >= 3 and args[1].lower() == "create":
            r = await create_label(" ".join(args[2:]))
            await update.message.reply_text(f"Label created: {r['name']}")

        elif sub == "label" and len(args) >= 3 and args[1].lower() == "delete":
            r = await delete_label(" ".join(args[2:]))
            await update.message.reply_text(f"Label deleted: {r['name']}")

        elif sub == "preview" and len(args) >= 2:
            query = " ".join(args[1:])
            msgs = await preview_messages(query)
            if not msgs:
                await update.message.reply_text(f"No messages match: {query}")
                return
            lines = [f"Preview — {len(msgs)} message(s) for \"{query}\":\n"]
            for m in msgs:
                sender = m["from"].split("<")[0].strip() if "<" in m["from"] else m["from"]
                lines.append(f"  {sender}: {m['subject']}")
            await update.message.reply_text("\n".join(lines))

        elif sub == "bulk" and len(args) >= 3:
            action = args[1].lower()
            if action == "label":
                rest = " ".join(args[2:])
                if "|" not in rest:
                    await update.message.reply_text(
                        "Usage: /gmail bulk label <query> | <label>\n"
                        "Example: /gmail bulk label from:noreply | Newsletters")
                    return
                q, lbl = rest.rsplit("|", 1)
                q, lbl = q.strip(), lbl.strip()
                if not q or not lbl:
                    await update.message.reply_text("Both query and label name required.")
                    return
                r = await bulk_label(q, lbl)
                await update.message.reply_text(f"Labeled {r['count']} message(s) with \"{r['label']}\".")
            elif action == "archive":
                q = " ".join(args[2:])
                r = await bulk_archive(q)
                await update.message.reply_text(f"Archived {r['count']} message(s).")
            elif action == "read":
                q = " ".join(args[2:])
                r = await bulk_read(q)
                await update.message.reply_text(f"Marked {r['count']} message(s) as read.")
            else:
                await update.message.reply_text(f"Unknown bulk action: {action}. Use: label, archive, read")

        elif sub == "history":
            _maybe_ensure_tables()
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT operation, query, label, message_count, created_at "
                    "FROM gmail_bulk_ops ORDER BY id DESC LIMIT 10").fetchall()
            finally:
                conn.close()
            if not rows:
                await update.message.reply_text("No bulk operations recorded yet.")
                return
            lines = ["Recent bulk operations:\n"]
            for r in rows:
                lbl = f" -> {r['label']}" if r["label"] else ""
                lines.append(f"  {r['created_at'][:16]} | {r['operation']}{lbl} | "
                             f"{r['message_count']} msgs | q: {r['query'][:40]}")
            await update.message.reply_text("\n".join(lines))

        else:
            await update.message.reply_text(f"Unknown subcommand: {sub}\n\n{HELP_TEXT}")

    except ValueError as e:
        await update.message.reply_text(f"Error: {e}")
    except Exception as e:
        log.exception("gmail_manager error")
        await update.message.reply_text(f"Gmail Manager error: {type(e).__name__}: {e}")
