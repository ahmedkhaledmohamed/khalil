"""WhatsApp bidirectional channel via Meta Cloud API."""

from __future__ import annotations

import logging

import httpx
import keyring

from channels import (
    ActionButton,
    Channel,
    ChannelType,
    IncomingMessage,
    SentMessage,
)
from config import KEYRING_SERVICE

log = logging.getLogger("khalil.channels.whatsapp")

GRAPH_API_URL = "https://graph.facebook.com/v18.0"


class WhatsAppChannel(Channel):
    """Bidirectional WhatsApp channel using Meta Cloud API."""

    channel_type = ChannelType.WHATSAPP

    def __init__(self, phone_number_id: str, access_token: str):
        self._phone_number_id = phone_number_id
        self._access_token = access_token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: list[list[ActionButton]] | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage:
        url = f"{GRAPH_API_URL}/{self._phone_number_id}/messages"

        # WhatsApp text message
        payload: dict = {
            "messaging_product": "whatsapp",
            "to": str(chat_id),
            "type": "text",
            "text": {"body": text[:4096]},  # WhatsApp limit
        }

        # If buttons provided, use interactive message
        if buttons:
            flat_buttons = [btn for row in buttons for btn in row][:3]  # WhatsApp max 3
            payload = {
                "messaging_product": "whatsapp",
                "to": str(chat_id),
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": text[:1024]},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {
                                    "id": btn.callback_data,
                                    "title": btn.label[:20],
                                },
                            }
                            for btn in flat_buttons
                        ]
                    },
                },
            }

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()

        msg_id = data.get("messages", [{}])[0].get("id", "")
        return SentMessage(chat_id=chat_id, message_id=msg_id, channel=self)

    async def edit_message(self, chat_id, message_id, text, *, parse_mode=None) -> None:
        # WhatsApp doesn't support message editing
        pass

    async def delete_message(self, chat_id, message_id) -> None:
        # WhatsApp has limited delete support, skip for now
        pass

    async def send_typing(self, chat_id: int | str) -> None:
        """Mark message as read (closest to typing indicator in WhatsApp)."""
        # WhatsApp doesn't have a typing indicator API for business
        pass

    @staticmethod
    def extract_incoming(webhook_data: dict) -> IncomingMessage | None:
        """Extract an IncomingMessage from a WhatsApp webhook payload.

        Returns None if the payload doesn't contain a text message.
        """
        try:
            entry = webhook_data.get("entry", [{}])[0]
            changes = entry.get("changes", [{}])[0]
            value = changes.get("value", {})
            messages = value.get("messages", [])

            if not messages:
                return None

            msg = messages[0]
            if msg.get("type") != "text":
                return None  # Only handle text for now

            text = msg.get("text", {}).get("body", "")
            sender = msg.get("from", "")

            command = None
            command_args = None
            if text.startswith("/"):
                parts = text.split(maxsplit=1)
                command = parts[0].lstrip("/")
                command_args = parts[1] if len(parts) > 1 else ""

            return IncomingMessage(
                text=text,
                chat_id=sender,  # WhatsApp uses phone number as chat ID
                user_id=sender,
                channel_type=ChannelType.WHATSAPP,
                raw=webhook_data,
                command=command,
                command_args=command_args,
            )
        except (IndexError, KeyError) as e:
            log.warning("Failed to parse WhatsApp webhook: %s", e)
            return None
