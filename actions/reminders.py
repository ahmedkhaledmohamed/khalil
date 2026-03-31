"""Local reminders stored in SQLite, delivered via Telegram push."""

import logging
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import APPLE_REMINDERS_SYNC, DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.reminders")

SKILL = {
    "name": "reminders",
    "description": "Local reminders stored in SQLite, delivered via Telegram push",
    "category": "productivity",
    "command": "remind",
    "patterns": [
        (r"\bremind\s+me\b", "reminder"),
        (r"\bset\s+(?:a\s+)?reminder\b", "reminder"),
        (r"\bdon'?t\s+(?:let\s+me\s+)?forget\b", "reminder"),
    ],
    "actions": [
        {"type": "reminder", "handler": "handle_intent", "keywords": "remind reminder set forget", "description": "Create a reminder"},
    ],
    "examples": ["Remind me to call Sarah in 2 hours", "Set a reminder for tomorrow 9am"],
    "sensor": {"function": "sense_reminders", "interval_min": 5, "identify_opportunities": "identify_reminder_opportunities"},
    "voice": {"response_style": "brief"},
}


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

    if APPLE_REMINDERS_SYNC:
        try:
            import asyncio
            from actions.apple_reminders import sync_to_apple
            due_str = due_at.strftime("%Y-%m-%d %H:%M") if due_at else None
            asyncio.get_event_loop().create_task(sync_to_apple(text, due_date=due_str))
        except Exception as exc:
            log.warning("Apple Reminders sync failed: %s", exc)

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


# --- #56: iCloud Reminders Sync ---


def get_icloud_reminders() -> list[dict]:
    """Read reminders from Apple Reminders.app via osascript.

    Returns list of {name, due_date, completed} dicts.
    """
    script = (
        'tell application "Reminders"\n'
        '  set output to ""\n'
        '  repeat with r in (reminders of default list whose completed is false)\n'
        '    set output to output & name of r & "|||" & (due date of r as string) & "\\n"\n'
        '  end repeat\n'
        '  return output\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            log.warning("osascript failed: %s", err)
            if "assistive" in err.lower() or "not allowed" in err.lower():
                log.error("Reminders: accessibility permission denied")
            return []

        reminders = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|||", 1)
            name = parts[0].strip()
            due_date = parts[1].strip() if len(parts) > 1 else ""
            reminders.append({"name": name, "due_date": due_date, "completed": False})
        return reminders
    except subprocess.TimeoutExpired:
        log.warning("iCloud reminders fetch timed out (20s)")
        return []
    except FileNotFoundError:
        log.warning("osascript not found — not on macOS?")
        return []


def create_icloud_reminder(text: str, due_date: str | None = None) -> dict:
    """Create a reminder in Apple Reminders.app via osascript.

    Args:
        text: Reminder text.
        due_date: Optional due date string (e.g. "2026-03-20 09:00").

    Returns dict with {name, created, due_date}.
    """
    if due_date:
        script = (
            f'tell application "Reminders"\n'
            f'  set d to date "{due_date}"\n'
            f'  make new reminder in default list with properties '
            f'{{name:"{text}", due date:d}}\n'
            f'end tell'
        )
    else:
        script = (
            f'tell application "Reminders"\n'
            f'  make new reminder in default list with properties '
            f'{{name:"{text}"}}\n'
            f'end tell'
        )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log.warning("osascript create reminder failed: %s", result.stderr.strip())
            return {"name": text, "created": False, "error": result.stderr.strip()}

        log.info("iCloud reminder created: %s", text)
        return {"name": text, "created": True, "due_date": due_date or ""}
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("iCloud reminder creation failed: %s", e)
        return {"name": text, "created": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Agent loop sensor
# ---------------------------------------------------------------------------

async def sense_reminders() -> dict:
    """Sensor: check for overdue and upcoming reminders."""
    try:
        reminders = list_reminders()
        now = datetime.now(ZoneInfo(TIMEZONE))
        overdue = []
        upcoming = []
        for r in reminders:
            due_str = r.get("due_at", "")
            if not due_str:
                continue
            try:
                due_dt = datetime.fromisoformat(due_str).replace(tzinfo=ZoneInfo(TIMEZONE))
                if due_dt < now:
                    overdue.append(r)
                elif due_dt < now + timedelta(hours=1):
                    upcoming.append(r)
            except (ValueError, TypeError):
                pass
        return {"overdue": overdue, "upcoming": upcoming}
    except Exception as e:
        log.debug("Reminder sensor failed: %s", e)
        return {"overdue": [], "upcoming": []}


def identify_reminder_opportunities(state: dict, last_state: dict, cooldowns: dict):
    """Identify actionable opportunities from reminder sensor data."""
    import time as _time
    from agent_loop import Opportunity, Urgency, _on_cooldown

    opps = []
    now = _time.monotonic()

    for r in state.get("reminders", {}).get("overdue", []):
        opp_id = f"reminder_overdue_{r.get('id', r.get('text', '')[:20])}"
        if _on_cooldown(opp_id, cooldowns, now, hours=4):
            continue
        opps.append(Opportunity(
            id=opp_id, source="reminders",
            summary=f"\u23f0 Overdue reminder: {r.get('text', 'unknown')}",
            urgency=Urgency.MEDIUM, action_type=None, payload={"reminder": r},
        ))

    for r in state.get("reminders", {}).get("upcoming", []):
        opp_id = f"reminder_upcoming_{r.get('id', r.get('text', '')[:20])}"
        if _on_cooldown(opp_id, cooldowns, now, hours=2):
            continue
        opps.append(Opportunity(
            id=opp_id, source="reminders",
            summary=f"\U0001f4cb Reminder coming up: {r.get('text', 'unknown')}",
            urgency=Urgency.LOW, action_type=None,
        ))

    return opps


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "reminder":
        time_str = intent.get("time", "")
        text = intent.get("text", "")
        if not text:
            return False

        due_at = _parse_relative_time(time_str) if time_str else None
        if not due_at:
            await ctx.reply(
                f"I understood you want a reminder for: {text}\n"
                f"But I couldn't parse the time \"{time_str}\".\n"
                "Try: /remind in 2 hours {text}"
            )
            return True

        result = create_reminder(text, due_at)
        await ctx.reply(
            f"\u23f0 Reminder set!\n\n"
            f"#{result['id']}: {result['text']}\n"
            f"Due: {result['due_at']}"
        )
        return True
    return False
