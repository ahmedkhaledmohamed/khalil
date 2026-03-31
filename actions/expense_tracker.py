"""Expense tracker — log expenses, set budgets, view spending reports.

SQLite-backed, no API key required. Supports categories, monthly budgets,
and spending breakdowns.
"""

import logging
import re
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.expense_tracker")

SKILL = {
    "name": "expense_tracker",
    "description": "Track expenses, set budgets, view spending reports",
    "category": "finance",
    "patterns": [
        (r"\bspent\s+\$?\d+", "expense_log"),
        (r"\bbought\s+", "expense_log"),
        (r"\bpaid\s+\$?\d+", "expense_log"),
        (r"\blog\s+(?:an?\s+)?expense\b", "expense_log"),
        (r"\bexpense\s+(?:for|of)\b", "expense_log"),
        (r"\bspending\s+(?:today|this\s+(?:week|month)|summary|report)\b", "expense_summary"),
        (r"\bhow\s+much\s+(?:have\s+I\s+)?spent\b", "expense_summary"),
        (r"\bexpense\s+(?:summary|report|breakdown)\b", "expense_summary"),
        (r"\bmonthly\s+(?:spending|expenses?|budget)\b", "expense_summary"),
        (r"\bset\s+(?:a\s+)?budget\b", "expense_budget"),
        (r"\bbudget\s+(?:for|of)\b", "expense_budget"),
    ],
    "actions": [
        {"type": "expense_log", "handler": "handle_intent", "keywords": "expense spent bought paid log cost money", "description": "Log an expense"},
        {"type": "expense_summary", "handler": "handle_intent", "keywords": "expense spending summary report monthly breakdown how much", "description": "View spending summary"},
        {"type": "expense_budget", "handler": "handle_intent", "keywords": "expense budget set monthly limit category", "description": "Set a budget"},
    ],
    "examples": [
        "Spent $45 on groceries",
        "How much have I spent this month?",
        "Set budget for dining to $500",
        "Expense breakdown this month",
    ],
}

CATEGORIES = [
    "groceries", "dining", "transport", "entertainment", "shopping",
    "health", "utilities", "subscriptions", "education", "travel", "other",
]


def _ensure_tables():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expense_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT DEFAULT 'other',
            logged_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expense_budgets (
            category TEXT PRIMARY KEY,
            monthly_limit REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def log_expense(description: str, amount: float, category: str = "other") -> int:
    _ensure_tables()
    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "INSERT INTO expense_log (description, amount, category, logged_at) VALUES (?, ?, ?, ?)",
        (description, amount, category.lower(), now),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_monthly_summary(year: int | None = None, month: int | None = None) -> dict:
    _ensure_tables()
    today = date.today()
    if year is None:
        year = today.year
    if month is None:
        month = today.month
    prefix = f"{year}-{month:02d}"
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT category, SUM(amount), COUNT(*) FROM expense_log WHERE logged_at LIKE ? GROUP BY category ORDER BY SUM(amount) DESC",
        (f"{prefix}%",),
    ).fetchall()
    total = conn.execute("SELECT COALESCE(SUM(amount),0) FROM expense_log WHERE logged_at LIKE ?", (f"{prefix}%",)).fetchone()[0]
    budgets = dict(conn.execute("SELECT category, monthly_limit FROM expense_budgets").fetchall())
    conn.close()
    return {
        "year": year, "month": month, "total": total,
        "by_category": [{"category": r[0], "amount": r[1], "count": r[2], "budget": budgets.get(r[0])} for r in rows],
        "budgets": budgets,
    }


def set_budget(category: str, monthly_limit: float):
    _ensure_tables()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("INSERT OR REPLACE INTO expense_budgets (category, monthly_limit) VALUES (?, ?)", (category.lower(), monthly_limit))
    conn.commit()
    conn.close()


def _guess_category(text: str) -> str:
    text_lower = text.lower()
    hints = {
        "groceries": ["grocery", "groceries", "supermarket", "food", "produce"],
        "dining": ["restaurant", "dining", "lunch", "dinner", "coffee", "cafe", "pizza", "sushi", "takeout"],
        "transport": ["uber", "lyft", "gas", "fuel", "parking", "transit", "subway", "taxi"],
        "entertainment": ["movie", "netflix", "spotify", "game", "concert", "ticket"],
        "shopping": ["amazon", "clothes", "shoes", "electronics", "furniture"],
        "health": ["pharmacy", "doctor", "gym", "medicine", "dental"],
        "utilities": ["electric", "water", "internet", "phone", "bill"],
        "subscriptions": ["subscription", "membership", "premium", "plan"],
        "education": ["book", "course", "class", "tuition", "tutorial"],
        "travel": ["hotel", "flight", "airbnb", "vacation", "trip"],
    }
    for cat, keywords in hints.items():
        if any(kw in text_lower for kw in keywords):
            return cat
    return "other"


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "expense_log":
        amount_match = re.search(r"\$?(\d+(?:\.\d{1,2})?)", query)
        if not amount_match:
            await ctx.reply("How much was it? Try: \"spent $45 on groceries\"")
            return True
        amount = float(amount_match.group(1))

        # Extract description
        desc = re.sub(r"\$?\d+(?:\.\d{1,2})?", "", query)
        desc = re.sub(r"\b(?:i\s+)?(?:spent|paid|bought|log(?:ged)?)\b", "", desc, flags=re.IGNORECASE)
        desc = re.sub(r"\b(?:on|for|an?\s+expense)\b", "", desc, flags=re.IGNORECASE)
        desc = desc.strip().strip(",. ") or "Expense"

        category = _guess_category(desc + " " + query)
        log_expense(desc, amount, category)

        summary = get_monthly_summary()
        await ctx.reply(
            f"✅ Logged: **{desc}** — ${amount:.2f} ({category})\n"
            f"📊 This month: ${summary['total']:.2f} total"
        )
        return True

    elif action == "expense_summary":
        summary = get_monthly_summary()
        if not summary["by_category"]:
            await ctx.reply("No expenses logged this month.")
            return True

        month_name = date(summary["year"], summary["month"], 1).strftime("%B %Y")
        lines = [f"💰 **Spending — {month_name}**: ${summary['total']:.2f}\n"]
        for cat in summary["by_category"]:
            budget_str = ""
            if cat["budget"]:
                pct = int(cat["amount"] / cat["budget"] * 100)
                status = "🔴" if pct > 100 else "🟡" if pct > 80 else "🟢"
                budget_str = f" {status} ({pct}% of ${cat['budget']:.0f})"
            lines.append(f"  • **{cat['category']}**: ${cat['amount']:.2f} ({cat['count']} items){budget_str}")
        await ctx.reply("\n".join(lines))
        return True

    elif action == "expense_budget":
        amount_match = re.search(r"\$?(\d+(?:\.\d{1,2})?)", query)
        cat_match = None
        for cat in CATEGORIES:
            if cat in query.lower():
                cat_match = cat
                break

        if amount_match and cat_match:
            val = float(amount_match.group(1))
            set_budget(cat_match, val)
            await ctx.reply(f"✅ Budget set: **{cat_match}** — ${val:.0f}/month")
        elif not cat_match:
            await ctx.reply(f"Which category? Options: {', '.join(CATEGORIES)}")
        else:
            await ctx.reply("What's the budget amount? Try: \"set budget for dining to $500\"")
        return True

    return False
