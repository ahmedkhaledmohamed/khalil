"""Combined DigitalOcean dashboard — droplet status + billing in one command.

Merges the frequently co-used 'digitalocean_spend' and 'digitalocean_status'
actions into a single /do command with subcommands.

API: DigitalOcean REST API v2 (https://docs.digitalocean.com/reference/api/)
Auth: Personal access token (read scope) stored in system keyring.
Setup: keyring.set_password('khalil-assistant', 'digitalocean-api-token', 'YOUR_TOKEN')
"""

import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import keyring

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.digitalocean_spend_digitalocean_status")

_BASE_URL = "https://api.digitalocean.com/v2"
_TOKEN_KEY = "digitalocean-api-token"
_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create tables for logging DO dashboard checks. Called once at startup."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS digitalocean_dashboard_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            droplet_count INTEGER,
            mtd_spend TEXT,
            notes TEXT
        )"""
    )
    conn.commit()
    _tables_ensured = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _get_token() -> str:
    """Read the DigitalOcean API token from the system keyring."""
    token = keyring.get_password(KEYRING_SERVICE, _TOKEN_KEY)
    if not token:
        raise ValueError(
            f"DigitalOcean token not found. Set via:\n"
            f"  keyring.set_password('{KEYRING_SERVICE}', '{_TOKEN_KEY}', 'YOUR_TOKEN')"
        )
    return token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }


# --- Core sync functions (called via asyncio.to_thread) ---


def _fetch_droplets() -> list[dict]:
    """List all droplets with key attributes."""
    with httpx.Client(timeout=15) as client:
        resp = client.get(f"{_BASE_URL}/droplets", headers=_headers())
        resp.raise_for_status()
    droplets = resp.json().get("droplets", [])
    return [
        {
            "id": d["id"],
            "name": d["name"],
            "status": d["status"],
            "ip": (d.get("networks", {}).get("v4", [{}])[0].get("ip_address")),
            "memory": d["memory"],
            "vcpus": d["vcpus"],
            "region": d["region"]["slug"],
        }
        for d in droplets
    ]


def _fetch_billing() -> dict:
    """Get month-to-date billing balance."""
    with httpx.Client(timeout=15) as client:
        resp = client.get(f"{_BASE_URL}/customers/my/balance", headers=_headers())
        resp.raise_for_status()
    return resp.json()


def _log_dashboard(droplets: list[dict], billing: dict):
    """Log a combined dashboard check to the DB."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).isoformat()
    mtd = billing.get("month_to_date_usage", "?")
    conn = _get_conn()
    try:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO digitalocean_dashboard_log (checked_at, droplet_count, mtd_spend) "
            "VALUES (?, ?, ?)",
            (now, len(droplets), str(mtd)),
        )
        conn.commit()
    finally:
        conn.close()


