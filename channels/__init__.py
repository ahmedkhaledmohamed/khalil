"""Channel abstraction layer for Khalil.

Decouples message I/O from any specific platform (Telegram, Discord, etc.)
so the core logic in server.py never imports platform-specific libraries.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ChannelType(enum.Enum):
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    WHATSAPP = "whatsapp"


@dataclass
class ActionButton:
    """Platform-agnostic inline button."""
    label: str
    callback_data: str


@dataclass
class IncomingMessage:
    """Normalized inbound message from any channel."""
    text: str
    chat_id: int | str
    user_id: int | str | None = None
    channel_type: ChannelType = ChannelType.TELEGRAM
    reply_to_msg_id: int | str | None = None
    raw: Any = None  # original platform object (e.g., telegram Update)

    # Extracted command info (set by channel adapter)
    command: str | None = None       # e.g. "search" (without /)
    command_args: str | None = None  # everything after the command


@dataclass
class SentMessage:
    """Handle to a message we sent — allows editing/deleting later."""
    chat_id: int | str
    message_id: int | str
    channel: "Channel | None" = None

    async def edit(self, text: str, **kwargs) -> None:
        if self.channel:
            await self.channel.edit_message(self.chat_id, self.message_id, text, **kwargs)

    async def delete(self) -> None:
        if self.channel:
            await self.channel.delete_message(self.chat_id, self.message_id)


class Channel(ABC):
    """Abstract messaging channel interface."""

    channel_type: ChannelType

    @abstractmethod
    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: list[list[ActionButton]] | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage:
        """Send a text message, optionally with inline buttons."""
        ...

    @abstractmethod
    async def edit_message(
        self,
        chat_id: int | str,
        message_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        """Edit a previously sent message."""
        ...

    @abstractmethod
    async def delete_message(
        self,
        chat_id: int | str,
        message_id: int | str,
    ) -> None:
        """Delete a previously sent message."""
        ...

    @abstractmethod
    async def send_typing(self, chat_id: int | str) -> None:
        """Show typing/processing indicator."""
        ...
