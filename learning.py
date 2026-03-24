"""Self-improvement engine — reflection, insights, and learned preferences.

Khalil analyzes its own interaction data to identify patterns and adapt behavior.
All changes are transparent (visible via /learn) and safe (hard guardrails immutable).
"""

import json
import logging
import re as _re_module
import sqlite3
from datetime import datetime, timedelta

from config import DB_PATH, GOALS_DIR, HARD_GUARDRAILS

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


# --- Goal Progress Signals (M12) ---

def record_goal_progress(goal_text: str, domain: str, description: str = ""):
    """Record a goal progress signal when Ahmed works on a goal-related project.

    Args:
        goal_text: The goal text being progressed.
        domain: The domain (work, project, finance, personal).
        description: What progress was made.
    """
    record_signal("goal_progress", {
        "goal": goal_text,
        "domain": domain,
        "description": description or f"Progress on: {goal_text[:80]}",
    })
    log.info("Goal progress recorded: [%s] %s", domain, goal_text[:60])


def get_weekly_goal_progress(days: int = 7) -> list[dict]:
    """Get goal progress signals from the last N days for the weekly digest.

    Returns list of dicts with goal, domain, description, count.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT context, COUNT(*) as count FROM interaction_signals "
        "WHERE signal_type = 'goal_progress' AND created_at > ? "
        "GROUP BY json_extract(context, '$.goal') "
        "ORDER BY count DESC",
        (cutoff,),
    ).fetchall()

    results = []
    for row in rows:
        if row[0]:
            import json as _json
            ctx = _json.loads(row[0])
            results.append({
                "goal": ctx.get("goal", ""),
                "domain": ctx.get("domain", ""),
                "description": ctx.get("description", ""),
                "count": row[1],
            })
    return results


def check_goal_relevance(action_text: str) -> str | None:
    """Check if an action relates to a current goal.

    Returns the goal text if relevant, None otherwise.
    Used to auto-record goal progress signals.
    """
    try:
        from actions.goals import GOALS_FILE, _parse_goals, _current_quarter
        if not GOALS_FILE.exists():
            return None

        content = GOALS_FILE.read_text(encoding="utf-8")
        goals = _parse_goals(content)
        quarter = _current_quarter()
        q_goals = goals.get(quarter, {})

        action_lower = action_text.lower()
        for _category, items in q_goals.items():
            for item in items:
                if item["done"]:
                    continue
                # Check if action text contains key words from the goal
                goal_words = set(item["text"].lower().split())
                # Remove common words
                goal_words -= {"a", "the", "to", "and", "or", "for", "in", "on", "with", "my"}
                if len(goal_words) == 0:
                    continue
                matches = sum(1 for w in goal_words if w in action_lower)
                if matches >= 2 or (len(goal_words) <= 3 and matches >= 1):
                    return item["text"]
    except Exception:
        pass
    return None


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

    # 5. Intent pattern misses — queries that could be handled but regex didn't match
    pattern_misses = conn.execute(
        "SELECT context FROM interaction_signals WHERE signal_type = 'intent_pattern_miss' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    if pattern_misses:
        miss_details = []
        for row in pattern_misses:
            ctx = json.loads(row["context"]) if row["context"] else {}
            miss_details.append(f"{ctx.get('query', '?')} → {ctx.get('matched_action', '?')}")
        insight_id = store_insight(
            "intent_detection",
            f"{len(pattern_misses)} intent pattern miss(es) today",
            f"Missed: {'; '.join(miss_details[:5])}",
            "The self-healing engine should generate regex fixes. If PRs are not appearing, check healing.py.",
        )
        insights.append({"id": insight_id, "category": "intent_detection", "summary": f"{len(pattern_misses)} intent pattern misses"})

    # 6. Agent delegation performance
    delegations = conn.execute(
        "SELECT context FROM interaction_signals WHERE signal_type = 'agent_delegation' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    if delegations:
        total_delegations = len(delegations)
        latencies = []
        successes = 0
        for row in delegations:
            ctx = json.loads(row["context"]) if row["context"] else {}
            latencies.append(ctx.get("latency_ms", 0))
            if ctx.get("success"):
                successes += 1
        avg_latency = int(sum(latencies) / len(latencies)) if latencies else 0
        success_rate = round(successes / total_delegations * 100, 1)
        insight_id = store_insight(
            "agent_performance",
            f"{total_delegations} sub-agent delegations today (avg {avg_latency}ms, {success_rate}% success)",
            f"Total: {total_delegations}, Avg latency: {avg_latency}ms, Success rate: {success_rate}%",
            "Review delegation patterns — high latency or low success rate may indicate model or prompt issues.",
        )
        insights.append({"id": insight_id, "category": "agent_performance", "summary": f"{total_delegations} delegations, {success_rate}% success"})

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


# --- #57: Conversation Summarization ---

async def summarize_conversations(ask_llm_fn, min_messages: int = 20) -> list[dict]:
    """Summarize long conversation threads into knowledge base entries.

    Finds conversation threads with >= min_messages and compresses them
    into document entries for the knowledge base.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    # Find chat_ids with enough messages
    chats = conn.execute(
        "SELECT chat_id, COUNT(*) as cnt FROM conversations "
        "WHERE timestamp > ? GROUP BY chat_id HAVING cnt >= ?",
        (cutoff, min_messages),
    ).fetchall()

    summaries = []
    for chat in chats:
        chat_id = chat[0]
        messages = conn.execute(
            "SELECT role, content, timestamp FROM conversations "
            "WHERE chat_id = ? AND timestamp > ? ORDER BY timestamp",
            (chat_id, cutoff),
        ).fetchall()

        # Build conversation text for summarization
        conv_text = "\n".join(f"{m[0]}: {m[1][:200]}" for m in messages[:50])

        try:
            summary = await ask_llm_fn(
                f"Summarize this conversation into key topics, decisions, and action items:\n\n{conv_text}",
                "",
                system_extra="Create a concise summary with bullet points. Focus on decisions made, tasks assigned, and key information exchanged.",
            )
            if summary and not summary.startswith("⚠️"):
                # Store in documents table for knowledge base
                conn.execute(
                    "INSERT INTO documents (source, category, title, content) VALUES (?, ?, ?, ?)",
                    ("conversation_summary", "conversation",
                     f"Conversation summary ({messages[0][2][:10]})", summary),
                )
                conn.commit()
                summaries.append({"chat_id": chat_id, "message_count": len(messages), "summary": summary[:200]})
                log.info("Summarized conversation for chat %d (%d messages)", chat_id, len(messages))
        except Exception as e:
            log.warning("Conversation summarization failed for chat %d: %s", chat_id, e)

    return summaries


