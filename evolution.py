"""Agentic evolution engine — unified orchestrator for self-extension, self-healing, and learning.

Ties together extend.py (code generation), healing.py (failure patching), and learning.py
(signal tracking + insights) into a continuous evolution cycle:

    SENSE → GATHER → RANK → EXECUTE → VERIFY

Runs 4x/day via scheduler + opportunistically via agent loop when signals accumulate.
Post-interaction hooks record signals cheaply (no LLM) for the next cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from config import DB_PATH

log = logging.getLogger("khalil.evolution")

# --- Configuration ---

EVOLUTION_MAX_EXECUTIONS = 2       # Max PRs per cycle
EVOLUTION_SIGNAL_THRESHOLD = 5     # Min signals to trigger early cycle via agent loop
EVOLUTION_COOLDOWN_HOURS = 4       # Min hours between agent-loop-triggered cycles
CANDIDATE_MAX_FAILURES = 2         # Abandon after this many failed attempts

# Suboptimal response heuristics (no LLM)
_SEARCH_MISS_PHRASES = [
    "don't have information", "couldn't find", "not in my archives",
    "i don't have", "no data on", "outside my knowledge",
]
_MANUAL_ACTION_PHRASES = [
    "you could try", "try running", "you'll need to manually",
    "run this command", "you can use", "open it yourself",
]
_CORRECTION_STARTERS = {"no", "actually", "i meant", "not that", "wrong", "nope"}


# --- Data Model ---

@dataclass
class EvolutionCandidate:
    id: str
    source: str          # "learning" | "healing" | "post_interaction" | "proactive"
    category: str        # "fix" | "extend" | "improve" | "integrate"
    summary: str
    evidence: list[dict] = field(default_factory=list)
    impact_score: float = 0.5
    feasibility_score: float = 0.5
    priority: float = 0.25
    status: str = "pending"
    result: str = ""
    pr_url: str = ""
    failure_count: int = 0
    created_at: str = ""
    merged_at: str = ""      # #13: MTTR tracking — when PR was merged
    verified_at: str = ""    # #13: MTTR tracking — when fix was verified

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        self.priority = self.impact_score * self.feasibility_score


@dataclass
class EvolutionCycleResult:
    candidates_found: int = 0
    executed: int = 0
    queued: int = 0
    skipped: int = 0
    prs_created: list[str] = field(default_factory=list)


# --- DB Helpers ---

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_evolution_table():
    """Create the evolution_candidates table if it doesn't exist."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evolution_candidates (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            summary TEXT NOT NULL,
            evidence TEXT,
            impact_score REAL DEFAULT 0.5,
            feasibility_score REAL DEFAULT 0.5,
            priority REAL DEFAULT 0.25,
            status TEXT DEFAULT 'pending',
            result TEXT DEFAULT '',
            failure_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            executed_at TEXT,
            pr_url TEXT DEFAULT '',
            merged_at TEXT,
            verified_at TEXT
        )
    """)
    # Migrate existing tables: add columns if missing
    try:
        conn.execute("ALTER TABLE evolution_candidates ADD COLUMN merged_at TEXT")
    except Exception:
        pass  # Column already exists
    try:
        conn.execute("ALTER TABLE evolution_candidates ADD COLUMN verified_at TEXT")
    except Exception:
        pass  # Column already exists
    conn.commit()
    conn.close()


def save_candidate(c: EvolutionCandidate):
    """Upsert a candidate."""
    ensure_evolution_table()
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO evolution_candidates "
        "(id, source, category, summary, evidence, impact_score, feasibility_score, "
        "priority, status, result, failure_count, created_at, pr_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (c.id, c.source, c.category, c.summary, json.dumps(c.evidence),
         c.impact_score, c.feasibility_score, c.priority, c.status,
         c.result, c.failure_count, c.created_at, c.pr_url),
    )
    conn.commit()
    conn.close()


def load_pending_candidates() -> list[EvolutionCandidate]:
    """Load all pending candidates."""
    ensure_evolution_table()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM evolution_candidates WHERE status = 'pending' ORDER BY priority DESC"
    ).fetchall()
    conn.close()
    return [_row_to_candidate(r) for r in rows]


def _row_to_candidate(row) -> EvolutionCandidate:
    return EvolutionCandidate(
        id=row["id"], source=row["source"], category=row["category"],
        summary=row["summary"],
        evidence=json.loads(row["evidence"]) if row["evidence"] else [],
        impact_score=row["impact_score"], feasibility_score=row["feasibility_score"],
        priority=row["priority"], status=row["status"],
        result=row["result"] or "", pr_url=row["pr_url"] or "",
        failure_count=row["failure_count"], created_at=row["created_at"],
    )


def _candidate_exists(candidate_id: str) -> bool:
    ensure_evolution_table()
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM evolution_candidates WHERE id = ? AND status NOT IN ('completed', 'abandoned')",
        (candidate_id,),
    ).fetchone()
    conn.close()
    return row is not None


# --- Signal Counting (for agent loop sensor) ---

def count_pending_signals() -> int:
    """Count evolution-relevant signals since last cycle. Cheap — no LLM."""
    try:
        last = get_last_cycle_time()
        cutoff = last or (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        conn = _get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM interaction_signals "
            "WHERE signal_type IN ("
            "'user_correction', 'search_miss', 'capability_gap_detected', "
            "'response_suggests_manual_action', 'intent_detection_failure', "
            "'action_execution_failure', 'slow_response', 'extension_runtime_failure', "
            "'tool_result_inadequate'"
            ") AND created_at > ?",
            (cutoff,),
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


def get_last_cycle_time() -> str | None:
    """Get timestamp of last evolution cycle run."""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'last_evolution_cycle'"
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _record_cycle_time():
    """Record that an evolution cycle just ran."""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('last_evolution_cycle', ?)",
        (now,),
    )
    conn.commit()
    conn.close()


# --- Signal Gathering ---

def gather_evolution_signals(hours: int = 6) -> list[EvolutionCandidate]:
    """Aggregate all signal sources into evolution candidates.

    Sources:
    1. learning.get_insights(status="pending") — unresolved insights
    2. healing.detect_recurring_failures() — failure patterns
    3. _detect_suboptimal_responses() — corrections, retries, slow responses
    4. _detect_usage_opportunities() — action pairs frequently used together
    """
    candidates = []

    # 1. Pending insights from learning
    try:
        from learning import get_insights
        for insight in get_insights(status="pending", limit=20):
            cid = f"insight_{insight['id']}"
            if _candidate_exists(cid):
                continue
            cat = "extend" if insight.get("category") in ("capability_gap", "knowledge_gap") else "improve"
            candidates.append(EvolutionCandidate(
                id=cid, source="learning", category=cat,
                summary=insight.get("summary", ""),
                evidence=[{"insight_id": insight["id"], "recommendation": insight.get("recommendation", "")}],
                impact_score=0.6, feasibility_score=0.5,
            ))
    except Exception as e:
        log.debug("gather: insights failed: %s", e)

    # 2. Recurring failures from healing
    try:
        from healing import detect_recurring_failures
        for trigger in detect_recurring_failures():
            cid = f"heal_{trigger['fingerprint']}"
            if _candidate_exists(cid):
                continue
            candidates.append(EvolutionCandidate(
                id=cid, source="healing", category="fix",
                summary=f"Recurring failure: {trigger['fingerprint']} ({trigger['failure_count']}x)",
                evidence=trigger.get("sample_signals", [])[:3],
                impact_score=min(0.3 + trigger["failure_count"] * 0.1, 1.0),
                feasibility_score=0.7,
            ))
    except Exception as e:
        log.debug("gather: healing failed: %s", e)

    # 3. Suboptimal responses
    candidates.extend(_detect_suboptimal_responses(hours=max(hours, 24)))

    # 4. Usage opportunities
    candidates.extend(_detect_usage_opportunities(days=7))

    return candidates


def _detect_suboptimal_responses(hours: int = 24) -> list[EvolutionCandidate]:
    """Find user corrections, repeated queries, slow responses."""
    candidates = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    try:
        conn = _get_conn()

        # User corrections
        rows = conn.execute(
            "SELECT context, created_at FROM interaction_signals "
            "WHERE signal_type = 'user_correction' AND created_at > ? ORDER BY created_at DESC LIMIT 10",
            (cutoff,),
        ).fetchall()
        if len(rows) >= 2:
            cid = f"corrections_{hashlib.md5(cutoff.encode()).hexdigest()[:8]}"
            if not _candidate_exists(cid):
                candidates.append(EvolutionCandidate(
                    id=cid, source="post_interaction", category="improve",
                    summary=f"User corrected Khalil {len(rows)}x in last {hours}h — response quality issue",
                    evidence=[json.loads(r["context"]) for r in rows if r["context"]],
                    impact_score=0.7, feasibility_score=0.4,
                ))

        # Slow responses
        rows = conn.execute(
            "SELECT context FROM interaction_signals "
            "WHERE signal_type = 'response_latency' AND created_at > ? "
            "AND json_extract(context, '$.latency_ms') > 5000 LIMIT 10",
            (cutoff,),
        ).fetchall()
        if len(rows) >= 3:
            cid = f"slow_{hashlib.md5(cutoff.encode()).hexdigest()[:8]}"
            if not _candidate_exists(cid):
                candidates.append(EvolutionCandidate(
                    id=cid, source="proactive", category="improve",
                    summary=f"{len(rows)} slow responses (>5s) in last {hours}h",
                    evidence=[json.loads(r["context"]) for r in rows if r["context"]],
                    impact_score=0.5, feasibility_score=0.3,
                ))

        # Search misses
        rows = conn.execute(
            "SELECT context FROM interaction_signals "
            "WHERE signal_type = 'search_miss' AND created_at > ? LIMIT 10",
            (cutoff,),
        ).fetchall()
        if len(rows) >= 2:
            cid = f"search_miss_{hashlib.md5(cutoff.encode()).hexdigest()[:8]}"
            if not _candidate_exists(cid):
                candidates.append(EvolutionCandidate(
                    id=cid, source="post_interaction", category="extend",
                    summary=f"{len(rows)} search misses in last {hours}h — knowledge gap",
                    evidence=[json.loads(r["context"]) for r in rows if r["context"]],
                    impact_score=0.6, feasibility_score=0.5,
                ))

        # Capability gaps (from post_interaction_check gap tag extraction + regex fallback)
        rows = conn.execute(
            "SELECT context FROM interaction_signals "
            "WHERE signal_type = 'capability_gap_detected' AND created_at > ? LIMIT 10",
            (cutoff,),
        ).fetchall()
        for row in rows:
            ctx = json.loads(row["context"]) if row["context"] else {}
            gap_name = ctx.get("gap_name", "unknown")
            cid = f"gap_{gap_name}"
            if not _candidate_exists(cid):
                candidates.append(EvolutionCandidate(
                    id=cid, source="post_interaction", category="extend",
                    summary=f"Capability gap: {ctx.get('gap_description', gap_name)}",
                    evidence=[ctx],
                    impact_score=0.8, feasibility_score=0.6,
                ))

        # Tool result inadequacy (tools ran but results were poor)
        rows = conn.execute(
            "SELECT context FROM interaction_signals "
            "WHERE signal_type = 'tool_result_inadequate' AND created_at > ? LIMIT 10",
            (cutoff,),
        ).fetchall()
        if rows:
            # Group by issue type
            issues: dict[str, list] = {}
            for row in rows:
                ctx = json.loads(row["context"]) if row["context"] else {}
                issue = ctx.get("issue", "unknown")
                issues.setdefault(issue, []).append(ctx)
            for issue, evidence_list in issues.items():
                cid = f"tool_inadequate_{issue}_{hashlib.md5(cutoff.encode()).hexdigest()[:8]}"
                if not _candidate_exists(cid):
                    candidates.append(EvolutionCandidate(
                        id=cid, source="post_interaction", category="improve",
                        summary=f"Tool produced inadequate results: {issue} ({len(evidence_list)}x)",
                        evidence=evidence_list[:3],
                        impact_score=0.8, feasibility_score=0.7,
                    ))

        conn.close()
    except Exception as e:
        log.debug("_detect_suboptimal_responses failed: %s", e)

    return candidates


def _detect_usage_opportunities(days: int = 7) -> list[EvolutionCandidate]:
    """Find action pairs frequently used together that could be combined."""
    candidates = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    try:
        conn = _get_conn()
        # Find actions used within 2-minute windows
        rows = conn.execute(
            "SELECT signal_type, context, created_at FROM interaction_signals "
            "WHERE signal_type = 'capability_usage' AND created_at > ? ORDER BY created_at",
            (cutoff,),
        ).fetchall()

        if len(rows) < 10:
            conn.close()
            return candidates

        # Group into 2-minute windows and count action pairs
        pair_counts: dict[tuple[str, str], int] = {}
        for i, row in enumerate(rows):
            ctx = json.loads(row["context"]) if row["context"] else {}
            action_a = ctx.get("action", "")
            # Look ahead for actions within 2 minutes
            for j in range(i + 1, min(i + 5, len(rows))):
                ctx_b = json.loads(rows[j]["context"]) if rows[j]["context"] else {}
                action_b = ctx_b.get("action", "")
                if action_a and action_b and action_a != action_b:
                    pair = tuple(sorted([action_a, action_b]))
                    pair_counts[pair] = pair_counts.get(pair, 0) + 1

        # Flag pairs used together 5+ times
        for (a, b), count in sorted(pair_counts.items(), key=lambda x: -x[1]):
            if count < 5:
                break
            cid = f"pair_{a}_{b}"
            if not _candidate_exists(cid):
                candidates.append(EvolutionCandidate(
                    id=cid, source="proactive", category="integrate",
                    summary=f"Actions '{a}' and '{b}' used together {count}x — integration opportunity",
                    evidence=[{"action_a": a, "action_b": b, "count": count}],
                    impact_score=min(0.3 + count * 0.05, 0.8),
                    feasibility_score=0.4,
                ))
                if len(candidates) >= 3:
                    break

        conn.close()
    except Exception as e:
        log.debug("_detect_usage_opportunities failed: %s", e)

    return candidates


# --- Ranking ---

def rank_candidates(candidates: list[EvolutionCandidate]) -> list[EvolutionCandidate]:
    """Score and sort candidates by priority (impact x feasibility)."""
    for c in candidates:
        c.priority = c.impact_score * c.feasibility_score
    return sorted(candidates, key=lambda c: c.priority, reverse=True)


# --- Execution ---

async def execute_evolution_cycle(
    channel, chat_id: int, ask_llm, autonomy,
    max_executions: int = EVOLUTION_MAX_EXECUTIONS,
) -> EvolutionCycleResult:
    """Main evolution cycle: gather → rank → execute top candidates → notify."""
    result = EvolutionCycleResult()

    # Check outcomes from previous cycle first
    _check_evolution_outcomes()

    # Gather and rank
    candidates = gather_evolution_signals()
    candidates = rank_candidates(candidates)
    result.candidates_found = len(candidates)

    if not candidates:
        log.info("Evolution cycle: no candidates found")
        _record_cycle_time()
        return result

    # Save all candidates
    for c in candidates:
        save_candidate(c)

    # Execute top N
    executed = 0
    for c in candidates:
        if executed >= max_executions:
            result.queued += 1
            continue

        # Check rate limit (respect extend.py cooldown)
        try:
            from actions.extend import _last_generation_time, GENERATION_COOLDOWN_SECONDS
            if time.time() - _last_generation_time < GENERATION_COOLDOWN_SECONDS:
                log.info("Evolution: skipping %s — generation cooldown active", c.id)
                result.skipped += 1
                continue
        except Exception:
            pass

        log.info("Evolution: executing candidate %s (%s/%s)", c.id, c.category, c.source)
        pr_url = await _execute_candidate(c, channel, chat_id, ask_llm)
        if pr_url:
            c.status = "completed"
            c.pr_url = pr_url
            c.result = f"PR created: {pr_url}"
            result.prs_created.append(pr_url)
            executed += 1
        else:
            c.failure_count += 1
            if c.failure_count >= CANDIDATE_MAX_FAILURES:
                c.status = "abandoned"
                c.result = "Abandoned after repeated failures"
            else:
                c.status = "pending"  # retry next cycle
                c.result = f"Failed attempt {c.failure_count}"

        save_candidate(c)

    result.executed = executed
    _record_cycle_time()
    log.info(
        "Evolution cycle: %d candidates, %d executed, %d queued, %d skipped, %d PRs",
        result.candidates_found, result.executed, result.queued, result.skipped, len(result.prs_created),
    )
    return result


async def _execute_candidate(
    candidate: EvolutionCandidate, channel, chat_id: int, ask_llm,
) -> str | None:
    """Route candidate to the right execution engine. Returns PR URL or None."""

    if candidate.category == "fix":
        return await _execute_healing(candidate, channel, chat_id)

    if candidate.category in ("extend", "integrate"):
        return await _execute_extension(candidate, channel, chat_id)

    if candidate.category == "improve":
        # Performance improvements go through healing with a perf-focused framing
        return await _execute_healing(candidate, channel, chat_id)

    log.warning("Unknown candidate category: %s", candidate.category)
    return None


async def _execute_healing(candidate: EvolutionCandidate, channel, chat_id: int) -> str | None:
    """Route to healing pipeline."""
    try:
        from healing import detect_recurring_failures, run_self_healing

        # For healing-sourced candidates, re-detect and run
        triggers = detect_recurring_failures()
        if not triggers:
            log.info("Evolution: no healing triggers found for %s", candidate.id)
            return None

        # Find the matching trigger
        matching = [t for t in triggers if candidate.id == f"heal_{t['fingerprint']}"]
        if not matching:
            # Run all triggers if we can't match specifically
            matching = triggers[:1]

        await run_self_healing(matching, channel, chat_id)

        # run_self_healing creates PRs internally — check for new PRs
        # We can't easily get the URL back, so return a placeholder
        return f"healing:{candidate.id}"
    except Exception as e:
        log.error("Evolution healing execution failed: %s", e)
        return None


async def _execute_extension(candidate: EvolutionCandidate, channel, chat_id: int) -> str | None:
    """Route to extend pipeline."""
    try:
        from actions.extend import generate_and_pr

        # Build a spec from the candidate's evidence
        spec = {
            "name": candidate.id.replace("insight_", "").replace("pair_", ""),
            "description": candidate.summary,
            "command": "",  # extend.py will determine
        }

        # If this came from an insight, use its recommendation
        if candidate.evidence:
            rec = candidate.evidence[0].get("recommendation", "")
            if rec:
                spec["description"] += f" — {rec}"

        result = await generate_and_pr({"spec": spec})
        if result and "PR" in result:
            return result
        return None
    except Exception as e:
        log.error("Evolution extension execution failed: %s", e)
        return None


# --- Outcome Verification ---

def _check_evolution_outcomes():
    """Check if previously executed candidates had their PRs merged and signals improved.

    For each completed candidate with a pr_url:
    1. Check merge status via `gh pr view`
    2. Update merged_at timestamp if merged
    3. Record heal_verified or heal_failed signal
    """
    import subprocess as _sp

    try:
        ensure_evolution_table()
        conn = _get_conn()
        # Find completed candidates from last 7 days that haven't been verified yet
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        rows = conn.execute(
            "SELECT id, category, summary, pr_url FROM evolution_candidates "
            "WHERE status = 'completed' AND created_at > ? "
            "AND pr_url != '' AND (merged_at IS NULL OR merged_at = '')",
            (cutoff,),
        ).fetchall()
        conn.close()

        for row in rows:
            pr_url = row["pr_url"]
            if not pr_url:
                continue

            # Extract PR number from URL (e.g., https://github.com/user/repo/pull/123)
            pr_num = pr_url.rstrip("/").split("/")[-1]
            if not pr_num.isdigit():
                continue

            try:
                result = _sp.run(
                    ["gh", "pr", "view", pr_num, "--json", "state,mergedAt"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode != 0:
                    continue

                pr_data = json.loads(result.stdout)
                state = pr_data.get("state", "").upper()

                if state == "MERGED":
                    merged_at = pr_data.get("mergedAt", datetime.now(timezone.utc).isoformat())
                    # Update merged_at in DB
                    conn2 = _get_conn()
                    conn2.execute(
                        "UPDATE evolution_candidates SET merged_at = ? WHERE id = ?",
                        (merged_at, row["id"]),
                    )
                    conn2.commit()
                    conn2.close()

                    log.info("Heal verified: %s merged (%s)", row["id"], pr_url)
                    try:
                        from learning import record_signal
                        record_signal("heal_verified", {
                            "candidate_id": row["id"],
                            "pr_url": pr_url,
                            "category": row["category"],
                            "summary": row["summary"][:200],
                        })
                    except Exception:
                        pass

                elif state == "CLOSED":
                    # PR was closed without merge — heal failed
                    log.info("Heal rejected: %s closed without merge (%s)", row["id"], pr_url)
                    conn2 = _get_conn()
                    conn2.execute(
                        "UPDATE evolution_candidates SET status = 'failed', "
                        "merged_at = 'closed' WHERE id = ?",
                        (row["id"],),
                    )
                    conn2.commit()
                    conn2.close()

                    try:
                        from learning import record_signal
                        record_signal("heal_failed", {
                            "candidate_id": row["id"],
                            "pr_url": pr_url,
                            "reason": "pr_closed_without_merge",
                        })
                    except Exception:
                        pass

            except Exception as e:
                log.debug("PR check failed for %s: %s", pr_url, e)
                continue

    except Exception as e:
        log.debug("_check_evolution_outcomes failed: %s", e)


# --- Hallucination Detection ---

# Regex for extracting factual entities: numbers, dates, emails, URLs, proper nouns
_ENTITY_PATTERNS = [
    re.compile(r'\b\d{1,2}[:/]\d{2}\s*(?:AM|PM|am|pm)?\b'),  # times
    re.compile(r'\b\d{4}-\d{2}-\d{2}\b'),  # ISO dates
    re.compile(r'\b\d+(?:\.\d+)?%\b'),  # percentages
    re.compile(r'\$\d+(?:,\d{3})*(?:\.\d{2})?\b'),  # dollar amounts
    re.compile(r'\b\d+(?:,\d{3})+\b'),  # large numbers with commas
    re.compile(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b'),  # proper nouns (2+ words)
]


def _check_grounding(response: str, context_sources: list[str]) -> dict | None:
    """Check if factual entities in the response appear in the provided context.

    Returns {ratio, total, grounded, ungrounded} or None if no entities found.
    Pure string matching — no LLM calls.
    """
    if not response or len(response) < 30:
        return None

    # Build context corpus from all sources
    context_lower = " ".join(context_sources).lower() if context_sources else ""
    if not context_lower:
        return None

    # Extract entities from response
    entities = set()
    for pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(response):
            entity = match.group().strip()
            if len(entity) > 2:
                entities.add(entity)

    if not entities:
        return None

    # Check grounding
    grounded = 0
    ungrounded_list = []
    for entity in entities:
        if entity.lower() in context_lower:
            grounded += 1
        else:
            ungrounded_list.append(entity)

    total = len(entities)
    ratio = grounded / total if total > 0 else 1.0

    return {
        "ratio": round(ratio, 3),
        "total": total,
        "grounded": grounded,
        "ungrounded": ungrounded_list,
    }


# --- Post-Interaction Hook ---

async def post_interaction_check(
    query: str, response: str, latency_ms: float,
    *, gap_tags: list[tuple] | None = None,
    tool_results: list[str] | None = None,
):
    """Lightweight post-interaction signal recording. No LLM calls.

    Called as fire-and-forget after every non-trivial response.
    Records signals that the next evolution cycle will pick up.

    Args:
        gap_tags: Extracted [CAPABILITY_GAP] tuples (name, command, description)
                  from the raw LLM response, passed by server.py.
        tool_results: Raw tool result strings from tool-use path, for adequacy analysis.
    """
    try:
        from learning import record_signal, detect_search_miss

        # 1. Search miss detection (reuses existing learning.py logic)
        if detect_search_miss(response):
            record_signal("search_miss", {"query": query[:200], "response_snippet": response[:100]})

        # 2. Manual action suggestion detection
        resp_lower = response.lower()
        for phrase in _MANUAL_ACTION_PHRASES:
            if phrase in resp_lower:
                record_signal("response_suggests_manual_action", {
                    "query": query[:200], "phrase": phrase,
                })
                break

        # 3. Explicit capability gap tags (emitted by LLM)
        if gap_tags:
            for name, command, description in gap_tags:
                record_signal("capability_gap_detected", {
                    "query": query[:200],
                    "gap_name": name,
                    "gap_command": command,
                    "gap_description": description,
                    "source": "llm_tag",
                })

        # 4. Regex-based gap detection fallback (catches refusals without explicit tags)
        if not gap_tags:
            from actions.extend import detect_capability_gap
            if detect_capability_gap(response):
                record_signal("capability_gap_detected", {
                    "query": query[:200],
                    "response_snippet": response[:200],
                    "source": "regex_gate",
                })

        # 5. Tool result adequacy check — detect tools that ran but produced poor results
        if tool_results:
            _check_tool_result_adequacy(query, response, tool_results, record_signal)

        # 6. Hallucination detection — check if factual claims are grounded in context
        grounding = _check_grounding(response, tool_results or [])
        if grounding is not None:
            record_signal("grounding_check", {
                "grounding_ratio": grounding["ratio"],
                "entities_total": grounding["total"],
                "entities_grounded": grounding["grounded"],
                "ungrounded": grounding["ungrounded"][:3],  # sample
            })

        # 7. Implicit preference detection from user query
        try:
            from learning import detect_implicit_preferences
            implicit_prefs = detect_implicit_preferences(query)
            for pref in implicit_prefs:
                record_signal("implicit_preference", {
                    "key": pref["key"],
                    "value": pref["value"],
                    "query_snippet": query[:100],
                })
        except Exception:
            pass

        # 8. Latency already recorded by server.py, no need to duplicate

    except Exception as e:
        log.debug("post_interaction_check failed: %s", e)


# Patterns indicating a tool produced inadequate results
_INADEQUATE_RESULT_PATTERNS = [
    # High unmatched/failure ratios
    (r"(\d+)\s+unmatched", r"processed\s+(\d+)", "high_unmatched_ratio"),
    # Tool returned errors or empty results
    (r"command not found", None, "tool_command_not_found"),
    (r"no (?:results?|output|data|matches)", None, "tool_empty_result"),
    (r"0 (?:labeled|processed|matched|found)", None, "tool_zero_results"),
]

# Response phrases suggesting the LLM couldn't fulfill the request despite having tools
_TOOL_INSUFFICIENCY_PHRASES = [
    "i'll need to create",
    "let me build",
    "i'll set up a",
    "there isn't currently a way",
    "the current tool doesn't support",
    "this feature isn't available yet",
    "i don't have a way to",
]


def _check_tool_result_adequacy(
    query: str, response: str, tool_results: list[str], record_signal,
):
    """Detect when tools ran but results were inadequate for the user's request.

    This catches the gap between 'capability exists' and 'capability is sufficient' —
    e.g., email categorizer runs but only matches 16/50 emails, or a tool runs but
    can't do what the user actually asked (like archiving emails).
    """
    combined_results = "\n".join(tool_results).lower()
    resp_lower = response.lower()

    # Check for high unmatched ratios in tool output
    import re as _re
    processed_match = _re.search(r"processed\s+(\d+)", combined_results)
    unmatched_match = _re.search(r"(\d+)\s+unmatched", combined_results)
    if processed_match and unmatched_match:
        processed = int(processed_match.group(1))
        unmatched = int(unmatched_match.group(1))
        if processed > 0 and unmatched / processed > 0.5:
            record_signal("tool_result_inadequate", {
                "query": query[:200],
                "issue": "high_unmatched_ratio",
                "detail": f"{unmatched}/{processed} unmatched ({unmatched*100//processed}%)",
                "tool_output": combined_results[:300],
            })

    # Check for zero-result patterns
    if _re.search(r"\b0\s+(?:labeled|processed|matched|found)\b", combined_results):
        record_signal("tool_result_inadequate", {
            "query": query[:200],
            "issue": "zero_results",
            "tool_output": combined_results[:300],
        })

    # Check for command-not-found (tool confusion — tried shell instead of action)
    if "command not found" in combined_results:
        record_signal("tool_result_inadequate", {
            "query": query[:200],
            "issue": "tool_command_not_found",
            "tool_output": combined_results[:300],
        })

    # Check if the LLM's response suggests the tool was insufficient
    for phrase in _TOOL_INSUFFICIENCY_PHRASES:
        if phrase in resp_lower:
            record_signal("tool_result_inadequate", {
                "query": query[:200],
                "issue": "tool_insufficient_for_request",
                "phrase": phrase,
                "response_snippet": response[:200],
            })
            break
