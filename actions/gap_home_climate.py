"""Control home thermostat via Home Assistant REST API.

Requires a long-lived access token (not a bot token).
Setup:
    keyring.set_password("khalil-assistant", "homeassistant-token", "<long-lived-token>")
    keyring.set_password("khalil-assistant", "homeassistant-url", "http://homeassistant.local:8123")
    # Optional — defaults to first discovered climate entity:
    keyring.set_password("khalil-assistant", "homeassistant-climate-entity", "climate.living_room")
"""

import asyncio
import logging
import sqlite3

import httpx
import keyring

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.gap_home_climate")

_tables_ready = False
VALID_MODES = {"heat", "cool", "auto", "off", "heat_cool", "fan_only", "dry"}
TEMP_MIN, TEMP_MAX = 10.0, 35.0  # °C


def ensure_tables(conn: sqlite3.Connection):
    """Create tables. Called once at startup."""
    global _tables_ready
    conn.execute(
        "CREATE TABLE IF NOT EXISTS climate_actions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, "
        "detail TEXT, old_value TEXT, new_value TEXT, "
        "status TEXT DEFAULT 'ok', created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.commit()
    _tables_ready = True


def _ensure_tables_once():
    global _tables_ready
    if _tables_ready:
        return
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
    finally:
        conn.close()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _log_action(action: str, detail: str = "", old_value: str = "",
                new_value: str = "", status: str = "ok"):
    _ensure_tables_once()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO climate_actions (action, detail, old_value, new_value, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (action, detail, old_value, new_value, status),
        )
        conn.commit()
    finally:
        conn.close()


def _get_ha_config() -> tuple[str, str, str]:
    token = keyring.get_password(KEYRING_SERVICE, "homeassistant-token")
    base_url = keyring.get_password(KEYRING_SERVICE, "homeassistant-url")
    if not token or not base_url:
        raise RuntimeError(
            "Home Assistant not configured. Run:\n"
            '  keyring.set_password("khalil-assistant", "homeassistant-token", "<token>")\n'
            '  keyring.set_password("khalil-assistant", "homeassistant-url", "<url>")'
        )
    entity_id = keyring.get_password(KEYRING_SERVICE, "homeassistant-climate-entity") or ""
    return base_url.rstrip("/"), token, entity_id


# --- Core sync functions (called via asyncio.to_thread) ---

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _discover_entity_sync(base_url: str, token: str) -> str:
    resp = httpx.get(f"{base_url}/api/states", headers=_headers(token), timeout=10)
    resp.raise_for_status()
    for s in resp.json():
        if s.get("entity_id", "").startswith("climate."):
            return s["entity_id"]
    raise RuntimeError("No climate entities found in Home Assistant.")


def _resolve_entity(base_url: str, token: str, entity_id: str) -> str:
    return entity_id if entity_id else _discover_entity_sync(base_url, token)


def _get_state_sync(base_url: str, token: str, entity_id: str) -> dict:
    resp = httpx.get(f"{base_url}/api/states/{entity_id}", headers=_headers(token), timeout=10)
    resp.raise_for_status()
    data = resp.json()
    a = data.get("attributes", {})
    return {
        "entity_id": entity_id,
        "state": data.get("state", "unknown"),
        "current_temp": a.get("current_temperature"),
        "target_temp": a.get("temperature"),
        "target_temp_high": a.get("target_temp_high"),
        "target_temp_low": a.get("target_temp_low"),
        "hvac_action": a.get("hvac_action", ""),
        "unit": a.get("temperature_unit", "°C"),
        "friendly_name": a.get("friendly_name", entity_id),
    }


def _set_temp_sync(base_url: str, token: str, entity_id: str, temp: float):
    resp = httpx.post(
        f"{base_url}/api/services/climate/set_temperature",
        headers=_headers(token), json={"entity_id": entity_id, "temperature": temp}, timeout=10,
    )
    resp.raise_for_status()


def _set_mode_sync(base_url: str, token: str, entity_id: str, mode: str):
    resp = httpx.post(
        f"{base_url}/api/services/climate/set_hvac_mode",
        headers=_headers(token), json={"entity_id": entity_id, "hvac_mode": mode}, timeout=10,
    )
    resp.raise_for_status()


def _get_history_sync(limit: int = 10) -> list[dict]:
    _ensure_tables_once()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT action, old_value, new_value, status, created_at "
            "FROM climate_actions ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Async wrappers ---

async def _status() -> dict:
    base_url, token, eid = _get_ha_config()
    eid = await asyncio.to_thread(_resolve_entity, base_url, token, eid)
    return await asyncio.to_thread(_get_state_sync, base_url, token, eid)


async def _set_temp(temp: float) -> dict:
    base_url, token, eid = _get_ha_config()
    eid = await asyncio.to_thread(_resolve_entity, base_url, token, eid)
    cur = await asyncio.to_thread(_get_state_sync, base_url, token, eid)
    old = cur.get("target_temp", "?")
    await asyncio.to_thread(_set_temp_sync, base_url, token, eid, temp)
    _log_action("set_temperature", eid, str(old), str(temp))
    return {"old": old, "new": temp}


async def _set_mode(mode: str) -> dict:
    base_url, token, eid = _get_ha_config()
    eid = await asyncio.to_thread(_resolve_entity, base_url, token, eid)
    cur = await asyncio.to_thread(_get_state_sync, base_url, token, eid)
    old = cur.get("state", "?")
    await asyncio.to_thread(_set_mode_sync, base_url, token, eid, mode)
    _log_action("set_mode", eid, old, mode)
    return {"old": old, "new": mode}


# --- Telegram command handler ---

USAGE = (
    "Usage:\n"
    "  /climate status\n"
    "  /climate set <temp>\n"
    "  /climate mode <heat|cool|auto|off>\n"
    "  /climate preview set <temp>\n"
    "  /climate preview mode <mode>\n"
    "  /climate history [n]"
)


def _fmt(s: dict) -> str:
    u = s.get("unit", "°C")
    lines = [f"🌡 **{s['friendly_name']}**", f"  Mode: {s['state']}"]
    if s.get("hvac_action"):
        lines.append(f"  Action: {s['hvac_action']}")
    if s.get("current_temp") is not None:
        lines.append(f"  Current: {s['current_temp']}{u}")
    if s.get("target_temp") is not None:
        lines.append(f"  Target: {s['target_temp']}{u}")
    if s.get("target_temp_low") is not None and s.get("target_temp_high") is not None:
        lines.append(f"  Range: {s['target_temp_low']}–{s['target_temp_high']}{u}")
    return "\n".join(lines)


def _parse_temp(s: str) -> float | None:
    try:
        t = float(s)
        return t if TEMP_MIN <= t <= TEMP_MAX else None
    except ValueError:
        return None


async def handle_climate(update, context):
    """Handle /climate command."""
    reply = update.message.reply_text
    args = context.args or []
    if not args:
        await reply(USAGE)
        return

    sub = args[0].lower()
    try:
        if sub == "status":
            await reply(_fmt(await _status()))

        elif sub == "preview" and len(args) >= 3:
            st = await _status()
            u = st.get("unit", "°C")
            psub, pval = args[1].lower(), args[2].lower()
            if psub == "set":
                t = _parse_temp(pval)
                if t is None:
                    await reply(f"Invalid or out-of-range temperature: {pval}")
                    return
                await reply(
                    f"🔍 Preview (no changes)\n"
                    f"  Target: {st.get('target_temp', '?')}{u} → {t}{u}\n"
                    f"  Room: {st.get('current_temp', '?')}{u} | Mode: {st['state']}"
                )
            elif psub == "mode":
                if pval not in VALID_MODES:
                    await reply(f"Invalid mode. Valid: {', '.join(sorted(VALID_MODES))}")
                    return
                await reply(
                    f"🔍 Preview (no changes)\n"
                    f"  Mode: {st['state']} → {pval}\n"
                    f"  Temp: {st.get('current_temp', '?')}{u} | Target: {st.get('target_temp', '?')}{u}"
                )
            else:
                await reply(USAGE)

        elif sub == "set" and len(args) >= 2:
            t = _parse_temp(args[1])
            if t is None:
                await reply(f"Invalid or out-of-range temperature: {args[1]} (allowed {TEMP_MIN}–{TEMP_MAX}°C)")
                return
            r = await _set_temp(t)
            await reply(f"✅ Temperature: {r['old']}°C → {r['new']}°C")

        elif sub == "mode" and len(args) >= 2:
            mode = args[1].lower()
            if mode not in VALID_MODES:
                await reply(f"Invalid mode: {mode}\nValid: {', '.join(sorted(VALID_MODES))}")
                return
            r = await _set_mode(mode)
            await reply(f"✅ Mode: {r['old']} → {r['new']}")

        elif sub == "history":
            n = 10
            if len(args) >= 2:
                try:
                    n = min(int(args[1]), 50)
                except ValueError:
                    pass
            hist = await asyncio.to_thread(_get_history_sync, n)
            if not hist:
                await reply("No climate actions recorded.")
                return
            lines = [f"📋 Last {len(hist)} actions\n"]
            for h in hist:
                ts = h.get("created_at", "?")[:16]
                lines.append(f"  {ts} | {h['action']}: {h.get('old_value','')} → {h.get('new_value','')}")
            await reply("\n".join(lines)[:4096])

        else:
            await reply(USAGE)

    except RuntimeError as e:
        await reply(f"⚠️ {e}")
    except httpx.HTTPStatusError as e:
        _log_action(sub, str(args), "", "", status="error")
        await reply(f"⚠️ HA API error {e.response.status_code}: {e.response.text[:200]}")
    except httpx.ConnectError:
        await reply("⚠️ Cannot reach Home Assistant. Check URL and that HA is running.")
