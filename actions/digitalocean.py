"""DigitalOcean infrastructure monitoring via REST API v2.

Provides droplet status, health metrics, billing, and App Platform deployments.
Auth: API token stored in system keyring ('digitalocean-api-token').
All public functions are async.
"""

import logging

import httpx
import keyring

from config import KEYRING_SERVICE

log = logging.getLogger("pharoclaw.actions.digitalocean")

_BASE_URL = "https://api.digitalocean.com/v2"
_TOKEN_KEY = "digitalocean-api-token"

SKILL = {
    "name": "digitalocean",
    "description": "DigitalOcean — droplet status, health, and billing",
    "category": "infrastructure",
    "patterns": [
        (r"\b(?:server|droplet)\s+(?:status|health)\b", "digitalocean_status"),
        (r"\bdigitalocean\b", "digitalocean_status"),
        (r"\b(?:server|digitalocean)\s+(?:cost|bill|spend)\b", "digitalocean_spend"),
    ],
    "actions": [
        {"type": "digitalocean_status", "handler": "handle_intent", "keywords": "server droplet status health digitalocean", "description": "Droplet status"},
        {"type": "digitalocean_spend", "handler": "handle_intent", "keywords": "server cost bill spend digitalocean", "description": "Monthly spend"},
    ],
    "examples": ["Server status", "DigitalOcean spending"],
}


def _get_token() -> str:
    """Read the DigitalOcean API token from the system keyring."""
    token = keyring.get_password(KEYRING_SERVICE, _TOKEN_KEY)
    if not token:
        raise ValueError(
            f"DigitalOcean token not found. Set via: "
            f"keyring.set_password('{KEYRING_SERVICE}', '{_TOKEN_KEY}', 'YOUR_TOKEN')"
        )
    return token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }


async def get_droplets() -> list[dict]:
    """List all droplets with key attributes."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{_BASE_URL}/droplets", headers=_headers())
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


async def get_droplet_health(droplet_id: int) -> dict:
    """Fetch monitoring metrics (bandwidth, cpu, memory) for a droplet."""
    metrics = {}
    async with httpx.AsyncClient(timeout=15) as client:
        for metric in ("bandwidth", "cpu", "memory_free"):
            try:
                resp = await client.get(
                    f"{_BASE_URL}/monitoring/metrics/droplet/{metric}",
                    headers=_headers(),
                    params={"host_id": str(droplet_id), "start": "1710000000", "end": "9999999999"},
                )
                resp.raise_for_status()
                metrics[metric] = resp.json().get("data", {})
            except httpx.HTTPStatusError as e:
                log.warning("Metric %s failed for droplet %s: %s", metric, droplet_id, e)
                metrics[metric] = {"error": str(e)}
    return {"droplet_id": droplet_id, "metrics": metrics}


async def get_monthly_spend() -> dict:
    """Get month-to-date billing balance."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{_BASE_URL}/customers/my/balance", headers=_headers())
        resp.raise_for_status()
    return resp.json()


async def get_deployments(app_id: str) -> list[dict]:
    """List deployments for an App Platform app."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{_BASE_URL}/apps/{app_id}/deployments", headers=_headers())
        resp.raise_for_status()
    return resp.json().get("deployments", [])


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "digitalocean_status":
        try:
            droplets = await get_droplets()
            if not droplets:
                await ctx.reply("No droplets found.")
            else:
                lines = [f"\U0001f5a5 Droplets ({len(droplets)}):\n"]
                for d in droplets:
                    lines.append(f"  \u2022 {d.get('name', '?')} \u2014 {d.get('status', '?')} "
                                 f"({d.get('memory', '?')}MB, {d.get('region', '?')})")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"\u274c DigitalOcean failed: {e}")
        return True
    elif action == "digitalocean_spend":
        try:
            spend = await get_monthly_spend()
            await ctx.reply(f"\U0001f4b5 DigitalOcean MTD: ${spend.get('month_to_date_usage', '?')}\n"
                            f"   Balance: ${spend.get('account_balance', '?')}")
        except Exception as e:
            await ctx.reply(f"\u274c DigitalOcean billing failed: {e}")
        return True
    return False
