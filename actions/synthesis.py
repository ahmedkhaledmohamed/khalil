"""Cross-source synthesis — aggregate data from multiple sources for rich answers.

Provides composite tools that gather from calendar, email, knowledge base, etc.
and return structured bundles for the LLM to reason over via tool-use.
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import TIMEZONE

log = logging.getLogger("khalil.actions.synthesis")

SKILL = {
    "name": "synthesis",
    "description": "Cross-source synthesis — meeting prep, daily focus, weekly review, topic briefs",
    "category": "productivity",
    "patterns": [
        (r"\bprep\s+(?:me\s+)?(?:for|my)\b.*\b(?:meeting|1[:\-]1|one.on.one)\b", "meeting_prep"),
        (r"\b(?:daily\s+focus|what\s+should\s+i\s+focus)\b", "daily_focus"),
        (r"\b(?:weekly\s+review|summarize?\s+(?:my\s+)?week)\b", "weekly_review"),
    ],
    "actions": [
        {"type": "meeting_prep", "handler": "handle_intent",
         "keywords": "prep prepare meeting 1:1 one-on-one agenda",
         "description": "Prepare for a meeting — pulls calendar, emails, and relevant context",
         "parameters": {
             "meeting_title": {"type": "string", "description": "Meeting title or attendee name"},
         }},
        {"type": "daily_focus", "handler": "handle_intent",
         "keywords": "focus today priorities plan day",
         "description": "Daily focus — calendar, reminders, emails, and goals for today"},
        {"type": "weekly_review", "handler": "handle_intent",
         "keywords": "week review summary recap accomplishments",
         "description": "Weekly review — summarize calendar, completed tasks, email activity"},
        {"type": "context_brief", "handler": "handle_intent",
         "keywords": "brief context background research topic",
         "description": "Topic brief — gather knowledge base, emails, and notes on a topic",
         "parameters": {
             "topic": {"type": "string", "description": "Topic to research"},
         }},
    ],
    "examples": ["Prep me for my 1:1 with my manager", "What should I focus on today?", "Weekly review"],
}


async def _gather_calendar(days: int = 1) -> str:
    """Gather calendar events."""
    try:
        from actions.calendar import get_today_events, get_upcoming_events, format_events_text
        if days <= 1:
            events = await get_today_events()
        else:
            events = await get_upcoming_events(days=days)
        if not events:
            return "No calendar events."
        return format_events_text(events)
    except Exception as e:
        return f"Calendar unavailable: {e}"


async def _gather_reminders() -> str:
    """Gather active reminders."""
    try:
        from actions.reminders import list_reminders
        reminders = list_reminders()
        if not reminders:
            return "No active reminders."
        lines = [f"- {r['text']} (due {r['due_at'][:16]})" for r in reminders[:10]]
        return "\n".join(lines)
    except Exception as e:
        return f"Reminders unavailable: {e}"


async def _gather_recent_emails(query: str = "", limit: int = 5) -> str:
    """Gather recent emails, optionally filtered by query."""
    try:
        from knowledge.search import hybrid_search
        search_query = query or "recent email"
        results = await hybrid_search(search_query, limit=limit, category="email")
        if not results:
            return "No relevant emails found."
        lines = []
        for r in results:
            lines.append(f"- [{r.get('category', '')}] {r['title']}: {r['content'][:150]}...")
        return "\n".join(lines)
    except Exception as e:
        return f"Email search unavailable: {e}"


async def _gather_knowledge(query: str, limit: int = 5) -> str:
    """Gather relevant knowledge base entries."""
    try:
        from knowledge.search import hybrid_search
        results = await hybrid_search(query, limit=limit)
        if not results:
            return "No relevant knowledge base entries."
        lines = []
        for r in results:
            lines.append(f"- [{r.get('category', '')}] {r['title']}: {r['content'][:200]}...")
        return "\n".join(lines)
    except Exception as e:
        return f"Knowledge search unavailable: {e}"


async def _gather_goals() -> str:
    """Gather current goals."""
    try:
        from knowledge.context import get_relevant_context
        return get_relevant_context("goals priorities objectives", max_chars=1000)
    except Exception as e:
        return f"Goals unavailable: {e}"


async def meeting_prep(meeting_title: str) -> str:
    """Prepare for a meeting by gathering cross-source context."""
    calendar, emails, knowledge, goals = await asyncio.gather(
        _gather_calendar(days=1),
        _gather_recent_emails(query=meeting_title),
        _gather_knowledge(meeting_title),
        _gather_goals(),
        return_exceptions=True,
    )

    sections = [
        f"## Meeting Prep: {meeting_title}",
        f"\n### Today's Calendar\n{calendar}",
        f"\n### Relevant Emails\n{emails}",
        f"\n### Background Context\n{knowledge}",
        f"\n### Your Goals\n{goals}",
    ]
    return "\n".join(str(s) for s in sections)


async def daily_focus() -> str:
    """Build a daily focus brief from calendar, reminders, emails, and goals."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    calendar, reminders, emails, goals = await asyncio.gather(
        _gather_calendar(days=1),
        _gather_reminders(),
        _gather_recent_emails(limit=5),
        _gather_goals(),
        return_exceptions=True,
    )

    sections = [
        f"## Daily Focus — {now.strftime('%A, %B %d')}",
        f"\n### Calendar\n{calendar}",
        f"\n### Reminders\n{reminders}",
        f"\n### Recent Emails\n{emails}",
        f"\n### Goals\n{goals}",
    ]
    return "\n".join(str(s) for s in sections)


async def weekly_review() -> str:
    """Build a weekly review from calendar, emails, and goals."""
    calendar, emails, goals = await asyncio.gather(
        _gather_calendar(days=7),
        _gather_recent_emails(limit=10),
        _gather_goals(),
        return_exceptions=True,
    )

    sections = [
        "## Weekly Review",
        f"\n### This Week's Calendar\n{calendar}",
        f"\n### Email Activity\n{emails}",
        f"\n### Goals Progress\n{goals}",
    ]
    return "\n".join(str(s) for s in sections)


async def context_brief(topic: str) -> str:
    """Build a topic brief from knowledge base, emails, and notes."""
    knowledge, emails = await asyncio.gather(
        _gather_knowledge(topic, limit=8),
        _gather_recent_emails(query=topic, limit=5),
        return_exceptions=True,
    )

    sections = [
        f"## Brief: {topic}",
        f"\n### Knowledge Base\n{knowledge}",
        f"\n### Related Emails\n{emails}",
    ]
    return "\n".join(str(s) for s in sections)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle synthesis intents."""
    if action == "meeting_prep":
        title = intent.get("meeting_title", "upcoming meeting")
        result = await meeting_prep(title)
        await ctx.reply(result)
        return True

    elif action == "daily_focus":
        result = await daily_focus()
        await ctx.reply(result)
        return True

    elif action == "weekly_review":
        result = await weekly_review()
        await ctx.reply(result)
        return True

    elif action == "context_brief":
        topic = intent.get("topic", "")
        if not topic:
            await ctx.reply("What topic should I brief you on?")
            return True
        result = await context_brief(topic)
        await ctx.reply(result)
        return True

    return False
