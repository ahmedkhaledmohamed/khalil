"""Google Tasks state provider — fetch pending and completed tasks."""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import TOKEN_FILE_TASKS, TIMEZONE

log = logging.getLogger("khalil.state.tasks")


def _get_tasks_service():
    """Get Google Tasks API service using existing OAuth tokens."""
    from googleapiclient.discovery import build
    from oauth_utils import load_credentials

    scopes = ["https://www.googleapis.com/auth/tasks.readonly"]
    creds = load_credentials(TOKEN_FILE_TASKS, scopes, allow_interactive=False)
    return build("tasks", "v1", credentials=creds)


def _fetch_all_tasks_sync(include_completed: bool = True) -> list[dict]:
    """Fetch tasks from all task lists (sync, runs in thread)."""
    service = _get_tasks_service()

    # Get all task lists
    lists_result = service.tasklists().list(maxResults=100).execute()
    task_lists = lists_result.get("items", [])

    all_tasks = []
    for tl in task_lists:
        list_id = tl["id"]
        list_name = tl.get("title", "Tasks")

        params = {
            "tasklist": list_id,
            "maxResults": 100,
            "showCompleted": include_completed,
        }
        tasks_result = service.tasks().list(**params).execute()

        for task in tasks_result.get("items", []):
            if not task.get("title", "").strip():
                continue
            all_tasks.append({
                "title": task["title"],
                "status": task.get("status", "needsAction"),
                "due": task.get("due", ""),
                "notes": task.get("notes", ""),
                "list_name": list_name,
                "updated": task.get("updated", ""),
            })

    return all_tasks


async def get_all_tasks(include_completed: bool = True) -> list[dict]:
    """Get all tasks from Google Tasks. Returns list of task dicts."""
    return await asyncio.to_thread(_fetch_all_tasks_sync, include_completed)


async def get_pending_tasks() -> list[dict]:
    """Get pending (incomplete) tasks from Google Tasks."""
    all_tasks = await get_all_tasks(include_completed=False)
    return [t for t in all_tasks if t["status"] != "completed"]
