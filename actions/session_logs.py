"""Session logs — search and query Khalil's conversation history.

Queries the existing conversations table in khalil.db to provide
user-facing history search, recall, and usage analytics.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

log = logging.getLogger("khalil.actions.session_logs")

SKILL = {
    "name": "session_logs",
    "description": "Search conversation history, recall past interactions, and view usage stats",
    "category": "productivity",
    "patterns": [
        (r"\bwhat\s+did\s+I\s+(?:ask|say|tell)\b", "session_recall"),
        (r"\bsearch\s+(?:my\s+)?(?:history|conversations?|chat)\b", "session_search"),
        (r"\bfind\s+(?:that|the)\s+(?:thing|message|conversation)\b", "session_search"),
        (r"\bmy\s+(?:most\s+)?used\s+skills\b", "session_stats"),
        (r"\bhow\s+(?:many|often)\s+(?:times?\s+)?(?:did\s+I|have\s+I)\s+(?:use|ask)\b", "session_stats"),
        (r"\busage\s+stats\b", "session_stats"),
        (r"\bconversation\s+(?:history|log)\b", "session_search"),
    ],
    "actions": [
        {"type": "session_search", "handler": "handle_intent", "keywords": "search history conversation chat find message", "description": "Search past conversations"},
        {"type": "session_stats", "handler": "handle_intent", "keywords": "usage stats skills frequency how often most used", "description": "View usage statistics"},
        {"type": "session_recall", "handler": "handle_intent", "keywords": "what did I ask say tell recall remember", "description": "Recall past interactions"},
    ],
    "examples": [
        "What did I ask you yesterday?",
        "Search my history for expense",
        "My most used skills",
    ],
    "voice": {"response_style": "brief"},
}


def _get_db():
    """Get the main khalil database connection."""
    from knowledge.indexer import init_db
    return init_db()


def _parse_time_reference(text: str) -> tuple[datetime | None, datetime | None]:
    """Parse natural time references like 'yesterday', 'last week', 'today'."""
    now = datetime.now()
    text_lower = text.lower()

    if "yesterday" in text_lower:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=1)
        return start, end
    if "today" in text_lower:
        start = now.replace(hour=0, minute=0, second=0)
        return start, now
    if "last week" in text_lower:
        start = now - timedelta(days=7)
        return start, now
    if "this week" in text_lower:
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0)
        return start, now
    if "this month" in text_lower:
        start = now.replace(day=1, hour=0, minute=0, second=0)
        return start, now

    # Match "N days ago"
    m = re.search(r"(\d+)\s+days?\s+ago", text_lower)
    if m:
        days = int(m.group(1))
        start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=1)
        return start, end

    return None, None


def search_history(keyword: str, limit: int = 20, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    """Search conversation history by keyword with optional time bounds."""
    conn = _get_db()
    if start and end:
        rows = conn.execute(
            "SELECT role, content, timestamp FROM conversations "
            "WHERE content LIKE ? AND timestamp BETWEEN ? AND ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (f"%{keyword}%", start.isoformat(), end.isoformat(), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT role, content, timestamp FROM conversations "
            "WHERE content LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (f"%{keyword}%", limit),
        ).fetchall()
    return [{"role": r[0], "content": r[1][:200], "timestamp": r[2]} for r in rows]


def get_recent_messages(limit: int = 20, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    """Get recent messages with optional time bounds."""
    conn = _get_db()
    if start and end:
        rows = conn.execute(
            "SELECT role, content, timestamp FROM conversations "
            "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp DESC LIMIT ?",
            (start.isoformat(), end.isoformat(), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT role, content, timestamp FROM conversations "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"role": r[0], "content": r[1][:200], "timestamp": r[2]} for r in rows]


def get_usage_stats(days: int = 7) -> dict:
    """Get usage statistics for the past N days."""
    conn = _get_db()
    since = (datetime.now() - timedelta(days=days)).isoformat()

    total = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE role = 'user' AND timestamp > ?",
        (since,),
    ).fetchone()[0]

    by_day = conn.execute(
        "SELECT DATE(timestamp) as day, COUNT(*) as cnt "
        "FROM conversations WHERE role = 'user' AND timestamp > ? "
        "GROUP BY day ORDER BY day",
        (since,),
    ).fetchall()

    return {
        "total_messages": total,
        "days": days,
        "avg_per_day": round(total / max(days, 1), 1),
        "by_day": [(r[0], r[1]) for r in by_day],
    }


def _format_messages(messages: list[dict], title: str) -> str:
    """Format a list of messages for display."""
    if not messages:
        return f"{title}\n\nNo messages found."
    lines = [title, ""]
    for msg in messages[:15]:
        role = "You" if msg["role"] == "user" else "Khalil"
        ts = msg["timestamp"][:16] if msg["timestamp"] else ""
        content = msg["content"]
        if len(content) > 150:
            content = content[:150] + "..."
        lines.append(f"[{ts}] {role}: {content}")
    if len(messages) > 15:
        lines.append(f"\n...and {len(messages) - 15} more")
    return "\n".join(lines)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle session log intents."""
    query = intent.get("query", "") or intent.get("user_query", "")

    try:
        return await _dispatch_intent(action, query, ctx)
    except Exception as e:
        from resilience import format_user_error
        await ctx.reply(format_user_error(e, skill_name="Session Logs"))
        return True


async def _dispatch_intent(action: str, query: str, ctx) -> bool:
    """Inner dispatch — separated for clean error handling."""
    if action == "session_search":
        # Extract search keyword — strip command words
        keyword = re.sub(
            r"\b(?:search|find|history|conversation|chat|my|the|that|thing|message|for|about|in)\b",
            "", query, flags=re.IGNORECASE,
        ).strip()
        start, end = _parse_time_reference(query)
        if not keyword and not start:
            await ctx.reply("What should I search for? Give me a keyword or time range.")
            return True
        if keyword:
            results = search_history(keyword, limit=20, start=start, end=end)
            title = f"🔍 Search results for \"{keyword}\":"
        else:
            results = get_recent_messages(limit=20, start=start, end=end)
            title = "📜 Recent messages:"
        await ctx.reply(_format_messages(results, title))
        return True

    if action == "session_recall":
        start, end = _parse_time_reference(query)
        # Extract topic keyword
        keyword = re.sub(
            r"\b(?:what|did|I|ask|say|tell|you|about|regarding|yesterday|today|last\s+week|this\s+week|ago|days?)\b",
            "", query, flags=re.IGNORECASE,
        ).strip()
        keyword = re.sub(r"\d+", "", keyword).strip()
        if keyword:
            results = search_history(keyword, limit=10, start=start, end=end)
            title = f"📜 What you asked about \"{keyword}\":"
        else:
            results = get_recent_messages(limit=10, start=start, end=end)
            title = "📜 Recent conversations:"
        await ctx.reply(_format_messages(results, title))
        return True

    if action == "session_stats":
        # Parse time range
        days = 7
        m = re.search(r"(\d+)\s+days?", query)
        if m:
            days = int(m.group(1))
        elif "month" in query.lower():
            days = 30
        elif "year" in query.lower():
            days = 365

        stats = get_usage_stats(days)
        lines = [
            f"📊 Usage Stats (last {stats['days']} days)",
            f"  Total messages: {stats['total_messages']}",
            f"  Avg per day: {stats['avg_per_day']}",
        ]
        if stats["by_day"]:
            lines.append("\n  Daily breakdown:")
            for day, count in stats["by_day"][-7:]:
                bar = "█" * min(count, 30)
                lines.append(f"    {day}: {count:3d} {bar}")
        await ctx.reply("\n".join(lines))
        return True

    return False
