"""Continuous agent loop — sense → think → act → report.

Runs as a background async task alongside the scheduler. Monitors state changes,
identifies actionable opportunities, and executes within the user's autonomy settings.

Unlike the scheduler (time-triggered) or message handler (user-triggered), the agent
loop is STATE-triggered — it acts when the world changes, not on a fixed schedule.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
from zoneinfo import ZoneInfo

from config import TIMEZONE, AutonomyLevel

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
# Sensors — each returns a dict fragment of current state
# ---------------------------------------------------------------------------

async def _sense_reminders() -> dict:
    """Check for overdue and upcoming reminders."""
    try:
        from actions.reminders import list_reminders
        reminders = list_reminders()
        now = datetime.now(ZoneInfo(TIMEZONE))
        overdue = []
        upcoming = []
        for r in reminders:
            if not r.get("active"):
                continue
            due = r.get("due")
            if due:
                try:
                    due_dt = datetime.fromisoformat(due).replace(tzinfo=ZoneInfo(TIMEZONE))
                    if due_dt < now:
                        overdue.append(r)
                    elif due_dt < now + timedelta(hours=1):
                        upcoming.append(r)
                except (ValueError, TypeError):
                    pass
        return {"overdue": overdue, "upcoming": upcoming}
    except Exception as e:
        log.debug("Reminder sensor failed: %s", e)
        return {"overdue": [], "upcoming": []}


async def _sense_calendar() -> dict:
    """Check for upcoming meetings in next 2 hours."""
    try:
        from actions.calendar import get_today_events
        events = await get_today_events()
        now = datetime.now(ZoneInfo(TIMEZONE))
        upcoming = []
        for ev in (events or []):
            start_str = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
            if not start_str:
                continue
            try:
                start = datetime.fromisoformat(start_str)
                if not start.tzinfo:
                    start = start.replace(tzinfo=ZoneInfo(TIMEZONE))
                delta = (start - now).total_seconds() / 60
                if 0 < delta <= 120:  # within next 2 hours
                    ev["_minutes_until"] = int(delta)
                    upcoming.append(ev)
            except (ValueError, TypeError):
                pass
        return {"upcoming_events": upcoming}
    except Exception as e:
        log.debug("Calendar sensor failed: %s", e)
        return {"upcoming_events": []}


async def _sense_github() -> dict:
    """Check for unread GitHub notifications."""
    try:
        from actions.github_api import get_notifications
        notifications = await get_notifications(unread_only=True)
        return {"unread_notifications": notifications or []}
    except Exception as e:
        log.debug("GitHub sensor failed: %s", e)
        return {"unread_notifications": []}


async def _sense_system() -> dict:
    """Basic system health check."""
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
    """Compare current state to last state and identify actionable items."""
    opps: list[Opportunity] = []
    now = time.monotonic()

    # --- Overdue reminders ---
    for r in state.get("reminders", {}).get("overdue", []):
        opp_id = f"reminder_overdue_{r.get('id', r.get('text', '')[:20])}"
        if _on_cooldown(opp_id, cooldowns, now, hours=4):
            continue
        opps.append(Opportunity(
            id=opp_id,
            source="reminders",
            summary=f"⏰ Overdue reminder: {r.get('text', 'unknown')}",
            urgency=Urgency.MEDIUM,
            action_type=None,  # alert only — user decides
            payload={"reminder": r},
        ))

    # --- Upcoming reminders (within 1 hour) ---
    for r in state.get("reminders", {}).get("upcoming", []):
        opp_id = f"reminder_upcoming_{r.get('id', r.get('text', '')[:20])}"
        if _on_cooldown(opp_id, cooldowns, now, hours=2):
            continue
        opps.append(Opportunity(
            id=opp_id,
            source="reminders",
            summary=f"📋 Reminder coming up: {r.get('text', 'unknown')}",
            urgency=Urgency.LOW,
            action_type=None,
        ))

    # --- Meeting in 30 minutes, no prep sent ---
    for ev in state.get("calendar", {}).get("upcoming_events", []):
        mins = ev.get("_minutes_until", 999)
        if mins <= 35:
            title = ev.get("summary", "meeting")
            opp_id = f"meeting_prep_{title[:30]}_{ev.get('start', {}).get('dateTime', '')[:10]}"
            if _on_cooldown(opp_id, cooldowns, now, hours=12):
                continue
            opps.append(Opportunity(
                id=opp_id,
                source="calendar",
                summary=f"📅 Meeting in {mins}min: {title}",
                urgency=Urgency.MEDIUM,
                action_type="meeting_prep",
                payload={"event": ev},
                requires_llm=True,
            ))

    # --- GitHub notifications (batched) ---
    gh_notifs = state.get("github", {}).get("unread_notifications", [])
    if gh_notifs:
        # Only surface if new since last tick
        last_count = len(last_state.get("github", {}).get("unread_notifications", []))
        if len(gh_notifs) > last_count:
            opp_id = "github_notifications_new"
            if not _on_cooldown(opp_id, cooldowns, now, hours=1):
                titles = [n.get("subject", {}).get("title", "?") for n in gh_notifs[:5]]
                opps.append(Opportunity(
                    id=opp_id,
                    source="github",
                    summary=f"🔔 {len(gh_notifs)} GitHub notification(s):\n" + "\n".join(f"  • {t}" for t in titles),
                    urgency=Urgency.LOW,
                    action_type=None,
                ))

    # --- Disk space warning ---
    disk_pct = state.get("system", {}).get("disk_pct_used", 0)
    if disk_pct > 90:
        opp_id = "disk_space_warning"
        if not _on_cooldown(opp_id, cooldowns, now, hours=24):
            opps.append(Opportunity(
                id=opp_id,
                source="system",
                summary=f"💾 Disk space warning: {disk_pct}% used",
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

        # 1. SENSE — collect state from all sensors in parallel
        sensor_results = await asyncio.gather(
            _sense_reminders(),
            _sense_calendar(),
            _sense_github(),
            _sense_system(),
            return_exceptions=True,
        )
        state = {}
        sensor_names = ["reminders", "calendar", "github", "system"]
        for name, result in zip(sensor_names, sensor_results):
            if isinstance(result, Exception):
                log.debug("Sensor %s failed: %s", name, result)
                state[name] = {}
            else:
                state[name] = result

        # 2. THINK — identify opportunities
        opportunities = _identify_opportunities(state, self._last_state, self._cooldowns)
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
            try:
                from knowledge.context import get_relevant_context
                context = get_relevant_context(title, max_chars=1500)
                prep = await self.ask_llm(
                    f"Generate a brief meeting prep for: {title}. "
                    f"Include key context, talking points, and questions to ask.",
                    context,
                )
                await self.channel.send_message(
                    self.chat_id,
                    f"📝 **Meeting prep** — {title}\n\n{prep}",
                )
                return f"Meeting prep sent for: {title}"
            except Exception as e:
                log.warning("Meeting prep failed: %s", e)
                return f"Meeting prep failed: {e}"

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
