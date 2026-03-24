"""Discord bidirectional channel — send and receive messages via Discord bot."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

import discord

from channels import (
    ActionButton,
    Channel,
    ChannelType,
    IncomingMessage,
    SentMessage,
)
from channels.message_context import MessageContext

log = logging.getLogger("khalil.channels.discord")

_DISCORD_MSG_LIMIT = 2000


class DiscordChannel(Channel):
    """Bidirectional Discord channel using discord.py."""

    channel_type = ChannelType.DISCORD

    def __init__(self, token: str):
        self._token = token
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        self._client = discord.Client(intents=intents)
        self._ready = asyncio.Event()

    # --- Channel protocol ---

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: list[list[ActionButton]] | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage:
        await self._ready.wait()
        channel = await self._resolve_channel(chat_id)

        # Build view with buttons if provided
        view = None
        if buttons:
            view = discord.ui.View(timeout=300)
            for row in buttons:
                for btn in row:
                    button = discord.ui.Button(
                        label=btn.label,
                        custom_id=btn.callback_data,
                        style=discord.ButtonStyle.primary,
                    )
                    view.add_item(button)

        # Discord has a 2000 char limit -- split long messages
        last_msg = None
        chunks = _split_message(text)
        for i, chunk in enumerate(chunks):
            v = view if (i == len(chunks) - 1) else None
            last_msg = await channel.send(chunk, view=v)

        return SentMessage(chat_id=chat_id, message_id=last_msg.id, channel=self)

    async def edit_message(
        self,
        chat_id: int | str,
        message_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        await self._ready.wait()
        channel = await self._resolve_channel(chat_id)
        msg = await channel.fetch_message(int(message_id))
        await msg.edit(content=text[:_DISCORD_MSG_LIMIT])

    async def delete_message(
        self,
        chat_id: int | str,
        message_id: int | str,
    ) -> None:
        await self._ready.wait()
        channel = await self._resolve_channel(chat_id)
        msg = await channel.fetch_message(int(message_id))
        await msg.delete()

    async def send_typing(self, chat_id: int | str) -> None:
        await self._ready.wait()
        channel = self._client.get_channel(int(chat_id))
        if channel:
            await channel.typing()

    async def send_photo(
        self, chat_id: int | str, photo_path: str, caption: str = ""
    ) -> SentMessage | None:
        await self._ready.wait()
        channel = await self._resolve_channel(chat_id)
        msg = await channel.send(content=caption, file=discord.File(photo_path))
        return SentMessage(chat_id=chat_id, message_id=msg.id, channel=self)

    async def send_voice(
        self, chat_id: int | str, audio_path: str
    ) -> SentMessage | None:
        await self._ready.wait()
        channel = await self._resolve_channel(chat_id)
        msg = await channel.send(file=discord.File(audio_path))
        return SentMessage(chat_id=chat_id, message_id=msg.id, channel=self)

    # --- Helpers ---

    async def _resolve_channel(self, chat_id: int | str) -> discord.abc.Messageable:
        """Get a Discord channel by ID, falling back to fetch for DMs."""
        ch = self._client.get_channel(int(chat_id))
        if ch is not None:
            return ch
        try:
            return await self._client.fetch_channel(int(chat_id))
        except discord.NotFound:
            log.error("Discord channel %s not found", chat_id)
            raise

    @staticmethod
    def extract_incoming(message: discord.Message) -> IncomingMessage:
        """Convert a Discord Message into a normalized IncomingMessage."""
        text = message.content or ""

        command = None
        command_args = None
        if text.startswith("/") or text.startswith("!"):
            parts = text.split(maxsplit=1)
            command = parts[0].lstrip("/!").lower()
            command_args = parts[1] if len(parts) > 1 else ""

        return IncomingMessage(
            text=text,
            chat_id=message.channel.id,
            user_id=message.author.id,
            channel_type=ChannelType.DISCORD,
            raw=message,
            command=command,
            command_args=command_args,
        )

    # --- Bot lifecycle ---

    async def start_bot(self, message_callback: Callable) -> None:
        """Start the Discord bot and listen for messages.

        Args:
            message_callback: async function(ctx: MessageContext) to process messages
        """

        @self._client.event
        async def on_ready():
            log.info("Discord bot connected as %s", self._client.user)
            self._ready.set()

        @self._client.event
        async def on_message(message: discord.Message):
            # Ignore own messages and other bots
            if message.author == self._client.user:
                return
            if message.author.bot:
                return

            # Only respond to DMs and @mentions
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = (
                self._client.user in message.mentions if not is_dm else False
            )

            if not is_dm and not is_mentioned:
                return

            # Strip bot mention from text if present
            text = message.content
            if is_mentioned and self._client.user:
                text = text.replace(f"<@{self._client.user.id}>", "").strip()

            incoming = IncomingMessage(
                text=text,
                chat_id=message.channel.id,
                user_id=message.author.id,
                channel_type=ChannelType.DISCORD,
                raw=message,
            )

            # Extract command if present
            if text.startswith("/") or text.startswith("!"):
                parts = text.split(maxsplit=1)
                incoming.command = parts[0].lstrip("/!").lower()
                incoming.command_args = parts[1] if len(parts) > 1 else ""

            ctx = MessageContext(
                channel=self,
                chat_id=incoming.chat_id,
                user_id=incoming.user_id,
                channel_type=ChannelType.DISCORD,
                incoming=incoming,
            )

            try:
                await message_callback(ctx)
            except Exception as e:
                log.error("Error processing Discord message: %s", e)
                try:
                    await self.send_message(incoming.chat_id, f"Error: {e}")
                except Exception:
                    pass

        log.info("Starting Discord bot...")
        await self._client.start(self._token)


def _split_message(text: str) -> list[str]:
    """Split a message into chunks that fit within Discord's 2000 char limit."""
    if len(text) <= _DISCORD_MSG_LIMIT:
        return [text]

    chunks = []
    while text:
        if len(text) <= _DISCORD_MSG_LIMIT:
            chunks.append(text)
            break

        split_at = text.rfind("\n", 0, _DISCORD_MSG_LIMIT)
        if split_at == -1 or split_at < _DISCORD_MSG_LIMIT // 2:
            split_at = text.rfind(" ", 0, _DISCORD_MSG_LIMIT)
        if split_at == -1 or split_at < _DISCORD_MSG_LIMIT // 2:
            split_at = _DISCORD_MSG_LIMIT

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks
