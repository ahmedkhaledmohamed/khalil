"""Live Gmail integration — search, draft, and send emails.

Reuses OAuth pattern from scripts/google_sync.py.
Read operations use the existing readonly token.
Write operations (draft/send) use a separate token with gmail.compose scope.

All public functions are async — sync Google API calls run in asyncio.to_thread().
"""

import asyncio
import base64
import logging
import re
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from config import (
    TOKEN_FILE, TOKEN_FILE_COMPOSE, TOKEN_FILE_MODIFY,
    TOKEN_FILE_CONTACTS, TOKEN_FILE_TASKS, TOKEN_FILE_DRIVE_WRITE, TOKEN_FILE_WORK,
)

log = logging.getLogger("khalil.actions.gmail")

SKILL = {
    "name": "gmail",
    "description": "Gmail integration — search, draft, and send emails",
    "category": "communication",
    "patterns": [
        (r"\bemail\b.*\babout\b", "email"),
        (r"\bsend\s+(?:an?\s+)?email\b", "email"),
        (r"\bdraft\s+(?:an?\s+)?email\b", "email"),
        (r"\bwrite\s+(?:an?\s+)?email\b", "email"),
        (r"\bsearch\s+(?:my\s+)?work\s+email\b", "email_work"),
        (r"\bcheck\s+(?:my\s+)?work\s+(?:inbox|email|mail)\b", "email_work"),
        (r"\bsearch\s+(?:my\s+)?personal\s+email\b", "email_personal"),
        (r"\bcheck\s+(?:my\s+)?personal\s+(?:inbox|email|mail)\b", "email_personal"),
        (r"\b(?:read|show|get|pull\s+up|look\s+at)\s+(?:my\s+)?(?:latest|new|recent)?\s*emails?\b", "email_personal"),
        (r"\bany\s+(?:new|unread)?\s*emails?\b", "email_personal"),
        (r"\b(?:check|show)\s+(?:my\s+)?(?:inbox|emails?|mail)\b", "email_personal"),
        (r"\bemails?\s+from\s+\w+", "email_personal"),
        (r"\b(?:important|urgent)\s+(?:emails?|mail)\b", "email_personal"),
        (r"\bemails?\s+(?:from\s+)?(?:today|this\s+week|recent)\b", "email_personal"),
        (r"\b(?:anything|something)\s+(?:in\s+)?(?:my\s+)?inbox\b", "email_personal"),
    ],
    "actions": [
        {"type": "email", "handler": "handle_intent", "keywords": "email send draft write about", "description": "Send or draft an email",
         "parameters": {
             "to": {"type": "string", "description": "Recipient email address"},
             "subject": {"type": "string", "description": "Email subject"},
         }},
        {"type": "email_work", "handler": "handle_intent", "keywords": "search check work email inbox", "description": "Search work email"},
        {"type": "email_personal", "handler": "handle_intent", "keywords": "search check personal email inbox", "description": "Search personal email"},
    ],
    "examples": ["Send an email to John about the meeting", "Check my work inbox"],
    "voice": {"confirm_before_execute": True, "response_style": "brief"},
}

SCOPES_READ = ["https://www.googleapis.com/auth/gmail.readonly"]
SCOPES_COMPOSE = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
SCOPES_MODIFY = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
SCOPES_CONTACTS = ["https://www.googleapis.com/auth/contacts.readonly"]
SCOPES_TASKS = ["https://www.googleapis.com/auth/tasks.readonly"]
SCOPES_TASKS_WRITE = [
    "https://www.googleapis.com/auth/tasks.readonly",
    "https://www.googleapis.com/auth/tasks",
]
SCOPES_DRIVE_WRITE = ["https://www.googleapis.com/auth/drive.file"]

# #55: Multi-account Gmail configuration
GMAIL_ACCOUNTS = {
    "personal": TOKEN_FILE,
    "work": TOKEN_FILE_WORK,
}


def _get_credentials(scopes: list[str], token_file):
    """Get or refresh OAuth credentials."""
    from oauth_utils import load_credentials
    return load_credentials(token_file, scopes)


def _get_gmail_service(write: bool = False, modify: bool = False):
    """Get Gmail API service. Use write=True for draft/send, modify=True for label ops."""
    if modify:
        creds = _get_credentials(SCOPES_MODIFY, TOKEN_FILE_MODIFY)
    elif write:
        creds = _get_credentials(SCOPES_COMPOSE, TOKEN_FILE_COMPOSE)
    else:
        creds = _get_credentials(SCOPES_READ, TOKEN_FILE)
    return build("gmail", "v1", credentials=creds)


