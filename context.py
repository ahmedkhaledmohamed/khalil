"""Context assembly — intent-aware context gathering.

Different intents need different context:
- CONTINUATION: task context + last 5 messages (no KB search, no live state)
- QUESTION: KB search + memories + last 10 messages + live state
- TASK: KB search + auto-read full docs + last 10 messages + live state
- CHAT: memories + last 10 messages (no KB search, no live state)

This replaces the monolithic "gather everything for every message" approach
that caused KB noise injection on continuations and shallow context on tasks.
"""

import asyncio
import json
import logging
import sqlite3

from config import DB_PATH
from intent import Intent
from knowledge.context import get_relevant_context
from knowledge.search import hybrid_search

log = logging.getLogger("khalil.context")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


async def _search_kb(query: str, limit: int = 6) -> list[dict]:
    """KB search with timeout."""
    try:
        return await asyncio.wait_for(hybrid_search(query, limit=limit), timeout=15.0)
    except asyncio.TimeoutError:
        log.warning("hybrid_search timed out for: %s", query[:80])
        return []


def _format_kb_results(results: list[dict], max_chars: int = 12000) -> str:
    """Format search results with source citations."""
    lines = []
    total = 0
    for r in results:
        category = r.get("category", "")
        title = r["title"]
        tag = f"[Source: {category} — {title}]" if category else f"[Source: {title}]"
        entry = f"{tag}\n{r['content']}\n"
        if total + len(entry) > max_chars:
            break
        lines.append(entry)
        total += len(entry)
    return "\n---\n".join(lines)


async def _auto_read_full_documents(results: list[dict], top_n: int = 2, max_chars: int = 4000) -> str:
    """Auto-read full documents for the top N KB search results.

    This gives the LLM deep context for TASK intents instead of just snippets.
    """
    if not results:
        return ""

    parts = []
    conn = _get_conn()
    seen_categories = set()

    for r in results[:top_n]:
        category = r.get("category", "")
        if not category or category in seen_categories:
            continue
        seen_categories.add(category)

        try:
            rows = conn.execute(
                "SELECT title, content FROM documents WHERE category = ? ORDER BY id",
                (category,),
            ).fetchall()

            if not rows:
                # Try prefix match
                rows = conn.execute(
                    "SELECT title, content FROM documents WHERE category LIKE ? ORDER BY id LIMIT 50",
                    (category + "%",),
                ).fetchall()

            if rows:
                full_text = ""
                current_title = ""
                for row in rows:
                    title = row["title"]
                    content = row["content"]
                    if title != current_title:
                        if current_title:
                            full_text += "\n\n---\n\n"
                        full_text += f"## {title}\n\n"
                        current_title = title
                    full_text += content + "\n"
                    if len(full_text) >= max_chars:
                        full_text = full_text[:max_chars] + "\n[... truncated]"
                        break
                parts.append(f"[Full Document: {category}]\n{full_text}")
        except Exception as e:
            log.debug("Auto-read failed for %s: %s", category, e)

    conn.close()
    return "\n\n".join(parts)