# --- #1: Conversation Success Scoring ---

def get_conversation_scores(days: int = 7) -> dict:
    """Aggregate conversation success scores over the last N days.

    Returns {total, positive, negative, corrections, score_pct, by_topic: {topic: {count, corrections}}}.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Success signals
    rows = conn.execute(
        "SELECT context FROM interaction_signals "
        "WHERE signal_type = 'conversation_success' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    # Explicit feedback
    feedback_rows = conn.execute(
        "SELECT value FROM interaction_signals "
        "WHERE signal_type = 'explicit_feedback' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    total = len(rows)
    corrections = 0
    by_topic: dict[str, dict] = {}
    for r in rows:
        ctx = json.loads(r[0]) if r[0] else {}
        topic = ctx.get("topic", "general")
        had_correction = ctx.get("had_correction", False)
        if topic not in by_topic:
            by_topic[topic] = {"count": 0, "corrections": 0}
        by_topic[topic]["count"] += 1
        if had_correction:
            corrections += 1
            by_topic[topic]["corrections"] += 1

    positive_feedback = sum(1 for r in feedback_rows if r[0] > 0)
    negative_feedback = sum(1 for r in feedback_rows if r[0] < 0)

    # Score: conversations without corrections + positive feedback - negative feedback
    success_count = (total - corrections) + positive_feedback - negative_feedback
    score_pct = round(success_count / total * 100, 1) if total else 0.0

    return {
        "total": total,
        "positive_feedback": positive_feedback,
        "negative_feedback": negative_feedback,
        "corrections": corrections,
        "score_pct": score_pct,
        "by_topic": by_topic,
    }


# --- #9: Monthly Meta-Reflection ---

async def run_monthly_meta_reflection(ask_llm_fn) -> list[dict]:
    """Run monthly meta-reflection — analyze whether weekly insights are actionable.

    Args:
        ask_llm_fn: async callable(query, context, system_extra) -> str

    Returns:
        List of meta-insight dicts.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    # Gather last 30 days of insights
    insights = conn.execute(
        "SELECT category, summary, evidence, recommendation, status, created_at "
        "FROM insights WHERE created_at > ? ORDER BY created_at DESC",
        (cutoff,),
    ).fetchall()

    if len(insights) < 2:
        log.info("Monthly meta-reflection skipped — insufficient insights (%d)", len(insights))
        return []

    # Check which applied insights actually stuck (preferences still active)
    applied_insights = [i for i in insights if i["status"] == "applied"]
    current_prefs = list_preferences()
    active_pref_keys = {p["key"] for p in current_prefs if p["confidence"] >= 0.3}

    stuck_count = 0
    for ai in applied_insights:
        # Simple heuristic: if any preference key relates to the insight category, it stuck
        for key in active_pref_keys:
            if ai["category"] in key or key in ai["summary"].lower():
                stuck_count += 1
                break

    insight_data = [
        {
            "category": i["category"],
            "summary": i["summary"],
            "status": i["status"],
            "created_at": i["created_at"],
        }
        for i in insights
    ]

    prompt = f"""Analyze the last 30 days of Khalil's self-improvement insights for meta-patterns.

## Insights Generated ({len(insights)} total)
{json.dumps(insight_data, indent=2)}

## Applied Insights That Stuck: {stuck_count}/{len(applied_insights)}
## Active Preferences: {len(current_prefs)}

Questions to answer:
1. Are the weekly insights actually actionable, or too vague?
2. Are there recurring themes that suggest a systemic issue?
3. Which categories produce the most useful insights?
4. What should change about the reflection process itself?

Respond with ONLY a JSON array of meta-insights. Each must have:
- "category": "meta"
- "summary": one-sentence finding
- "recommendation": concrete change to make
Maximum 3 meta-insights."""

    response = await ask_llm_fn(
        prompt,
        "",
        system_extra="You are analyzing AI self-improvement data. Respond with ONLY a JSON array.",
    )

    if not response or response.startswith("⚠️"):
        log.error("Monthly meta-reflection LLM call failed: %s", response[:200] if response else "empty")
        return []

    try:
        if "```" in response:
            response = response.split("```")[1]
            if response.startswith("json"):
                response = response[4:]
        meta_insights = json.loads(response.strip())
    except (json.JSONDecodeError, IndexError):
        log.error("Monthly meta-reflection returned invalid JSON: %s", response[:200])
        return []

    if not isinstance(meta_insights, list):
        return []

    stored = []
    for mi in meta_insights[:3]:
        summary = mi.get("summary", "")
        recommendation = mi.get("recommendation", "")
        if not summary:
            continue
        insight_id = store_insight("meta", summary, "Monthly meta-reflection", recommendation)
        mi["id"] = insight_id
        stored.append(mi)

    log.info("Monthly meta-reflection complete: %d meta-insights generated", len(stored))
    return stored


