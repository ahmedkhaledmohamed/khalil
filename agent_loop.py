"""Continuous agent loop — sense → think → act → report.

Runs as a background async task alongside the scheduler. Monitors state changes,
identifies actionable opportunities, and executes within the user's autonomy settings.

Unlike the scheduler (time-triggered) or message handler (user-triggered), the agent
loop is STATE-triggered — it acts when the world changes, not on a fixed schedule.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from zoneinfo import ZoneInfo

from config import DB_PATH, TIMEZONE, AutonomyLevel

log = logging.getLogger("khalil.agent_loop")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class Urgency(IntEnum):
    LOW = 1       # can wait, batch with others
    MEDIUM = 2    # surface within this tick
    HIGH = 3      # act immediately, even in quiet hours


@dataclass
class Opportunity:
    """Something the agent loop identified as actionable."""
    id: str                     # unique key for dedup / cooldown
    source: str                 # sensor name: "calendar", "reminders", etc.
    summary: str                # human-readable description
    urgency: Urgency
    action_type: str | None     # action to execute (e.g., "reminder_nudge"), or None for alert-only
    payload: dict = field(default_factory=dict)
    requires_llm: bool = False  # True if the action needs LLM generation (e.g., meeting prep)


@dataclass
class LoopResult:
    """What happened in one tick."""
    sensed: dict                # raw state snapshot
    opportunities: list[Opportunity]
    acted: list[tuple[Opportunity, str]]   # (opp, result_summary)
    alerted: list[Opportunity]             # sent as notifications
    suppressed: list[Opportunity]          # skipped (cooldown, quiet hours, etc.)


# ---------------------------------------------------------------------------
# Sensor discovery — pulls from SkillRegistry, with built-in fallbacks
# ---------------------------------------------------------------------------

def _get_sensors() -> list[tuple[str, object]]:
    """Get all sensors: registry-discovered + built-in fallbacks.

    Returns list of (name, async_callable) tuples.
    """
    sensors: dict[str, object] = {}

    # Discover from skill registry
    try:
        from skills import get_registry
        for sensor_config in get_registry().get_sensors():
            sensors[sensor_config.name] = sensor_config.function
    except Exception as e:
        log.debug("Sensor discovery from registry failed: %s", e)

    # Built-in fallbacks (only if not already registered by a skill)
    if "system" not in sensors:
        sensors["system"] = _sense_system_builtin
    if "evolution" not in sensors:
        sensors["evolution"] = _sense_evolution_builtin

    return list(sensors.items())


async def _sense_evolution_builtin() -> dict:
    """Count pending evolution signals since last cycle. Lightweight — no LLM."""
    try:
        from evolution import count_pending_signals, get_last_cycle_time
        return {"pending_signals": count_pending_signals(), "last_cycle": get_last_cycle_time()}
    except Exception:
        return {}


def _get_opportunity_fns() -> list[object]:
    """Get all identify_opportunities callbacks from registered sensors."""
    fns = []
    try:
        from skills import get_registry
        for sensor_config in get_registry().get_sensors():
            if sensor_config.identify_opportunities:
                fns.append(sensor_config.identify_opportunities)
    except Exception:
        pass
    return fns


async def _sense_system_builtin() -> dict:
    """Built-in system health check (no SKILL module)."""
    try:
        import shutil
        total, used, free = shutil.disk_usage("/")
        pct_used = used / total * 100
        return {"disk_pct_used": round(pct_used, 1)}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Opportunity identification — heuristic, no LLM
# ---------------------------------------------------------------------------

def _identify_opportunities(state: dict, last_state: dict, cooldowns: dict) -> list[Opportunity]:
    """Built-in opportunity detection + proactive behavior triggers.

    Sources:
    1. System health (disk space)
    2. Follow-up persistence (pending follow-ups past their due time)
    3. Routine drift detection (unusual silence)
    4. Time-aware nudges (upcoming meetings, end-of-day reminders)
    """
    opps: list[Opportunity] = []
    now = time.monotonic()
    now_dt = datetime.now(ZoneInfo(TIMEZONE))
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    current_hour = now_dt.hour

    # --- 1. Disk space warning (built-in system sensor) ---
    disk_pct = state.get("system", {}).get("disk_pct_used", 0)
    if disk_pct > 90:
        opp_id = "disk_space_warning"
        if not _on_cooldown(opp_id, cooldowns, now, hours=24):
            opps.append(Opportunity(
                id=opp_id,
                source="system",
                summary=f"\U0001f4be Disk space warning: {disk_pct}% used",
                urgency=Urgency.LOW,
                action_type=None,
            ))

    # --- 2. Follow-up persistence ---
    try:
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute(
            "SELECT id, source, summary, action_type, payload, nudge_count "
            "FROM follow_ups WHERE status = 'pending' AND follow_up_at <= ?",
            (now_str,),
        ).fetchall()
        for row in rows:
            fid, source, summary, action_type, payload, nudge_count = row
            opp_id = f"followup_{fid}"
            if nudge_count >= 3:
                # Auto-expire after 3 nudges
                conn.execute("UPDATE follow_ups SET status = 'expired' WHERE id = ?", (fid,))
                conn.commit()
                continue
            if not _on_cooldown(opp_id, cooldowns, now, hours=1):
                opps.append(Opportunity(
                    id=opp_id,
                    source=source,
                    summary=f"🔔 Follow-up: {summary}",
                    urgency=Urgency.MEDIUM,
                    action_type="follow_up_nudge",
                    payload={"follow_up_id": fid, "nudge_count": nudge_count},
                ))
        conn.close()
    except Exception as e:
        log.debug("Follow-up check failed: %s", e)

    # --- 3. Routine drift detection ---
    try:
        from learning import detect_routine_drift
        drifts = detect_routine_drift()
        for drift in drifts:
            opp_id = f"routine_drift_{drift['type']}"
            if not _on_cooldown(opp_id, cooldowns, now, hours=24):
                opps.append(Opportunity(
                    id=opp_id,
                    source="learning",
                    summary=drift["summary"],
                    urgency=Urgency.LOW,
                    action_type=None,  # alert only
                ))
    except Exception as e:
        log.debug("Routine drift check failed: %s", e)

    # --- 4. Time-aware nudges ---

    # 4a. End-of-day reminder sweep (5-6 PM)
    if 17 <= current_hour <= 18:
        opp_id = "eod_reminder_sweep"
        if not _on_cooldown(opp_id, cooldowns, now, hours=20):
            try:
                from actions.reminders import list_reminders
                active = list_reminders(status="active")
                if active and len(active) >= 1:
                    count = len(active)
                    opps.append(Opportunity(
                        id=opp_id,
                        source="reminders",
                        summary=f"📋 You have {count} incomplete reminder{'s' if count > 1 else ''} today",
                        urgency=Urgency.LOW,
                        action_type="reminder_sweep",
                        payload={"reminders": [r["text"][:80] for r in active[:5]]},
                    ))
            except Exception as e:
                log.debug("EOD reminder sweep failed: %s", e)

    # --- 5. Stale goals (M5: Goal-Aware Agent) ---
    if not _on_cooldown("stale_goals_check", cooldowns, now, hours=168):  # weekly
        try:
            from learning import get_stale_goals
            stale = get_stale_goals(days=7)
            if stale:
                names = ", ".join(g["text"][:40] for g in stale[:3])
                suffix = f" (+{len(stale) - 3} more)" if len(stale) > 3 else ""
                opps.append(Opportunity(
                    id="stale_goals_check",
                    source="goals",
                    summary=f"🎯 {len(stale)} goal(s) with no progress in 7+ days: {names}{suffix}",
                    urgency=Urgency.LOW,
                    action_type=None,  # alert only
                    payload={"stale_goals": stale},
                ))
        except Exception as e:
            log.debug("Stale goals check failed: %s", e)

    # --- 6. Evolution cycle readiness ---
    evo = state.get("evolution", {})
    pending = evo.get("pending_signals", 0)
    last_cycle = evo.get("last_cycle")
    hours_since = 24  # default: trigger if never run
    if last_cycle:
        try:
            last_dt = datetime.fromisoformat(last_cycle)
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        except Exception:
            pass
    from evolution import EVOLUTION_SIGNAL_THRESHOLD, EVOLUTION_COOLDOWN_HOURS
    if (pending >= EVOLUTION_SIGNAL_THRESHOLD or hours_since >= 6) and not _on_cooldown(
        "evolution_cycle", cooldowns, now, hours=EVOLUTION_COOLDOWN_HOURS
    ):
        opps.append(Opportunity(
            id="evolution_cycle",
            source="evolution",
            summary=f"Evolution cycle ready: {pending} signals, {hours_since:.0f}h since last run",
            urgency=Urgency.LOW,
            action_type="evolution_cycle",
        ))

    return opps


async def _identify_opportunities_llm(
    state: dict, snapshot, ask_llm_fn, cooldowns: dict,
) -> list[Opportunity]:
    """M3: LLM-powered opportunity detection for novel patterns heuristics can't cover.

    Runs every 3rd tick (~15 min) in background. Falls back to empty list on failure.
    Prefers READ-only suggestions (conservative).
    """
    if not ask_llm_fn or not snapshot:
        return []

    try:
        from synthesis.aggregator import snapshot_to_text
        state_text = snapshot_to_text(snapshot)
    except Exception:
        state_text = json.dumps({k: str(v)[:200] for k, v in state.items()})

    # Get recent user intents for context
    recent_intents = ""
    try:
        from learning import get_recent_user_intents
        intents = get_recent_user_intents(hours=24)
        if intents:
            recent_intents = "\n".join(f"  - {i}" for i in intents[:10])
    except Exception:
        pass

    prompt = (
        "Given the user's current state, identify 0-3 actionable opportunities "
        "that a simple heuristic system would miss. Be conservative — prefer READ "
        "actions (check, review, surface) over WRITE actions (send, create, modify).\n\n"
        f"Current state:\n{state_text[:2000]}\n\n"
    )
    if recent_intents:
        prompt += f"Recent activity (last 24h):\n{recent_intents}\n\n"
    prompt += (
        "Respond with a JSON array (or empty array [] if nothing notable):\n"
        '[{"id": "unique_key", "summary": "human-readable description", '
        '"urgency": 1, "action_type": null}]\n\n'
        "urgency: 1=low, 2=medium, 3=high. action_type: null for alert-only."
    )

    try:
        response = await ask_llm_fn(
            prompt, "",
            "Respond with ONLY a JSON array. No markdown fences, no explanation.",
        )
        response = response.strip()
        if response.startswith("```"):
            response = response.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        items = json.loads(response) if response and response.startswith("[") else []
        opps = []
        now = time.monotonic()
        for item in items[:3]:
            opp_id = f"llm_{item.get('id', 'unknown')}"
            if not _on_cooldown(opp_id, cooldowns, now, hours=8):
                opps.append(Opportunity(
                    id=opp_id,
                    source="llm_reasoning",
                    summary=item.get("summary", ""),
                    urgency=Urgency(min(item.get("urgency", 1), 3)),
                    action_type=item.get("action_type"),
                    payload=item.get("payload", {}),
                    requires_llm=True,
                ))
        return opps
    except Exception as e:
        log.debug("LLM opportunity detection failed: %s", e)
        return []


def acknowledge_follow_ups(source: str | None = None):
    """Mark pending follow-ups as acknowledged. Called when user engages."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        if source:
            conn.execute(
                "UPDATE follow_ups SET status = 'acknowledged' WHERE status = 'pending' AND source = ?",
                (source,),
            )
        else:
            conn.execute("UPDATE follow_ups SET status = 'acknowledged' WHERE status = 'pending'")
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug("Failed to acknowledge follow-ups: %s", e)


