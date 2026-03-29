"""Slack bidirectional channel — send and receive messages via Slack.

Uses slack-sdk AsyncWebClient for sending and slack-bolt AsyncApp with
AsyncSocketModeHandler for receiving (no public URL needed).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from channels import (
    ActionButton,
    Channel,
    ChannelType,
    IncomingMessage,
    SentMessage,
)
from channels.message_context import MessageContext

log = logging.getLogger("pharoclaw.channels.slack")


def _buttons_to_blocks(text: str, buttons: list[list[ActionButton]] | None) -> list[dict]:
    """Convert generic ActionButton grid to Slack Block Kit blocks.

    Always includes a section block for the message text, plus an actions
    block if buttons are provided.
    """
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]
    if not buttons:
        return blocks

    elements = []
    for row in buttons:
        for btn in row:
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": btn.label},
                "action_id": btn.callback_data,
                "value": btn.callback_data,
            })
    if elements:
        blocks.append({"type": "actions", "elements": elements})
    return blocks


class SlackChannel(Channel):
    """Bidirectional Slack channel using slack-sdk and slack-bolt."""

    channel_type = ChannelType.SLACK

    def __init__(self, bot_token: str, app_token: str):
        self._bot_token = bot_token
        self._app_token = app_token
        self._client = None  # AsyncWebClient, lazy init
        self._app = None  # AsyncApp, lazy init

    def _get_client(self):
        if self._client is None:
            from slack_sdk.web.async_client import AsyncWebClient
            self._client = AsyncWebClient(token=self._bot_token)
        return self._client

    # --- Channel protocol ---

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: list[list[ActionButton]] | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage:
        client = self._get_client()
        kwargs: dict[str, Any] = {"channel": str(chat_id), "text": text}
        if buttons:
            kwargs["blocks"] = _buttons_to_blocks(text, buttons)

        resp = await client.chat_postMessage(**kwargs)
        msg_ts = resp.get("ts", "")
        return SentMessage(chat_id=chat_id, message_id=msg_ts, channel=self)

    async def edit_message(
        self,
        chat_id: int | str,
        message_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        client = self._get_client()
        await client.chat_update(
            channel=str(chat_id),
            ts=str(message_id),
            text=text,
        )

    async def delete_message(
        self,
        chat_id: int | str,
        message_id: int | str,
    ) -> None:
        client = self._get_client()
        await client.chat_delete(
            channel=str(chat_id),
            ts=str(message_id),
        )

    async def send_typing(self, chat_id: int | str) -> None:
        # Slack doesn't have a persistent typing indicator API for bots.
        # Best-effort no-op.
        pass

    async def send_photo(
        self, chat_id: int | str, photo_path: str, caption: str = ""
    ) -> SentMessage | None:
        client = self._get_client()
        resp = await client.files_upload_v2(
            channel=str(chat_id),
            file=photo_path,
            initial_comment=caption,
        )
        ts = _extract_file_ts(resp)
        return SentMessage(chat_id=chat_id, message_id=ts or "", channel=self)

    async def send_voice(
        self, chat_id: int | str, audio_path: str
    ) -> SentMessage | None:
        client = self._get_client()
        resp = await client.files_upload_v2(
            channel=str(chat_id),
            file=audio_path,
            initial_comment="Voice message",
        )
        ts = _extract_file_ts(resp)
        return SentMessage(chat_id=chat_id, message_id=ts or "", channel=self)

    # --- Slack-specific helpers ---

    @staticmethod
    def extract_incoming(event: dict) -> IncomingMessage:
        """Convert a Slack event dict into a normalized IncomingMessage."""
        text = event.get("text", "")
        channel_id = event.get("channel", "")
        user_id = event.get("user", "")

        command = None
        command_args = None
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            command = parts[0].lstrip("/")
            command_args = parts[1] if len(parts) > 1 else ""

        return IncomingMessage(
            text=text,
            chat_id=channel_id,
            user_id=user_id,
            channel_type=ChannelType.SLACK,
            reply_to_msg_id=event.get("thread_ts"),
            raw=event,
            command=command,
            command_args=command_args,
        )

    async def start_socket_mode(self, message_callback: Callable) -> None:
        """Start receiving messages via Slack Socket Mode.

        Socket Mode connects via WebSocket — no public URL required.

        Args:
            message_callback: async function(ctx: MessageContext) to process messages.
        """
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        app = AsyncApp(token=self._bot_token)
        self._app = app

        @app.event("message")
        async def handle_message_event(event, say):
            # Ignore bot's own messages and message subtypes (edits, joins, etc.)
            if event.get("bot_id") or event.get("subtype"):
                return

            incoming = self.extract_incoming(event)
            ctx = MessageContext(
                channel=self,
                chat_id=incoming.chat_id,
                user_id=incoming.user_id,
                channel_type=ChannelType.SLACK,
                incoming=incoming,
            )

            try:
                await message_callback(ctx)
            except Exception as e:
                log.error("Error processing Slack message: %s", e)
                await self.send_message(incoming.chat_id, f"Error: {e}")

        @app.event("app_mention")
        async def handle_mention(event, say):
            # Same as message but triggered by @mention in channels
            await handle_message_event(event, say)

        handler = AsyncSocketModeHandler(app, self._app_token)
        log.info("Starting Slack Socket Mode...")
        await handler.start_async()


def _extract_file_ts(resp: dict) -> str | None:
    """Extract the message timestamp from a files_upload_v2 response."""
    try:
        shares = resp["file"]["shares"]
        # Shares can be under "public" or "private" depending on channel type
        share_map = shares.get("public") or shares.get("private") or {}
        first_channel = next(iter(share_map.values()))
        return first_channel[0]["ts"]
    except (KeyError, StopIteration, IndexError):
        return None
