"""Telegram channel adapter — wraps python-telegram-bot behind Channel protocol."""

from __future__ import annotations

import logging
import re
from typing import Any

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from channels import (
    ActionButton,
    Channel,
    ChannelType,
    IncomingMessage,
    SentMessage,
)

log = logging.getLogger("pharoclaw.channels.telegram")

_MD2_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!"


def escape_md2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return re.sub(r"([" + re.escape(_MD2_ESCAPE_CHARS) + r"])", r"\\\1", text)


def _buttons_to_markup(buttons: list[list[ActionButton]] | None) -> InlineKeyboardMarkup | None:
    """Convert generic ActionButton grid to Telegram InlineKeyboardMarkup."""
    if not buttons:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(btn.label, callback_data=btn.callback_data) for btn in row]
        for row in buttons
    ])


class TelegramChannel(Channel):
    """Channel implementation backed by python-telegram-bot."""

    channel_type = ChannelType.TELEGRAM

    def __init__(self, bot: Bot):
        self._bot = bot

    @classmethod
    def from_application(cls, app: Application) -> "TelegramChannel":
        return cls(app.bot)

    # --- Channel protocol ---

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: list[list[ActionButton]] | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage:
        markup = _buttons_to_markup(buttons)
        msg = await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=parse_mode,
        )
        return SentMessage(chat_id=chat_id, message_id=msg.message_id, channel=self)

    async def edit_message(
        self,
        chat_id: int | str,
        message_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        await self._bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
        )

    async def delete_message(
        self,
        chat_id: int | str,
        message_id: int | str,
    ) -> None:
        await self._bot.delete_message(chat_id=chat_id, message_id=message_id)

    async def send_typing(self, chat_id: int | str) -> None:
        await self._bot.send_chat_action(chat_id=chat_id, action="typing")

    async def send_photo(self, chat_id: int | str, photo_path: str, caption: str = "") -> SentMessage:
        with open(photo_path, "rb") as f:
            msg = await self._bot.send_photo(chat_id=chat_id, photo=f, caption=caption)
        return SentMessage(chat_id=chat_id, message_id=msg.message_id, channel=self)

    async def send_voice(self, chat_id: int | str, audio_path: str) -> SentMessage:
        with open(audio_path, "rb") as f:
            msg = await self._bot.send_voice(chat_id=chat_id, voice=f)
        return SentMessage(chat_id=chat_id, message_id=msg.message_id, channel=self)

    async def download_file(self, file_id: str, dest_path: str) -> str | None:
        tg_file = await self._bot.get_file(file_id)
        await tg_file.download_to_drive(dest_path)
        return dest_path

    # --- Telegram-specific helpers ---

    @staticmethod
    def extract_incoming(update: Update) -> IncomingMessage:
        """Convert a Telegram Update into a normalized IncomingMessage."""
        msg = update.message or update.edited_message
        text = msg.text or "" if msg else ""
        command = None
        command_args = None

        if msg and msg.text and msg.text.startswith("/"):
            parts = msg.text.split(maxsplit=1)
            command = parts[0].lstrip("/").split("@")[0]  # strip /command@botname
            command_args = parts[1] if len(parts) > 1 else ""

        return IncomingMessage(
            text=text,
            chat_id=update.effective_chat.id if update.effective_chat else 0,
            user_id=update.effective_user.id if update.effective_user else None,
            channel_type=ChannelType.TELEGRAM,
            reply_to_msg_id=msg.reply_to_message.message_id if msg and msg.reply_to_message else None,
            raw=update,
            command=command,
            command_args=command_args,
        )

    @staticmethod
    def approve_deny_buttons() -> list[list[ActionButton]]:
        """Standard approve/deny button row."""
        return [[
            ActionButton("✅ Approve", "action_approve"),
            ActionButton("❌ Deny", "action_deny"),
        ]]
