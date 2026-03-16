"""Self-improvement engine — reflection, insights, and learned preferences.

Khalil analyzes its own interaction data to identify patterns and adapt behavior.
All changes are transparent (visible via /learn) and safe (hard guardrails immutable).
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from config import DB_PATH, HARD_GUARDRAILS

log = logging.getLogger("khalil.learning")

# --- Preference Access ---

_db_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """Get or reuse a DB connection."""
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(str(DB_PATH))
        _db_conn.row_factory = sqlite3.Row
    return _db_conn


def set_conn(conn: sqlite3.Connection):
    """Set the shared DB connection (called from server startup)."""
    global _db_conn
    _db_conn = conn


def get_preference(key: str, default=None):
    """Read a learned preference. Returns default if missing or low confidence."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT value, confidence FROM learned_preferences WHERE key = ?", (key,)
    ).fetchone()
    if row and row["confidence"] >= 0.3:
        return json.loads(row["value"])
    return default


def set_preference(key: str, value, source_insight_id: int | None = None, confidence: float = 0.5):
    """Write or update a learned preference."""
    conn = _get_conn()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO learned_preferences (key, value, source_insight_id, confidence, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
             value = excluded.value,
             source_insight_id = excluded.source_insight_id,
             confidence = excluded.confidence,
             updated_at = excluded.updated_at""",
        (key, json.dumps(value), source_insight_id, confidence, now, now),
    )
    conn.commit()
    log.info("Preference set: %s = %s (confidence=%.2f)", key, value, confidence)


def list_preferences() -> list[dict]:
    """List all active learned preferences."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT key, value, confidence, source_insight_id, updated_at FROM learned_preferences ORDER BY updated_at DESC"
    ).fetchall()
    return [
        {
            "key": r["key"],
            "value": json.loads(r["value"]),
            "confidence": r["confidence"],
            "source_insight_id": r["source_insight_id"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def reset_preferences():
    """Clear all learned preferences."""
    conn = _get_conn()
    conn.execute("DELETE FROM learned_preferences")
    conn.commit()
    log.info("All learned preferences cleared")


# --- Signal Recording ---

def record_signal(signal_type: str, context: dict | None = None, value: float = 1.0):
    """Record an interaction signal for future reflection."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO interaction_signals (signal_type, context, value) VALUES (?, ?, ?)",
        (signal_type, json.dumps(context) if context else None, value),
    )
    conn.commit()


# --- Capability Usage Heatmap (#10) ---

def get_capability_heatmap(days: int = 7) -> list[dict]:
    """Return capability usage counts for the last N days, sorted by frequency."""
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT json_extract(context, '$.action') as action, COUNT(*) as count "
        "FROM interaction_signals "
        "WHERE signal_type = 'capability_usage' AND created_at > ? "
        "GROUP BY action ORDER BY count DESC",
        (cutoff,),
    ).fetchall()
    return [{"action": r[0], "count": r[1]} for r in rows]


# --- Intent Detection Accuracy (#3) ---

def get_intent_accuracy(days: int = 7) -> dict:
    """Compute intent detection accuracy over the last N days.

    Returns {total, matches, accuracy_pct, mismatches: [{pattern_hint, llm_action, count}]}.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT context FROM interaction_signals "
        "WHERE signal_type = 'intent_accuracy' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    total = len(rows)
    matches = 0
    mismatch_counts: dict[tuple, int] = {}
    for r in rows:
        ctx = json.loads(r[0]) if r[0] else {}
        if ctx.get("match"):
            matches += 1
        else:
            key = (ctx.get("pattern_hint", "?"), ctx.get("llm_action", "?"))
            mismatch_counts[key] = mismatch_counts.get(key, 0) + 1

    mismatches = [
        {"pattern_hint": k[0], "llm_action": k[1], "count": v}
        for k, v in sorted(mismatch_counts.items(), key=lambda x: -x[1])
    ]

    return {
        "total": total,
        "matches": matches,
        "accuracy_pct": round(matches / total * 100, 1) if total else 0.0,
        "mismatches": mismatches,
    }


# --- Extension Usage Monitoring (#31) ---

def get_extension_health(days: int = 7) -> list[dict]:
    """Return per-extension usage stats: invocations, successes, errors, error_rate."""
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT context FROM interaction_signals "
        "WHERE signal_type = 'extension_usage' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    stats: dict[str, dict] = {}
    for r in rows:
        ctx = json.loads(r[0]) if r[0] else {}
        ext = ctx.get("extension", "unknown")
        status = ctx.get("status", "unknown")
        if ext not in stats:
            stats[ext] = {"invoked": 0, "success": 0, "error": 0}
        if status in stats[ext]:
            stats[ext][status] += 1

    result = []
    for ext, s in sorted(stats.items()):
        total = s["invoked"]
        error_rate = round(s["error"] / total * 100, 1) if total else 0.0
        result.append({
            "extension": ext,
            "invocations": total,
            "successes": s["success"],
            "errors": s["error"],
            "error_rate_pct": error_rate,
        })
    return result


# --- Insight Management ---

def store_insight(category: str, summary: str, evidence: str, recommendation: str) -> int:
    """Store a new insight. Returns its ID."""
    conn = _get_conn()
    cursor = conn.execute(
        "INSERT INTO insights (category, summary, evidence, recommendation) VALUES (?, ?, ?, ?)",
        (category, summary, evidence, recommendation),
    )
    conn.commit()
    return cursor.lastrowid


def get_insights(status: str | None = None, limit: int = 10) -> list[dict]:
    """Get insights, optionally filtered by status."""
    conn = _get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM insights WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM insights ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def apply_insight(insight_id: int, resolved_by: str = "user") -> bool:
    """Apply a pending insight — generates the corresponding preference."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM insights WHERE id = ? AND status = 'pending'", (insight_id,)).fetchone()
    if not row:
        return False

    # Parse recommendation to extract preference key/value
    # The reflection engine stores these in a structured way
    conn.execute(
        "UPDATE insights SET status = 'applied', resolved_at = ?, resolved_by = ? WHERE id = ?",
        (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), resolved_by, insight_id),
    )
    conn.commit()
    log.info("Insight #%d applied by %s: %s", insight_id, resolved_by, row["summary"])
    return True


def dismiss_insight(insight_id: int) -> bool:
    """Dismiss a pending insight."""
    conn = _get_conn()
    result = conn.execute(
        "UPDATE insights SET status = 'dismissed', resolved_at = ?, resolved_by = 'user' WHERE id = ? AND status = 'pending'",
        (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), insight_id),
    )
    conn.commit()
    return result.rowcount > 0


# --- Reflection Engine ---

# Safe insight categories that can be auto-applied (never autonomy)
AUTO_APPLY_CATEGORIES = {"preference", "knowledge_gap"}
MAX_AUTO_APPLY_PER_CYCLE = 5

# Phrases that indicate the LLM couldn't find an answer
SEARCH_MISS_PHRASES = [
    "don't have information",
    "couldn't find",
    "not in my archives",
    "no relevant",
    "i don't have",
    "i couldn't find",
    "not available in",
    "no data on",
    "no records of",
]


def detect_search_miss(response: str) -> bool:
    """Check if an LLM response indicates a knowledge gap."""
    response_lower = response.lower()
    return any(phrase in response_lower for phrase in SEARCH_MISS_PHRASES)


def _gather_weekly_data(conn: sqlite3.Connection) -> dict:
    """Gather last 7 days of interaction data for reflection."""
    cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    # Approval/denial signals
    signals = conn.execute(
        "SELECT signal_type, context, value, created_at FROM interaction_signals WHERE created_at > ? ORDER BY created_at",
        (cutoff,),
    ).fetchall()

    # Audit log entries
    audit = conn.execute(
        "SELECT action_type, description, result, autonomy_level, timestamp FROM audit_log WHERE timestamp > ? ORDER BY timestamp",
        (cutoff,),
    ).fetchall()

    # Conversation topics (just user messages)
    conversations = conn.execute(
        "SELECT content, timestamp FROM conversations WHERE role = 'user' AND timestamp > ? ORDER BY timestamp",
        (cutoff,),
    ).fetchall()

    # Pending action outcomes
    actions = conn.execute(
        "SELECT action_type, status, created_at, resolved_at FROM pending_actions WHERE created_at > ?",
        (cutoff,),
    ).fetchall()

    return {
        "signals": [dict(r) for r in signals],
        "audit": [dict(r) for r in audit],
        "conversations": [dict(r) for r in conversations],
        "actions": [dict(r) for r in actions],
    }


def _build_reflection_prompt(data: dict, current_prefs: list[dict]) -> str:
    """Build the structured prompt for weekly reflection."""
    # Summarize signals by type
    signal_summary = {}
    for s in data["signals"]:
        st = s["signal_type"]
        signal_summary.setdefault(st, []).append(s)

    # Format action decisions
    action_decisions = signal_summary.get("action_decision", [])
    approve_count = sum(1 for a in action_decisions if a["value"] == 1)
    deny_count = sum(1 for a in action_decisions if a["value"] == 0)

    # Format search misses
    search_misses = signal_summary.get("search_miss", [])
    miss_queries = []
    for m in search_misses:
        ctx = json.loads(m["context"]) if m["context"] else {}
        miss_queries.append(ctx.get("query", "unknown"))

    # Format digest engagement
    digest_sent = len(signal_summary.get("digest_sent", []))
    digest_engaged = len(signal_summary.get("digest_engaged", []))

    # Conversation topics (first 30 chars of each)
    topics = [c["content"][:80] for c in data["conversations"][:30]]

    # Current preferences
    pref_text = "\n".join(f"- {p['key']}: {p['value']} (confidence={p['confidence']})" for p in current_prefs)
    if not pref_text:
        pref_text = "None set yet."

    prompt = f"""Analyze Khalil's interaction data for the past week to identify patterns and suggest improvements.

## Action Decisions
- Approvals: {approve_count}
- Denials: {deny_count}
- Details: {json.dumps(action_decisions[:10], default=str)}

## Search Misses (queries with no good answer)
{json.dumps(miss_queries[:15])}

## Digest Engagement
- Digests sent: {digest_sent}
- User engaged within 30 min: {digest_engaged}

## User Conversation Topics (recent)
{json.dumps(topics)}

## Current Learned Preferences
{pref_text}

---

Based on this data, generate a JSON array of insights. Each insight must have:
- "category": one of "autonomy", "preference", "knowledge_gap", "prompt", "schedule"
- "summary": one-sentence human-readable finding
- "evidence": brief description of supporting data
- "recommendation": concrete action or preference to set
- "auto_apply": boolean — true ONLY for low-risk preference/knowledge_gap changes

Rules:
- NEVER recommend weakening hard guardrails ({json.dumps(HARD_GUARDRAILS)})
- Only recommend autonomy changes after 5+ consistent approval/denial signals
- Autonomy changes must ALWAYS have auto_apply=false
- Be specific and evidence-based — no generic suggestions
- Maximum 5 insights per reflection
- If there isn't enough data for meaningful insights, return an empty array []

Respond with ONLY a JSON array. No markdown, no explanation."""

    return prompt


def generate_reflection_diff() -> str:
    """#5: Generate a diff report showing what changed since last reflection.

    Returns a human-readable summary of new/decayed preferences and new insights.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    lines = []

    # New or updated preferences
    new_prefs = conn.execute(
        "SELECT key, value, confidence FROM learned_preferences WHERE updated_at > ?",
        (cutoff,),
    ).fetchall()
    if new_prefs:
        lines.append("📊 Preferences changed this week:")
        for p in new_prefs:
            lines.append(f"  • {p[0]}: {p[1]} (confidence={p[2]})")

    # Decayed preferences (low confidence)
    decayed = conn.execute(
        "SELECT key, value, confidence FROM learned_preferences WHERE confidence < 0.3"
    ).fetchall()
    if decayed:
        lines.append("📉 Decaying preferences (low confidence):")
        for p in decayed:
            lines.append(f"  • {p[0]}: {p[1]} (confidence={p[2]})")

    # New insights this week
    new_insights = conn.execute(
        "SELECT category, summary, status FROM insights WHERE created_at > ? ORDER BY created_at DESC",
        (cutoff,),
    ).fetchall()
    if new_insights:
        lines.append(f"💡 {len(new_insights)} new insight(s) this week:")
        for i in new_insights[:5]:
            status_icon = "✅" if i[2] == "applied" else "⏳"
            lines.append(f"  {status_icon} [{i[0]}] {i[1]}")

    # Capability heatmap summary
    heatmap = get_capability_heatmap(days=7)
    if heatmap:
        top3 = heatmap[:3]
        lines.append("🔥 Most used capabilities:")
        for h in top3:
            lines.append(f"  • {h['action']}: {h['count']}x")

    return "\n".join(lines) if lines else "No significant changes since last reflection."


async def run_weekly_reflection(ask_llm_fn) -> list[dict]:
    """Run the weekly reflection — analyze signals and generate insights.

    Args:
        ask_llm_fn: async callable(query, context, system_extra) -> str

    Returns:
        List of generated insight dicts.
    """
    conn = _get_conn()
    data = _gather_weekly_data(conn)

    # Skip if not enough data
    total_signals = len(data["signals"]) + len(data["conversations"])
    if total_signals < 3:
        log.info("Weekly reflection skipped — insufficient data (%d signals)", total_signals)
        return []

    current_prefs = list_preferences()
    prompt = _build_reflection_prompt(data, current_prefs)

    response = await ask_llm_fn(
        prompt,
        "",
        system_extra="You are analyzing interaction data. Respond with ONLY a JSON array of insights.",
    )

    # Parse insights from LLM response
    response = response.strip()
    if response.startswith("⚠️"):
        log.error("Reflection LLM call failed: %s", response)
        return []

    try:
        # Handle markdown code blocks
        if "```" in response:
            response = response.split("```")[1]
            if response.startswith("json"):
                response = response[4:]
        insights = json.loads(response.strip())
    except (json.JSONDecodeError, IndexError):
        log.error("Reflection returned invalid JSON: %s", response[:200])
        return []

    if not isinstance(insights, list):
        log.error("Reflection returned non-array: %s", type(insights))
        return []

    # Store insights and auto-apply safe ones
    stored = []
    auto_applied = 0
    for insight in insights[:5]:  # Cap at 5
        category = insight.get("category", "preference")
        summary = insight.get("summary", "")
        evidence = insight.get("evidence", "")
        recommendation = insight.get("recommendation", "")
        auto_apply = insight.get("auto_apply", False)

        if not summary:
            continue

        insight_id = store_insight(category, summary, evidence, recommendation)
        insight["id"] = insight_id
        stored.append(insight)

        # Auto-apply safe insights (within limits)
        if (
            auto_apply
            and category in AUTO_APPLY_CATEGORIES
            and auto_applied < MAX_AUTO_APPLY_PER_CYCLE
            and category != "autonomy"
        ):
            apply_insight(insight_id, resolved_by="auto")
            auto_applied += 1
            log.info("Auto-applied insight #%d: %s", insight_id, summary)

    log.info("Weekly reflection complete: %d insights generated, %d auto-applied", len(stored), auto_applied)
    return stored


async def run_daily_micro_reflection(ask_llm_fn) -> list[dict]:
    """Daily response quality check — search misses, manual action suggestions, capability gaps."""
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    insights = []

    # 1. Search misses
    misses = conn.execute(
        "SELECT context FROM interaction_signals WHERE signal_type = 'search_miss' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    if len(misses) >= 2:
        miss_queries = []
        for m in misses:
            ctx = json.loads(m["context"]) if m["context"] else {}
            miss_queries.append(ctx.get("query", "unknown"))
        insight_id = store_insight(
            "knowledge_gap",
            f"Failed to answer {len(misses)} queries today",
            f"Queries: {', '.join(miss_queries[:5])}",
            "Consider indexing more content related to these topics, or running /sync",
        )
        insights.append({"id": insight_id, "category": "knowledge_gap", "summary": f"{len(misses)} unanswered queries today"})

    # 2. LLM suggested commands instead of executing (needs direct intent templates)
    manual_actions = conn.execute(
        "SELECT context FROM interaction_signals WHERE signal_type = 'response_suggests_manual_action' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    if manual_actions:
        queries = []
        cmds = []
        for row in manual_actions:
            ctx = json.loads(row["context"]) if row["context"] else {}
            queries.append(ctx.get("query", "unknown"))
            cmds.append(ctx.get("suggested_cmd", "unknown"))
        insight_id = store_insight(
            "response_quality",
            f"LLM suggested {len(manual_actions)} command(s) instead of executing them",
            f"Queries: {', '.join(queries[:5])}\nCommands: {', '.join(cmds[:5])}",
            "Add direct intent templates in _try_direct_shell_intent() for these query patterns so the LLM is bypassed",
        )
        insights.append({"id": insight_id, "category": "response_quality", "summary": f"{len(manual_actions)} suggest-instead-of-execute"})

    # 3. Capability gaps detected
    gaps = conn.execute(
        "SELECT context FROM interaction_signals WHERE signal_type = 'capability_gap_detected' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    if len(gaps) >= 2:
        gap_queries = []
        for row in gaps:
            ctx = json.loads(row["context"]) if row["context"] else {}
            gap_queries.append(ctx.get("query", "unknown"))
        insight_id = store_insight(
            "capability_gap",
            f"Detected {len(gaps)} capability gaps today",
            f"Queries: {', '.join(gap_queries[:5])}",
            "Check if self-extension PRs were generated. If gaps persist, review the gap detection and extension pipeline.",
        )
        insights.append({"id": insight_id, "category": "capability_gap", "summary": f"{len(gaps)} capability gaps"})

    # 4. User corrections
    corrections = conn.execute(
        "SELECT context FROM interaction_signals WHERE signal_type = 'user_correction' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    if len(corrections) >= 2:
        correction_queries = []
        for row in corrections:
            ctx = json.loads(row["context"]) if row["context"] else {}
            correction_queries.append(ctx.get("query", "unknown"))
        insight_id = store_insight(
            "response_quality",
            f"User corrected Khalil {len(corrections)} times today",
            f"Corrections: {', '.join(correction_queries[:5])}",
            "Review conversation history around these corrections to identify systematic issues",
        )
        insights.append({"id": insight_id, "category": "response_quality", "summary": f"{len(corrections)} user corrections"})

    if not insights:
        log.info("Daily micro-reflection: no quality issues detected")

    return insights


def detect_recurring_failures() -> list[dict]:
    """Proxy to healing module's failure detection. Returns healing triggers."""
    try:
        from healing import detect_recurring_failures as _detect
        return _detect()
    except Exception as e:
        log.debug("Healing detection unavailable: %s", e)
        return []


def decay_preferences():
    """Monthly confidence decay — preferences not re-confirmed lose confidence."""
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    # Decrement confidence for stale preferences
    conn.execute(
        "UPDATE learned_preferences SET confidence = MAX(0, confidence - 0.1) WHERE updated_at < ?",
        (cutoff,),
    )
    # Delete dead preferences (confidence reached 0)
    conn.execute("DELETE FROM learned_preferences WHERE confidence <= 0")
    conn.commit()
    log.info("Preference confidence decay applied")
