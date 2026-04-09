"""Production metrics dashboard — computes industry-standard agent metrics from the live DB.

Pulls from existing tables (conversations, interaction_signals, insights, settings)
to measure task completion, tool reliability, coherence, and self-improvement.

Usage:
    python -m eval.metrics              # print current metrics
    python -m eval.metrics --json       # output as JSON
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


@dataclass
class MetricsSnapshot:
    """A point-in-time capture of production metrics."""
    timestamp: str
    # Task completion (GAIA / TheAgentCompany inspired)
    task_completion_rate: float | None = None
    total_plans: int = 0
    completed_plans: int = 0
    # Tool reliability (τ-bench inspired)
    tool_success_rate: float | None = None
    total_tool_calls: int = 0
    successful_tool_calls: int = 0
    # User corrections (custom)
    user_correction_rate: float | None = None
    total_interactions: int = 0
    user_corrections: int = 0
    # Self-healing (custom)
    self_heal_success_rate: float | None = None
    total_failures: int = 0
    auto_recovered: int = 0
    # Response latency
    latency_p50: float | None = None
    latency_p95: float | None = None
    # Error cascade rate (Microsoft taxonomy)
    error_cascade_rate: float | None = None
    cascaded_failures: int = 0
    # Conversation abandonment (UX research)
    abandonment_rate: float | None = None
    total_sessions: int = 0
    abandoned_sessions: int = 0
    # Per-tool accuracy breakdown (τ-bench inspired)
    per_tool_metrics: dict = field(default_factory=dict)  # {tool: {calls, failures, success_rate}}
    # Cost tracking
    cost_per_task_p50: float | None = None
    cost_per_task_p95: float | None = None
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    # MTTR (Mean Time To Recovery) — hours from failure detection to verified fix
    mttr_hours: float | None = None
    mttr_samples: int = 0
    # Hallucination / grounding
    grounding_ratio_avg: float | None = None
    grounding_checks: int = 0


def compute_metrics(db_path: str | None = None) -> MetricsSnapshot:
    """Compute all metrics from the production database."""
    if db_path is None:
        db_path = str(Path(__file__).resolve().parent.parent / "data" / "khalil.db")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    snapshot = MetricsSnapshot(timestamp=now.strftime("%Y%m%d_%H%M%S"))

    # --- Task Completion Rate ---
    # From active_plans / settings tracking
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'pending_daily_plan'"
        ).fetchone()
        if row:
            plans = json.loads(row["value"])
            snapshot.total_plans = len(plans)
        # Count completed plans from signals
        completed = conn.execute(
            "SELECT COUNT(*) as cnt FROM interaction_signals "
            "WHERE signal_type = 'plan_completed'"
        ).fetchone()
        snapshot.completed_plans = completed["cnt"] if completed else 0
        if snapshot.total_plans > 0:
            snapshot.task_completion_rate = snapshot.completed_plans / snapshot.total_plans
    except Exception:
        pass

    # --- Tool Success Rate ---
    # From conversations with message_type = 'tool_call' and 'tool_result'
    try:
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM conversations WHERE message_type = 'tool_call'"
        ).fetchone()
        snapshot.total_tool_calls = total["cnt"] if total else 0

        # Tool results that don't contain error indicators
        errors = conn.execute(
            "SELECT COUNT(*) as cnt FROM conversations "
            "WHERE message_type = 'tool_result' AND ("
            "  content LIKE '%error%' OR content LIKE '%Error%' "
            "  OR content LIKE '%traceback%' OR content LIKE '%failed%'"
            ")"
        ).fetchone()
        error_count = errors["cnt"] if errors else 0
        snapshot.successful_tool_calls = max(0, snapshot.total_tool_calls - error_count)
        if snapshot.total_tool_calls > 0:
            snapshot.tool_success_rate = snapshot.successful_tool_calls / snapshot.total_tool_calls
    except Exception:
        pass

    # --- User Correction Rate ---
    # Signals where user corrected Khalil
    try:
        total_msg = conn.execute(
            "SELECT COUNT(*) as cnt FROM conversations WHERE role = 'user'"
        ).fetchone()
        snapshot.total_interactions = total_msg["cnt"] if total_msg else 0

        corrections = conn.execute(
            "SELECT COUNT(*) as cnt FROM interaction_signals "
            "WHERE signal_type IN ('user_correction', 'response_preference') "
            "AND value < 0"
        ).fetchone()
        snapshot.user_corrections = corrections["cnt"] if corrections else 0
        if snapshot.total_interactions > 0:
            snapshot.user_correction_rate = snapshot.user_corrections / snapshot.total_interactions
    except Exception:
        pass

    # --- Self-Heal Success Rate ---
    try:
        total_heals = conn.execute(
            "SELECT COUNT(*) as cnt FROM interaction_signals "
            "WHERE signal_type = 'self_heal_attempt'"
        ).fetchone()
        snapshot.total_failures = total_heals["cnt"] if total_heals else 0

        successful_heals = conn.execute(
            "SELECT COUNT(*) as cnt FROM interaction_signals "
            "WHERE signal_type = 'self_heal_success'"
        ).fetchone()
        snapshot.auto_recovered = successful_heals["cnt"] if successful_heals else 0
        if snapshot.total_failures > 0:
            snapshot.self_heal_success_rate = snapshot.auto_recovered / snapshot.total_failures
    except Exception:
        pass

    # --- Response Latency P50/P95 ---
    try:
        rows = conn.execute(
            "SELECT CAST(json_extract(context, '$.latency_ms') AS REAL) as lat "
            "FROM interaction_signals "
            "WHERE signal_type = 'response_latency' AND context IS NOT NULL "
            "ORDER BY lat"
        ).fetchall()
        if rows:
            latencies = [r["lat"] for r in rows if r["lat"] is not None]
            if latencies:
                n = len(latencies)
                snapshot.latency_p50 = latencies[n // 2] / 1000.0
                snapshot.latency_p95 = latencies[int(n * 0.95)] / 1000.0
    except Exception:
        pass

    # --- Error Cascade Rate ---
    try:
        total_errors = conn.execute(
            "SELECT COUNT(*) as cnt FROM interaction_signals "
            "WHERE signal_type LIKE '%error%' OR signal_type LIKE '%failure%'"
        ).fetchone()
        total_err = total_errors["cnt"] if total_errors else 0

        cascaded = conn.execute(
            "SELECT COUNT(*) as cnt FROM interaction_signals "
            "WHERE signal_type = 'cascaded_failure'"
        ).fetchone()
        snapshot.cascaded_failures = cascaded["cnt"] if cascaded else 0
        if total_err > 0:
            snapshot.error_cascade_rate = snapshot.cascaded_failures / total_err
    except Exception:
        pass

    # --- Conversation Abandonment ---
    # Sessions where the last user message got no response or got a frustration signal
    try:
        sessions = conn.execute(
            "SELECT COUNT(DISTINCT chat_id) as cnt FROM conversations"
        ).fetchone()
        snapshot.total_sessions = sessions["cnt"] if sessions else 0

        abandoned = conn.execute(
            "SELECT COUNT(*) as cnt FROM interaction_signals "
            "WHERE signal_type = 'session_abandoned'"
        ).fetchone()
        snapshot.abandoned_sessions = abandoned["cnt"] if abandoned else 0
        if snapshot.total_sessions > 0:
            snapshot.abandonment_rate = snapshot.abandoned_sessions / snapshot.total_sessions
    except Exception:
        pass

    # --- Grounding / Hallucination Rate ---
    try:
        grounding_rows = conn.execute(
            "SELECT CAST(json_extract(context, '$.grounding_ratio') AS REAL) as ratio "
            "FROM interaction_signals "
            "WHERE signal_type = 'grounding_check' AND context IS NOT NULL"
        ).fetchall()
        if grounding_rows:
            ratios = [r["ratio"] for r in grounding_rows if r["ratio"] is not None]
            if ratios:
                snapshot.grounding_ratio_avg = sum(ratios) / len(ratios)
                snapshot.grounding_checks = len(ratios)
    except Exception:
        pass

    # --- MTTR (Mean Time To Recovery) ---
    try:
        mttr_rows = conn.execute(
            "SELECT created_at, merged_at, verified_at FROM evolution_candidates "
            "WHERE status = 'completed' AND created_at IS NOT NULL "
            "AND (merged_at IS NOT NULL OR verified_at IS NOT NULL)"
        ).fetchall()
        if mttr_rows:
            from datetime import datetime as _dt_mttr
            mttrs = []
            for row in mttr_rows:
                try:
                    t_start = _dt_mttr.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                    t_end_str = row["verified_at"] or row["merged_at"]
                    t_end = _dt_mttr.fromisoformat(t_end_str.replace("Z", "+00:00"))
                    hours = (t_end - t_start).total_seconds() / 3600
                    if 0 < hours < 720:  # sanity: <30 days
                        mttrs.append(hours)
                except Exception:
                    continue
            if mttrs:
                snapshot.mttr_hours = sum(mttrs) / len(mttrs)
                snapshot.mttr_samples = len(mttrs)
    except Exception:
        pass

    # --- Cost Per Task ---
    try:
        cost_rows = conn.execute(
            "SELECT CAST(json_extract(context, '$.cost_usd') AS REAL) as cost "
            "FROM interaction_signals "
            "WHERE signal_type = 'llm_token_usage' AND context IS NOT NULL "
            "ORDER BY cost"
        ).fetchall()
        if cost_rows:
            costs = [r["cost"] for r in cost_rows if r["cost"] is not None and r["cost"] > 0]
            if costs:
                n = len(costs)
                snapshot.cost_per_task_p50 = costs[n // 2]
                snapshot.cost_per_task_p95 = costs[int(n * 0.95)]
                snapshot.total_cost_usd = sum(costs)

        token_rows = conn.execute(
            "SELECT SUM(CAST(json_extract(context, '$.prompt_tokens') AS INTEGER) + "
            "CAST(json_extract(context, '$.completion_tokens') AS INTEGER)) as total "
            "FROM interaction_signals WHERE signal_type = 'llm_token_usage'"
        ).fetchone()
        if token_rows and token_rows["total"]:
            snapshot.total_tokens = token_rows["total"]
    except Exception:
        pass

    # --- Per-Tool Accuracy Breakdown ---
    try:
        # Total calls per tool from capability_usage signals
        usage_rows = conn.execute(
            "SELECT json_extract(context, '$.action') as tool, COUNT(*) as cnt "
            "FROM interaction_signals WHERE signal_type = 'capability_usage' "
            "AND context IS NOT NULL GROUP BY tool ORDER BY cnt DESC"
        ).fetchall()

        # Failures per tool
        failure_rows = conn.execute(
            "SELECT json_extract(context, '$.action') as tool, COUNT(*) as cnt "
            "FROM interaction_signals "
            "WHERE signal_type IN ('tool_failure', 'action_execution_failure') "
            "AND context IS NOT NULL GROUP BY tool"
        ).fetchall()
        failure_map = {r["tool"]: r["cnt"] for r in failure_rows if r["tool"]}

        for row in usage_rows:
            tool = row["tool"]
            if not tool:
                continue
            calls = row["cnt"]
            failures = failure_map.get(tool, 0)
            success_rate = (calls - failures) / calls if calls > 0 else 0
            snapshot.per_tool_metrics[tool] = {
                "calls": calls,
                "failures": failures,
                "success_rate": round(success_rate, 3),
            }
    except Exception:
        pass

    conn.close()
    return snapshot


def save_metrics(snapshot: MetricsSnapshot, output_dir: Path | None = None) -> Path:
    """Save metrics snapshot to reports directory."""
    out_dir = output_dir or REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"metrics_{snapshot.timestamp}.json"
    path.write_text(json.dumps(asdict(snapshot), indent=2, default=str))
    return path


def print_metrics(snapshot: MetricsSnapshot) -> None:
    """Pretty-print metrics to stdout."""
    print(f"\n{'=' * 60}")
    print(f"KHALIL PRODUCTION METRICS — {snapshot.timestamp}")
    print(f"{'=' * 60}")

    metrics = [
        ("Task Completion Rate", snapshot.task_completion_rate,
         f"{snapshot.completed_plans}/{snapshot.total_plans} plans", ">50%"),
        ("Tool Success Rate", snapshot.tool_success_rate,
         f"{snapshot.successful_tool_calls}/{snapshot.total_tool_calls} calls", ">90%"),
        ("User Correction Rate", snapshot.user_correction_rate,
         f"{snapshot.user_corrections}/{snapshot.total_interactions} interactions", "<10%"),
        ("Self-Heal Success Rate", snapshot.self_heal_success_rate,
         f"{snapshot.auto_recovered}/{snapshot.total_failures} attempts", ">50%"),
        ("Latency P50", snapshot.latency_p50,
         f"{snapshot.latency_p50:.2f}s" if snapshot.latency_p50 else "N/A", "<2s"),
        ("Latency P95", snapshot.latency_p95,
         f"{snapshot.latency_p95:.2f}s" if snapshot.latency_p95 else "N/A", "<10s"),
        ("Error Cascade Rate", snapshot.error_cascade_rate,
         f"{snapshot.cascaded_failures} cascaded", "<5%"),
        ("Abandonment Rate", snapshot.abandonment_rate,
         f"{snapshot.abandoned_sessions}/{snapshot.total_sessions} sessions", "<15%"),
        ("Grounding Ratio", snapshot.grounding_ratio_avg,
         f"{snapshot.grounding_checks} checks" if snapshot.grounding_checks else "N/A", ">95%"),
        ("MTTR (avg)", snapshot.mttr_hours,
         f"{snapshot.mttr_hours:.1f}h ({snapshot.mttr_samples} samples)" if snapshot.mttr_hours else "N/A", "<24h"),
        ("Cost Per Task P50", snapshot.cost_per_task_p50,
         f"${snapshot.cost_per_task_p50:.4f}" if snapshot.cost_per_task_p50 else "N/A", "track"),
        ("Cost Per Task P95", snapshot.cost_per_task_p95,
         f"${snapshot.cost_per_task_p95:.4f}" if snapshot.cost_per_task_p95 else "N/A", "track"),
    ]

    for name, value, detail, target in metrics:
        if value is not None:
            pct = f"{value:.1%}"
            print(f"  {name:30s} {pct:>8s}  ({detail})  target: {target}")
        else:
            print(f"  {name:30s}      N/A  ({detail})")

    # Per-tool breakdown
    if snapshot.per_tool_metrics:
        print(f"\n  {'Per-Tool Accuracy':30s}")
        print(f"  {'─' * 55}")
        sorted_tools = sorted(snapshot.per_tool_metrics.items(), key=lambda x: x[1]["calls"], reverse=True)
        for tool, data in sorted_tools[:15]:
            rate = f"{data['success_rate']:.0%}"
            status = "✓" if data["success_rate"] >= 0.9 else "⚠" if data["success_rate"] >= 0.75 else "✗"
            print(f"  {status} {tool:25s} {rate:>5s}  ({data['calls']} calls, {data['failures']} failures)")

    print(f"{'=' * 60}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    snapshot = compute_metrics()

    if "--json" in sys.argv:
        print(json.dumps(asdict(snapshot), indent=2, default=str))
    else:
        print_metrics(snapshot)
        path = save_metrics(snapshot)
        print(f"Saved to {path}")


if __name__ == "__main__":
    main()
