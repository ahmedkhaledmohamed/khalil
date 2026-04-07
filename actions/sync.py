"""Knowledge sync skill — expose /sync as a tool so the LLM can trigger indexing.

When a user asks to "ingest", "sync", "index", or "update knowledge", the LLM
can call this tool instead of running find/cat commands that don't persist.
"""

import logging

log = logging.getLogger("khalil.actions.sync")

SKILL = {
    "name": "sync",
    "description": "Sync and index knowledge sources — email, Notion, documents, work repos",
    "category": "knowledge",
    "patterns": [
        (r"\b(?:sync|ingest|index|reindex)\s+(?:all|knowledge|sources?|documents?|everything)\b", "sync_all"),
        (r"\b(?:sync|ingest|index)\s+(?:my\s+)?(?:work|repos?|directories|docs)\b", "sync_all"),
        (r"\benrich\s+(?:your\s+)?knowledge\b", "sync_all"),
        (r"\b(?:sync|ingest|index)\s+(?:personal\s+)?email\b", "sync_email"),
        (r"\bupdate\s+(?:your\s+)?knowledge\s+(?:base|db|database)\b", "sync_all"),
    ],
    "actions": [
        {
            "type": "sync_all",
            "handler": "handle_intent",
            "keywords": "sync ingest index reindex knowledge enrich update documents repos work email",
            "description": "Sync all knowledge sources (email, Notion, Readwise, tasks, work repos) into the knowledge database",
        },
        {
            "type": "sync_email",
            "handler": "handle_intent",
            "keywords": "sync email gmail inbox personal",
            "description": "Sync personal email only",
        },
    ],
    "examples": [
        "Sync my knowledge base",
        "Ingest more knowledge about my work",
        "Enrich your knowledge DB",
        "Index all my documents",
        "Sync emails",
    ],
}


async def handle_intent(action_type: str, intent: dict, ctx):
    """Handle sync/ingest requests."""
    if action_type == "sync_email":
        return await _sync_email(ctx)
    else:
        return await _sync_all(ctx)


async def _sync_all(ctx):
    """Sync all live sources + run incremental document indexing."""
    await ctx.reply("🔄 Syncing all knowledge sources... this may take a minute.")

    results = {}

    # 1. Live sources (email, Notion, Readwise, Tasks, work email)
    try:
        from knowledge.live_sources import index_all_live_sources
        from knowledge.indexer import init_db
        conn = init_db()
        live_results = await index_all_live_sources(conn)
        results.update(live_results)
    except Exception as e:
        log.warning("Live source sync failed: %s", e)
        results["live_sources"] = f"error: {e}"

    # 2. Personal email
    try:
        from actions.gmail_sync import sync_new_emails
        email_result = await sync_new_emails()
        results["personal_email"] = email_result.get("indexed", 0)
    except Exception as e:
        log.warning("Email sync failed: %s", e)
        results["personal_email"] = f"error: {e}"

    # 3. Incremental document indexing (work repos, side projects, archives)
    try:
        from knowledge.indexer import index_incremental
        index_incremental()
        results["document_reindex"] = "completed"
    except Exception as e:
        log.warning("Incremental index failed: %s", e)
        results["document_reindex"] = f"error: {e}"

    # Format report
    lines = []
    total = 0
    for source, count in sorted(results.items()):
        if isinstance(count, int):
            lines.append(f"  {source}: {count} items")
            total += count
        else:
            lines.append(f"  {source}: {count}")

    report = f"✅ Knowledge sync complete — {total} new items indexed:\n" + "\n".join(lines)
    await ctx.reply(report)
    return True


async def _sync_email(ctx):
    """Sync personal email only."""
    try:
        from actions.gmail_sync import sync_new_emails
        result = await sync_new_emails()
        await ctx.reply(
            f"✅ Email sync: {result['fetched']} fetched, {result['indexed']} indexed"
        )
    except Exception as e:
        await ctx.reply(f"❌ Email sync failed: {e}")
    return True
