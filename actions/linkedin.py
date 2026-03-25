"""LinkedIn integration — scraper approach via Voyager API + httpx.

LinkedIn deprecated their personal API, so this uses session cookies
and the internal Voyager API endpoints (more stable than HTML scraping).

Auth: li_at session cookie stored in keyring under KEYRING_SERVICE.
"""

import logging

import httpx
import keyring
from bs4 import BeautifulSoup

from config import KEYRING_SERVICE

log = logging.getLogger("khalil.actions.linkedin")

KEYRING_KEY = "linkedin-session-cookie"
VOYAGER_BASE = "https://www.linkedin.com/voyager/api"
HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/vnd.linkedin.normalized+json+2.1",
    "x-restli-protocol-version": "2.0.0",
}


def _get_session_cookie() -> str:
    """Read li_at cookie from keyring. Raises ValueError if missing."""
    token = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY)
    if not token:
        raise ValueError(
            f"LinkedIn session cookie not found in keyring "
            f"(service={KEYRING_SERVICE!r}, key={KEYRING_KEY!r}). "
            f"Set it with: keyring set {KEYRING_SERVICE} {KEYRING_KEY}"
        )
    return token


def _build_client() -> httpx.AsyncClient:
    """Build an httpx client with LinkedIn auth headers."""
    token = _get_session_cookie()
    headers = {**HEADERS_BASE, "Cookie": f"li_at={token}"}
    return httpx.AsyncClient(
        headers=headers, timeout=15, follow_redirects=True,
    )


async def _voyager_get(path: str, params: dict | None = None) -> dict:
    """Make an authenticated GET to Voyager API. Handles common errors."""
    async with _build_client() as client:
        resp = await client.get(f"{VOYAGER_BASE}{path}", params=params)

        if resp.status_code == 401:
            raise ValueError("LinkedIn session expired — refresh your li_at cookie.")
        if resp.status_code == 429:
            raise RuntimeError("LinkedIn rate limit hit — try again later.")

        resp.raise_for_status()
        return resp.json()


async def get_profile_views() -> dict:
    """Get profile view count and recent viewers."""
    try:
        data = await _voyager_get(
            "/identity/wvmpCards",
            params={"q": "cardType", "cardTypes": "List(PROFILE_VIEWS)"},
        )
        # Extract view count from response
        cards = data.get("included", [])
        view_count = 0
        viewers = []
        for item in cards:
            if "numViews" in item:
                view_count = item["numViews"]
            if "viewerName" in item or "publicIdentifier" in item:
                viewers.append({
                    "name": item.get("viewerName", "LinkedIn Member"),
                    "headline": item.get("viewerHeadline", ""),
                })
        return {"view_count": view_count, "viewers": viewers}
    except Exception as e:
        log.error("Failed to fetch profile views: %s", e)
        raise


async def get_connection_requests() -> list[dict]:
    """Get pending connection invitations."""
    try:
        data = await _voyager_get(
            "/relationships/invitationViews",
            params={"q": "receivedInvitation", "count": 20},
        )
        invitations = []
        for item in data.get("included", []):
            if item.get("$type", "").endswith("Invitation"):
                invitations.append({
                    "from": item.get("fromMemberName", "Unknown"),
                    "headline": item.get("fromMemberHeadline", ""),
                    "message": item.get("message", ""),
                    "sent_at": item.get("sentTime", ""),
                })
        return invitations
    except Exception as e:
        log.error("Failed to fetch connection requests: %s", e)
        raise


async def get_recruiter_messages(limit: int = 5) -> list[dict]:
    """Get recent InMail / messaging conversations."""
    try:
        data = await _voyager_get(
            "/messaging/conversations",
            params={"keyVersion": "LEGACY_INBOX", "count": limit},
        )
        messages = []
        for item in data.get("included", []):
            if item.get("$type", "").endswith("MessagingMessage"):
                sender = item.get("from", {})
                messages.append({
                    "from": sender.get("entityName", "Unknown"),
                    "subject": item.get("subject", ""),
                    "body": item.get("body", {}).get("text", "")[:300],
                    "timestamp": item.get("deliveredAt", ""),
                })
        return messages[:limit]
    except Exception as e:
        log.error("Failed to fetch messages: %s", e)
        raise


async def search_jobs(query: str, location: str = "Toronto") -> list[dict]:
    """Search LinkedIn jobs. Returns list of job dicts."""
    try:
        data = await _voyager_get(
            "/search/hits",
            params={
                "q": "bolts",
                "queryContext": f"List(spellCorrectionEnabled->true,"
                               f"relatedSearchesEnabled->true)",
                "keywords": query,
                "location": location,
                "origin": "JOBS_HOME_SEARCH_CARDS",
                "count": 10,
            },
        )
        jobs = []
        for item in data.get("included", []):
            if "title" in item and "companyName" in item:
                jobs.append({
                    "title": item.get("title", ""),
                    "company": item.get("companyName", ""),
                    "location": item.get("formattedLocation", location),
                    "url": item.get("jobPostingUrl", ""),
                    "listed_at": item.get("listedAt", ""),
                })
        return jobs
    except Exception as e:
        log.error("LinkedIn job search failed: %s", e)
        raise
