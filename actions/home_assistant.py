"""Home Assistant integration — control devices via the HA REST API.

Requires KHALIL_HA_URL and KHALIL_HA_TOKEN env vars.
This is a complement to the homekit.py skill — Home Assistant provides
a universal hub for all smart home devices regardless of protocol.
"""

import asyncio
import json
import logging
import os
import re
from urllib.request import urlopen, Request

log = logging.getLogger("khalil.actions.home_assistant")

HA_URL = os.environ.get("KHALIL_HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("KHALIL_HA_TOKEN", "")

SKILL = {
    "name": "home_assistant",
    "description": "Control smart home devices via Home Assistant",
    "category": "home",
    "patterns": [
        (r"\bhome\s+assistant\b", "ha_status"),
        (r"\bha\s+(?:status|devices?|entities)\b", "ha_status"),
        (r"\bturn\s+(?:on|off)\s+(?:the\s+)?(?!lights)", "ha_toggle"),
        (r"\btoggle\s+(?:the\s+)?\w+", "ha_toggle"),
        (r"\bha\s+(?:turn|toggle|switch)\b", "ha_toggle"),
        (r"\bcall\s+(?:ha\s+)?service\b", "ha_service"),
        (r"\bha\s+scenes?\b", "ha_scenes"),
        (r"\bactivate\s+(?:ha\s+)?scene\b", "ha_scenes"),
        (r"\bha\s+automations?\b", "ha_automations"),
    ],
    "actions": [
        {"type": "ha_status", "handler": "handle_intent", "keywords": "home assistant status devices entities states", "description": "Show HA device status"},
        {"type": "ha_toggle", "handler": "handle_intent", "keywords": "home assistant turn on off toggle switch device", "description": "Toggle a device"},
        {"type": "ha_service", "handler": "handle_intent", "keywords": "home assistant call service action", "description": "Call an HA service"},
        {"type": "ha_scenes", "handler": "handle_intent", "keywords": "home assistant scene activate", "description": "Activate an HA scene"},
        {"type": "ha_automations", "handler": "handle_intent", "keywords": "home assistant automation trigger", "description": "List/trigger automations"},
    ],
    "examples": [
        "Home Assistant status",
        "Turn on the fan",
        "HA scenes",
        "Toggle the bedroom light",
    ],
}


async def _ha_request(method: str, path: str, data: dict | None = None) -> dict | list | None:
    if not HA_TOKEN:
        raise ValueError("KHALIL_HA_TOKEN not set")
    url = f"{HA_URL}/api/{path}"
    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    })
    loop = asyncio.get_event_loop()
    try:
        resp = await loop.run_in_executor(None, lambda: urlopen(req, timeout=10).read())
        return json.loads(resp)
    except Exception as e:
        log.warning("HA API error: %s", e)
        raise


async def get_states(domain: str | None = None) -> list[dict]:
    states = await _ha_request("GET", "states")
    if domain:
        states = [s for s in states if s.get("entity_id", "").startswith(f"{domain}.")]
    return [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
        }
        for s in states
        if s["state"] not in ("unavailable", "unknown")
    ]


async def toggle_entity(entity_id: str) -> bool:
    try:
        await _ha_request("POST", "services/homeassistant/toggle", {"entity_id": entity_id})
        return True
    except Exception:
        return False


async def turn_on(entity_id: str) -> bool:
    try:
        await _ha_request("POST", "services/homeassistant/turn_on", {"entity_id": entity_id})
        return True
    except Exception:
        return False


async def turn_off(entity_id: str) -> bool:
    try:
        await _ha_request("POST", "services/homeassistant/turn_off", {"entity_id": entity_id})
        return True
    except Exception:
        return False


async def get_scenes() -> list[dict]:
    states = await get_states("scene")
    return states


async def activate_scene(entity_id: str) -> bool:
    try:
        await _ha_request("POST", "services/scene/turn_on", {"entity_id": entity_id})
        return True
    except Exception:
        return False


async def _find_entity(name: str) -> dict | None:
    """Fuzzy match an entity by friendly name."""
    states = await get_states()
    name_lower = name.lower()
    # Exact match
    for s in states:
        if s["name"].lower() == name_lower:
            return s
    # Contains
    for s in states:
        if name_lower in s["name"].lower():
            return s
    # Entity ID contains
    for s in states:
        if name_lower.replace(" ", "_") in s["entity_id"]:
            return s
    return None


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    query = intent.get("query", "") or intent.get("user_query", "")

    if not HA_TOKEN:
        await ctx.reply("Set `KHALIL_HA_URL` and `KHALIL_HA_TOKEN` env vars to connect to Home Assistant.")
        return True

    if action == "ha_status":
        try:
            states = await get_states()
            # Group by domain
            domains: dict[str, list] = {}
            for s in states:
                domain = s["entity_id"].split(".")[0]
                domains.setdefault(domain, []).append(s)

            lines = [f"🏠 **Home Assistant** — {len(states)} entities\n"]
            for domain in sorted(domains):
                entities = domains[domain]
                on_count = sum(1 for e in entities if e["state"] == "on")
                lines.append(f"  • **{domain}**: {len(entities)} ({on_count} on)")
            await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"❌ HA connection failed: {e}")
        return True

    elif action == "ha_toggle":
        # Extract device name and on/off intent
        text = re.sub(r"\b(?:ha\s+)?(?:turn|toggle|switch)\s+(?:on|off)\s+(?:the\s+)?", "", query, flags=re.IGNORECASE)
        text = text.strip()
        if not text:
            await ctx.reply("Which device?")
            return True

        entity = await _find_entity(text)
        if not entity:
            await ctx.reply(f"❌ No device found matching \"{text}\".")
            return True

        turn_on_match = re.search(r"\bturn\s+on\b", query, re.IGNORECASE)
        turn_off_match = re.search(r"\bturn\s+off\b", query, re.IGNORECASE)

        if turn_on_match:
            ok = await turn_on(entity["entity_id"])
            await ctx.reply(f"✅ Turned on **{entity['name']}**" if ok else f"❌ Failed to turn on {entity['name']}")
        elif turn_off_match:
            ok = await turn_off(entity["entity_id"])
            await ctx.reply(f"✅ Turned off **{entity['name']}**" if ok else f"❌ Failed to turn off {entity['name']}")
        else:
            ok = await toggle_entity(entity["entity_id"])
            await ctx.reply(f"✅ Toggled **{entity['name']}**" if ok else f"❌ Failed to toggle {entity['name']}")
        return True

    elif action == "ha_scenes":
        try:
            scenes = await get_scenes()
            if not scenes:
                await ctx.reply("No HA scenes found.")
            else:
                lines = [f"🏠 **HA Scenes** ({len(scenes)}):\n"]
                for s in scenes:
                    lines.append(f"  • **{s['name']}**")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"❌ {e}")
        return True

    elif action == "ha_service":
        await ctx.reply("Use format: \"call service light.turn_on entity_id=light.bedroom\"")
        return True

    elif action == "ha_automations":
        try:
            states = await get_states("automation")
            if not states:
                await ctx.reply("No HA automations found.")
            else:
                lines = [f"🏠 **HA Automations** ({len(states)}):\n"]
                for s in states:
                    status = "✅" if s["state"] == "on" else "❌"
                    lines.append(f"  {status} **{s['name']}**")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"❌ {e}")
        return True

    return False
