"""Webhook handler abstraction for inbound event triggers."""

from __future__ import annotations
from abc import ABC, abstractmethod


class WebhookHandler(ABC):
    """Base class for webhook handlers."""

    source: str  # "github", "stripe", etc.

    @abstractmethod
    async def validate(self, headers: dict, body: bytes) -> bool:
        """Validate the webhook signature/authenticity. Return True if valid."""
        ...

    @abstractmethod
    async def handle(self, payload: dict) -> str | None:
        """Process the webhook payload. Return a notification message or None."""
        ...