def _get_recent_messages(chat_id: int, limit: int = 30) -> str:
    """Get recent conversation messages."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT role, content, message_type, metadata FROM conversations "
        "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    conn.close()

    if not rows:
        return ""

    rows = list(reversed(rows))
    lines = []
    for r in rows:
        role, content, msg_type, meta = r["role"], r["content"], r["message_type"] or "text", r["metadata"]
        if msg_type == "tool_call":
            try:
                info = json.loads(meta) if meta else {}
                tool_name = info.get("tool_name", "tool")
                lines.append(f"Assistant: [Called tool: {tool_name}]")
            except Exception:
                lines.append("Assistant: [Tool call]")
        elif msg_type == "tool_result":
            try:
                info = json.loads(meta) if meta else {}
                tool_name = info.get("tool_name", "tool")
                lines.append(f"Tool ({tool_name}): {content[:2000]}")
            except Exception:
                lines.append(f"Tool: {content[:2000]}")
        else:
            lines.append(f"{role.title()}: {content}")

    return "[Source: recent messages]\n" + "\n".join(lines)


async def _get_memories(query: str) -> str:
    """Search conversation memories relevant to query."""
    try:
        from knowledge.search import search_memories
        memories = await search_memories(query, limit=5)
        if memories:
            memory_lines = [f"- [{m['memory_type']}] {m['content']}" for m in memories]
            return "[Source: conversation memories]\n" + "\n".join(memory_lines)
    except Exception as e:
        log.debug("Memory search unavailable: %s", e)
    return ""


def _get_session_summary(chat_id: int) -> str:
    """Get latest conversation summary."""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT summary FROM conversation_summaries WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        conn.close()
        if row:
            return f"[Source: previous conversation summary]\n{row['summary']}"
    except Exception:
        pass
    return ""


def _get_active_plans(chat_id: int) -> str:
    """Get active task plans for this chat."""
    try:
        from orchestrator import get_active_plans_for_chat, ensure_table as ensure_plans_table
        ensure_plans_table()
        active_plans = get_active_plans_for_chat(chat_id)
        if active_plans:
            plan_lines = []
            for plan in active_plans:
                plan_lines.append(f"Plan: {plan.query[:100]}")
                plan_lines.append(f"  Status: {plan.status} (ID: {plan.plan_id})")
                for step in plan.steps:
                    status_label = {
                        "completed": "DONE", "failed": "FAILED", "pending": "TODO",
                        "running": "RUNNING", "blocked": "BLOCKED", "skipped": "SKIPPED",
                    }.get(step.status, "?")
                    line = f"  [{status_label}] {step.description}"
                    if step.result:
                        line += f" -> {step.result[:150]}"
                    if step.error:
                        line += f" ERROR: {step.error[:100]}"
                    plan_lines.append(line)
            return "[Source: active task plans]\n" + "\n".join(plan_lines)
    except Exception as e:
        log.debug("Active plans injection failed: %s", e)
    return ""


async def _get_live_state() -> str:
    """Collect live device/app state."""
    try:
        from state.collector import collect_live_state, format_for_prompt
        live = await asyncio.wait_for(collect_live_state(), timeout=5.0)
        return format_for_prompt(live)
    except asyncio.TimeoutError:
        log.warning("Live state timed out")
        return ""
    except Exception as e:
        log.debug("Live state failed: %s", e)
        return ""


async def _get_proactive_context(chat_id: int, query: str) -> str:
    """Session continuity + entity resolution."""
    try:
        from memory.session_continuity import get_session_continuity
        from knowledge.entity_resolver import get_entity_resolver

        async def _session():
            return get_session_continuity(chat_id, query)

        async def _entity():
            resolver = get_entity_resolver()
            entities = await resolver.resolve_entities_in_query(query)
            return resolver.format_entity_context(entities)

        session_ctx, entity_ctx = await asyncio.gather(
            _session(), _entity(), return_exceptions=True,
        )
        parts = []
        if isinstance(session_ctx, str) and session_ctx:
            parts.append(session_ctx)
        if isinstance(entity_ctx, str) and entity_ctx:
            parts.append(entity_ctx)
        return "\n\n".join(parts) if parts else ""
    except Exception as e:
        log.debug("Proactive context failed: %s", e)
        return ""


async def assemble_context(
    intent: Intent,
    query: str,
    chat_id: int,
    task=None,
    voice_mode: bool = False,
) -> str:
    """Assemble context based on intent type.

    Returns the full context string ready for the LLM prompt.
    """
    parts = []

    # Task context always injected first when present
    if task:
        from task_manager import TaskManager
        mgr = TaskManager()
        parts.append(mgr.get_task_context_for_llm(task))

    if intent == Intent.CONTINUATION:
        # Minimal context — task state + short recent history
        # No KB search, no live state, no proactive context
        messages = _get_recent_messages(chat_id, limit=10)  # ~5 logical turns
        summary = _get_session_summary(chat_id)
        plans = _get_active_plans(chat_id)
        if summary:
            parts.append(summary)
        if plans:
            parts.append(plans)
        if messages:
            parts.append(messages)

    elif intent == Intent.CHAT:
        # Conversation context only — no KB search, no live state
        memories = await _get_memories(query)
        messages = _get_recent_messages(chat_id, limit=20)  # ~10 logical turns
        personal = get_relevant_context(query, max_chars=2000)
        summary = _get_session_summary(chat_id)
        plans = _get_active_plans(chat_id)
        if memories:
            parts.append(memories)
        if summary:
            parts.append(summary)
        if plans:
            parts.append(plans)
        if messages:
            parts.append(messages)
        parts.append(f"[Source: CONTEXT.md]\n{personal}")

    elif intent == Intent.QUESTION:
        # KB search + full conversation context + live state
        kb_results, memories, live, proactive = await asyncio.gather(
            _search_kb(query, limit=6),
            _get_memories(query),
            _get_live_state(),
            _get_proactive_context(chat_id, query),
        )
        messages = _get_recent_messages(chat_id, limit=20)
        personal = get_relevant_context(query, max_chars=2000)
        summary = _get_session_summary(chat_id)
        plans = _get_active_plans(chat_id)

        archive = _format_kb_results(kb_results) if kb_results else "No relevant archive data found."

        if proactive:
            parts.append(proactive)
        if live:
            parts.append(f"[Source: live state]\n{live}")
        if memories:
            parts.append(memories)
        if summary:
            parts.append(summary)
        if plans:
            parts.append(plans)
        if messages:
            parts.append(messages)
        parts.append(f"[Source: CONTEXT.md]\n{personal}")
        parts.append(f"[Source: knowledge base search]\n{archive}")

    elif intent == Intent.TASK:
        # Rich context: KB search + auto-read full docs + live state
        kb_results, memories, live, proactive = await asyncio.gather(
            _search_kb(query, limit=6),
            _get_memories(query),
            _get_live_state(),
            _get_proactive_context(chat_id, query),
        )
        # Auto-read full documents for top 2 results (deep retrieval)
        full_docs = await _auto_read_full_documents(kb_results, top_n=2, max_chars=4000)

        messages = _get_recent_messages(chat_id, limit=20)
        personal = get_relevant_context(query, max_chars=2000)
        summary = _get_session_summary(chat_id)
        plans = _get_active_plans(chat_id)

        archive = _format_kb_results(kb_results) if kb_results else "No relevant archive data found."

        if proactive:
            parts.append(proactive)
        if live:
            parts.append(f"[Source: live state]\n{live}")
        if memories:
            parts.append(memories)
        if summary:
            parts.append(summary)
        if plans:
            parts.append(plans)
        if messages:
            parts.append(messages)
        parts.append(f"[Source: CONTEXT.md]\n{personal}")
        parts.append(f"[Source: knowledge base search]\n{archive}")
        if full_docs:
            parts.append(full_docs)

    # Voice mode modifier
    if voice_mode:
        parts.insert(0,
            "[Voice mode: User is speaking via voice. Keep your response concise "
            "(1-3 sentences), conversational, and easy to read aloud. "
            "Avoid markdown formatting, bullet lists, and emojis.]"
        )

    context = "\n\n".join(parts)
    log.info("Context assembled for %s intent: %d chars", intent.value, len(context))
    return context
