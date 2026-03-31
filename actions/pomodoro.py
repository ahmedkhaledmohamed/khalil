"""Pomodoro focus timer — timed work sessions with break reminders.

SQLite-backed session history. Integrates with macOS Focus (DND) if available.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.pomodoro")

SKILL = {
    "name": "pomodoro",
    "description": "Pomodoro focus timer with work/break cycles",
    "category": "productivity",
    "patterns": [
        # Specific patterns first — order matters
        (r"\bpomodoro\s+(?:status|timer)\b", "pomodoro_status"),
        (r"\bam\s+I\s+(?:in\s+)?(?:a\s+)?(?:focus|pomodoro)\b", "pomodoro_status"),
        (r"\bpomodoro\s+(?:history|stats|today)\b", "pomodoro_history"),
        (r"\bhow\s+many\s+pomodoros?\b", "pomodoro_history"),
        (r"\b(?:stop|cancel)\s+(?:the\s+)?(?:pomodoro|focus\s+(?:session|timer))\b", "pomodoro_stop"),
        # Generic start patterns last (catch-all)
        (r"\bstart\s+(?:a\s+)?(?:pomodoro|focus\s+(?:session|timer))\b", "pomodoro_start"),
        (r"\bfocus\s+for\s+\d+\s*min", "pomodoro_start"),
        (r"\bpomodoro\b", "pomodoro_start"),
    ],
    "actions": [
        {"type": "pomodoro_start", "handler": "handle_intent", "keywords": "pomodoro focus timer start session work concentrate", "description": "Start a focus session"},
        {"type": "pomodoro_stop", "handler": "handle_intent", "keywords": "pomodoro focus stop cancel end timer", "description": "Stop focus session"},
        {"type": "pomodoro_status", "handler": "handle_intent", "keywords": "pomodoro focus status timer current remaining", "description": "Check focus timer status"},
        {"type": "pomodoro_history", "handler": "handle_intent", "keywords": "pomodoro focus history stats today sessions count", "description": "View focus session history"},
    ],
    "examples": [
        "Start a pomodoro",
        "Focus for 45 minutes",
        "How many pomodoros today?",
        "Stop the timer",
    ],
}

# Active timer state (in-memory, one per process)
_active_timer: dict | None = None
_timer_task: asyncio.Task | None = None


def _ensure_table():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pomodoro_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            duration_min INTEGER NOT NULL,
            completed INTEGER DEFAULT 0,
            label TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()


def _record_session(started_at: str, duration_min: int, completed: bool, label: str = ""):
    _ensure_table()
    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO pomodoro_log (started_at, ended_at, duration_min, completed, label) VALUES (?, ?, ?, ?, ?)",
        (started_at, now, duration_min, 1 if completed else 0, label),
    )
    conn.commit()
    conn.close()


def get_today_stats() -> dict:
    _ensure_table()
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT COUNT(*), SUM(duration_min), SUM(completed) FROM pomodoro_log WHERE started_at LIKE ?",
        (f"{today}%",),
    ).fetchone()
    conn.close()
    return {"sessions": rows[0] or 0, "total_min": rows[1] or 0, "completed": rows[2] or 0}


async def _timer_callback(duration_min: int, ctx, label: str):
    """Background task that waits for the timer to expire."""
    global _active_timer
    try:
        await asyncio.sleep(duration_min * 60)
        # Timer completed
        if _active_timer:
            _record_session(_active_timer["started_at"], duration_min, completed=True, label=label)
            _active_timer = None
            # Try to disable DND
            try:
                from actions.macos_focus import toggle_dnd
                await toggle_dnd(False)
            except Exception:
                pass
            stats = get_today_stats()
            await ctx.reply(
                f"🍅 **Pomodoro complete!** ({duration_min} min)\n"
                f"  Take a break. Today: {stats['completed']} sessions, {stats['total_min']} min total."
            )
    except asyncio.CancelledError:
        pass


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    import re
    global _active_timer, _timer_task
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "pomodoro_start":
        if _active_timer:
            try:
                elapsed = (datetime.now(ZoneInfo(TIMEZONE)) - datetime.strptime(
                    _active_timer["started_at"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=ZoneInfo(TIMEZONE))).total_seconds() / 60
                remaining = _active_timer["duration_min"] - elapsed
                await ctx.reply(f"Already in a focus session! {remaining:.0f} min remaining.")
            except (ValueError, KeyError):
                await ctx.reply("A focus session is already running.")
            return True

        # Extract duration
        duration = 25  # default pomodoro
        m = re.search(r"(\d+)\s*(?:min(?:utes?)?|m)\b", query, re.IGNORECASE)
        if m:
            duration = int(m.group(1))

        # Extract label
        label = re.sub(r"\b(?:start|begin|a|the|pomodoro|focus|session|timer|for|minutes?|min|m|\d+)\b", "", query, flags=re.IGNORECASE).strip()

        now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
        _active_timer = {"started_at": now, "duration_min": duration, "label": label}
        _timer_task = asyncio.create_task(_timer_callback(duration, ctx, label))

        # Try to enable DND
        dnd_status = ""
        try:
            from actions.macos_focus import toggle_dnd
            ok = await toggle_dnd(True)
            if ok:
                dnd_status = " 🔕 DND enabled."
        except Exception:
            pass

        await ctx.reply(
            f"🍅 **Focus session started** — {duration} min" +
            (f" ({label})" if label else "") + f"\n  I'll notify you when time's up.{dnd_status}"
        )
        return True

    elif action == "pomodoro_stop":
        if not _active_timer:
            await ctx.reply("No active focus session.")
            return True

        elapsed = (datetime.now(ZoneInfo(TIMEZONE)) - datetime.strptime(
            _active_timer["started_at"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=ZoneInfo(TIMEZONE))).total_seconds() / 60
        _record_session(_active_timer["started_at"], int(elapsed), completed=False, label=_active_timer.get("label", ""))
        _active_timer = None
        if _timer_task:
            _timer_task.cancel()
            _timer_task = None

        # Disable DND
        try:
            from actions.macos_focus import toggle_dnd
            await toggle_dnd(False)
        except Exception:
            pass

        await ctx.reply(f"⏹ Focus session stopped after {elapsed:.0f} min.")
        return True

    elif action == "pomodoro_status":
        if not _active_timer:
            stats = get_today_stats()
            await ctx.reply(
                f"No active focus session.\n"
                f"Today: {stats['completed']} completed, {stats['total_min']} min total."
            )
            return True

        elapsed = (datetime.now(ZoneInfo(TIMEZONE)) - datetime.strptime(
            _active_timer["started_at"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=ZoneInfo(TIMEZONE))).total_seconds() / 60
        remaining = max(0, _active_timer["duration_min"] - elapsed)
        pct = min(100, int(elapsed / _active_timer["duration_min"] * 100))
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        await ctx.reply(
            f"🍅 **Focus Session**\n"
            f"  Elapsed: {elapsed:.0f} min / {_active_timer['duration_min']} min\n"
            f"  [{bar}] {pct}%\n"
            f"  Remaining: {remaining:.0f} min"
        )
        return True

    elif action == "pomodoro_history":
        stats = get_today_stats()
        await ctx.reply(
            f"🍅 **Today's Focus**\n"
            f"  Sessions: {stats['sessions']} ({stats['completed']} completed)\n"
            f"  Total focus time: {stats['total_min']} min ({stats['total_min'] / 60:.1f}h)"
        )
        return True

    return False
