"""Local reminders stored in SQLite, delivered via Telegram push."""

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.reminders")


def _parse_relative_time(time_str: str) -> datetime | None:
    """Parse relative time expressions like 'in 2 hours', 'in 30 minutes', 'tomorrow 9am'."""
    time_str = time_str.strip().lower()
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    # "in N minutes/hours/days"
    m = re.match(r"in\s+(\d+)\s+(minute|minutes|min|hour|hours|hr|day|days)", time_str)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("min"):
            return now + timedelta(minutes=n)
        elif unit.startswith("hour") or unit.startswith("hr"):
            return now + timedelta(hours=n)
        elif unit.startswith("day"):
            return now + timedelta(days=n)

    # "tomorrow [at] Ham/pm"
    m = re.match(r"tomorrow(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", time_str)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    # "tomorrow" (default 9am)
    if time_str == "tomorrow":
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    # Try ISO format
    try:
        return datetime.fromisoformat(time_str)
    except ValueError:
        pass

    return None


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def create_reminder(text: str, due_at: datetime) -> dict:
    """Create a new reminder. Returns the reminder dict."""
    conn = _get_conn()
    cursor = conn.execute(
        "INSERT INTO reminders (text, due_at, status) VALUES (?, ?, 'active')",
        (text, due_at.isoformat()),
    )
    conn.commit()
    reminder_id = cursor.lastrowid
    conn.close()

    log.info(f"Reminder created: #{reminder_id} '{text}' due {due_at}")
    return {"id": reminder_id, "text": text, "due_at": due_at.isoformat(), "status": "active"}


def list_reminders() -> list[dict]:
    """List all active reminders."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, text, due_at, status, created_at FROM reminders WHERE status = 'active' ORDER BY due_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cancel_reminder(reminder_id: int) -> bool:
    """Cancel (soft delete) a reminder."""
    conn = _get_conn()
    result = conn.execute(
        "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'active'",
        (reminder_id,),
    )
    conn.commit()
    conn.close()
    return result.rowcount > 0


# --- Recurring reminders ---


def _parse_natural_cron(text: str) -> str | None:
    """Parse natural language schedule to cron expression.

    Supports: 'every monday 9am', 'every day', 'every day 9am',
    'first of month', 'first of month 9am', 'every friday 5pm'
    """
    text = text.strip().lower()

    # Day name mapping
    days = {
        "monday": "1", "tuesday": "2", "wednesday": "3", "thursday": "4",
        "friday": "5", "saturday": "6", "sunday": "0",
        "mon": "1", "tue": "2", "wed": "3", "thu": "4",
        "fri": "5", "sat": "6", "sun": "0",
    }

    def _parse_time(s: str) -> tuple[int, int]:
        """Extract hour/minute from a time string, default 9:00."""
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
        if not m:
            return 9, 0
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return hour, minute

    # "every day [at Xam/pm]"
    if text.startswith("every day"):
        h, m = _parse_time(text)
        return f"{m} {h} * * *"

    # "every <dayname> [at Xam/pm]"
    for day_name, day_num in days.items():
        if f"every {day_name}" in text:
            h, m = _parse_time(text)
            return f"{m} {h} * * {day_num}"

    # "first of month [at Xam/pm]"
    if "first of" in text and "month" in text:
        h, m = _parse_time(text)
        return f"{m} {h} 1 * *"

    return None


def create_recurring(text: str, cron_expr: str) -> dict:
    """Create a recurring reminder. Returns the reminder dict."""
    from croniter import croniter

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    cron = croniter(cron_expr, now)
    next_fire = cron.get_next(datetime)

    conn = _get_conn()
    cursor = conn.execute(
        "INSERT INTO recurring_reminders (text, cron_expression, next_fire_at, status) VALUES (?, ?, ?, 'active')",
        (text, cron_expr, next_fire.isoformat()),
    )
    conn.commit()
    rid = cursor.lastrowid
    conn.close()

    log.info(f"Recurring reminder #{rid}: '{text}' cron={cron_expr} next={next_fire}")
    return {"id": rid, "text": text, "cron_expression": cron_expr, "next_fire_at": next_fire.isoformat(), "status": "active"}


def list_recurring() -> list[dict]:
    """List all active recurring reminders."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, text, cron_expression, next_fire_at, status, created_at "
        "FROM recurring_reminders WHERE status = 'active' ORDER BY next_fire_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cancel_recurring(reminder_id: int) -> bool:
    """Cancel a recurring reminder."""
    conn = _get_conn()
    result = conn.execute(
        "UPDATE recurring_reminders SET status = 'cancelled' WHERE id = ? AND status = 'active'",
        (reminder_id,),
    )
    conn.commit()
    conn.close()
    return result.rowcount > 0


def check_recurring_due() -> list[dict]:
    """Find recurring reminders that are due, fire them, and reschedule next occurrence."""
    from croniter import croniter

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, text, cron_expression, next_fire_at FROM recurring_reminders "
        "WHERE status = 'active' AND next_fire_at <= ?",
        (now.isoformat(),),
    ).fetchall()

    fired = []
    for r in rows:
        # Reschedule to next occurrence
        cron = croniter(r["cron_expression"], now)
        next_fire = cron.get_next(datetime)
        conn.execute(
            "UPDATE recurring_reminders SET next_fire_at = ? WHERE id = ?",
            (next_fire.isoformat(), r["id"]),
        )
        fired.append({"id": r["id"], "text": r["text"]})
        log.info(f"Recurring #{r['id']} fired, next at {next_fire}")

    conn.commit()
    conn.close()
    return fired


def check_due_reminders() -> list[dict]:
    """Find and mark reminders that are due. Returns list of fired reminders."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).isoformat()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, text, due_at FROM reminders WHERE status = 'active' AND due_at <= ?",
        (now,),
    ).fetchall()

    fired = []
    for r in rows:
        conn.execute(
            "UPDATE reminders SET status = 'fired', fired_at = ? WHERE id = ?",
            (now, r["id"]),
        )
        fired.append(dict(r))

    conn.commit()
    conn.close()
    return fired
