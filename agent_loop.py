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
from datetime import datetime, timedelta
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

    return list(sensors.items())


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

    return opps


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

        if not opportunities:
            return  # nothing to do

        # 3. FILTER — quiet hours, user presence
        now_dt = datetime.now(ZoneInfo(TIMEZONE))
        in_quiet = self._in_quiet_hours(now_dt)

        acted: list[tuple[Opportunity, str]] = []
        alerted: list[Opportunity] = []
        suppressed: list[Opportunity] = []

        for opp in opportunities:
            # Skip low/medium urgency during quiet hours
            if in_quiet and opp.urgency < Urgency.HIGH:
                suppressed.append(opp)
                continue

            # Smart timing check
            from scheduler.proactive import is_good_time_for_alert
            if opp.urgency < Urgency.HIGH and not is_good_time_for_alert():
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
            await self._send_report(acted, alerted)

        elapsed = time.monotonic() - tick_start
        log.debug(
            "Agent loop tick #%d: %d opps, %d acted, %d alerted, %d suppressed (%.1fs)",
            self._tick_count, len(opportunities), len(acted), len(alerted), len(suppressed), elapsed,
        )

    async def _execute_action(self, opp: Opportunity) -> str:
        """Execute an action for an opportunity. Returns summary string."""
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
