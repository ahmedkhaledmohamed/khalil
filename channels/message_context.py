"""MessageContext — channel-agnostic reply interface for all message processing."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from channels import Channel, ChannelType, IncomingMessage, SentMessage

log = logging.getLogger("khalil.message_context")


@dataclass
class MessageContext:
    """Unified context for processing a message from any channel.

    All message handlers receive this instead of a Telegram Update.
    Use ctx.reply() instead of update.message.reply_text().

    When auto_save_replies is True, every reply() call automatically saves
    the response text to conversation history via _save_fn. This ensures
    action handler replies (skills, shell, extensions) are recorded even
    when the dispatch path returns early.
    """
    channel: Channel
    chat_id: int | str
    user_id: int | str | None = None
    channel_type: ChannelType | None = None
    incoming: IncomingMessage | None = None
    _raw_update: Any = None  # Telegram Update for backwards compat
    auto_save_replies: bool = False
    _save_fn: Callable[[int | str, str, str], None] | None = None
    _replied: bool = field(default=False, init=False, repr=False)

    async def reply(self, text: str, *, buttons=None, parse_mode=None,
                    reply_markup=None, disable_web_page_preview=False) -> SentMessage:
        """Send a text reply. Works across all channels.

        If auto_save_replies is True, also saves the reply to conversation
        history so action handlers don't need to call save_message explicitly.
        """
        result = await self.channel.send_message(
            self.chat_id, text, buttons=buttons, parse_mode=parse_mode,
        )
        if self.auto_save_replies and self._save_fn and text:
            try:
                self._save_fn(self.chat_id, "assistant", text[:4000])
                self._replied = True
            except Exception as e:
                log.warning("Auto-save reply failed: %s", e)
        return result

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

    async def reply_video(self, video_path: str, caption: str = "") -> SentMessage | None:
        """Send a video reply. Returns None if channel doesn't support video."""
        if hasattr(self.channel, 'send_video'):
            return await self.channel.send_video(self.chat_id, video_path, caption=caption)
        return None

    async def reply_document(self, doc_path: str, caption: str = "") -> SentMessage | None:
        """Send a document/file reply. Returns None if channel doesn't support documents."""
        if hasattr(self.channel, 'send_document'):
            return await self.channel.send_document(self.chat_id, doc_path, caption=caption)
        return None

    async def typing(self):
        """Show typing indicator."""
        await self.channel.send_typing(self.chat_id)
