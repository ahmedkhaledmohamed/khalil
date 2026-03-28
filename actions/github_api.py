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

SKILL = {
    "name": "github_api",
    "description": "GitHub notifications, pull requests, and issue management",
    "category": "development",
    "patterns": [
        (r"\bcreate\s+(?:a\s+)?(?:github\s+)?issue\b", "github_create_issue"),
        (r"\bopen\s+(?:a\s+)?(?:github\s+)?issue\b", "github_create_issue"),
        (r"\bfile\s+(?:an?\s+)?(?:github\s+)?issue\b", "github_create_issue"),
        (r"\bnew\s+(?:github\s+)?issue\b", "github_create_issue"),
        (r"\bcheck\s+(?:my\s+)?(?:pull\s+requests?|prs?)\b", "github_prs"),
        (r"\b(?:pr|pull\s+request)\s+status\b", "github_prs"),
        (r"\blist\s+(?:my\s+)?(?:open\s+)?(?:pull\s+requests?|prs?)\b", "github_prs"),
        (r"\bgithub\s+notifications?\b", "github_notifications"),
        (r"\bcheck\s+(?:my\s+)?(?:github\s+)?notifications?\b", "github_notifications"),
        (r"\bunread\s+(?:github\s+)?notifications?\b", "github_notifications"),
    ],
    "actions": [
        {"type": "github_notifications", "handler": "handle_intent", "keywords": "github notifications unread alerts", "description": "Check unread GitHub notifications"},
        {"type": "github_prs", "handler": "handle_intent", "keywords": "github pull requests prs open review", "description": "List open pull requests"},
        {"type": "github_create_issue", "handler": "handle_intent", "keywords": "github create new issue file open bug", "description": "Create a new GitHub issue"},
    ],
    "examples": ["GitHub notifications", "Check my PRs", "Create issue on khalil repo"],
}

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


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle a natural language intent. Returns True if handled."""
    if action == "github_notifications":
        try:
            notifs = await get_notifications(unread_only=True)
            if not notifs:
                await ctx.reply("No unread GitHub notifications.")
            else:
                lines = [f"🔔 GitHub Notifications ({len(notifs)}):\n"]
                for n in notifs[:15]:
                    emoji = {"PullRequest": "📋", "Issue": "🐛"}.get(n.get("type", ""), "📌")
                    lines.append(f"  {emoji} {n.get('repo', '?')}: {n.get('title', '?')}")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"❌ GitHub notifications failed: {e}")
        return True
    elif action == "github_prs":
        try:
            prs = await list_my_prs()
            if not prs:
                await ctx.reply("No open pull requests.")
            else:
                lines = [f"📋 Open PRs ({len(prs)}):\n"]
                for pr in prs[:15]:
                    lines.append(f"  • {pr.get('title', '?')} ({pr.get('repo', '?')})")
                await ctx.reply("\n".join(lines))
        except Exception as e:
            await ctx.reply(f"❌ GitHub PR check failed: {e}")
        return True
    elif action == "github_create_issue":
        try:
            repo = intent.get("repo", "")
            title = intent.get("title", intent.get("text", ""))
            body = intent.get("body", "")
            if not repo or not title:
                await ctx.reply("I need a repo and title to create an issue.\n"
                                "Example: create issue on user/repo titled 'Bug in login'")
                return True
            url = await create_issue(repo, title, body)
            await ctx.reply(f"✅ Issue created: {url}")
        except Exception as e:
            await ctx.reply(f"❌ GitHub issue creation failed: {e}")
        return True
    return False
