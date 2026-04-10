"""GitHub API integration — notifications, PRs, issues, repo activity.

Auth: Personal Access Token stored in keyring under KEYRING_SERVICE / "github-pat".
All public functions are async — HTTP calls use httpx.AsyncClient.
PR merge/create uses `gh` CLI for reliability.
"""

import logging
import subprocess
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
        (r"\b(?:any|show|my)\s+(?:open\s+)?(?:pull\s+requests?|prs?)\b", "github_prs"),
        (r"\bopen\s+(?:pull\s+requests?|prs?)\s+(?:on|for|in)\b", "github_prs"),
        (r"\bgithub\s+notifications?\b", "github_notifications"),
        (r"\bcheck\s+(?:my\s+)?(?:github\s+)?notifications?\b", "github_notifications"),
        (r"\b(?:show|any|my)\s+(?:github\s+)?notifications?\b", "github_notifications"),
        (r"\bunread\s+(?:github\s+)?notifications?\b", "github_notifications"),
        (r"\bmerge\s+(?:pr|pull\s+request)\s*#?\d+", "github_merge_pr"),
        (r"\bmerge\s+(?:the\s+)?(?:pr|pull\s+request)", "github_merge_pr"),
        (r"\bcreate\s+(?:a\s+)?(?:pr|pull\s+request)\b", "github_create_pr"),
        (r"\bopen\s+(?:a\s+)?(?:pr|pull\s+request)\b", "github_create_pr"),
    ],
    "actions": [
        {"type": "github_notifications", "handler": "handle_intent", "keywords": "github notifications unread alerts", "description": "Check unread GitHub notifications"},
        {"type": "github_prs", "handler": "handle_intent", "keywords": "github pull requests prs open review", "description": "List open pull requests"},
        {"type": "github_create_issue", "handler": "handle_intent", "keywords": "github create new issue file open bug", "description": "Create a new GitHub issue"},
        {
            "type": "github_merge_pr", "handler": "handle_intent",
            "keywords": "github merge pr pull request squash",
            "description": "Merge a pull request (squash merge)",
            "parameters": {
                "repo": {"type": "string", "description": "Repository in owner/name format", "required": True},
                "pr_number": {"type": "string", "description": "PR number to merge", "required": True},
            },
        },
        {
            "type": "github_create_pr", "handler": "handle_intent",
            "keywords": "github create open pr pull request branch",
            "description": "Create a new pull request",
            "parameters": {
                "repo": {"type": "string", "description": "Repository in owner/name format", "required": True},
                "title": {"type": "string", "description": "PR title", "required": True},
                "branch": {"type": "string", "description": "Source branch name"},
                "body": {"type": "string", "description": "PR description"},
            },
        },
    ],
    "examples": ["GitHub notifications", "Check my PRs", "Create issue on khalil repo", "Merge PR 184 on khalil", "Open a PR for my branch"],
    "sensor": {"function": "sense_github", "interval_min": 5, "identify_opportunities": "identify_github_opportunities"},
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


# ---------------------------------------------------------------------------
# Agent loop sensor
# ---------------------------------------------------------------------------

async def sense_github() -> dict:
    """Sensor: check for unread GitHub notifications."""
    try:
        notifs = await get_notifications(unread_only=True)
        return {"unread_notifications": notifs or []}
    except Exception as e:
        log.debug("GitHub sensor failed: %s", e)
        return {"unread_notifications": []}


def identify_github_opportunities(state: dict, last_state: dict, cooldowns: dict):
    """Identify new GitHub notifications worth surfacing."""
    import time as _time
    from agent_loop import Opportunity, Urgency, _on_cooldown

    opps = []
    now = _time.monotonic()

    gh_notifs = state.get("github_api", {}).get("unread_notifications", [])
    if gh_notifs:
        last_count = len(last_state.get("github_api", {}).get("unread_notifications", []))
        if len(gh_notifs) > last_count:
            opp_id = "github_notifications_new"
            if not _on_cooldown(opp_id, cooldowns, now, hours=1):
                titles = [n.get("title", "?") for n in gh_notifs[:5]]
                opps.append(Opportunity(
                    id=opp_id, source="github_api",
                    summary=f"\U0001f514 {len(gh_notifs)} GitHub notification(s):\n" + "\n".join(f"  \u2022 {t}" for t in titles),
                    urgency=Urgency.LOW, action_type=None,
                ))

    return opps


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
    elif action == "github_merge_pr":
        try:
            repo = intent.get("repo", "")
            pr_number = intent.get("pr_number", "")
            if not repo or not pr_number:
                await ctx.reply("I need a repo and PR number.\n"
                                "Example: merge PR 184 on ahmedkhaledmohamed/khalil")
                return True
            result = merge_pr(repo, int(pr_number))
            await ctx.reply(result)
        except Exception as e:
            await ctx.reply(f"❌ PR merge failed: {e}")
        return True
    elif action == "github_create_pr":
        try:
            repo = intent.get("repo", "")
            title = intent.get("title", "")
            branch = intent.get("branch", "")
            body = intent.get("body", "")
            if not repo or not title:
                await ctx.reply("I need a repo and title.\n"
                                "Example: create PR on user/repo titled 'Add feature X'")
                return True
            result = create_pr(repo, title, branch=branch, body=body)
            await ctx.reply(result)
        except Exception as e:
            await ctx.reply(f"❌ PR creation failed: {e}")
        return True
    return False


# ---------------------------------------------------------------------------
# PR operations via gh CLI
# ---------------------------------------------------------------------------

def merge_pr(repo: str, pr_number: int, method: str = "squash") -> str:
    """Merge a PR using gh CLI. Returns status message."""
    log.info("Merging PR #%d in %s via %s", pr_number, repo, method)
    result = subprocess.run(
        ["gh", "pr", "merge", str(pr_number),
         f"--{method}", "--delete-branch", "--repo", repo],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        return f"✅ PR #{pr_number} merged ({method}) on {repo}"
    return f"❌ Merge failed: {result.stderr.strip()[:200]}"


def create_pr(repo: str, title: str, branch: str = "", body: str = "") -> str:
    """Create a PR using gh CLI. Returns PR URL or error.

    Pre-flight checks:
    - Verifies the branch exists on the remote before attempting PR creation
    - Never accepts a pr_number parameter (GitHub assigns numbers automatically)
    """
    log.info("Creating PR in %s: %s (branch=%s)", repo, title, branch or "current")

    # Pre-flight: verify branch exists on remote
    if branch:
        check = subprocess.run(
            ["gh", "api", f"repos/{repo}/branches/{branch}"],
            capture_output=True, text=True, timeout=15,
        )
        if check.returncode != 0:
            return (
                f"❌ Branch '{branch}' not found on {repo}. "
                f"Did you push it? Run: git push -u origin {branch}"
            )

    cmd = ["gh", "pr", "create", "--title", title, "--repo", repo]
    if branch:
        cmd.extend(["--head", branch])
    if body:
        cmd.extend(["--body", body])
    else:
        cmd.extend(["--body", ""])
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        pr_url = result.stdout.strip()
        return f"✅ PR created: {pr_url}"
    return f"❌ PR creation failed: {result.stderr.strip()[:200]}"
