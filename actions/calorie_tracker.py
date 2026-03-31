"""Calorie and nutrition tracker — log meals, track daily intake, set goals.

SQLite-backed, no API key required. Supports logging food items with
calories and protein, daily summaries, and configurable goals.
"""

import logging
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.calorie_tracker")

SKILL = {
    "name": "calorie_tracker",
    "description": "Track daily calories, protein, and meals",
    "category": "health",
    "patterns": [
        (r"\blog\s+(?:a\s+)?(?:meal|food|snack|breakfast|lunch|dinner)\b", "calorie_log"),
        (r"\bate\s+", "calorie_log"),
        (r"\bhad\s+(?:a\s+)?(?:\d+|some)\s+", "calorie_log"),
        (r"\bcalories?\s+(?:today|so\s+far|total)\b", "calorie_summary"),
        (r"\bhow\s+many\s+calories\b", "calorie_summary"),
        (r"\bnutrition\s+(?:today|summary|total)\b", "calorie_summary"),
        (r"\bdaily\s+(?:intake|nutrition|calories)\b", "calorie_summary"),
        (r"\bcalorie\s+goal\b", "calorie_goal"),
        (r"\bset\s+(?:my\s+)?(?:calorie|protein)\s+goal\b", "calorie_goal"),
        (r"\bmeal\s+history\b", "calorie_history"),
        (r"\bwhat\s+did\s+I\s+eat\b", "calorie_history"),
    ],
    "actions": [
        {"type": "calorie_log", "handler": "handle_intent", "keywords": "calorie log meal food eat ate snack breakfast lunch dinner", "description": "Log a meal or food item"},
        {"type": "calorie_summary", "handler": "handle_intent", "keywords": "calorie summary today total intake nutrition daily", "description": "Daily calorie summary"},
        {"type": "calorie_goal", "handler": "handle_intent", "keywords": "calorie protein goal set daily target", "description": "Set calorie/protein goals"},
        {"type": "calorie_history", "handler": "handle_intent", "keywords": "calorie meal history what eat ate food log", "description": "View meal history"},
    ],
    "examples": [
        "I ate a chicken sandwich, about 450 calories",
        "How many calories today?",
        "Set my calorie goal to 2000",
        "What did I eat today?",
    ],
}


def _ensure_tables():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calorie_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            calories INTEGER DEFAULT 0,
            protein_g INTEGER DEFAULT 0,
            meal_type TEXT DEFAULT 'snack',
            logged_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calorie_goals (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def log_meal(description: str, calories: int, protein_g: int = 0, meal_type: str = "snack") -> int:
    _ensure_tables()
    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "INSERT INTO calorie_log (description, calories, protein_g, meal_type, logged_at) VALUES (?, ?, ?, ?, ?)",
        (description, calories, protein_g, meal_type, now),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_daily_summary(target_date: date | None = None) -> dict:
    _ensure_tables()
    if target_date is None:
        target_date = date.today()
    date_str = target_date.strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute(
        "SELECT COALESCE(SUM(calories),0), COALESCE(SUM(protein_g),0), COUNT(*) FROM calorie_log WHERE logged_at LIKE ?",
        (f"{date_str}%",),
    ).fetchone()
    goals = dict(conn.execute("SELECT key, value FROM calorie_goals").fetchall())
    conn.close()
    return {
        "calories": row[0], "protein_g": row[1], "meals": row[2],
        "calorie_goal": goals.get("calories", 2000),
        "protein_goal": goals.get("protein_g", 150),
    }


def get_meals(target_date: date | None = None) -> list[dict]:
    _ensure_tables()
    if target_date is None:
        target_date = date.today()
    date_str = target_date.strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT description, calories, protein_g, meal_type, logged_at FROM calorie_log WHERE logged_at LIKE ? ORDER BY logged_at",
        (f"{date_str}%",),
    ).fetchall()
    conn.close()
    return [{"description": r[0], "calories": r[1], "protein_g": r[2], "meal_type": r[3], "logged_at": r[4]} for r in rows]