def extract_body(payload: dict) -> str:
    """Extract plain text body from Gmail message payload.

    Prefers text/plain, falls back to stripped HTML.
    """
    # Direct body
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    # Multipart — prefer text/plain
    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")

    # Fallback: text/html stripped
    for part in parts:
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            html = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", html).strip()

    return ""


def _search_emails_sync(query: str, max_results: int = 10) -> list[dict]:
    """Synchronous Gmail search — called via asyncio.to_thread()."""
    service = _get_gmail_service(write=False)
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
        body = extract_body(full.get("payload", {}))
        emails.append({
            "id": msg["id"],
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "date": headers.get("Date", ""),
            "snippet": full.get("snippet", ""),
            "body": body[:2000],  # Cap at 2000 chars
        })

    return emails


def _draft_email_sync(to: str, subject: str, body: str) -> dict:
    """Synchronous draft creation — called via asyncio.to_thread()."""
    service = _get_gmail_service(write=True)

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    draft = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()

    log.info(f"Draft created: {draft['id']} → {to}: {subject}")
    return {
        "draft_id": draft["id"],
        "to": to,
        "subject": subject,
        "body": body,
    }


def _send_draft_sync(draft_id: str) -> dict:
    """Synchronous draft send — called via asyncio.to_thread()."""
    service = _get_gmail_service(write=True)

    result = service.users().drafts().send(
        userId="me", body={"id": draft_id}
    ).execute()

    log.info(f"Draft {draft_id} sent, message ID: {result['id']}")
    return {"message_id": result["id"], "status": "sent"}


async def search_emails(query: str, max_results: int = 10) -> list[dict]:
    """Search Gmail with a query string. Returns list of email dicts."""
    return await asyncio.to_thread(_search_emails_sync, query, max_results)


async def draft_email(to: str, subject: str, body: str) -> dict:
    """Create a Gmail draft. Returns draft ID and preview."""
    return await asyncio.to_thread(_draft_email_sync, to, subject, body)


async def send_draft(draft_id: str) -> dict:
    """Send an existing Gmail draft. Returns the sent message ID."""
    return await asyncio.to_thread(_send_draft_sync, draft_id)


# --- #46: Gmail Label Management ---

def _list_labels_sync() -> list[dict]:
    """Synchronous label listing — called via asyncio.to_thread()."""
    service = _get_gmail_service(modify=True)
    results = service.users().labels().list(userId="me").execute()
    labels = results.get("labels", [])
    return [{"id": lbl["id"], "name": lbl["name"], "type": lbl.get("type", "")} for lbl in labels]


