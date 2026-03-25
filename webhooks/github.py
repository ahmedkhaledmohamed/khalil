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
        if "pusher" in payload:
            return self._handle_push(payload)
        elif "pull_request" in payload:
            return self._handle_pr(payload)
        elif "issue" in payload:
            return self._handle_issue(payload)
        return None

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