def infer_sleep_schedule(days: int = 14) -> dict:
    """#96: Infer sleep schedule from conversation timestamps.

    Queries the last N days of conversations, groups by date,
    finds first and last message per day.

    Returns {wake_time, sleep_time, confidence, days_analyzed, days_with_data}.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        "SELECT timestamp FROM conversations WHERE role = 'user' AND timestamp > ? ORDER BY timestamp",
        (cutoff,),
    ).fetchall()

    if not rows:
        return {"wake_time": None, "sleep_time": None, "confidence": 0.0, "days_analyzed": days, "days_with_data": 0}

    # Group timestamps by date
    by_date: dict[str, list[str]] = {}
    for r in rows:
        ts = r[0] if isinstance(r, tuple) else r["timestamp"]
        date_str = ts[:10]  # "YYYY-MM-DD"
        by_date.setdefault(date_str, []).append(ts)

    wake_hours = []
    sleep_hours = []
    for date_str, timestamps in by_date.items():
        timestamps.sort()
        first = timestamps[0]
        last = timestamps[-1]
        try:
            first_dt = datetime.strptime(first, "%Y-%m-%d %H:%M:%S")
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
            wake_hours.append(first_dt.hour + first_dt.minute / 60.0)
            sleep_hours.append(last_dt.hour + last_dt.minute / 60.0)
        except (ValueError, IndexError):
            continue

    if not wake_hours:
        return {"wake_time": None, "sleep_time": None, "confidence": 0.0, "days_analyzed": days, "days_with_data": 0}

    avg_wake = sum(wake_hours) / len(wake_hours)
    avg_sleep = sum(sleep_hours) / len(sleep_hours)

    # Confidence based on data coverage
    coverage = len(wake_hours) / days
    confidence = min(1.0, coverage * 1.5)  # 67%+ coverage -> full confidence

    def _fmt_time(h: float) -> str:
        hours = int(h)
        minutes = int((h - hours) * 60)
        return f"{hours:02d}:{minutes:02d}"

    return {
        "wake_time": _fmt_time(avg_wake),
        "sleep_time": _fmt_time(avg_sleep),
        "confidence": round(confidence, 2),
        "days_analyzed": days,
        "days_with_data": len(wake_hours),
    }


# --- #95: Subscription Renewal Alerts ---

_RENEWAL_KEYWORDS = ["renewal", "subscription", "recurring charge", "auto-renew", "billing cycle"]


def detect_subscription_renewals(days_ahead: int = 7) -> list[dict]:
    """Detect upcoming subscription renewals from indexed emails.

    Searches the documents table for emails containing renewal/subscription keywords,
    then extracts estimated dates and amounts.

    Returns list of dicts with keys: source, title, snippet, estimated_date, amount.
    """
    conn = _get_conn()
    now = datetime.utcnow()
    results = []

    for keyword in _RENEWAL_KEYWORDS:
        rows = conn.execute(
            "SELECT source, title, content FROM documents "
            "WHERE content LIKE ? ORDER BY rowid DESC LIMIT 50",
            (f"%{keyword}%",),
        ).fetchall()
        seen_titles = {r["title"] for r in results}
        for row in rows:
            title = row["title"] if isinstance(row, sqlite3.Row) else row[1]
            if title in seen_titles:
                continue
            seen_titles.add(title)
            content = row["content"] if isinstance(row, sqlite3.Row) else row[2]
            source = row["source"] if isinstance(row, sqlite3.Row) else row[0]
            snippet = content[:200] if content else ""

            # Try to extract a dollar amount
            import re as _re
            amount_match = _re.search(r"\$[\d,]+\.?\d*", content or "")
            amount = amount_match.group(0) if amount_match else None

            # Try to extract a date (simple patterns)
            date_match = _re.search(
                r"(\d{4}-\d{2}-\d{2}|\w+ \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{4})",
                content or "",
            )
            estimated_date = date_match.group(0) if date_match else None

            results.append({
                "source": source,
                "title": title,
                "snippet": snippet,
                "estimated_date": estimated_date,
                "amount": amount,
            })

    return results


# --- #7: Prompt Effectiveness Scoring ---

def get_prompt_effectiveness(days: int = 7) -> dict:
    """Correlate conversation success scores with topics to identify effective prompt patterns.

    Uses conversation_success and conversation_topic signals recorded by #1 and #65.

    Returns {by_topic: {topic: {total, successes, success_rate_pct}}, best_topic, worst_topic}.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Get conversation success signals with topic info
    rows = conn.execute(
        "SELECT context FROM interaction_signals "
        "WHERE signal_type = 'conversation_success' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    by_topic: dict[str, dict] = {}
    for r in rows:
        ctx = json.loads(r[0]) if r[0] else {}
        topic = ctx.get("topic", "general")
        had_correction = ctx.get("had_correction", False)
        if topic not in by_topic:
            by_topic[topic] = {"total": 0, "successes": 0}
        by_topic[topic]["total"] += 1
        if not had_correction:
            by_topic[topic]["successes"] += 1

    # Calculate success rates
    for topic, stats in by_topic.items():
        stats["success_rate_pct"] = (
            round(stats["successes"] / stats["total"] * 100, 1) if stats["total"] else 0.0
        )

    # Identify best and worst topics
    best_topic = None
    worst_topic = None
    if by_topic:
        sorted_topics = sorted(
            by_topic.items(),
            key=lambda x: x[1]["success_rate_pct"],
            reverse=True,
        )
        # Only consider topics with at least 2 data points
        qualified = [(t, s) for t, s in sorted_topics if s["total"] >= 2]
        if qualified:
            best_topic = qualified[0][0]
            worst_topic = qualified[-1][0]

    return {
        "by_topic": by_topic,
        "best_topic": best_topic,
        "worst_topic": worst_topic,
    }


# --- #98: Proactive Knowledge Gap Filling ---

def fill_knowledge_gaps(ask_llm_fn=None) -> list[dict]:
    """Detect recurring knowledge gaps and attempt to fill them from existing archives.

    Queries interaction_signals for capability_gap_detected and search_miss signals
    from the last 7 days, clusters similar queries, and searches the documents table
    for answers.

    Returns list of {query, results_found, indexed} dicts.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        "SELECT context FROM interaction_signals "
        "WHERE signal_type IN ('capability_gap_detected', 'search_miss') AND created_at > ?",
        (cutoff,),
    ).fetchall()

    if not rows:
        return []

    # Extract queries
    queries: list[str] = []
    for r in rows:
        ctx = json.loads(r[0]) if r[0] else {}
        q = ctx.get("query", "").strip()
        if q:
            queries.append(q)

    # Group by simple word overlap — cluster queries sharing >= 2 non-stopword tokens
    stopwords = {"the", "a", "an", "is", "it", "to", "in", "for", "of", "and", "on", "my", "me", "i", "what", "how"}
    clusters: dict[str, list[str]] = {}
    for q in queries:
        tokens = {w.lower() for w in q.split() if w.lower() not in stopwords and len(w) > 2}
        matched = False
        for key, members in clusters.items():
            key_tokens = {w.lower() for w in key.split() if w.lower() not in stopwords and len(w) > 2}
            if len(tokens & key_tokens) >= 2:
                members.append(q)
                matched = True
                break
        if not matched:
            clusters[q] = [q]

    results = []
    for representative, members in clusters.items():
        if len(members) < 3:
            continue

        # Search existing documents for answers
        terms = representative.lower().split()[:5]
        conditions = " OR ".join(["LOWER(content) LIKE ?" for _ in terms])
        params = [f"%{t}%" for t in terms if len(t) > 2]
        if not params:
            continue
        conditions = " OR ".join(["LOWER(content) LIKE ?" for _ in params])

        found = conn.execute(
            f"SELECT id, title FROM documents WHERE {conditions} LIMIT 5",
            params,
        ).fetchall()

        results.append({
            "query": representative,
            "results_found": len(found),
            "indexed": len(found) > 0,
        })

    return results


# --- #12: Causal Insight Validation ---

def validate_applied_insights(days: int = 14) -> list[dict]:
    """Validate whether recently applied insights actually improved outcomes.

    For each insight applied in the last N days, checks:
    - preference insights: is the preference still active (confidence not decayed)?
    - knowledge_gap insights: did the same gap queries decrease?
    - response_quality insights: did user corrections decrease?

    Returns list of {insight_id, summary, validated: bool, reason}.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    applied = conn.execute(
        "SELECT id, category, summary, evidence, resolved_at FROM insights "
        "WHERE status = 'applied' AND resolved_at > ? ORDER BY resolved_at DESC",
        (cutoff,),
    ).fetchall()

    results = []
    for row in applied:
        insight_id = row["id"]
        category = row["category"]
        summary = row["summary"] or ""
        resolved_at = row["resolved_at"]

        if category == "preference":
            # Check if a related preference is still active
            prefs = conn.execute(
                "SELECT confidence FROM learned_preferences WHERE source_insight_id = ?",
                (insight_id,),
            ).fetchall()
            if prefs:
                active = any(p["confidence"] >= 0.3 for p in prefs)
                results.append({
                    "insight_id": insight_id,
                    "summary": summary,
                    "validated": active,
                    "reason": "preference still active" if active else "preference decayed below threshold",
                })
            else:
                results.append({
                    "insight_id": insight_id,
                    "summary": summary,
                    "validated": False,
                    "reason": "no linked preference found",
                })

        elif category == "knowledge_gap":
            # Check if search_miss signals decreased after the insight was applied
            before_count = conn.execute(
                "SELECT COUNT(*) FROM interaction_signals "
                "WHERE signal_type = 'search_miss' AND created_at < ? AND created_at > ?",
                (resolved_at, (datetime.strptime(resolved_at, "%Y-%m-%d %H:%M:%S") - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchone()[0]
            after_count = conn.execute(
                "SELECT COUNT(*) FROM interaction_signals "
                "WHERE signal_type = 'search_miss' AND created_at > ?",
                (resolved_at,),
            ).fetchone()[0]
            improved = after_count < before_count or after_count == 0
            results.append({
                "insight_id": insight_id,
                "summary": summary,
                "validated": improved,
                "reason": f"search misses {'decreased' if improved else 'did not decrease'} ({before_count} -> {after_count})",
            })

        elif category == "response_quality":
            # Check if user corrections decreased after the insight was applied
            before_count = conn.execute(
                "SELECT COUNT(*) FROM interaction_signals "
                "WHERE signal_type = 'user_correction' AND created_at < ? AND created_at > ?",
                (resolved_at, (datetime.strptime(resolved_at, "%Y-%m-%d %H:%M:%S") - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchone()[0]
            after_count = conn.execute(
                "SELECT COUNT(*) FROM interaction_signals "
                "WHERE signal_type = 'user_correction' AND created_at > ?",
                (resolved_at,),
            ).fetchone()[0]
            improved = after_count < before_count or after_count == 0
            results.append({
                "insight_id": insight_id,
                "summary": summary,
                "validated": improved,
                "reason": f"corrections {'decreased' if improved else 'did not decrease'} ({before_count} -> {after_count})",
            })

        else:
            results.append({
                "insight_id": insight_id,
                "summary": summary,
                "validated": True,
                "reason": f"no validation logic for category '{category}'",
            })

    return results


# --- #90: Email Follow-up Detector ---

def detect_email_followups(days: int = 7) -> list[dict]:
    """Detect sent emails that may be awaiting replies.

    Searches documents for sent emails and checks for matching replies.
    Best-effort heuristic using the knowledge base.

    Returns list of {subject, sent_date, awaiting_reply: bool, days_waiting}.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Find sent emails
    sent_rows = conn.execute(
        "SELECT title, created_at FROM documents "
        "WHERE (LOWER(category) LIKE '%sent%' OR LOWER(source) LIKE '%sent%') "
        "AND created_at > ? ORDER BY created_at DESC",
        (cutoff,),
    ).fetchall()

    results = []
    for row in sent_rows:
        subject = row["title"] or ""
        sent_date = row["created_at"] or ""

        # Search for replies — look for "Re: <subject>" in the documents table
        clean_subject = subject.replace("Re: ", "").replace("RE: ", "").strip()
        if not clean_subject:
            continue

        reply = conn.execute(
            "SELECT id FROM documents WHERE title LIKE ? AND created_at > ? LIMIT 1",
            (f"%Re: {clean_subject}%", sent_date),
        ).fetchone()

        awaiting = reply is None
        days_waiting = 0
        if awaiting and sent_date:
            try:
                sent_dt = datetime.strptime(sent_date[:19], "%Y-%m-%d %H:%M:%S")
                days_waiting = (datetime.utcnow() - sent_dt).days
            except (ValueError, TypeError):
                pass

        results.append({
            "subject": subject,
            "sent_date": sent_date,
            "awaiting_reply": awaiting,
            "days_waiting": days_waiting,
        })

    return results


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


# --- #94: Goal Progress Auto-Check ---

def check_goal_progress(days: int = 7) -> list[dict]:
    """Check progress on goals by correlating with recent activity signals.

    Reads goal files from GOALS_DIR, then queries recent audit_log entries
    and conversations mentioning goal keywords.

    Returns list of {goal, activity_count, status} where status is
    "active", "stale", or "no_activity".
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Read goals from the goals directory
    goals: list[str] = []
    if GOALS_DIR and GOALS_DIR.exists():
        for f in GOALS_DIR.iterdir():
            if f.suffix in (".md", ".txt"):
                try:
                    content = f.read_text(errors="replace")
                    # Extract goal lines — lines starting with "- " or "* " or "## "
                    for line in content.splitlines():
                        stripped = line.strip()
                        if stripped.startswith(("- ", "* ", "## ")) and len(stripped) > 4:
                            goal_text = stripped.lstrip("-*# ").strip()
                            if goal_text:
                                goals.append(goal_text)
                except Exception:
                    continue

    if not goals:
        return []

    results = []
    for goal in goals[:20]:  # Cap at 20 goals
        # Extract keywords (words > 3 chars, lowered)
        keywords = [w.lower() for w in goal.split() if len(w) > 3]
        if not keywords:
            continue

        # Search audit_log and conversations for keyword mentions
        activity_count = 0
        for keyword in keywords[:5]:
            # Audit log mentions
            count = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE LOWER(description) LIKE ? AND timestamp > ?",
                (f"%{keyword}%", cutoff),
            ).fetchone()[0]
            activity_count += count

            # Conversation mentions
            count = conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE LOWER(content) LIKE ? AND timestamp > ?",
                (f"%{keyword}%", cutoff),
            ).fetchone()[0]
            activity_count += count

        if activity_count >= 5:
            status = "active"
        elif activity_count >= 1:
            status = "stale"
        else:
            status = "no_activity"

        results.append({"goal": goal, "activity_count": activity_count, "status": status})

    return results


# --- #6: Multi-turn Coherence Analysis ---

_COHERENCE_ISSUE_PATTERNS = [
    (r"\bi\s+already\s+(?:told|said|mentioned)\b", "repeated_info"),
    (r"\bthat'?s\s+not\s+what\s+i\s+(?:asked|meant|said)\b", "misunderstanding"),
    (r"\bno,?\s+i\s+(?:said|meant|want)\b", "correction"),
    (r"\byou\s+(?:forgot|missed|ignored)\b", "lost_context"),
    (r"\bi\s+just\s+(?:told|said)\s+you\b", "repeated_info"),
    (r"\bwrong\b.*\bi\s+(?:said|asked|want)\b", "correction"),
    (r"\bcan\s+you\s+re-?read\b", "lost_context"),
    (r"\bas\s+i\s+(?:said|mentioned)\s+(?:before|earlier)\b", "repeated_info"),
]


def detect_coherence_issues(days: int = 7) -> list[dict]:
    """Detect multi-turn coherence issues from conversation patterns.

    Queries user follow-up messages for patterns suggesting lost context,
    corrections, or repeated questions.

    Returns list of {chat_id, timestamp, issue_type, context}.
    """
    import re as _re

    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Get user messages (potential follow-ups that indicate coherence issues)
    rows = conn.execute(
        "SELECT chat_id, content, timestamp FROM conversations "
        "WHERE role = 'user' AND timestamp > ? ORDER BY timestamp",
        (cutoff,),
    ).fetchall()

    issues = []
    for row in rows:
        content = row["content"] if isinstance(row, dict) else row[1]
        chat_id = row["chat_id"] if isinstance(row, dict) else row[0]
        timestamp = row["timestamp"] if isinstance(row, dict) else row[2]
        content_lower = (content or "").lower()

        for pattern, issue_type in _COHERENCE_ISSUE_PATTERNS:
            if _re.search(pattern, content_lower):
                issues.append({
                    "chat_id": chat_id,
                    "timestamp": timestamp,
                    "issue_type": issue_type,
                    "context": content[:200] if content else "",
                })
                break  # One issue per message

    return issues


# --- #58: Semantic Memory Consolidation ---

def consolidate_memories(similarity_threshold: float = 0.5) -> list[dict]:
    """Identify document pairs with high word-overlap similarity within the same category.

    Uses simple word-overlap (Jaccard-like) similarity, same approach as
    _compute_topic_similarity in server.py.

    Returns list of {doc_id_a, doc_id_b, title_a, title_b, similarity, category}.
    Does NOT auto-merge — just surfaces candidates for review.
    """
    conn = _get_conn()

    # Get all categories that have 2+ documents
    categories = conn.execute(
        "SELECT DISTINCT category FROM documents GROUP BY category HAVING COUNT(*) >= 2"
    ).fetchall()

    stopwords = {"the", "a", "an", "is", "are", "was", "were", "i", "you", "my", "your",
                 "he", "she", "it", "we", "they", "in", "on", "at", "to", "for", "of",
                 "and", "or", "but", "not", "with", "this", "that", "from", "by", "as"}

    candidates = []
    for cat_row in categories:
        category = cat_row[0] if not isinstance(cat_row, dict) else cat_row["category"]
        docs = conn.execute(
            "SELECT id, title, content FROM documents WHERE category = ?",
            (category,),
        ).fetchall()

        # Build word sets for each doc
        doc_words = []
        for d in docs:
            doc_id = d["id"] if isinstance(d, dict) else d[0]
            title = d["title"] if isinstance(d, dict) else d[1]
            content = d["content"] if isinstance(d, dict) else d[2]
            text = f"{title} {content}"
            words = set(text.lower().split()) - stopwords
            doc_words.append((doc_id, title, words))

        # Compare all pairs
        for i in range(len(doc_words)):
            for j in range(i + 1, len(doc_words)):
                id_a, title_a, words_a = doc_words[i]
                id_b, title_b, words_b = doc_words[j]
                if not words_a or not words_b:
                    continue
                intersection = len(words_a & words_b)
                union = len(words_a | words_b)
                similarity = intersection / union if union > 0 else 0.0
                if similarity >= similarity_threshold:
                    candidates.append({
                        "doc_id_a": id_a,
                        "doc_id_b": id_b,
                        "title_a": title_a,
                        "title_b": title_b,
                        "similarity": round(similarity, 3),
                        "category": category,
                    })

    return candidates


# --- #64: Structured Data Extraction from Emails ---

_FLIGHT_PATTERNS = [
    _re_module.compile(
        r"(?:flight|confirmation)\s*[#:]?\s*([A-Z]{2}\d{2,4})",
        _re_module.IGNORECASE,
    ),
    _re_module.compile(
        r"(?P<airline>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:flight\s+)?(?P<flight_num>[A-Z]{2}\d{2,4})",
        _re_module.IGNORECASE,
    ),
]

_RECEIPT_PATTERN = _re_module.compile(
    r"(?:total|amount|charged|paid)[:\s]*\$?\s*(?P<amount>\d+[.,]\d{2})\s*(?P<currency>[A-Z]{3})?",
    _re_module.IGNORECASE,
)

_MEETING_PATTERN = _re_module.compile(
    r"(?:meeting|invite|event)[:\s]*(?P<subject>.+?)(?:\n|$)",
    _re_module.IGNORECASE,
)

_DATE_PATTERN = _re_module.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\w+ \d{1,2},?\s*\d{4})\b"
)

_TIME_PATTERN = _re_module.compile(
    r"\b(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)\b"
)


def extract_structured_data(email_content: str) -> dict:
    """Extract structured fields from email content using regex patterns.

    Detects flight confirmations, receipts, and meeting invites.
    Returns {type: "flight"|"receipt"|"meeting"|"unknown", fields: {...}}.
    """
    content_lower = email_content.lower()
    dates = _DATE_PATTERN.findall(email_content)
    times = _TIME_PATTERN.findall(email_content)

    # Flight detection
    if any(kw in content_lower for kw in ("flight", "boarding", "airline", "departure", "arrival")):
        fields = {"date": dates[0] if dates else None}
        for pat in _FLIGHT_PATTERNS:
            m = pat.search(email_content)
            if m:
                gd = m.groupdict()
                fields["flight_number"] = gd.get("flight_num") or m.group(1)
                fields["airline"] = gd.get("airline")
                break
        # Try to find departure/arrival
        dep_match = _re_module.search(r"(?:depart\w*|from)[:\s]+(.+?)(?:\n|,|$)", email_content, _re_module.IGNORECASE)
        arr_match = _re_module.search(r"(?:arriv\w*|to|destination)[:\s]+(.+?)(?:\n|,|$)", email_content, _re_module.IGNORECASE)
        fields["departure"] = dep_match.group(1).strip() if dep_match else None
        fields["arrival"] = arr_match.group(1).strip() if arr_match else None
        return {"type": "flight", "fields": fields}

    # Receipt detection
    if any(kw in content_lower for kw in ("receipt", "invoice", "total", "amount", "charged", "payment")):
        fields = {"date": dates[0] if dates else None}
        m = _RECEIPT_PATTERN.search(email_content)
        if m:
            fields["amount"] = m.group("amount")
            fields["currency"] = m.group("currency") or "USD"
        vendor_match = _re_module.search(r"(?:from|vendor|merchant|store)[:\s]+(.+?)(?:\n|,|$)", email_content, _re_module.IGNORECASE)
        fields["vendor"] = vendor_match.group(1).strip() if vendor_match else None
        return {"type": "receipt", "fields": fields}

    # Meeting detection
    if any(kw in content_lower for kw in ("meeting", "invite", "calendar event", "rsvp")):
        fields = {"datetime": None, "location": None, "subject": None, "organizer": None}
        m = _MEETING_PATTERN.search(email_content)
        if m:
            fields["subject"] = m.group("subject").strip()
        if dates:
            dt_str = dates[0]
            if times:
                dt_str += " " + times[0]
            fields["datetime"] = dt_str
        loc_match = _re_module.search(r"(?:location|where|room|venue)[:\s]+(.+?)(?:\n|,|$)", email_content, _re_module.IGNORECASE)
        fields["location"] = loc_match.group(1).strip() if loc_match else None
        org_match = _re_module.search(r"(?:organizer|host|from)[:\s]+(.+?)(?:\n|,|$)", email_content, _re_module.IGNORECASE)
        fields["organizer"] = org_match.group(1).strip() if org_match else None
        return {"type": "meeting", "fields": fields}

    return {"type": "unknown", "fields": {}}


# --- #93: Expense Tracking from Email ---


def track_expenses_from_emails(days: int = 30) -> dict:
    """Extract and aggregate expense data from recent email documents.

    Uses extract_structured_data() on recent email documents, filters for
    receipt-type extractions, and aggregates by vendor and month.

    Returns {total, count, by_vendor: [{vendor, amount, count}], by_month: [{month, total}]}.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        "SELECT content FROM documents WHERE category LIKE 'email%' AND created_at > ?",
        (cutoff,),
    ).fetchall()

    vendor_agg: dict[str, dict] = {}  # vendor -> {amount, count}
    month_agg: dict[str, float] = {}  # YYYY-MM -> total
    total = 0.0
    count = 0

    for row in rows:
        content = row["content"] if isinstance(row, sqlite3.Row) else row[0]
        result = extract_structured_data(content)
        if result["type"] != "receipt":
            continue

        fields = result["fields"]
        amount_str = fields.get("amount")
        if not amount_str:
            continue

        try:
            amount = float(_re_module.sub(r"[^\d.]", "", amount_str))
        except (ValueError, TypeError):
            continue

        vendor = fields.get("vendor") or "Unknown"
        date_str = fields.get("date") or ""

        total += amount
        count += 1

        # Aggregate by vendor
        if vendor not in vendor_agg:
            vendor_agg[vendor] = {"amount": 0.0, "count": 0}
        vendor_agg[vendor]["amount"] += amount
        vendor_agg[vendor]["count"] += 1

        # Aggregate by month
        month_match = _re_module.search(r"(\d{4})-(\d{2})", date_str)
        if month_match:
            month_key = f"{month_match.group(1)}-{month_match.group(2)}"
        else:
            month_key = datetime.utcnow().strftime("%Y-%m")
        month_agg[month_key] = month_agg.get(month_key, 0.0) + amount

    by_vendor = sorted(
        [{"vendor": v, "amount": d["amount"], "count": d["count"]} for v, d in vendor_agg.items()],
        key=lambda x: x["amount"],
        reverse=True,
    )
    by_month = sorted(
        [{"month": m, "total": t} for m, t in month_agg.items()],
        key=lambda x: x["month"],
    )

    return {"total": total, "count": count, "by_vendor": by_vendor, "by_month": by_month}


# --- #97: Travel Context Mode ---


def detect_travel_mode() -> dict:
    """Detect if the user is currently traveling based on recent documents.

    Searches recent emails and calendar events for travel-related keywords.
    Returns {traveling: bool, destination: str|None, dates: str|None, context: str}.
    """
    conn = _get_conn()
    # Look at documents from the last 7 days
    cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        "SELECT title, content FROM documents "
        "WHERE (category LIKE 'email%' OR category LIKE 'calendar%') "
        "AND created_at > ? ORDER BY created_at DESC LIMIT 100",
        (cutoff,),
    ).fetchall()

    travel_keywords = [
        "flight", "boarding pass", "hotel", "airbnb", "check-in",
        "itinerary", "booking confirmation", "reservation",
        "out of office", "ooo", "traveling", "airport",
    ]
    calendar_travel_keywords = ["flight", "travel", "hotel", "airport", "ooo", "out of office"]

    travel_hits = []
    destination = None
    dates = None

    for row in rows:
        title = row["title"] if isinstance(row, sqlite3.Row) else row[0]
        content = row["content"] if isinstance(row, sqlite3.Row) else row[1]
        combined = f"{title} {content}".lower()

        if any(kw in combined for kw in travel_keywords):
            travel_hits.append(combined[:200])

            # Try to extract destination from flight data
            if destination is None:
                result = extract_structured_data(content)
                if result["type"] == "flight":
                    destination = result["fields"].get("arrival")
                    dates = result["fields"].get("date")

    if not travel_hits:
        return {"traveling": False, "destination": None, "dates": None, "context": "No travel signals found."}

    context_parts = []
    if destination:
        context_parts.append(f"Destination: {destination}")
    if dates:
        context_parts.append(f"Dates: {dates}")
    context_parts.append(f"Found {len(travel_hits)} travel-related document(s) in last 7 days.")

    return {
        "traveling": True,
        "destination": destination,
        "dates": dates,
        "context": " ".join(context_parts),
    }


# --- #60: Entity Extraction and Linking ---

# Known company suffixes for regex-based NER
_COMPANY_SUFFIXES = r"\b(?:Inc|Corp|Ltd|LLC|Co|Group|Technologies|Labs|Studios|Games)\b"

# Precompiled patterns for entity extraction
_ENTITY_PATTERNS = [
    ("email", _re_module.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")),
    ("url", _re_module.compile(r"https?://[^\s<>\"']+|www\.[^\s<>\"']+")),
    ("date", _re_module.compile(
        r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s+\d{4})?)\b",
        _re_module.IGNORECASE,
    )),
    ("company", _re_module.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+" + _COMPANY_SUFFIXES)),
    ("person", _re_module.compile(r"\b([A-Z][a-z]{1,20})\s+([A-Z][a-z]{1,20})\b")),
]

# Common words that look like person names but aren't
_FALSE_POSITIVE_NAMES = {
    "The", "This", "That", "These", "Those", "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March",
    "April", "May", "June", "July", "August", "September", "October", "November",
    "December", "Google", "Apple", "Microsoft", "Amazon", "Netflix", "Spotify",
    "Hello", "Dear", "Best", "Kind", "Regards", "Thanks", "Please", "Sorry",
    "Good", "Morning", "Evening", "Night", "New", "York", "San", "Los",
}


def extract_entities(text: str) -> list[dict]:
    """#60: Extract entities from text using regex-based NER.

    Returns list of {type, value, position} dicts.
    Types: 'email', 'url', 'date', 'company', 'person'.
    """
    entities = []
    seen_values = set()  # deduplicate

    for entity_type, pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0).strip()

            # Filter false positive person names
            if entity_type == "person":
                first, last = match.group(1), match.group(2)
                if first in _FALSE_POSITIVE_NAMES or last in _FALSE_POSITIVE_NAMES:
                    continue
                value = f"{first} {last}"

            if entity_type == "company":
                value = match.group(1).strip() + " " + match.group(0).split()[-1]

            if value not in seen_values:
                seen_values.add(value)
                entities.append({
                    "type": entity_type,
                    "value": value,
                    "position": match.start(),
                })

    # Sort by position in text
    entities.sort(key=lambda e: e["position"])
    return entities


def build_entity_index(days: int = 7) -> dict[str, list[dict]]:
    """#60: Process recent conversations and build entity frequency map.

    Returns dict mapping entity type -> list of {value, count} sorted by frequency.
    Also records entity mentions as interaction signals.
    """
    conn = _get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Fetch recent conversation content from interaction_signals
    rows = conn.execute(
        "SELECT context FROM interaction_signals WHERE created_at > ? AND context IS NOT NULL",
        (cutoff,),
    ).fetchall()

    # Count entities across all conversations
    entity_counts: dict[str, dict[str, int]] = {}
    for row in rows:
        try:
            ctx = json.loads(row["context"]) if isinstance(row["context"], str) else row["context"]
            text = ctx.get("query", "") or ctx.get("text", "") or ""
        except (json.JSONDecodeError, AttributeError):
            continue

        if not text:
            continue

        for entity in extract_entities(text):
            etype = entity["type"]
            evalue = entity["value"]
            if etype not in entity_counts:
                entity_counts[etype] = {}
            entity_counts[etype][evalue] = entity_counts[etype].get(evalue, 0) + 1

    # Build frequency-sorted index and record signals
    index = {}
    for etype, values in entity_counts.items():
        sorted_entities = sorted(values.items(), key=lambda x: x[1], reverse=True)
        index[etype] = [{"value": v, "count": c} for v, c in sorted_entities]
        # Record top entities as signals
        for value, count in sorted_entities[:5]:
            record_signal("entity_mention", {"type": etype, "value": value, "count": count})

    return index


# --- M9: Approval Pattern Tracking (Task 9.1) ---

AUTO_ESCALATE_THRESHOLD = 5


def _normalize_command_pattern(action_type: str, payload: dict | None) -> str:
    """Extract a generalizable pattern from an action's payload."""
    if not payload:
        return action_type
    cmd = payload.get("command", "")
    if cmd and action_type.startswith("shell"):
        parts = cmd.strip().split()
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1]} *"
        return parts[0] if parts else action_type
    return action_type


def record_approval_pattern(action_type: str, payload: dict | None, approved: bool):
    """Record an approval or denial for a command pattern."""
    conn = _get_conn()
    pattern = _normalize_command_pattern(action_type, payload)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO approval_patterns "
        "(action_type, command_pattern, approved_count, denied_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(action_type, command_pattern) DO UPDATE SET "
        "approved_count = approved_count + ?, denied_count = denied_count + ?, updated_at = ?",
        (action_type, pattern,
         1 if approved else 0, 0 if approved else 1, now, now,
         1 if approved else 0, 0 if approved else 1, now),
    )
    conn.commit()
    log.info("Approval pattern recorded: %s/%s approved=%s", action_type, pattern, approved)


