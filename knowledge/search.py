"""Hybrid search: vector similarity + keyword matching."""

import logging
import math
import sqlite3
import struct
from datetime import datetime, timedelta

from config import DB_PATH, DATA_DIR, EMBED_DIM
from knowledge.embedder import embed_text

log = logging.getLogger("khalil.search")


# --- #63: Knowledge Freshness Scoring ---

def _compute_freshness_score(created_at: str | None, half_life_days: int = 30) -> float:
    """Compute a time-decay freshness score between 0.0 and 1.0.

    Uses exponential decay with configurable half-life. Documents with no
    timestamp get a neutral score of 0.5.
    """
    if not created_at:
        return 0.5
    try:
        doc_time = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        try:
            doc_time = datetime.strptime(created_at[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return 0.5
    age_days = (datetime.utcnow() - doc_time).total_seconds() / 86400
    if age_days < 0:
        age_days = 0
    return math.exp(-0.693 * age_days / half_life_days)  # 0.693 = ln(2)


def get_db() -> sqlite3.Connection:
    """Get database connection with sqlite-vec loaded."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row
    return conn


def serialize_float32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


async def vector_search(query: str, limit: int = 10, category: str | None = None) -> list[dict]:
    """Search documents by semantic similarity. Returns [] if embedding fails."""
    query_embedding = await embed_text(query)
    if query_embedding is None:
        return []
    conn = get_db()

    if category:
        rows = conn.execute(
            """
            SELECT d.id, d.source, d.category, d.title, d.content, d.metadata,
                   e.distance
            FROM document_embeddings e
            JOIN documents d ON d.id = e.id
            WHERE e.embedding MATCH ? AND k = ?
              AND d.category LIKE ?
            ORDER BY e.distance
            """,
            (serialize_float32(query_embedding), limit * 2, f"%{category}%"),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT d.id, d.source, d.category, d.title, d.content, d.metadata,
                   e.distance
            FROM document_embeddings e
            JOIN documents d ON d.id = e.id
            WHERE e.embedding MATCH ? AND k = ?
            ORDER BY e.distance
            """,
            (serialize_float32(query_embedding), limit),
        ).fetchall()

    conn.close()
    return [dict(r) for r in rows[:limit]]


def keyword_search(query: str, limit: int = 10, category: str | None = None) -> list[dict]:
    """Search documents by keyword matching."""
    conn = get_db()
    terms = query.lower().split()
    # Build LIKE clauses for each term
    conditions = " AND ".join(
        ["(LOWER(d.title) LIKE ? OR LOWER(d.content) LIKE ?)" for _ in terms]
    )
    params = []
    for term in terms:
        params.extend([f"%{term}%", f"%{term}%"])

    if category:
        conditions += " AND d.category LIKE ?"
        params.append(f"%{category}%")

    rows = conn.execute(
        f"""
        SELECT d.id, d.source, d.category, d.title, d.content, d.metadata
        FROM documents d
        WHERE {conditions}
        ORDER BY d.id DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def _compute_rerank_score(result: dict, query_terms: list[str]) -> float:
    """Compute a combined reranking score for a search result.

    Combines:
    - Semantic distance (inverted, if available from vector search)
    - Keyword match count (how many query terms appear in the content)
    - Freshness score (time-decay)

    Higher score = more relevant.
    """
    score = 0.0

    # 1. Semantic distance — lower distance = better. Invert and normalize to 0-1 range.
    distance = result.get("distance")
    if distance is not None:
        # Typical cosine distances are 0.0-2.0; normalize so 0.0 -> 1.0, 2.0 -> 0.0
        score += max(0.0, 1.0 - distance / 2.0) * 0.4
    elif result.get("match_type") == "keyword":
        # Keyword-only results get a baseline semantic score
        score += 0.1

    # 2. Keyword match count — proportion of query terms found in content
    content = ((result.get("title") or "") + " " + (result.get("content") or "")).lower()
    if query_terms:
        match_count = sum(1 for t in query_terms if t in content)
        score += (match_count / len(query_terms)) * 0.35

    # 3. Freshness
    score += result.get("freshness", 0.5) * 0.25

    return round(score, 4)


async def hybrid_search(query: str, limit: int = 8, category: str | None = None) -> list[dict]:
    """Combine vector and keyword search, deduplicate, rerank.

    Falls back to keyword-only search if Ollama is unavailable.
    """
    vector_results = await vector_search(query, limit=limit, category=category)
    if not vector_results:
        log.info("Vector search returned no results — using keyword-only fallback")
    kw_results = keyword_search(query, limit=limit, category=category)

    # Merge and deduplicate by id
    seen_ids = set()
    merged = []

    # Vector results first (semantic relevance)
    for r in vector_results:
        if r["id"] not in seen_ids:
            r["match_type"] = "semantic"
            merged.append(r)
            seen_ids.add(r["id"])

    # Then keyword results
    for r in kw_results:
        if r["id"] not in seen_ids:
            r["match_type"] = "keyword"
            merged.append(r)
            seen_ids.add(r["id"])

    # #63: Apply freshness boost — recent documents rank higher
    for r in merged:
        meta = r.get("metadata") or {}
        if isinstance(meta, str):
            try:
                import json
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        created_at = r.get("created_at") or meta.get("created_at")
        r["freshness"] = _compute_freshness_score(created_at)

    # #62: Rerank by combined score (semantic distance + keyword overlap + freshness)
    query_terms = [t.lower() for t in query.split() if len(t) > 2]
    for r in merged:
        r["rerank_score"] = _compute_rerank_score(r, query_terms)

    merged.sort(key=lambda r: -r["rerank_score"])

    return merged[:limit]


def get_stats() -> dict:
    """Get database statistics."""
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    by_category = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM documents GROUP BY category ORDER BY cnt DESC"
    ).fetchall()
    conn.close()
    return {
        "total_documents": total,
        "by_category": {r["category"]: r["cnt"] for r in by_category},
    }
