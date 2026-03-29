"""App Store Connect API integration — downloads, ratings, crashes, revenue.

Uses App Store Connect API v2 (REST) with JWT authentication.
Credentials stored in keyring: key ID, issuer ID, private key (.p8 contents).

All public functions are async.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import keyring

from config import KEYRING_SERVICE

log = logging.getLogger("pharoclaw.actions.appstore")

BASE_URL = "https://api.appstoreconnect.apple.com/v1"

# Keyring key names
_KEY_ID_KEY = "appstore-key-id"
_ISSUER_ID_KEY = "appstore-issuer-id"
_PRIVATE_KEY_KEY = "appstore-private-key"

SKILL = {
    "name": "appstore",
    "description": "App Store Connect — ratings, reviews, and download stats",
    "category": "apps",
    "patterns": [
        (r"\bapp\s+store\s+(?:rating|reviews?)\b", "appstore_ratings"),
        (r"\bzia\s+(?:rating|reviews?)\b", "appstore_ratings"),
        (r"\bapp\s+(?:downloads?|stats?)\b", "appstore_downloads"),
        (r"\bzia\s+(?:downloads?|stats?)\b", "appstore_downloads"),
        (r"\bhow\s+is\s+zia\b", "appstore_ratings"),
    ],
    "actions": [
        {"type": "appstore_ratings", "handler": "handle_intent", "keywords": "app store rating reviews zia", "description": "App ratings and reviews"},
        {"type": "appstore_downloads", "handler": "handle_intent", "keywords": "app store downloads stats zia", "description": "Download stats"},
    ],
    "examples": ["Zia App Store ratings", "App download stats"],
}


def _generate_jwt() -> str:
    """Create a signed JWT for App Store Connect API authentication."""
    key_id = keyring.get_password(KEYRING_SERVICE, _KEY_ID_KEY)
    issuer_id = keyring.get_password(KEYRING_SERVICE, _ISSUER_ID_KEY)
    private_key = keyring.get_password(KEYRING_SERVICE, _PRIVATE_KEY_KEY)

    if not all([key_id, issuer_id, private_key]):
        raise RuntimeError(
            "Missing App Store Connect credentials in keyring. Set: "
            f"{_KEY_ID_KEY}, {_ISSUER_ID_KEY}, {_PRIVATE_KEY_KEY}"
        )

    now = int(time.time())
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + 1200,  # 20-minute expiry
        "aud": "appstoreconnect-v1",
    }
    headers = {"kid": key_id}

    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


async def _request(method: str, path: str, params: dict | None = None) -> dict:
    """Make an authenticated request to the App Store Connect API."""
    token = _generate_jwt()
    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method, url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_app_downloads(app_id: str, days: int = 7) -> dict:
    """Fetch sales/download reports for the given app.

    Uses the salesReports endpoint with a date range filter.
    Returns raw report data as a dict.
    """
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    params = {
        "filter[reportType]": "SALES",
        "filter[reportSubType]": "SUMMARY",
        "filter[frequency]": "DAILY",
        "filter[vendorNumber]": app_id,
        "filter[reportDate]": start_date.isoformat(),
    }
    try:
        data = await _request("GET", "/salesReports", params=params)
        log.info("Fetched download report for app %s (%d days)", app_id, days)
        return data
    except httpx.HTTPStatusError as e:
        log.error("Failed to fetch downloads for %s: %s", app_id, e)
        return {"error": str(e), "status_code": e.response.status_code}


async def get_app_ratings(app_id: str) -> dict:
    """Fetch current ratings and review count for an app.

    Reads from /apps/{id}/customerReviews and aggregates.
    """
    try:
        data = await _request("GET", f"/apps/{app_id}/customerReviews", params={
            "limit": 200,
            "sort": "-createdDate",
        })
        reviews = data.get("data", [])
        if not reviews:
            return {"rating": None, "review_count": 0, "reviews": []}

        ratings = [r["attributes"]["rating"] for r in reviews if "attributes" in r]
        avg_rating = sum(ratings) / len(ratings) if ratings else None

        log.info("Fetched %d reviews for app %s (avg %.1f)", len(reviews), app_id, avg_rating or 0)
        return {
            "rating": round(avg_rating, 2) if avg_rating else None,
            "review_count": len(reviews),
            "reviews": [
                {
                    "rating": r["attributes"]["rating"],
                    "title": r["attributes"].get("title", ""),
                    "body": r["attributes"].get("body", ""),
                    "date": r["attributes"].get("createdDate", ""),
                }
                for r in reviews[:10]  # Return latest 10 for display
            ],
        }
    except httpx.HTTPStatusError as e:
        log.error("Failed to fetch ratings for %s: %s", app_id, e)
        return {"error": str(e), "status_code": e.response.status_code}


async def get_crash_reports(app_id: str, limit: int = 5) -> list[dict]:
    """Fetch recent diagnostic logs (crash reports) for an app."""
    try:
        data = await _request("GET", f"/apps/{app_id}/perfPowerMetrics", params={
            "filter[diagnosticType]": "CRASHES",
            "limit": limit,
        })
        logs = data.get("data", [])
        log.info("Fetched %d crash reports for app %s", len(logs), app_id)
        return [
            {
                "id": entry.get("id"),
                "type": entry.get("type"),
                "attributes": entry.get("attributes", {}),
            }
            for entry in logs[:limit]
        ]
    except httpx.HTTPStatusError as e:
        log.error("Failed to fetch crash reports for %s: %s", app_id, e)
        return [{"error": str(e), "status_code": e.response.status_code}]


async def get_app_revenue(app_id: str, days: int = 30) -> dict:
    """Fetch revenue data from sales reports.

    Uses the same salesReports endpoint with FINANCIAL report type.
    """
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    params = {
        "filter[reportType]": "FINANCIAL",
        "filter[reportSubType]": "SUMMARY",
        "filter[frequency]": "DAILY",
        "filter[vendorNumber]": app_id,
        "filter[reportDate]": start_date.isoformat(),
    }
    try:
        data = await _request("GET", "/financeReports", params=params)
        log.info("Fetched revenue report for app %s (%d days)", app_id, days)
        return data
    except httpx.HTTPStatusError as e:
        log.error("Failed to fetch revenue for %s: %s", app_id, e)
        return {"error": str(e), "status_code": e.response.status_code}


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "appstore_ratings":
        try:
            from config import ZIA_APP_ID
            app_id = intent.get("app_id", ZIA_APP_ID)
            if not app_id:
                await ctx.reply("No app ID configured. Set ZIA_APP_ID in config.py.")
                return True
            ratings = await get_app_ratings(app_id)
            avg = ratings.get("rating", "?")
            count = ratings.get("review_count", "?")
            await ctx.reply(f"\u2b50 App Store: {avg}\u2605 ({count} reviews)")
        except Exception as e:
            await ctx.reply(f"\u274c App Store fetch failed: {e}")
        return True
    elif action == "appstore_downloads":
        try:
            from config import ZIA_APP_ID
            app_id = intent.get("app_id", ZIA_APP_ID)
            if not app_id:
                await ctx.reply("No app ID configured. Set ZIA_APP_ID in config.py.")
                return True
            downloads = await get_app_downloads(app_id)
            await ctx.reply(f"\U0001f4ca Downloads (7d): {downloads.get('total', '?')}\n"
                            f"   Daily avg: {downloads.get('daily_avg', '?')}")
        except Exception as e:
            await ctx.reply(f"\u274c App Store fetch failed: {e}")
        return True
    return False
