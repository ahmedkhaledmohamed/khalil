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

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from config import CREDENTIALS_FILE, TOKEN_FILE, TOKEN_FILE_COMPOSE

log = logging.getLogger("khalil.actions.gmail")

SCOPES_READ = ["https://www.googleapis.com/auth/gmail.readonly"]
SCOPES_COMPOSE = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


def _get_credentials(scopes: list[str], token_file):
    """Get or refresh OAuth credentials. Reused from google_sync.py pattern."""
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Missing {CREDENTIALS_FILE}. "
                    "Download from Google Cloud Console → APIs → Credentials → OAuth 2.0"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), scopes)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return creds


def _get_gmail_service(write: bool = False):
    """Get Gmail API service. Use write=True for draft/send operations."""
    if write:
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
