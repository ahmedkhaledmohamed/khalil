"""GitHub API integration — notifications, PRs, issues, repo activity.

Auth: Personal Access Token stored in keyring under KEYRING_SERVICE / "github-pat".
All public functions are async — HTTP calls use httpx.AsyncClient.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
import keyring

from config import KEYRING_SERVICE

log = logging.getLogger("khalil.actions.github_api")

BASE_URL = "https://api.github.com"


def _get_token() -> str:
    """Read GitHub PAT from keyring. Raises ValueError if missing."""
    token = keyring.get_password(KEYRING_SERVICE, "github-pat")
    if not token:
        raise ValueError(
            "GitHub PAT not found in keyring. Store it with:\n"
            f'  python3 -c "import keyring; keyring.set_password(\'{KEYRING_SERVICE}\', \'github-pat\', \'ghp_...\')"'
        )
    return token


def _headers() -> dict[str, str]:
    """Build auth + accept headers."""
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Accept": "application/vnd.github+json",
    }


async def get_notifications(unread_only: bool = True) -> list[dict]:
    """GET /notifications — fetch GitHub notifications."""
    log.debug("Fetching notifications (unread_only=%s)", unread_only)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{BASE_URL}/notifications",
            headers=_headers(),
            params={"all": str(not unread_only).lower()},
        )
        resp.raise_for_status()
        items = resp.json()
        return [
            {
                "id": n["id"],
                "repo": n["repository"]["full_name"],
                "title": n["subject"]["title"],
                "type": n["subject"]["type"],
                "reason": n["reason"],
                "updated_at": n["updated_at"],
                "unread": n["unread"],
            }
            for n in items
        ]


async def get_pr_reviews_requested() -> list[dict]:
    """GET /search/issues — PRs where review is requested from me."""
    log.debug("Fetching PRs awaiting my review")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{BASE_URL}/search/issues",
            headers=_headers(),
            params={"q": "type:pr review-requested:@me state:open"},
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "title": item["title"],
                "repo": item["repository_url"].split("/repos/")[-1],
                "url": item["html_url"],
                "user": item["user"]["login"],
                "created_at": item["created_at"],
            }
            for item in data.get("items", [])
        ]


async def get_repo_activity(repo: str, days: int = 7) -> dict:
    """Fetch commit, PR, and issue counts for a repo over the last N days."""
    log.debug("Fetching repo activity for %s (last %d days)", repo, days)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    headers = _headers()

    async with httpx.AsyncClient(timeout=10.0) as client:
        commits_resp, prs_resp, issues_resp = await _gather_repo_stats(
            client, headers, repo, since,
        )
        return {
            "repo": repo,
            "days": days,
            "commits": len(commits_resp),
            "open_prs": prs_resp.get("total_count", 0),
            "open_issues": issues_resp.get("total_count", 0),
        }


async def _gather_repo_stats(
    client: httpx.AsyncClient, headers: dict, repo: str, since: str,
) -> tuple[list, dict, dict]:
    """Parallel fetch of commits, PRs, and issues for a repo."""
    import asyncio

    async def _commits():
        resp = await client.get(
            f"{BASE_URL}/repos/{repo}/commits",
            headers=headers,
            params={"since": since, "per_page": 100},
        )
        resp.raise_for_status()
        return resp.json()

    async def _prs():
        resp = await client.get(
            f"{BASE_URL}/search/issues",
            headers=headers,
            params={"q": f"repo:{repo} type:pr state:open"},
        )
        resp.raise_for_status()
        return resp.json()

    async def _issues():
        resp = await client.get(
            f"{BASE_URL}/search/issues",
            headers=headers,
            params={"q": f"repo:{repo} type:issue state:open"},
        )
        resp.raise_for_status()
        return resp.json()

    return await asyncio.gather(_commits(), _prs(), _issues())


async def create_issue(repo: str, title: str, body: str = "") -> str:
    """POST /repos/{repo}/issues — create an issue and return its URL."""
    log.info("Creating issue in %s: %s", repo, title)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{BASE_URL}/repos/{repo}/issues",
            headers=_headers(),
            json={"title": title, "body": body},
        )
        resp.raise_for_status()
        return resp.json()["html_url"]


async def list_my_prs(state: str = "open") -> list[dict]:
    """GET /search/issues — list PRs authored by me."""
    log.debug("Fetching my PRs (state=%s)", state)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{BASE_URL}/search/issues",
            headers=_headers(),
            params={"q": f"type:pr author:@me state:{state}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "title": item["title"],
                "repo": item["repository_url"].split("/repos/")[-1],
                "url": item["html_url"],
                "state": item["state"],
                "created_at": item["created_at"],
                "comments": item["comments"],
            }
            for item in data.get("items", [])
        ]


async def get_pr_status(repo: str, pr_number: int) -> dict:
    """GET /repos/{repo}/pulls/{pr_number} — get detailed PR status."""
    log.debug("Fetching PR #%d in %s", pr_number, repo)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{BASE_URL}/repos/{repo}/pulls/{pr_number}",
            headers=_headers(),
        )
        resp.raise_for_status()
        pr = resp.json()
        return {
            "title": pr["title"],
            "state": pr["state"],
            "mergeable": pr.get("mergeable"),
            "merged": pr.get("merged", False),
            "url": pr["html_url"],
            "user": pr["user"]["login"],
            "reviewers": [r["login"] for r in pr.get("requested_reviewers", [])],
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changed_files", 0),
        }
