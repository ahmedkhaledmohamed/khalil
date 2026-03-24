"""Webhook handler registry."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from webhooks import WebhookHandler

log = logging.getLogger("khalil.webhooks")

_handlers: dict[str, WebhookHandler] = {}


def register(source: str, handler: WebhookHandler) -> None:
    _handlers[source] = handler
    log.info("Webhook handler registered: %s", source)


def get(source: str) -> WebhookHandler | None:
    return _handlers.get(source)


def list_sources() -> list[str]:
    return list(_handlers.keys())