def _get_history(limit: int = 10) -> list[dict]:
    """Fetch recent dashboard check history."""
    conn = _get_conn()
    try:
        ensure_tables(conn)
        rows = conn.execute(
            "SELECT checked_at, droplet_count, mtd_spend, notes "
            "FROM digitalocean_dashboard_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Async wrappers ---


async def _async_fetch_all() -> tuple[list[dict], dict]:
    """Fetch droplets and billing concurrently."""
    droplets, billing = await asyncio.gather(
        asyncio.to_thread(_fetch_droplets),
        asyncio.to_thread(_fetch_billing),
    )
    await asyncio.to_thread(_log_dashboard, droplets, billing)
    return droplets, billing


async def _async_get_history(limit: int = 10) -> list[dict]:
    return await asyncio.to_thread(_get_history, limit)


# --- Formatting ---


def _format_dashboard(droplets: list[dict], billing: dict) -> str:
    """Format combined status + spend for Telegram."""
    lines = ["\U0001f5a5 DigitalOcean Dashboard", ""]

    # Billing
    mtd = billing.get("month_to_date_usage", "?")
    balance = billing.get("account_balance", "?")
    generated = billing.get("month_to_date_balance", "?")
    lines.append(f"\U0001f4b5 Billing (month-to-date)")
    lines.append(f"  Usage: ${mtd}")
    lines.append(f"  Balance: ${balance}")
    if generated != "?":
        lines.append(f"  Generated: ${generated}")
    lines.append("")

    # Droplets
    if not droplets:
        lines.append("No droplets found.")
    else:
        lines.append(f"\U0001f4e1 Droplets ({len(droplets)})")
        for d in droplets:
            status_icon = "\u2705" if d["status"] == "active" else "\u26a0\ufe0f"
            lines.append(
                f"  {status_icon} {d['name']} — {d['status']} "
                f"({d['vcpus']}vCPU, {d['memory']}MB, {d['region']})"
            )
            if d.get("ip"):
                lines.append(f"      IP: {d['ip']}")

    return "\n".join(lines)


def _format_droplets_only(droplets: list[dict]) -> str:
    """Format droplet status only."""
    if not droplets:
        return "No droplets found."
    lines = [f"\U0001f4e1 Droplets ({len(droplets)})", ""]
    for d in droplets:
        status_icon = "\u2705" if d["status"] == "active" else "\u26a0\ufe0f"
        lines.append(
            f"  {status_icon} {d['name']} — {d['status']} "
            f"({d['vcpus']}vCPU, {d['memory']}MB, {d['region']})"
        )
        if d.get("ip"):
            lines.append(f"      IP: {d['ip']}")
    return "\n".join(lines)


def _format_billing_only(billing: dict) -> str:
    """Format billing info only."""
    mtd = billing.get("month_to_date_usage", "?")
    balance = billing.get("account_balance", "?")
    generated = billing.get("month_to_date_balance", "?")
    lines = ["\U0001f4b5 DigitalOcean Billing", ""]
    lines.append(f"  Month-to-date: ${mtd}")
    lines.append(f"  Balance: ${balance}")
    if generated != "?":
        lines.append(f"  Generated: ${generated}")
    return "\n".join(lines)


def _format_history(records: list[dict]) -> str:
    """Format dashboard history for Telegram."""
    if not records:
        return "No dashboard check history yet."
    lines = ["\U0001f4ca Recent DO Checks", ""]
    for r in records:
        ts = r["checked_at"][:16]
        lines.append(f"  {ts}  droplets={r['droplet_count']}  MTD=${r['mtd_spend']}")
    return "\n".join(lines)


# --- Telegram handler ---


async def handle_do(update, context):
    """Handle /do command — combined DigitalOcean dashboard.

    Subcommands:
        /do              — full dashboard (droplets + billing)
        /do status       — droplet status only
        /do spend        — billing only
        /do history      — recent dashboard check history
        /do history N    — last N checks (default 10, max 50)
    """
    args = context.args or []
    sub = args[0].lower() if args else ""

    try:
        if sub == "history":
            limit = 10
            if len(args) > 1:
                try:
                    limit = min(int(args[1]), 50)
                except ValueError:
                    await update.message.reply_text("Usage: /do history [N]")
                    return
            records = await _async_get_history(limit)
            await update.message.reply_text(_format_history(records))

        elif sub == "status":
            await update.message.reply_text("Checking droplets...")
            droplets = await asyncio.to_thread(_fetch_droplets)
            await update.message.reply_text(_format_droplets_only(droplets))

        elif sub == "spend":
            await update.message.reply_text("Checking billing...")
            billing = await asyncio.to_thread(_fetch_billing)
            await update.message.reply_text(_format_billing_only(billing))

        else:
            # Default: full dashboard
            await update.message.reply_text("Checking DigitalOcean...")
            droplets, billing = await _async_fetch_all()
            text = _format_dashboard(droplets, billing)
            # Respect Telegram 4096 char limit
            if len(text) > 4096:
                text = text[:4090] + "\n..."
            await update.message.reply_text(text)

    except ValueError as e:
        # Missing token
        await update.message.reply_text(f"\u26a0\ufe0f {e}")
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(f"\u274c DO API error: HTTP {e.response.status_code}")
    except httpx.ConnectError:
        await update.message.reply_text("\u274c Cannot reach DigitalOcean API")
    except Exception as e:
        log.exception("DigitalOcean dashboard error")
        await update.message.reply_text(f"\u274c Error: {e}")