def _apply_label_sync(message_id: str, label_name: str) -> dict:
    """Synchronous label application — called via asyncio.to_thread()."""
    service = _get_gmail_service(modify=True)
    # Resolve label name to ID
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    label_id = None
    for lbl in labels:
        if lbl["name"].lower() == label_name.lower():
            label_id = lbl["id"]
            break
    if not label_id:
        raise ValueError(f"Label not found: {label_name}")

    service.users().messages().modify(
        userId="me", id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()
    log.info("Applied label %s (%s) to message %s", label_name, label_id, message_id)
    return {"message_id": message_id, "label": label_name, "action": "applied"}


def _remove_label_sync(message_id: str, label_name: str) -> dict:
    """Synchronous label removal — called via asyncio.to_thread()."""
    service = _get_gmail_service(modify=True)
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    label_id = None
    for lbl in labels:
        if lbl["name"].lower() == label_name.lower():
            label_id = lbl["id"]
            break
    if not label_id:
        raise ValueError(f"Label not found: {label_name}")

    service.users().messages().modify(
        userId="me", id=message_id,
        body={"removeLabelIds": [label_id]},
    ).execute()
    log.info("Removed label %s (%s) from message %s", label_name, label_id, message_id)
    return {"message_id": message_id, "label": label_name, "action": "removed"}


async def list_labels() -> list[dict]:
    """List all Gmail labels. Requires gmail.modify scope (#46)."""
    return await asyncio.to_thread(_list_labels_sync)


async def apply_label(message_id: str, label_name: str) -> dict:
    """Apply a label to a Gmail message. Requires gmail.modify scope (#46)."""
    return await asyncio.to_thread(_apply_label_sync, message_id, label_name)


async def remove_label(message_id: str, label_name: str) -> dict:
    """Remove a label from a Gmail message. Requires gmail.modify scope (#46)."""
    return await asyncio.to_thread(_remove_label_sync, message_id, label_name)


# --- #49: Google Contacts Search ---

def _get_people_service():
    """Get Google People API service for contacts search."""
    creds = _get_credentials(SCOPES_CONTACTS, TOKEN_FILE_CONTACTS)
    return build("people", "v1", credentials=creds)


def _search_contacts_sync(query: str, max_results: int = 10) -> list[dict]:
    """Synchronous contacts search via People API."""
    service = _get_people_service()
    results = service.people().searchContacts(
        query=query,
        readMask="names,emailAddresses,phoneNumbers",
        pageSize=max_results,
    ).execute()

    contacts = []
    for person in results.get("results", []):
        p = person.get("person", {})
        names = p.get("names", [])
        emails = p.get("emailAddresses", [])
        phones = p.get("phoneNumbers", [])
        contacts.append({
            "name": names[0]["displayName"] if names else "",
            "email": emails[0]["value"] if emails else "",
            "phone": phones[0]["value"] if phones else "",
        })

    return contacts


async def search_contacts(query: str, max_results: int = 10) -> list[dict]:
    """Search Google Contacts by name or email. Returns list of {name, email, phone}."""
    return await asyncio.to_thread(_search_contacts_sync, query, max_results)


# --- #50: Google Tasks Integration ---

def _get_tasks_service(write: bool = False):
    """Get Google Tasks API service."""
    scopes = SCOPES_TASKS_WRITE if write else SCOPES_TASKS
    creds = _get_credentials(scopes, TOKEN_FILE_TASKS)
    return build("tasks", "v1", credentials=creds)


def _list_tasks_sync(tasklist: str = "@default") -> list[dict]:
    """Synchronous task listing — called via asyncio.to_thread()."""
    service = _get_tasks_service()
    results = service.tasks().list(tasklist=tasklist, showCompleted=False).execute()
    tasks = results.get("items", [])
    return [
        {
            "id": t["id"],
            "title": t.get("title", ""),
            "notes": t.get("notes", ""),
            "due": t.get("due", ""),
            "status": t.get("status", ""),
        }
        for t in tasks
    ]


def _create_task_sync(title: str, notes: str = "", due_date: str | None = None,
                      tasklist: str = "@default") -> dict:
    """Synchronous task creation — called via asyncio.to_thread()."""
    service = _get_tasks_service(write=True)
    body = {"title": title}
    if notes:
        body["notes"] = notes
    if due_date:
        # Tasks API expects RFC 3339 date
        body["due"] = due_date if "T" in due_date else f"{due_date}T00:00:00.000Z"

    task = service.tasks().insert(tasklist=tasklist, body=body).execute()
    log.info("Task created: %s — %s", task["id"], title)
    return {
        "id": task["id"],
        "title": task.get("title", ""),
        "notes": task.get("notes", ""),
        "due": task.get("due", ""),
        "status": task.get("status", ""),
    }


async def list_tasks(tasklist: str = "@default") -> list[dict]:
    """List Google Tasks. Returns list of {id, title, notes, due, status}."""
    return await asyncio.to_thread(_list_tasks_sync, tasklist)


async def create_task(title: str, notes: str = "", due_date: str | None = None) -> dict:
    """Create a Google Task. Returns the created task dict."""
    return await asyncio.to_thread(_create_task_sync, title, notes, due_date)


# --- #55: Multi-Account Gmail ---

def _search_emails_account_sync(query: str, account: str = "personal", max_results: int = 10) -> list[dict]:
    """Search emails for a specific account. Falls back to personal if account not configured."""
    token_file = GMAIL_ACCOUNTS.get(account, GMAIL_ACCOUNTS["personal"])
    creds = _get_credentials(SCOPES_READ, token_file)
    service = build("gmail", "v1", credentials=creds)

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
        body = extract_body(full.get("payload", {}))
        emails.append({
            "id": msg["id"],
            "account": account,
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "date": headers.get("Date", ""),
            "snippet": full.get("snippet", ""),
            "body": body[:2000],
        })

    return emails


async def search_emails_account(query: str, account: str = "personal", max_results: int = 10) -> list[dict]:
    """Search emails for a specific account (#55). Returns list of email dicts."""
    return await asyncio.to_thread(_search_emails_account_sync, query, account, max_results)


def _search_all_accounts_sync(query: str, max_results: int = 5) -> list[dict]:
    """Search across all configured Gmail accounts and merge results."""
    all_emails = []
    for account_name, token_file in GMAIL_ACCOUNTS.items():
        if not token_file.exists():
            continue
        try:
            emails = _search_emails_account_sync(query, account_name, max_results)
            all_emails.extend(emails)
        except Exception as e:
            log.warning("Failed to search %s account: %s", account_name, e)
    # Sort by date descending (newest first)
    all_emails.sort(key=lambda e: e.get("date", ""), reverse=True)
    return all_emails


async def search_all_accounts(query: str, max_results: int = 5) -> list[dict]:
    """Search across all configured Gmail accounts (#55). Merges and sorts results."""
    return await asyncio.to_thread(_search_all_accounts_sync, query, max_results)


# --- #54: Google Drive File Creation ---

def _get_drive_service():
    """Get Google Drive API service with write scope."""
    creds = _get_credentials(SCOPES_DRIVE_WRITE, TOKEN_FILE_DRIVE_WRITE)
    return build("drive", "v3", credentials=creds)


def _create_drive_doc_sync(title: str, content: str = "") -> dict:
    """Create a Google Doc via Drive API."""
    service = _get_drive_service()

    # Create the document
    file_metadata = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
    }
    doc = service.files().create(body=file_metadata, fields="id,name,webViewLink").execute()
    doc_id = doc["id"]
    doc_url = doc.get("webViewLink", f"https://docs.google.com/document/d/{doc_id}")

    # If content provided, update via Docs API
    if content:
        try:
            docs_service = build("docs", "v1", credentials=_get_credentials(SCOPES_DRIVE_WRITE, TOKEN_FILE_DRIVE_WRITE))
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
            ).execute()
        except Exception as e:
            log.warning("Could not insert content into doc %s: %s", doc_id, e)

    log.info("Created Google Doc: %s (%s)", title, doc_id)
    return {"id": doc_id, "title": title, "url": doc_url, "type": "document"}


