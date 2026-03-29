"""Knowledge base enrichment — detect gaps and fill them via web search.

Runs on a schedule or on-demand via /enrich. Finds recent queries where
PharoClaw lacked knowledge, web-searches for answers, fetches top pages,
and indexes the content into the knowledge base.
"""

import asyncio
import logging
import re
from urllib.parse import urlparse

log = logging.getLogger("pharoclaw.scheduler.enrichment")

# Rate limit between web fetches (seconds)
_FETCH_DELAY = 3
# Max URLs to fetch per gap query
_MAX_URLS_PER_GAP = 3
# Min content length to index (skip noise)
_MIN_CONTENT_CHARS = 200
# Max content per page
_MAX_FETCH_CHARS = 10000


async def enrich_knowledge(conn, notify_fn=None, forced_queries: list[str] | None = None) -> dict:
    """Detect knowledge gaps and fill them via web search + indexing.

    Args:
        conn: SQLite connection (from init_db()).
        notify_fn: Optional async callable(message: str) for Telegram notifications.
        forced_queries: If provided, skip gap detection and enrich these queries directly.

    Returns:
        {gaps_found, urls_fetched, docs_indexed, details: [{query, urls, indexed}]}
    """
    from actions.web import web_search, web_fetch
    from knowledge.indexer import chunk_text, index_source
    from knowledge.search import detect_knowledge_gaps

    # Step 1: Detect gaps (or use forced queries)
    if forced_queries:
        gaps = [{"query": q, "signal_type": "manual", "timestamp": ""} for q in forced_queries]
    else:
        gaps = detect_knowledge_gaps(conn, days=7, max_gaps=5)

    if not gaps:
        log.info("No knowledge gaps detected")
        return {"gaps_found": 0, "urls_fetched": 0, "docs_indexed": 0, "details": []}

    log.info("Found %d knowledge gaps to enrich", len(gaps))

    total_urls_fetched = 0
    total_docs_indexed = 0
    details = []

    for gap in gaps:
        query = gap["query"]
        gap_detail = {"query": query, "urls": [], "indexed": 0}

        try:
            # Step 2: Web search
            results = await web_search(query, max_results=_MAX_URLS_PER_GAP)
            if not results or (len(results) == 1 and results[0].get("url") == ""):
                log.info("No web results for gap: %s", query)
                details.append(gap_detail)
                continue

            for result in results[:_MAX_URLS_PER_GAP]:
                url = result.get("url", "")
                if not url:
                    continue

                # Check if URL already indexed
                existing = conn.execute(
                    "SELECT 1 FROM documents WHERE metadata LIKE ?",
                    (f"%source_url={url}%",),
                ).fetchone()
                if existing:
                    log.info("URL already indexed, skipping: %s", url)
                    continue

                # Rate limit
                if total_urls_fetched > 0:
                    await asyncio.sleep(_FETCH_DELAY)

                # Step 3: Fetch page content
                content = await web_fetch(url, max_chars=_MAX_FETCH_CHARS)
                total_urls_fetched += 1

                if content.startswith("Error fetching"):
                    log.warning("Failed to fetch %s: %s", url, content[:100])
                    continue

                if len(content) < _MIN_CONTENT_CHARS:
                    log.info("Content too short (%d chars), skipping: %s", len(content), url)
                    continue

                gap_detail["urls"].append(url)

                # Step 4: Chunk and index
                domain = urlparse(url).netloc or "web"
                title = result.get("title", query)[:200]
                chunks = chunk_text(content, chunk_size=500, overlap=50)

                entries = []
                for i, chunk in enumerate(chunks):
                    entries.append({
                        "title": f"{title} (part {i + 1})" if len(chunks) > 1 else title,
                        "content": chunk,
                        "metadata": (
                            f"source_url={url}; "
                            f"gap_query={query}; "
                            f"enrichment=autonomous"
                        ),
                    })

                # Determine category from query keywords
                category = _categorize_query(query)
                n = await index_source(conn, f"web:{domain}", f"web:{category}", entries)
                gap_detail["indexed"] += n
                total_docs_indexed += n
                log.info("Indexed %d chunks from %s for query: %s", n, url, query)

        except Exception as e:
            log.error("Enrichment failed for query '%s': %s", query, e)

        details.append(gap_detail)

    # Notify if we indexed anything
    summary = {
        "gaps_found": len(gaps),
        "urls_fetched": total_urls_fetched,
        "docs_indexed": total_docs_indexed,
        "details": details,
    }

    if total_docs_indexed > 0 and notify_fn:
        msg_lines = [f"Knowledge Enrichment Complete\n"]
        msg_lines.append(f"Gaps: {len(gaps)} | Pages: {total_urls_fetched} | Indexed: {total_docs_indexed}\n")
        for d in details:
            if d["indexed"] > 0:
                msg_lines.append(f"  {d['query']}: {d['indexed']} docs from {len(d['urls'])} pages")
        try:
            await notify_fn("\n".join(msg_lines))
        except Exception as e:
            log.warning("Enrichment notification failed: %s", e)

    return summary


def _categorize_query(query: str) -> str:
    """Simple keyword-based category assignment for enriched content."""
    q = query.lower()
    if any(w in q for w in ("python", "code", "programming", "api", "react", "javascript", "swift")):
        return "technology"
    if any(w in q for w in ("spotify", "product", "pm", "roadmap", "okr")):
        return "work"
    if any(w in q for w in ("finance", "invest", "stock", "tax", "rsu")):
        return "finance"
    if any(w in q for w in ("career", "interview", "resume", "job")):
        return "career"
    return "general"
