"""Fitbit and Garmin data sync — bridges wearable data into the health skill.

Reads data from Fitbit Web API or Garmin Connect (via garminconnect library).
Populates data/health_export.json for the apple_health skill to consume.

Requires one of:
- KHALIL_FITBIT_TOKEN env var (Fitbit OAuth access token)
- KHALIL_GARMIN_EMAIL + KHALIL_GARMIN_PASSWORD env vars
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from urllib.request import urlopen, Request
from zoneinfo import ZoneInfo

from config import DATA_DIR, TIMEZONE

log = logging.getLogger("khalil.actions.fitness_sync")

HEALTH_CACHE = DATA_DIR / "health_export.json"

SKILL = {
    "name": "fitness_sync",
    "description": "Sync Fitbit or Garmin health data",
    "category": "health",
    "patterns": [
        (r"\bsync\s+(?:my\s+)?(?:fitbit|garmin)\b", "fitness_sync"),
        (r"\bfitbit\s+(?:data|sync|steps|sleep|heart)\b", "fitness_sync_fitbit"),
        (r"\bgarmin\s+(?:data|sync|steps|sleep|heart|activities)\b", "fitness_sync_garmin"),
        (r"\bpull\s+(?:my\s+)?(?:fitness|health)\s+data\b", "fitness_sync"),
        (r"\bwearable\s+(?:data|sync)\b", "fitness_sync"),
    ],
    "actions": [
        {"type": "fitness_sync", "handler": "handle_intent", "keywords": "fitness sync fitbit garmin wearable health data pull", "description": "Sync wearable health data"},
        {"type": "fitness_sync_fitbit", "handler": "handle_intent", "keywords": "fitbit sync data steps sleep heart rate", "description": "Sync Fitbit data"},
        {"type": "fitness_sync_garmin", "handler": "handle_intent", "keywords": "garmin sync data steps sleep heart rate activities", "description": "Sync Garmin data"},
    ],
    "examples": [
        "Sync my Fitbit data",
        "Pull Garmin health data",
        "Sync my wearable data",
    ],
}


def _read_cache() -> dict:
    if not HEALTH_CACHE.exists():
        return {}
    try:
        return json.loads(HEALTH_CACHE.read_text())
    except Exception:
        return {}


def _write_cache(data: dict):
    HEALTH_CACHE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_CACHE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Fitbit
# ---------------------------------------------------------------------------

async def sync_fitbit() -> dict:
    """Fetch today's data from Fitbit Web API."""
    token = os.environ.get("KHALIL_FITBIT_TOKEN")
    if not token:
        raise ValueError("KHALIL_FITBIT_TOKEN not set")

    today = date.today().isoformat()
    results = {}

    async def _fitbit_get(path: str) -> dict:
        url = f"https://api.fitbit.com/1/user/-/{path}"
        req = Request(url, headers={"Authorization": f"Bearer {token}"})
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: urlopen(req, timeout=10).read())
        return json.loads(resp)

    # Steps
    try:
        data = await _fitbit_get(f"activities/date/{today}.json")
        summary = data.get("summary", {})
        results["steps"] = {
            "data": {
                "steps_today": summary.get("steps", 0),
                "goal": data.get("goals", {}).get("steps", 10000),
            },
            "updated_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
        }
    except Exception as e:
        log.warning("Fitbit steps sync failed: %s", e)

    # Sleep
    try:
        data = await _fitbit_get(f"sleep/date/{today}.json")
        sleep_data = data.get("summary", {})
        results["sleep"] = {
            "data": {
                "hours": sleep_data.get("totalMinutesAsleep", 0) / 60,
                "quality": "Good" if sleep_data.get("totalMinutesAsleep", 0) > 420 else "Fair",
            },
            "updated_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
        }
    except Exception as e:
        log.warning("Fitbit sleep sync failed: %s", e)

    # Heart rate
    try:
        data = await _fitbit_get(f"activities/heart/date/{today}/1d.json")
        hr_zones = data.get("activities-heart", [{}])[0].get("value", {})
        results["heart_rate"] = {
            "data": {
                "resting": hr_zones.get("restingHeartRate", 0),
            },
            "updated_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
        }
    except Exception as e:
        log.warning("Fitbit heart rate sync failed: %s", e)

    return results


# ---------------------------------------------------------------------------
# Garmin
# ---------------------------------------------------------------------------

