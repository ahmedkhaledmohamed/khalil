"""Surface unanswered queries and knowledge gaps for review.

Queries the interaction_signals table for recent search_miss and
capability_gap_detected signals, clusters them by topic, and presents
an actionable summary.  No external API — reads only from the local
SQLite database.
"""

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE

log = logging.getLogger("khalil.actions.49")

SKILL = {
    "name": "gaps",
    "description": "Failed to answer 7 queries today \u2014 Consider indexing more content related to these topics, or running /sync",
    "category": "extension",
    "patterns": [
        (r"\b(?:unanswered|failed|missed)\s+(?:queries|questions)\b", "gaps_list"),
        (r"\bknowledge\s+gaps?\b", "gaps_list"),
        (r"\bwhat\s+(?:couldn't|can't|could\s+not)\s+you\s+answer\b", "gaps_list"),
        (r"\bgaps?\s+(?:report|summary|today|this\s+week)\b", "gaps_list"),
    ],
    "actions": [
        {
            "type": "gaps_list",
            "handler": "handle_gaps",
            "description": "Failed to answer 7 queries today \u2014 Consider indexing more content related to these topics, or running /sync",
            "keywords": "gaps unanswered failed missed queries knowledge",
        },
    ],
    "examples": [
        "What are my knowledge gaps?",
        "Show unanswered queries",
        "What couldn't you answer today?",
    ],
    "sensor": {
        "function": "sense_gaps",
        "interval_min": 60,
        "identify_opportunities": "identify_gap_opportunities",
    },
}

# Module-level flag to ensure tables are created only once
_tables_ensured = False

STOPWORDS = frozenset({
    "the", "a", "an", "is", "it", "to", "in", "for", "of", "and",
    "on", "my", "me", "i", "what", "how", "can", "do", "you", "this",
    "that", "with", "about", "from", "have", "has", "are", "was", "be",
})


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(conn: sqlite3.Connection):
    """Create tables if needed. Called once at startup.

    This module reads from existing tables (interaction_signals, documents)
    and does not require its own tables.  This function is a no-op but
    satisfies the extension contract.
    """
    global _tables_ensured
    if _tables_ensured:
        return
    _tables_ensured = True


def _fetch_signals(days: int = 1, max_rows: int = 50) -> list[dict]:
    """Fetch recent search_miss and capability_gap_detected signals."""
    tz = ZoneInfo(TIMEZONE)
    cutoff = (datetime.now(tz) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT signal_type, context, created_at
            FROM interaction_signals
            WHERE signal_type IN ('search_miss', 'capability_gap_detected')
              AND created_at > ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (cutoff, max_rows),
        ).fetchall()
    finally:
        conn.close()

    signals = []
    for row in rows:
        try:
            ctx = json.loads(row["context"]) if row["context"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        query = ctx.get("query", "").strip()
        if not query or len(query) < 5:
            continue
        signals.append({
            "query": query,
            "signal_type": row["signal_type"],
            "timestamp": row["created_at"],
            "gap_name": ctx.get("gap_name", ""),
        })
    return signals


def _cluster_signals(signals: list[dict]) -> list[dict]:
    """Cluster signals by word overlap into topic groups.

    Returns [{topic, queries, count, signal_types}] sorted by count desc.
    """
    clusters: list[dict] = []

    for sig in signals:
        tokens = {
            w.lower()
            for w in re.findall(r"\b\w+\b", sig["query"])
            if w.lower() not in STOPWORDS and len(w) > 2
        }
        matched = False
        for cluster in clusters:
            if len(tokens & cluster["_tokens"]) >= 2:
                cluster["queries"].append(sig["query"])
                cluster["count"] += 1
                cluster["signal_types"].add(sig["signal_type"])
                cluster["_tokens"] |= tokens
                matched = True
                break
        if not matched:
            clusters.append({
                "topic": sig["query"][:80],
                "queries": [sig["query"]],
                "count": 1,
                "signal_types": {sig["signal_type"]},
                "_tokens": tokens,
            })

    # Sort by frequency, drop internal tokens set
    clusters.sort(key=lambda c: c["count"], reverse=True)
    for c in clusters:
        del c["_tokens"]
        c["signal_types"] = sorted(c["signal_types"])
    return clusters


def _deduplicate(signals: list[dict]) -> list[dict]:
    """Deduplicate signals by normalized query text."""
    seen: set[str] = set()
    unique = []
    for sig in signals:
        normalized = sig["query"].lower().strip()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(sig)
    return unique


def _format_report(signals: list[dict], clusters: list[dict], days: int) -> str:
    """Format a human-readable Telegram message."""
    period = "today" if days <= 1 else f"last {days} days"
    total = len(signals)

    if total == 0:
        return f"No unanswered queries {period}. Knowledge base is covering your questions well."

    lines = [f"\U0001f50d **{total} unanswered quer{'y' if total == 1 else 'ies'}** ({period})\n"]

    # Show clustered topics
    if clusters:
        lines.append("**By topic:**")
        for i, c in enumerate(clusters[:10], 1):
            type_tag = "\U0001f4da" if "search_miss" in c["signal_types"] else "\U0001f527"
            sample = c["queries"][0][:60]
            if c["count"] > 1:
                lines.append(f"  {type_tag} ({c['count']}x) {sample}")
            else:
                lines.append(f"  {type_tag} {sample}")
        remaining = len(clusters) - 10
        if remaining > 0:
            lines.append(f"  ...and {remaining} more topics")

    # Actionable suggestions
    lines.append("")
    search_misses = sum(1 for s in signals if s["signal_type"] == "search_miss")
    cap_gaps = sum(1 for s in signals if s["signal_type"] == "capability_gap_detected")

    if search_misses > 0:
        lines.append(f"\U0001f4a1 {search_misses} search miss{'es' if search_misses != 1 else ''} \u2014 consider /sync or indexing more content")
    if cap_gaps > 0:
        lines.append(f"\U0001f527 {cap_gaps} capability gap{'s' if cap_gaps != 1 else ''} \u2014 new features may be needed")

    # Truncate to Telegram limit
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3997] + "..."
    return text


