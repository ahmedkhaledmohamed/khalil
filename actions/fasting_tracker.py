"""Intermittent fasting tracker — start/stop fasts, track windows, history.

SQLite-backed, no API key required. Supports common fasting protocols
(16:8, 18:6, 20:4, OMAD) with timer and history.
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.fasting_tracker")

SKILL = {
    "name": "fasting_tracker",
    "description": "Track intermittent fasting windows and streaks",
    "category": "health",
    "patterns": [
        (r"\bstart\s+(?:a\s+)?fast(?:ing)?\b", "fasting_start"),
        (r"\bbegin\s+fast(?:ing)?\b", "fasting_start"),
        (r"\bstop\s+fast(?:ing)?\b", "fasting_stop"),
        (r"\bend\s+(?:my\s+)?fast\b", "fasting_stop"),
        (r"\bbreak\s+(?:my\s+)?fast\b", "fasting_stop"),
        (r"\bfasting\s+status\b", "fasting_status"),
        (r"\bam\s+I\s+fasting\b", "fasting_status"),
        (r"\bhow\s+long\s+(?:have\s+I\s+been\s+)?fasting\b", "fasting_status"),
        (r"\bfasting\s+history\b", "fasting_history"),
        (r"\bfasting\s+streak\b", "fasting_history"),
    ],
    "actions": [
        {"type": "fasting_start", "handler": "handle_intent", "keywords": "fasting start begin intermittent fast", "description": "Start a fast"},
        {"type": "fasting_stop", "handler": "handle_intent", "keywords": "fasting stop end break fast eating", "description": "End a fast"},
        {"type": "fasting_status", "handler": "handle_intent", "keywords": "fasting status how long current timer", "description": "Check fasting status"},
        {"type": "fasting_history", "handler": "handle_intent", "keywords": "fasting history streak record past", "description": "View fasting history"},
    ],
    "examples": [
        "Start a fast",
        "Am I fasting?",
        "Break my fast",
        "Show fasting history",
    ],
}

# Common protocols: name -> (fasting_hours, eating_hours)
PROTOCOLS = {
    "16:8": (16, 8),
    "18:6": (18, 6),
    "20:4": (20, 4),
    "omad": (23, 1),
}


def _ensure_table():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fasting_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            target_hours REAL DEFAULT 16,
            actual_hours REAL,
            protocol TEXT DEFAULT '16:8'
        )
    """)
    conn.commit()
    conn.close()


def start_fast(target_hours: float = 16, protocol: str = "16:8") -> bool:
    _ensure_table()
    conn = sqlite3.connect(str(DB_PATH))
    active = conn.execute("SELECT id FROM fasting_log WHERE ended_at IS NULL").fetchone()
    if active:
        conn.close()
        return False
    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO fasting_log (started_at, target_hours, protocol) VALUES (?, ?, ?)",
        (now, target_hours, protocol),
    )
    conn.commit()
    conn.close()
    return True


def stop_fast() -> dict | None:
    _ensure_table()
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT id, started_at, target_hours, protocol FROM fasting_log WHERE ended_at IS NULL").fetchone()
    if not row:
        conn.close()
        return None
    now = datetime.now(ZoneInfo(TIMEZONE))
    started = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(TIMEZONE))
    actual_hours = (now - started).total_seconds() / 3600
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE fasting_log SET ended_at = ?, actual_hours = ? WHERE id = ?",
        (now_str, round(actual_hours, 1), row[0]),
    )
    conn.commit()
    conn.close()
    return {"started_at": row[1], "actual_hours": round(actual_hours, 1), "target_hours": row[2], "protocol": row[3]}


def get_status() -> dict | None:
    _ensure_table()
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT started_at, target_hours, protocol FROM fasting_log WHERE ended_at IS NULL").fetchone()
    conn.close()
    if not row:
        return None
    started = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(TIMEZONE))
    elapsed = (datetime.now(ZoneInfo(TIMEZONE)) - started).total_seconds() / 3600
    return {
        "started_at": row[0], "elapsed_hours": round(elapsed, 1),
        "target_hours": row[1], "protocol": row[2],
        "remaining_hours": max(0, round(row[1] - elapsed, 1)),
    }


def get_history(limit: int = 10) -> list[dict]:
    _ensure_table()
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT started_at, ended_at, target_hours, actual_hours, protocol FROM fasting_log WHERE ended_at IS NOT NULL ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [{"started_at": r[0], "ended_at": r[1], "target_hours": r[2], "actual_hours": r[3], "protocol": r[4]} for r in rows]


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    import re
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "fasting_start":
        # Detect protocol
        target = 16.0
        protocol = "16:8"
        for name, (fh, _) in PROTOCOLS.items():
            if name in query.lower():
                target = float(fh)
                protocol = name
                break
        hours_match = re.search(r"(\d+)\s*(?:hour|hr|h)\b", query, re.IGNORECASE)
        if hours_match:
            target = float(hours_match.group(1))
            protocol = f"{int(target)}:{24 - int(target)}"

        ok = start_fast(target, protocol)
        if ok:
            await ctx.reply(f"⏱ Fast started ({protocol}). Target: {target:.0f} hours.\nSay \"break my fast\" when done.")
        else:
            status = get_status()
            await ctx.reply(f"Already fasting! {status['elapsed_hours']:.1f}h elapsed of {status['target_hours']:.0f}h target.")
        return True

    elif action == "fasting_stop":
        result = stop_fast()
        if not result:
            await ctx.reply("You're not currently fasting.")
        else:
            hit_target = result["actual_hours"] >= result["target_hours"]
            emoji = "🎉" if hit_target else "👍"
            await ctx.reply(
                f"{emoji} Fast ended!\n"
                f"  Duration: **{result['actual_hours']:.1f}h** / {result['target_hours']:.0f}h target\n"
                f"  {'✅ Goal reached!' if hit_target else '⚠️ Below target, but still good!'}"
            )
        return True

    elif action == "fasting_status":
        status = get_status()
        if not status:
            await ctx.reply("You're not currently fasting. Say \"start a fast\" to begin.")
        else:
            pct = min(100, int(status["elapsed_hours"] / status["target_hours"] * 100))
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            await ctx.reply(
                f"⏱ **Fasting** ({status['protocol']})\n"
                f"  Elapsed: {status['elapsed_hours']:.1f}h / {status['target_hours']:.0f}h\n"
                f"  [{bar}] {pct}%\n"
                f"  Remaining: {status['remaining_hours']:.1f}h"
            )
        return True

    elif action == "fasting_history":
        history = get_history()
        if not history:
            await ctx.reply("No fasting history yet.")
        else:
            completed = sum(1 for h in history if h["actual_hours"] >= h["target_hours"])
            lines = [f"⏱ **Fasting History** ({completed}/{len(history)} targets hit):\n"]
            for h in history:
                hit = "✅" if h["actual_hours"] >= h["target_hours"] else "❌"
                day = h["started_at"][:10]
                lines.append(f"  {hit} {day} — {h['actual_hours']:.1f}h / {h['target_hours']:.0f}h ({h['protocol']})")
            await ctx.reply("\n".join(lines))
        return True

    return False