def set_goal(key: str, value: int):
    _ensure_tables()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("INSERT OR REPLACE INTO calorie_goals (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    import re
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "calorie_log":
        # Extract calories from query
        cal_match = re.search(r"(\d+)\s*(?:cal(?:ories?)?|kcal)", query, re.IGNORECASE)
        prot_match = re.search(r"(\d+)\s*(?:g\s*(?:of\s*)?protein|protein\s*\d*g?)", query, re.IGNORECASE)
        calories = int(cal_match.group(1)) if cal_match else 0
        protein = int(prot_match.group(1)) if prot_match else 0

        # Extract meal type
        meal_type = "snack"
        for mt in ("breakfast", "lunch", "dinner", "snack"):
            if mt in query.lower():
                meal_type = mt
                break

        # Clean description
        desc = re.sub(r"\d+\s*(?:cal(?:ories?)?|kcal|g\s*protein)", "", query, flags=re.IGNORECASE)
        desc = re.sub(r"\b(?:i\s+)?(?:ate|had|log|logged)\b", "", desc, flags=re.IGNORECASE)
        desc = re.sub(r"\b(?:about|around|roughly|for\s+(?:breakfast|lunch|dinner))\b", "", desc, flags=re.IGNORECASE)
        desc = desc.strip().strip(",. ")
        if not desc:
            desc = "Food item"

        if calories == 0:
            await ctx.reply(f"How many calories in \"{desc}\"? Try: \"ate {desc}, 400 cal\"")
            return True

        log_meal(desc, calories, protein, meal_type)
        summary = get_daily_summary()
        pct = int(summary["calories"] / summary["calorie_goal"] * 100)
        await ctx.reply(
            f"✅ Logged: **{desc}** — {calories} cal" + (f", {protein}g protein" if protein else "") +
            f"\n📊 Today: {summary['calories']:,}/{summary['calorie_goal']:,} cal ({pct}%)"
        )
        return True

    elif action == "calorie_summary":
        summary = get_daily_summary()
        cal_pct = int(summary["calories"] / summary["calorie_goal"] * 100)
        prot_pct = int(summary["protein_g"] / summary["protein_goal"] * 100) if summary["protein_goal"] else 0
        bar = "█" * (cal_pct // 10) + "░" * (10 - cal_pct // 10)
        await ctx.reply(
            f"📊 **Today's Nutrition** ({summary['meals']} meals)\n\n"
            f"  Calories: {summary['calories']:,} / {summary['calorie_goal']:,} ({cal_pct}%)\n"
            f"  [{bar}]\n"
            f"  Protein: {summary['protein_g']}g / {summary['protein_goal']}g ({prot_pct}%)"
        )
        return True

    elif action == "calorie_goal":
        cal_match = re.search(r"(\d{3,5})\s*(?:cal(?:ories?)?|kcal)?", query)
        prot_match = re.search(r"(\d{2,4})\s*g?\s*protein", query, re.IGNORECASE)
        if cal_match:
            val = int(cal_match.group(1))
            set_goal("calories", val)
            await ctx.reply(f"✅ Calorie goal set to **{val:,}** cal/day")
        elif prot_match:
            val = int(prot_match.group(1))
            set_goal("protein_g", val)
            await ctx.reply(f"✅ Protein goal set to **{val}g**/day")
        else:
            summary = get_daily_summary()
            await ctx.reply(
                f"Current goals:\n  • Calories: {summary['calorie_goal']:,}/day\n"
                f"  • Protein: {summary['protein_goal']}g/day\n\n"
                "To change: \"set calorie goal to 2200\" or \"set protein goal to 180g\""
            )
        return True

    elif action == "calorie_history":
        meals = get_meals()
        if not meals:
            await ctx.reply("No meals logged today.")
        else:
            lines = [f"🍽 **Today's Meals** ({len(meals)}):\n"]
            for m in meals:
                time = m["logged_at"].split(" ")[1][:5]
                lines.append(f"  • {time} — **{m['description']}** ({m['calories']} cal)")
            await ctx.reply("\n".join(lines))
        return True

    return False