async def handle_gaps(update, context):
    """Handle /gaps command.

    Subcommands:
        /gaps          \u2014 today's unanswered queries
        /gaps week     \u2014 last 7 days
        /gaps topics   \u2014 cluster by topic (7 days)
        /gaps preview  \u2014 dry-run: show what /sync would re-check
    """
    args = context.args or []
    subcmd = args[0].lower() if args else ""

    if subcmd == "week":
        days = 7
    elif subcmd == "topics":
        days = 7
    elif subcmd == "preview":
        await _handle_preview(update)
        return
    else:
        days = 1

    signals = await asyncio.to_thread(_fetch_signals, days)
    unique = _deduplicate(signals)
    clusters = _cluster_signals(unique)

    text = _format_report(unique, clusters, days)
    await update.message.reply_text(text, parse_mode="Markdown")


async def _handle_preview(update):
    """Dry-run: show which topics fill_knowledge_gaps would attempt to fill."""
    try:
        from learning import fill_knowledge_gaps
    except ImportError:
        await update.message.reply_text("fill_knowledge_gaps not available.")
        return

    signals = await asyncio.to_thread(_fetch_signals, days=7, max_rows=50)
    unique = _deduplicate(signals)
    clusters = _cluster_signals(unique)

    if not clusters:
        await update.message.reply_text("No gaps to preview \u2014 knowledge base is healthy.")
        return

    lines = ["\U0001f441 **Preview: topics that would be re-checked**\n"]
    for i, c in enumerate(clusters[:15], 1):
        lines.append(f"  {i}. {c['topic'][:70]} ({c['count']}x)")

    lines.append(f"\nRun /sync to attempt filling these {len(clusters)} topic(s).")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3997] + "..."
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Agent loop sensor
# ---------------------------------------------------------------------------

async def sense_gaps() -> dict:
    """Sensor: count today's unanswered queries."""
    try:
        signals = await asyncio.to_thread(_fetch_signals, days=1)
        unique = _deduplicate(signals)
        clusters = _cluster_signals(unique)
        return {
            "total": len(unique),
            "clusters": len(clusters),
            "top_topics": [c["topic"][:60] for c in clusters[:3]],
        }
    except Exception as e:
        log.debug("Gaps sensor failed: %s", e)
        return {"total": 0, "clusters": 0, "top_topics": []}


def identify_gap_opportunities(state: dict, last_state: dict, cooldowns: dict):
    """Identify actionable opportunities from gap sensor data."""
    import time as _time
    from agent_loop import Opportunity, Urgency, _on_cooldown

    opps = []
    now = _time.monotonic()
    gap_data = state.get("gaps", {})
    total = gap_data.get("total", 0)

    if total < 3:
        return opps

    opp_id = f"knowledge_gaps_{total}"
    if _on_cooldown(opp_id, cooldowns, now, hours=12):
        return opps

    topics_str = ", ".join(gap_data.get("top_topics", [])[:3])
    summary = f"\U0001f50d Failed to answer {total} queries today"
    if topics_str:
        summary += f" \u2014 top topics: {topics_str}"
    summary += "\nConsider indexing more content or running /sync"

    opps.append(Opportunity(
        id=opp_id,
        source="gaps",
        summary=summary,
        urgency=Urgency.LOW,
        action_type=None,
        payload={"total": total, "clusters": gap_data.get("clusters", 0)},
    ))

    return opps


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action != "gaps_list":
        return False

    try:
        signals = await asyncio.to_thread(_fetch_signals, days=1)
        unique = _deduplicate(signals)
        clusters = _cluster_signals(unique)
        text = _format_report(unique, clusters, days=1)
        await ctx.reply(text)
    except Exception as e:
        from resilience import format_user_error
        await ctx.reply(format_user_error(e, skill_name="Knowledge Gaps"))
    return True
