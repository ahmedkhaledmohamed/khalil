"""Channel registry — auto-discovers available channels based on credentials."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from channels import Channel

log = logging.getLogger("pharoclaw.channels.registry")

_channels: dict[str, "Channel"] = {}


def register(name: str, channel: "Channel") -> None:
    """Register a channel instance."""
    _channels[name] = channel
    log.info("Channel registered: %s (%s)", name, channel.channel_type.value)


def get(name: str) -> "Channel | None":
    """Get a registered channel by name."""
    return _channels.get(name)


def get_active_channels() -> list["Channel"]:
    """Return all registered channels."""
    return list(_channels.values())


def get_primary() -> "Channel | None":
    """Return the primary channel (first registered, typically Telegram)."""
    return next(iter(_channels.values()), None)