async def _identify_cross_domain_opportunities(snapshot, cooldowns: dict) -> list[Opportunity]:
    """Detect compound opportunities from cross-domain synthesis.

    Uses the DomainSnapshot to find situations where multiple domains
    are stressed simultaneously, or where one domain's state implies
    action in another.
    """
    opps = []
    now = time.monotonic()

    # Count domains at yellow or red risk level
    domains_at_risk = []
    for domain_name in ("work", "finance", "goals", "health"):
        domain = getattr(snapshot, domain_name, None)
        if domain and hasattr(domain, "risk_level"):
            if domain.risk_level in ("yellow", "red"):
                domains_at_risk.append(domain_name)

    # Compound stress: 2+ domains at risk
    if len(domains_at_risk) >= 2:
        opp_id = "cross_domain_stress"
        if not _on_cooldown(opp_id, cooldowns, now, hours=12):
            risk_summary = ", ".join(domains_at_risk)
            opps.append(Opportunity(
                id=opp_id,
                source="synthesis",
                summary=f"⚠️ Multiple areas need attention: {risk_summary}",
                urgency=Urgency.MEDIUM,
                action_type=None,
            ))

    # Overcommitment check
    try:
        from synthesis.capacity import detect_overcommitment
        report = await detect_overcommitment(snapshot)
        if report.score > 70:
            opp_id = "capacity_warning"
            if not _on_cooldown(opp_id, cooldowns, now, hours=24):
                opps.append(Opportunity(
                    id=opp_id,
                    source="synthesis",
                    summary=f"🔴 Capacity score: {report.score}/100 — consider deferring lower-priority items",
                    urgency=Urgency.MEDIUM,
                    action_type=None,
                ))
    except Exception as e:
        log.debug("Capacity check failed: %s", e)

    # No deep work but goals behind
    if hasattr(snapshot, "health") and hasattr(snapshot, "goals"):
        if (snapshot.health.deep_work_hours_available < 1.0
                and snapshot.goals.risk_level in ("yellow", "red")):
            opp_id = "no_deep_work_goals_behind"
            if not _on_cooldown(opp_id, cooldowns, now, hours=24):
                opps.append(Opportunity(
                    id=opp_id,
                    source="synthesis",
                    summary="📊 Goals behind schedule but no deep work blocks today — consider protecting focus time",
                    urgency=Urgency.LOW,
                    action_type=None,
                ))

    return opps