async def sync_garmin() -> dict:
    """Fetch today's data from Garmin Connect."""
    email = os.environ.get("KHALIL_GARMIN_EMAIL")
    password = os.environ.get("KHALIL_GARMIN_PASSWORD")
    if not email or not password:
        raise ValueError("KHALIL_GARMIN_EMAIL and KHALIL_GARMIN_PASSWORD not set")

    results = {}

    try:
        from garminconnect import Garmin
        loop = asyncio.get_event_loop()
        client = await loop.run_in_executor(None, lambda: Garmin(email, password))
        await loop.run_in_executor(None, client.login)

        today = date.today().isoformat()

        # Steps
        stats = await loop.run_in_executor(None, lambda: client.get_stats(today))
        results["steps"] = {
            "data": {
                "steps_today": stats.get("totalSteps", 0),
                "goal": stats.get("dailyStepGoal", 10000),
            },
            "updated_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
        }

        # Sleep
        try:
            sleep = await loop.run_in_executor(None, lambda: client.get_sleep_data(today))
            sleep_minutes = sleep.get("dailySleepDTO", {}).get("sleepTimeSeconds", 0) / 60
            results["sleep"] = {
                "data": {"hours": round(sleep_minutes / 60, 1)},
                "updated_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
            }
        except Exception:
            pass

        # Heart rate
        try:
            hr = await loop.run_in_executor(None, lambda: client.get_heart_rates(today))
            results["heart_rate"] = {
                "data": {"resting": hr.get("restingHeartRate", 0)},
                "updated_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
            }
        except Exception:
            pass

        # Workouts
        try:
            activities = await loop.run_in_executor(None, lambda: client.get_activities(0, 7))
            workouts = []
            for a in activities[:7]:
                workouts.append({
                    "type": a.get("activityType", {}).get("typeKey", "Unknown"),
                    "duration_min": round(a.get("duration", 0) / 60000),
                    "calories": a.get("calories", 0),
                    "date": a.get("startTimeLocal", "")[:10],
                })
            if workouts:
                results["workouts"] = {
                    "data": {"workouts": workouts},
                    "updated_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
                }
        except Exception:
            pass

    except ImportError:
        raise ValueError("garminconnect library not installed. Run: pip install garminconnect")
    except Exception as e:
        raise RuntimeError(f"Garmin sync failed: {e}") from e

    return results


# ---------------------------------------------------------------------------
# Intent handler
# ---------------------------------------------------------------------------

async def handle_intent(action: str, intent: dict, ctx) -> bool:
    if action in ("fitness_sync", "fitness_sync_fitbit"):
        fitbit_token = os.environ.get("KHALIL_FITBIT_TOKEN")
        garmin_email = os.environ.get("KHALIL_GARMIN_EMAIL")

        if action == "fitness_sync_fitbit" or (action == "fitness_sync" and fitbit_token):
            if not fitbit_token:
                await ctx.reply("Set `KHALIL_FITBIT_TOKEN` env var with your Fitbit OAuth token.")
                return True
            try:
                results = await sync_fitbit()
                cache = _read_cache()
                cache.update(results)
                _write_cache(cache)
                metrics = list(results.keys())
                await ctx.reply(f"✅ Fitbit synced: {', '.join(metrics)}\nUse \"health summary\" to see the data.")
            except Exception as e:
                await ctx.reply(f"❌ Fitbit sync failed: {e}")
            return True

        if action == "fitness_sync" and garmin_email:
            # Fall through to Garmin
            action = "fitness_sync_garmin"

        if action == "fitness_sync" and not fitbit_token and not garmin_email:
            await ctx.reply(
                "No wearable configured. Set one of:\n"
                "  • `KHALIL_FITBIT_TOKEN` for Fitbit\n"
                "  • `KHALIL_GARMIN_EMAIL` + `KHALIL_GARMIN_PASSWORD` for Garmin"
            )
            return True

    if action == "fitness_sync_garmin":
        if not os.environ.get("KHALIL_GARMIN_EMAIL"):
            await ctx.reply("Set `KHALIL_GARMIN_EMAIL` and `KHALIL_GARMIN_PASSWORD` env vars.")
            return True
        try:
            results = await sync_garmin()
            cache = _read_cache()
            cache.update(results)
            _write_cache(cache)
            metrics = list(results.keys())
            await ctx.reply(f"✅ Garmin synced: {', '.join(metrics)}\nUse \"health summary\" to see the data.")
        except Exception as e:
            await ctx.reply(f"❌ Garmin sync failed: {e}")
        return True

    return False
