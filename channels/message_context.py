"""MessageContext — channel-agnostic reply interface for all message processing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from channels import Channel, ChannelType, IncomingMessage, SentMessage


@dataclass
class MessageContext:
    """Unified context for processing a message from any channel.

    All message handlers receive this instead of a Telegram Update.
    Use ctx.reply() instead of update.message.reply_text().
    """
    channel: Channel
    chat_id: int | str
    user_id: int | str | None = None
    channel_type: ChannelType | None = None
    incoming: IncomingMessage | None = None
    _raw_update: Any = None  # Telegram Update for backwards compat

    async def reply(self, text: str, *, buttons=None, parse_mode=None,
                    reply_markup=None, disable_web_page_preview=False) -> SentMessage:
        """Send a text reply. Works across all channels."""
        return await self.channel.send_message(
            self.chat_id, text, buttons=buttons, parse_mode=parse_mode,
        )

    async def reply_photo(self, photo_path: str, caption: str = "") -> SentMessage | None:
        """Send a photo reply. Returns None if channel doesn't support photos."""
        if hasattr(self.channel, 'send_photo'):
            return await self.channel.send_photo(self.chat_id, photo_path, caption=caption)
        return None

    async def reply_voice(self, audio_path: str) -> SentMessage | None:
        """Send a voice reply. Returns None if channel doesn't support voice."""
        if hasattr(self.channel, 'send_voice'):
            return await self.channel.send_voice(self.chat_id, audio_path)
        return None

    async def typing(self):
        """Show typing indicator."""
        await self.channel.send_typing(self.chat_id)
