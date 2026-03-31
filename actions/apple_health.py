"""Apple Health data via Shortcuts export — steps, sleep, heart rate, workouts.

Apple Health data isn't directly accessible via AppleScript. This module uses
macOS Shortcuts to export health data to JSON, then reads and summarizes it.
Requires pre-configured Shortcuts (see examples below).

Fallback: If Shortcuts aren't set up, reads from a cached JSON export
at data/health_export.json (can be populated manually or via cron).
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DATA_DIR, TIMEZONE

log = logging.getLogger("khalil.actions.apple_health")

HEALTH_CACHE = DATA_DIR / "health_export.json"

SKILL = {
    "name": "apple_health",
    "description": "Read Apple Health data — steps, sleep, heart rate, workouts",
    "category": "health",
    "patterns": [
        (r"\bsteps?\b.*\btoday\b", "health_steps"),
        (r"\bhow\s+many\s+steps\b", "health_steps"),
        (r"\bstep\s+count\b", "health_steps"),
        (r"\bsteps?\s+(?:this|last)\s+week\b", "health_steps_week"),
        (r"\bweekly\s+steps?\b", "health_steps_week"),
        (r"\bsleep\b.*\b(?:last\s+night|today|data)\b", "health_sleep"),
        (r"\bhow\s+(?:did\s+I|well\s+did\s+I)\s+sleep\b", "health_sleep"),
        (r"\bsleep\s+(?:quality|score|hours|duration)\b", "health_sleep"),
        (r"\bheart\s+rate\b", "health_heart_rate"),
        (r"\bbpm\b", "health_heart_rate"),
        (r"\bresting\s+heart\b", "health_heart_rate"),
        (r"\bworkouts?\b", "health_workouts"),
        (r"\bexercise\b.*\b(?:today|this\s+week|history)\b", "health_workouts"),
        (r"\bhealth\s+summary\b", "health_summary"),
        (r"\bhealth\s+(?:data|stats|overview)\b", "health_summary"),
        (r"\bapple\s+health\b", "health_summary"),
    ],
    "actions": [
        {"type": "health_steps", "handler": "handle_intent", "keywords": "health steps today count walking", "description": "Today's step count"},
        {"type": "health_steps_week", "handler": "handle_intent", "keywords": "health steps week weekly walking", "description": "Weekly step summary"},
        {"type": "health_sleep", "handler": "handle_intent", "keywords": "health sleep last night hours quality duration", "description": "Sleep data"},
        {"type": "health_heart_rate", "handler": "handle_intent", "keywords": "health heart rate bpm resting pulse", "description": "Heart rate data"},
        {"type": "health_workouts", "handler": "handle_intent", "keywords": "health workouts exercise fitness activity", "description": "Recent workouts"},
        {"type": "health_summary", "handler": "handle_intent", "keywords": "health summary stats overview apple", "description": "Health overview"},
    ],
    "examples": [
        "How many steps today?",
        "How did I sleep last night?",
        "What's my heart rate?",
        "Show my workouts this week",
        "Health summary",
    ],
    "sensor": {"function": "sense_health", "interval_min": 60},
}


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

async def _run_shortcut(name: str) -> str | None:
    """Run a Shortcut and capture its output."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "shortcuts", "run", name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            return stdout.decode().strip()
        log.warning("Shortcut '%s' failed (rc=%d): %s", name, proc.returncode, stderr.decode()[:200])
    except asyncio.TimeoutError:
        log.warning("Shortcut '%s' timed out", name)
    except Exception as e:
        log.warning("Shortcut '%s' error: %s", name, e)
    return None


def _read_cache() -> dict:
    """Read cached health export data."""
    if not HEALTH_CACHE.exists():
        return {}
    try:
        return json.loads(HEALTH_CACHE.read_text())
    except Exception:
        return {}


def _write_cache(data: dict):
    """Write health data to cache."""
    HEALTH_CACHE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_CACHE.write_text(json.dumps(data, indent=2))


async def _get_health_data(metric: str) -> dict | None:
    """Get health data for a metric, trying Shortcut first, then cache.

    Expected Shortcut names:
    - "Health Steps" → returns JSON: {"steps_today": 8234, "steps_week": [{"date": "...", "steps": 8234}, ...]}
    - "Health Sleep" → returns JSON: {"hours": 7.5, "quality": "Good", "bedtime": "23:30", "waketime": "07:00"}
    - "Health Heart Rate" → returns JSON: {"current": 72, "resting": 58, "min": 52, "max": 145}
    - "Health Workouts" → returns JSON: {"workouts": [{"type": "Running", "duration_min": 35, "calories": 320, "date": "..."}, ...]}
    """
    shortcut_name = f"Health {metric.title()}"
    output = await _run_shortcut(shortcut_name)

    if output:
        try:
            data = json.loads(output)
            # Update cache
            cache = _read_cache()
            cache[metric] = {
                "data": data,
                "updated_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
            }
            _write_cache(cache)
            return data
        except json.JSONDecodeError:
            log.debug("Shortcut '%s' returned non-JSON: %s", shortcut_name, output[:100])

    # Fallback to cache
    cache = _read_cache()
    if metric in cache:
        cached = cache[metric]
        log.info("Using cached %s data (from %s)", metric, cached.get("updated_at", "unknown"))
        return cached.get("data")

    return None


