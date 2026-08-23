"""Authenticated localhost webhook for Codex and Claude Code lifecycle events."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

import keyring

from config import KEYRING_SERVICE

log = logging.getLogger("khalil.webhooks.coding_agent")

_SECRET_KEY = "webhook-secret-coding-agent"
_MAX_CLOCK_SKEW_SECONDS = 300


class CodingAgentWebhookHandler:
    """Validate and deliver locally emitted coding-agent events."""

    async def validate(self, headers: dict, body: bytes) -> bool:
        secret = keyring.get_password(KEYRING_SERVICE, _SECRET_KEY)
        if not secret:
            log.warning("No coding-agent webhook secret configured in keyring")
            return False

        try:
            timestamp = int(headers.get("x-khalil-timestamp", ""))
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - timestamp) > _MAX_CLOCK_SKEW_SECONDS:
            return False

        signature = headers.get("x-khalil-signature", "")
        if not signature.startswith("sha256="):
            return False
        signed = str(timestamp).encode() + b"." + body
        expected = "sha256=" + hmac.new(
            secret.encode(), signed, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    async def deliver(self, payload: dict, channel, chat_id: int | str) -> dict:
        from actions.dev_tools import record_coding_agent_event

        return await record_coding_agent_event(payload, channel, chat_id)