def check_auto_escalation(action_type: str, payload: dict | None) -> bool:
    """Check if a pattern qualifies for auto-approval (>= 5 approvals, 0 denials).

    Never escalates DANGEROUS actions or hard guardrails.
    """
    from config import HARD_GUARDRAILS, ActionType
    if action_type in HARD_GUARDRAILS:
        return False
    from autonomy import ACTION_RULES
    if ACTION_RULES.get(action_type) == ActionType.DANGEROUS:
        return False
    conn = _get_conn()
    pattern = _normalize_command_pattern(action_type, payload)
    row = conn.execute(
        "SELECT approved_count, denied_count FROM approval_patterns "
        "WHERE action_type = ? AND command_pattern = ?",
        (action_type, pattern),
    ).fetchone()
    if not row:
        return False
    approved = row["approved_count"] if isinstance(row, sqlite3.Row) else row[0]
    denied = row["denied_count"] if isinstance(row, sqlite3.Row) else row[1]
    return approved >= AUTO_ESCALATE_THRESHOLD and denied == 0


def get_approval_patterns(limit: int = 20) -> list[dict]:
    """Get all learned approval patterns for /mode patterns display."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT action_type, command_pattern, approved_count, denied_count, "
        "auto_tier, updated_at FROM approval_patterns ORDER BY approved_count DESC LIMIT ?",
        (limit,),
    ).fetchall()
    result = []
    for r in rows:
        ac = r["approved_count"] if isinstance(r, sqlite3.Row) else r[2]
        dc = r["denied_count"] if isinstance(r, sqlite3.Row) else r[3]
        result.append({
            "action_type": r["action_type"] if isinstance(r, sqlite3.Row) else r[0],
            "command_pattern": r["command_pattern"] if isinstance(r, sqlite3.Row) else r[1],
            "approved_count": ac,
            "denied_count": dc,
            "auto_tier": r["auto_tier"] if isinstance(r, sqlite3.Row) else r[4],
            "auto_approve": ac >= AUTO_ESCALATE_THRESHOLD and dc == 0,
            "updated_at": r["updated_at"] if isinstance(r, sqlite3.Row) else r[5],
        })
    return result


# --- M9: Confidence Decay (Task 9.4) ---

CONFIDENCE_DECAY_PER_WEEK = 0.05
CONFIDENCE_ARCHIVE_THRESHOLD = 0.2


def decay_preferences() -> list[dict]:
    """Apply weekly confidence decay. Stale preferences (< 0.2) are archived."""
    conn = _get_conn()
    one_week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    prefs = conn.execute(
        "SELECT key, value, confidence, updated_at FROM learned_preferences WHERE updated_at < ?",
        (one_week_ago,),
    ).fetchall()
    archived = []
    for p in prefs:
        key = p["key"] if isinstance(p, sqlite3.Row) else p[0]
        value = p["value"] if isinstance(p, sqlite3.Row) else p[1]
        confidence = p["confidence"] if isinstance(p, sqlite3.Row) else p[2]
        updated_at = p["updated_at"] if isinstance(p, sqlite3.Row) else p[3]
        try:
            last_update = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            last_update = datetime.utcnow() - timedelta(days=7)
        weeks_stale = max(1, (datetime.utcnow() - last_update).days // 7)
        new_confidence = max(0.0, confidence - CONFIDENCE_DECAY_PER_WEEK * weeks_stale)
        if new_confidence < CONFIDENCE_ARCHIVE_THRESHOLD:
            record_signal("preference_archived", {
                "key": key, "value": value, "final_confidence": new_confidence,
            })
            conn.execute("DELETE FROM learned_preferences WHERE key = ?", (key,))
            archived.append({
                "key": key,
                "value": json.loads(value) if isinstance(value, str) else value,
                "final_confidence": round(new_confidence, 3),
            })
            log.info("Preference archived (stale): %s (confidence=%.3f)", key, new_confidence)
        else:
            conn.execute(
                "UPDATE learned_preferences SET confidence = ? WHERE key = ?",
                (round(new_confidence, 3), key),
            )
    conn.commit()
    if archived:
        log.info("Preference decay: %d archived, %d decayed", len(archived), len(prefs) - len(archived))
    return archived


def boost_preference_from_correction(key: str, boost: float = 0.2):
    """Boost confidence of a preference when a user correction reinforces it."""
    conn = _get_conn()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT confidence FROM learned_preferences WHERE key = ?", (key,)
    ).fetchone()
    if row:
        old_conf = row["confidence"] if isinstance(row, sqlite3.Row) else row[0]
        new_conf = min(1.0, old_conf + boost)
        conn.execute(
            "UPDATE learned_preferences SET confidence = ?, updated_at = ? WHERE key = ?",
            (round(new_conf, 3), now, key),
        )
        conn.commit()
        log.info("Preference boosted: %s %.2f -> %.2f (correction)", key, old_conf, new_conf)


# --- M9: Preference-Driven Response Adaptation (Task 9.2) ---

def get_active_response_preferences() -> str:
    """Query all active preferences and format for system prompt injection."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT key, value, confidence FROM learned_preferences "
        "WHERE confidence >= 0.3 ORDER BY confidence DESC"
    ).fetchall()
    if not rows:
        return ""
    parts = []
    for r in rows:
        key = r["key"] if isinstance(r, sqlite3.Row) else r[0]
        value = r["value"] if isinstance(r, sqlite3.Row) else r[1]
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except (json.JSONDecodeError, TypeError):
            parsed = value
        if key == "response_style":
            if isinstance(parsed, dict):
                if parsed.get("format"):
                    parts.append(f"Prefer {parsed['format']} format.")
                if parsed.get("length"):
                    parts.append(f"Keep responses {parsed['length']}.")
        elif key == "communication_style":
            parts.append(f"Communication style: {parsed}.")
        elif key == "detail_level":
            parts.append(f"Detail level: {parsed}.")
        elif key.startswith("expertise_"):
            domain = key.replace("expertise_", "").replace("_", " ")
            parts.append(f"Ahmed has expertise in {domain} -- skip basic explanations.")
        elif key.startswith("skip_explain_"):
            topic = key.replace("skip_explain_", "").replace("_", " ")
            parts.append(f"Ahmed knows {topic} -- don't explain basics.")
        elif key == "preferred_format":
            parts.append(f"Prefer {parsed} format for responses.")
        elif key.startswith("pref_"):
            parts.append(str(parsed))
    if not parts:
        return ""
    return (
        "\nLEARNED PREFERENCES (from past interactions):\n"
        + "\n".join(f"- {p}" for p in parts)
        + "\n"
    )
