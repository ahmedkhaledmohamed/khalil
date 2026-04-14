"""GitHub webhook handler — push, PR, and issue event notifications."""

import hashlib
import hmac
import logging

import keyring

from config import KEYRING_SERVICE
from webhooks import WebhookHandler

log = logging.getLogger("khalil.webhooks.github")


class GitHubWebhookHandler(WebhookHandler):
    source = "github"

    async def validate(self, headers: dict, body: bytes) -> bool:
        secret = keyring.get_password(KEYRING_SERVICE, "webhook-secret-github")
        if not secret:
            log.warning("No GitHub webhook secret configured in keyring")
            return False

        signature = headers.get("x-hub-signature-256", "")
        if not signature.startswith("sha256="):
            return False

        expected = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    async def handle(self, payload: dict) -> str | None:
        # Determine event type from payload structure
        event_type = None
        if "pusher" in payload:
            event_type = "push"
            result = self._handle_push(payload)
            # Tier 1: trigger knowledge reindex for changed files
            await self._trigger_reindex_from_push(payload)
        elif "pull_request" in payload:
            event_type = "pull_request"
            result = self._handle_pr(payload)
        elif "issue" in payload:
            event_type = "issue"
            result = self._handle_issue(payload)
        else:
            return None

        # Forward to workflow engine
        try:
            from workflows import get_engine
            engine = get_engine()
            if engine:
                await engine.evaluate_trigger("webhook", {
                    "source": "github",
                    "event": event_type,
                    "context": {
                        "repo": payload.get("repository", {}).get("full_name", ""),
                        "action": payload.get("action", ""),
                        "branch": payload.get("pull_request", {}).get("head", {}).get("ref", ""),
                        "merged": payload.get("pull_request", {}).get("merged", False),
                    },
                })
        except Exception as e:
            log.debug("Workflow trigger from webhook failed: %s", e)

        return result

    async def _trigger_reindex_from_push(self, payload: dict):
        """Tier 1: map changed files from push to local paths and reindex."""
        from config import REPO_PATH_MAP
        repo_name = payload.get("repository", {}).get("full_name", "")
        local_root = REPO_PATH_MAP.get(repo_name)
        if not local_root or not local_root.exists():
            return  # No local mapping — skip

        # git pull to get latest changes locally
        import subprocess
        try:
            subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=str(local_root), capture_output=True, timeout=30,
            )
        except Exception as e:
            log.warning("git pull failed for %s: %s", repo_name, e)

        # Collect changed files from all commits
        changed, removed = set(), set()
        for commit in payload.get("commits", []):
            changed.update(commit.get("added", []))
            changed.update(commit.get("modified", []))
            removed.update(commit.get("removed", []))

        SUPPORTED = {".md", ".csv"}
        local_changed = [
            str(local_root / f) for f in changed
            if any(f.endswith(ext) for ext in SUPPORTED)
        ]
        local_removed = [
            str(local_root / f) for f in removed
            if any(f.endswith(ext) for ext in SUPPORTED)
        ]

        if local_changed:
            try:
                from knowledge.watcher import trigger_reindex_files
                result = await trigger_reindex_files(local_changed)
                log.info("Webhook reindex for %s: %d indexed, %d skipped",
                         repo_name, result.get("indexed", 0), result.get("skipped", 0))
            except Exception as e:
                log.warning("Webhook reindex failed for %s: %s", repo_name, e)

        if local_removed:
            try:
                from knowledge.watcher import remove_indexed_files
                await remove_indexed_files(local_removed)
            except Exception as e:
                log.warning("Webhook remove failed for %s: %s", repo_name, e)

    def _handle_push(self, payload: dict) -> str:
        repo = payload.get("repository", {}).get("full_name", "unknown")
        ref = payload.get("ref", "").replace("refs/heads/", "")
        pusher = payload.get("pusher", {}).get("name", "unknown")
        commits = payload.get("commits", [])
        msg = f"\U0001f500 Push to {repo}/{ref} by {pusher}\n"
        msg += f"{len(commits)} commit(s)"
        if commits:
            msg += ":\n" + "\n".join(
                f"  \u2022 {c.get('message', '').split(chr(10))[0]}"
                for c in commits[:5]
            )
        return msg

    def _handle_pr(self, payload: dict) -> str:
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})
        repo = payload.get("repository", {}).get("full_name", "unknown")
        title = pr.get("title", "")
        number = pr.get("number", "")
        user = pr.get("user", {}).get("login", "unknown")

        # Hot-reload extensions when an extension PR is merged
        if action == "closed" and pr.get("merged"):
            branch = pr.get("head", {}).get("ref", "")
            if "ext/" in branch or "extend" in branch:
                try:
                    import subprocess
                    subprocess.run(["git", "pull", "origin", "main"], capture_output=True, timeout=30)
                    from actions.extend import reload_all_extensions
                    reloaded = reload_all_extensions()
                    log.info("Hot-reloaded %d extensions after PR #%s merge", len(reloaded), number)
                    return (
                        f"\U0001f4cb PR #{number} merged on {repo}\n{title}\n"
                        f"Hot-reloaded {len(reloaded)} extension(s): {', '.join(reloaded) if reloaded else 'none'}"
                    )
                except Exception as e:
                    log.warning("Extension hot-reload failed after PR #%s: %s", number, e)

        return f"\U0001f4cb PR #{number} {action} on {repo}\n{title}\nBy: {user}"

    def _handle_issue(self, payload: dict) -> str:
        action = payload.get("action", "")
        issue = payload.get("issue", {})
        repo = payload.get("repository", {}).get("full_name", "unknown")
        title = issue.get("title", "")
        number = issue.get("number", "")
        return f"\U0001f41b Issue #{number} {action} on {repo}\n{title}"