def _create_drive_sheet_sync(title: str, data: list[list[str]] | None = None) -> dict:
    """Create a Google Sheet via Drive API."""
    service = _get_drive_service()

    file_metadata = {
        "name": title,
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }
    sheet = service.files().create(body=file_metadata, fields="id,name,webViewLink").execute()
    sheet_id = sheet["id"]
    sheet_url = sheet.get("webViewLink", f"https://docs.google.com/spreadsheets/d/{sheet_id}")

    # If data provided, populate via Sheets API
    if data:
        try:
            sheets_service = build("sheets", "v4", credentials=_get_credentials(SCOPES_DRIVE_WRITE, TOKEN_FILE_DRIVE_WRITE))
            sheets_service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range="A1",
                valueInputOption="RAW",
                body={"values": data},
            ).execute()
        except Exception as e:
            log.warning("Could not populate sheet %s: %s", sheet_id, e)

    log.info("Created Google Sheet: %s (%s)", title, sheet_id)
    return {"id": sheet_id, "title": title, "url": sheet_url, "type": "spreadsheet"}


async def create_drive_doc(title: str, content: str = "") -> dict:
    """Create a Google Doc (#54). Returns {id, title, url, type}."""
    return await asyncio.to_thread(_create_drive_doc_sync, title, content)


async def create_drive_sheet(title: str, data: list[list[str]] | None = None) -> dict:
    """Create a Google Sheet (#54). Returns {id, title, url, type}."""
    return await asyncio.to_thread(_create_drive_sheet_sync, title, data)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "email":
        to_addr = intent.get("to", "")
        subject = intent.get("subject", "")
        if not to_addr or not subject:
            await ctx.reply("I need a recipient (to) and subject to draft an email. Try: 'email john@example.com about project update'")
        else:
            await ctx.reply(f"Draft email to {to_addr} about: {subject}\n(Use the Telegram interface to approve sending.)")
        return True

    if action in ("email_work", "email_personal"):
        account = "work" if action == "email_work" else "personal"
        query = intent.get("query", intent.get("text", ""))
        if not query:
            query = "is:unread"
        try:
            emails = await search_emails_account(query, account=account, max_results=10)
            if not emails:
                await ctx.reply(f"No {account} emails found for \"{query}\".")
            else:
                lines = [f"\U0001f4e7 {account.title()} Email — \"{query}\" ({len(emails)} results):\n"]
                for e in emails[:10]:
                    lines.append(f"  \u2022 {e.get('from', '?')}: {e.get('subject', '(no subject)')}")
                    if e.get("snippet"):
                        lines.append(f"    {e['snippet'][:80]}")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            from resilience import format_user_error
            await ctx.reply(format_user_error(e, skill_name=f"{account.title()} Email"))
        return True
    return False