def _on_cooldown(opp_id: str, cooldowns: dict, now: float, hours: int) -> bool:
    """Check if this opportunity was recently surfaced."""
    last = cooldowns.get(opp_id, 0)
    if now - last < hours * 3600:
        return True
    return False


# ---------------------------------------------------------------------------
# Agent Loop
# ---------------------------------------------------------------------------

class AgentLoop:
    """Continuous sense-think-act loop running in background."""

    # Max proactive notifications per day (resets at midnight)
    _DAILY_NOTIFICATION_CAP = 8

    def __init__(
        self,
        channel,
        chat_id: int | str,
        autonomy,                   # AutonomyController instance
        ask_llm_fn=None,            # async callable for LLM generation
        interval_s: int = 300,
        quiet_hours: tuple[int, int] = (23, 7),
    ):
        self.channel = channel
        self.chat_id = chat_id
        self.autonomy = autonomy
        self.ask_llm = ask_llm_fn
        self.interval_s = interval_s
        self.quiet_hours = quiet_hours
        self._running = False
        self._last_state: dict = {}
        self._cooldowns: dict[str, float] = {}  # opp_id -> monotonic timestamp
        self._tick_count = 0
        self._snapshot_cache: tuple[float, object] | None = None  # (monotonic_time, DomainSnapshot)
        self._daily_notification_count = 0
        self._daily_notification_date: str = ""  # YYYY-MM-DD, resets at midnight

    async def start(self):
        """Start the background loop. Call as asyncio.create_task(loop.start())."""
        self._running = True
        log.info("Agent loop started (interval=%ds)", self.interval_s)
        # Initial delay — let server finish startup
        await asyncio.sleep(30)
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                log.error("Agent loop tick failed: %s", e, exc_info=True)
            await asyncio.sleep(self.interval_s)

    def stop(self):
        self._running = False
        log.info("Agent loop stopped")

    async def _tick(self):
        """One sense → think → act → report cycle."""
        self._tick_count += 1
        tick_start = time.monotonic()

        # 1. SENSE — discover and run all sensors in parallel
        sensors = _get_sensors()
        sensor_results = await asyncio.gather(
            *(fn() for _, fn in sensors),
            return_exceptions=True,
        )
        state = {}
        for (name, _), result in zip(sensors, sensor_results):
            if isinstance(result, Exception):
                log.debug("Sensor %s failed: %s", name, result)
                state[name] = {}
            else:
                state[name] = result

        # 2. THINK — identify opportunities (built-in + skill-registered)
        opportunities = _identify_opportunities(state, self._last_state, self._cooldowns)

        # Also collect opportunities from skill sensors
        for opp_fn in _get_opportunity_fns():
            try:
                extra = opp_fn(state, self._last_state, self._cooldowns)
                if extra:
                    opportunities.extend(extra)
            except Exception as e:
                log.debug("Opportunity fn failed: %s", e)

        self._last_state = state

        # 2b. SYNTHESIZE — cross-domain opportunity detection (hourly)
        now_mono = time.monotonic()
        should_aggregate = (
            self._snapshot_cache is None
            or (now_mono - self._snapshot_cache[0]) > 3600  # refresh hourly
        )
        if should_aggregate:
            try:
                from synthesis.aggregator import aggregate_all_domains
                snapshot = await aggregate_all_domains()
                self._snapshot_cache = (now_mono, snapshot)
                cross_opps = await _identify_cross_domain_opportunities(snapshot, self._cooldowns)
                opportunities.extend(cross_opps)
            except Exception as e:
                log.debug("Cross-domain synthesis failed: %s", e)

        # M11: Check background agents
        try:
            from agents.coordinator import get_background_agents, run_background_agent, update_background_agent
            running_agents = get_background_agents(status="running")
            for agent_data in running_agents:
                # Check if expired
                created = agent_data.get("created_at", "")
                if created:
                    from datetime import datetime as _dt
                    try:
                        created_dt = _dt.fromisoformat(created.replace("Z", "+00:00"))
                        elapsed = (_dt.now(timezone.utc) - created_dt).total_seconds()
                        if elapsed > 3600:  # 1 hour default max
                            update_background_agent(agent_data["id"], status="expired",
                                                     final_result="Expired: exceeded max duration")
                            continue
                    except Exception:
                        pass
                # Run the agent
                try:
                    result = await run_background_agent(agent_data["id"], self.ask_llm)
                    if result:
                        opportunities.append(Opportunity(
                            id=f"bg_agent_{agent_data['id']}",
                            source="background_agent",
                            summary=f"🤖 Background task completed: {agent_data['task'][:60]}",
                            urgency=Urgency.MEDIUM,
                            action_type=None,
                            payload={"result": result[:500]},
                        ))
                except Exception as e:
                    log.debug("Background agent check failed for %s: %s", agent_data["id"], e)
        except Exception as e:
            log.debug("Background agents check failed: %s", e)

        # M9: Check temporal tasks every tick
        try:
            from temporal import check_temporal_tasks
            triggered = await check_temporal_tasks(ask_llm_fn=self.ask_llm)
            for task, reason in triggered:
                opp_id = f"temporal_{task.id}"
                if not _on_cooldown(opp_id, self._cooldowns, time.monotonic(), hours=0.5):
                    opportunities.append(Opportunity(
                        id=opp_id,
                        source="temporal",
                        summary=f"⏰ {task.description} — {reason}",
                        urgency=Urgency.MEDIUM,
                        action_type=task.action or None,
                        payload=task.params,
                    ))
        except Exception as e:
            log.debug("Temporal task check failed: %s", e)

        # M3: LLM-powered opportunity detection every 6th tick (~30 min)
        if self._tick_count % 6 == 0 and self.ask_llm:
            try:
                snapshot = self._snapshot_cache[1] if self._snapshot_cache else None
                llm_opps = await _identify_opportunities_llm(
                    state, snapshot, self.ask_llm, self._cooldowns,
                )
                if llm_opps:
                    opportunities.extend(llm_opps)
                    log.info("LLM reasoning found %d opportunities", len(llm_opps))
            except Exception as e:
                log.debug("LLM opportunity detection failed: %s", e)

        if not opportunities:
            return  # nothing to do

        # 3. FILTER — quiet hours, user presence, learned behavior, daily cap
        now_dt = datetime.now(ZoneInfo(TIMEZONE))
        in_quiet = self._in_quiet_hours(now_dt)

        # Reset daily notification counter at midnight
        today_str = now_dt.strftime("%Y-%m-%d")
        if today_str != self._daily_notification_date:
            self._daily_notification_count = 0
            self._daily_notification_date = today_str

        # Load behavior profile to filter by learned preferences
        suppress_skills = set()
        try:
            from learning import get_behavior_profile
            profile = get_behavior_profile()
            suppress_skills = set(profile.suppress_skills)
        except Exception:
            pass

        acted: list[tuple[Opportunity, str]] = []
        alerted: list[Opportunity] = []
        suppressed: list[Opportunity] = []

        for opp in opportunities:
            # Skip opportunities from broken/suppressed skills
            if opp.source in suppress_skills:
                suppressed.append(opp)
                continue

            # Skip low/medium urgency during quiet hours
            if in_quiet and opp.urgency < Urgency.HIGH:
                suppressed.append(opp)
                continue

            # Smart timing check
            from scheduler.proactive import is_good_time_for_alert
            if opp.urgency < Urgency.HIGH and not is_good_time_for_alert():
                suppressed.append(opp)
                continue

            # Daily notification cap — only HIGH urgency bypasses
            if opp.urgency < Urgency.HIGH and self._daily_notification_count >= self._DAILY_NOTIFICATION_CAP:
                suppressed.append(opp)
                continue

            # 4. ACT — execute actions or alert
            if opp.action_type and not self.autonomy.needs_approval(opp.action_type):
                # Can auto-execute
                result = await self._execute_action(opp)
                acted.append((opp, result))
            else:
                # Alert the user
                alerted.append(opp)

            # Mark cooldown
            self._cooldowns[opp.id] = time.monotonic()

        # 5. REPORT — batch notification
        if acted or alerted:
            self._daily_notification_count += 1
            await self._send_report(acted, alerted)

        elapsed = time.monotonic() - tick_start
        log.debug(
            "Agent loop tick #%d: %d opps, %d acted, %d alerted, %d suppressed (%.1fs)",
            self._tick_count, len(opportunities), len(acted), len(alerted), len(suppressed), elapsed,
        )

    async def _execute_action(self, opp: Opportunity) -> str:
        """Execute an action for an opportunity. Routes through execution bus when available."""
        # Try execution bus first
        try:
            from execution import get_execution_bus, ExecutionContext, ExecutionSource
            bus = get_execution_bus()
            if bus and opp.action_type:
                exec_ctx = ExecutionContext(
                    source=ExecutionSource.AGENT_LOOP,
                    chat_id=self.chat_id if isinstance(self.chat_id, int) else None,
                )
                result = bus_result = await bus.execute(
                    opp.action_type, opp.payload, exec_ctx,
                )
                if result.success:
                    return result.output or f"Completed: {opp.action_type}"
                if result.error and "No handler" not in (result.error or ""):
                    return f"{opp.action_type} failed: {result.error}"
                # Fall through to legacy handlers if no bus handler
        except ImportError:
            pass

        if opp.action_type == "meeting_prep" and self.ask_llm:
            event = opp.payload.get("event", {})
            title = event.get("summary", "meeting")
            attendees = event.get("attendees", [])
            context_str = f"Meeting: {title}"
            if attendees:
                context_str += f"\nAttendees: {', '.join(attendees[:5])}"
            try:
                # Try knowledge context, fall back to basic info
                try:
                    from knowledge.context import get_relevant_context
                    context_str += "\n" + get_relevant_context(title, max_chars=1500)
                except Exception:
                    pass
                prep = await self.ask_llm(
                    f"Generate a brief meeting prep for: {title}. "
                    f"Include key context, talking points, and questions to ask.",
                    context_str,
                )
                await self.channel.send_message(
                    self.chat_id,
                    f"📝 **Meeting prep** — {title}\n\n{prep}",
                )
                return f"Meeting prep sent for: {title}"
            except Exception as e:
                log.warning("Meeting prep failed: %s", e)
                return f"Meeting prep failed: {e}"

        if opp.action_type == "follow_up_nudge":
            fid = opp.payload.get("follow_up_id")
            nudge_count = opp.payload.get("nudge_count", 0)
            try:
                conn = sqlite3.connect(str(DB_PATH))
                # Increment nudge count and push follow_up_at by 2 hours
                next_at = (datetime.now(ZoneInfo(TIMEZONE)) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "UPDATE follow_ups SET nudge_count = nudge_count + 1, follow_up_at = ? WHERE id = ?",
                    (next_at, fid),
                )
                conn.commit()
                conn.close()
                return f"Follow-up nudge #{nudge_count + 1} sent"
            except Exception as e:
                log.warning("Follow-up nudge DB update failed: %s", e)
                return f"Follow-up nudge failed: {e}"

        if opp.action_type == "reminder_sweep":
            reminders = opp.payload.get("reminders", [])
            lines = "\n".join(f"  • {r}" for r in reminders)
            try:
                await self.channel.send_message(
                    self.chat_id,
                    f"📋 **End-of-day check** — incomplete reminders:\n{lines}\n\n"
                    "Reply 'done' to clear, or I'll check again tomorrow.",
                )
                return f"Reminder sweep: {len(reminders)} items surfaced"
            except Exception as e:
                log.warning("Reminder sweep failed: %s", e)
                return f"Reminder sweep failed: {e}"

        if opp.action_type == "evolution_cycle":
            from evolution import execute_evolution_cycle
            result = await execute_evolution_cycle(
                self.channel, self.chat_id, self.ask_llm, self.autonomy,
            )
            parts = [f"Evolution: {result.candidates_found} candidates"]
            if result.executed:
                parts.append(f"{result.executed} executed")
            if result.prs_created:
                parts.append(f"PRs: {', '.join(result.prs_created)}")
            return " | ".join(parts)

        return f"Unknown action: {opp.action_type}"

    async def _send_report(
        self,
        acted: list[tuple[Opportunity, str]],
        alerted: list[Opportunity],
    ):
        """Send a batched notification of what the agent loop did/found."""
        lines: list[str] = []

        if acted:
            lines.append("**🤖 Agent actions:**")
            for opp, result in acted:
                lines.append(f"  • {result}")

        if alerted:
            if acted:
                lines.append("")
            lines.append("**👀 Needs attention:**")
            for opp in alerted:
                lines.append(f"  • {opp.summary}")

        if lines:
            msg = "\n".join(lines)
            try:
                await self.channel.send_message(self.chat_id, msg)
            except Exception as e:
                log.warning("Failed to send agent loop report: %s", e)

    def _in_quiet_hours(self, now: datetime) -> bool:
        """Check if current time is in quiet hours."""
        start, end = self.quiet_hours
        hour = now.hour
        if start > end:  # wraps midnight (e.g., 23-7)
            return hour >= start or hour < end
        return start <= hour < end