# ---------------------------------------------------------------------------
# Intent handler
# ---------------------------------------------------------------------------

async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle health-related intents."""

    if action == "health_steps":
        data = await _get_health_data("steps")
        if not data:
            await ctx.reply(
                "No step data available. Set up a Shortcut named **\"Health Steps\"** "
                "that exports today's step count as JSON."
            )
            return True
        steps = data.get("steps_today", 0)
        goal = data.get("goal", 10000)
        pct = int(steps / goal * 100) if goal else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        await ctx.reply(
            f"🚶 **Steps Today**: {steps:,}\n"
            f"  Goal: {goal:,} ({pct}%)\n"
            f"  [{bar}]"
        )
        return True

    elif action == "health_steps_week":
        data = await _get_health_data("steps")
        if not data or "steps_week" not in data:
            await ctx.reply("No weekly step data available.")
            return True
        week = data["steps_week"]
        total = sum(d.get("steps", 0) for d in week)
        avg = total // len(week) if week else 0
        lines = [f"🚶 **Weekly Steps** (avg: {avg:,}/day, total: {total:,}):\n"]
        for day in week:
            steps = day.get("steps", 0)
            date = day.get("date", "?")
            bar = "█" * (steps // 1000)
            lines.append(f"  {date}: {steps:,} {bar}")
        await ctx.reply("\n".join(lines))
        return True

    elif action == "health_sleep":
        data = await _get_health_data("sleep")
        if not data:
            await ctx.reply(
                "No sleep data available. Set up a Shortcut named **\"Health Sleep\"** "
                "that exports last night's sleep as JSON."
            )
            return True
        hours = data.get("hours", 0)
        quality = data.get("quality", "Unknown")
        bedtime = data.get("bedtime", "?")
        waketime = data.get("waketime", "?")
        emoji = "😴" if hours >= 7 else "😐" if hours >= 6 else "😵"
        await ctx.reply(
            f"{emoji} **Sleep**: {hours:.1f} hours ({quality})\n"
            f"  Bedtime: {bedtime} → Wake: {waketime}"
        )
        return True

    elif action == "health_heart_rate":
        data = await _get_health_data("heart_rate")
        if not data:
            await ctx.reply(
                "No heart rate data available. Set up a Shortcut named **\"Health Heart Rate\"** "
                "that exports heart rate data as JSON."
            )
            return True
        resting = data.get("resting", "?")
        current = data.get("current", "?")
        hr_min = data.get("min", "?")
        hr_max = data.get("max", "?")
        await ctx.reply(
            f"❤️ **Heart Rate**\n"
            f"  Resting: {resting} bpm\n"
            f"  Current: {current} bpm\n"
            f"  Range: {hr_min}–{hr_max} bpm"
        )
        return True

    elif action == "health_workouts":
        data = await _get_health_data("workouts")
        if not data or "workouts" not in data:
            await ctx.reply(
                "No workout data available. Set up a Shortcut named **\"Health Workouts\"** "
                "that exports recent workouts as JSON."
            )
            return True
        workouts = data["workouts"]
        if not workouts:
            await ctx.reply("No recent workouts recorded.")
            return True
        lines = [f"💪 **Recent Workouts** ({len(workouts)}):\n"]
        for w in workouts[:7]:
            wtype = w.get("type", "Unknown")
            dur = w.get("duration_min", 0)
            cal = w.get("calories", 0)
            date = w.get("date", "")
            lines.append(f"  • **{wtype}** — {dur}min, {cal} cal ({date})")
        await ctx.reply("\n".join(lines))
        return True

    elif action == "health_summary":
        # Fetch all metrics
        steps_data = await _get_health_data("steps")
        sleep_data = await _get_health_data("sleep")
        hr_data = await _get_health_data("heart_rate")

        parts = ["📊 **Health Summary**\n"]

        if steps_data:
            steps = steps_data.get("steps_today", 0)
            parts.append(f"  🚶 Steps: {steps:,}")
        else:
            parts.append("  🚶 Steps: No data")

        if sleep_data:
            hours = sleep_data.get("hours", 0)
            parts.append(f"  😴 Sleep: {hours:.1f}h")
        else:
            parts.append("  😴 Sleep: No data")

        if hr_data:
            resting = hr_data.get("resting", "?")
            parts.append(f"  ❤️ Resting HR: {resting} bpm")
        else:
            parts.append("  ❤️ Heart Rate: No data")

        if not any([steps_data, sleep_data, hr_data]):
            parts.append("\nSet up Health Shortcuts to populate data.")

        await ctx.reply("\n".join(parts))
        return True

    return False


# ---------------------------------------------------------------------------
# Agent loop sensor
# ---------------------------------------------------------------------------

async def sense_health() -> dict:
    """Sensor: check daily step count from cache/Shortcuts."""
    try:
        data = await _get_health_data("steps")
        if data:
            return {"steps_today": data.get("steps_today", 0), "step_goal": data.get("goal", 10000)}
        return {}
    except Exception as e:
        log.debug("Health sensor failed: %s", e)
        return {}
