"""Live Google Drive integration — search files, retrieve content, list recent.

Reuses OAuth pattern from scripts/google_sync.py. All operations are read-only.

All public functions are async — sync Google API calls run in asyncio.to_thread().
"""

import asyncio
import logging
from datetime import datetime, timedelta

from googleapiclient.discovery import build

from config import TOKEN_FILE

log = logging.getLogger("pharoclaw.actions.drive")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Google Docs MIME types that can be exported as text
EXPORT_MIME_MAP = {
    "application/vnd.google-apps.document": ("text/plain", "Google Doc"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", "Google Sheet"),
    "application/vnd.google-apps.presentation": ("text/plain", "Google Slides"),
}


def _get_credentials():
    """Get or refresh OAuth credentials for Drive readonly."""
    from oauth_utils import load_credentials
    return load_credentials(TOKEN_FILE, SCOPES)


def _get_drive_service():
    """Get Drive API service."""
    creds = _get_credentials()
    return build("drive", "v3", credentials=creds)


def _search_files_sync(query: str, max_results: int = 10) -> list[dict]:
    """Synchronous Drive search — called via asyncio.to_thread()."""
    service = _get_drive_service()

    # If query doesn't look like a Drive API query, wrap it
    if "=" not in query and "in parents" not in query:
        api_query = f"name contains '{query}' and trashed = false"
    else:
        api_query = query

    results = service.files().list(
        q=api_query,
        pageSize=max_results,
        fields="files(id, name, mimeType, modifiedTime, size, webViewLink)",
    ).execute()

    files = results.get("files", [])
    return [
        {
            "id": f["id"],
            "name": f["name"],
            "type": f.get("mimeType", "unknown"),
            "modified": f.get("modifiedTime", "")[:10],
            "size": f.get("size", "—"),
            "link": f.get("webViewLink", ""),
        }
        for f in files
    ]


def _list_recent_sync(days: int = 7, max_results: int = 10) -> list[dict]:
    """Synchronous recent files — called via asyncio.to_thread()."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat() + "Z"
    service = _get_drive_service()

    results = service.files().list(
        q=f"modifiedTime > '{cutoff}' and trashed = false",
        pageSize=max_results,
        orderBy="modifiedTime desc",
        fields="files(id, name, mimeType, modifiedTime, size, webViewLink)",
    ).execute()

    files = results.get("files", [])
    return [
        {
            "id": f["id"],
            "name": f["name"],
            "type": f.get("mimeType", "unknown"),
            "modified": f.get("modifiedTime", "")[:10],
            "link": f.get("webViewLink", ""),
        }
        for f in files
    ]


def _get_file_content_sync(file_id: str, max_chars: int = 3000) -> str:
    """Synchronous file content retrieval — called via asyncio.to_thread()."""
    service = _get_drive_service()

    # Get file metadata to determine type
    meta = service.files().get(fileId=file_id, fields="mimeType, name").execute()
    mime = meta.get("mimeType", "")
    name = meta.get("name", "unknown")

    if mime in EXPORT_MIME_MAP:
        export_mime, type_name = EXPORT_MIME_MAP[mime]
        content = service.files().export(fileId=file_id, mimeType=export_mime).execute()
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        return f"[{type_name}: {name}]\n\n{text[:max_chars]}"
    else:
        # Binary file — just return metadata
        return f"[File: {name}] (type: {mime}) — binary content, cannot display as text"


async def search_files(query: str, max_results: int = 10) -> list[dict]:
    """Search Drive files by name."""
    return await asyncio.to_thread(_search_files_sync, query, max_results)


async def list_recent(days: int = 7, max_results: int = 10) -> list[dict]:
    """List recently modified files."""
    return await asyncio.to_thread(_list_recent_sync, days, max_results)


async def get_file_content(file_id: str, max_chars: int = 3000) -> str:
    """Get text content of a Drive file. Exports Google Docs as plain text."""
    return await asyncio.to_thread(_get_file_content_sync, file_id, max_chars)
