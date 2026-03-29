#!/usr/bin/env python3
"""PharoClaw CLI — local macOS terminal client.

Runs the full PharoClaw pipeline (skill registry, intent detection, action dispatch,
LLM with selective context injection) in an interactive REPL.

Usage:
    python cli.py
    python cli.py "what's the weather?"   # single query mode
"""

import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from channels import ActionButton, Channel, ChannelType, IncomingMessage, SentMessage
from channels.message_context import MessageContext

log = logging.getLogger("pharoclaw.cli")

# --- ANSI formatting ---
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"


class CLIChannel(Channel):
    """Terminal-based channel that prints responses to stdout."""

    channel_type = ChannelType.TELEGRAM  # Reuse to avoid enum changes
    _msg_counter = 0

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: list[list[ActionButton]] | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage:
        self._msg_counter += 1
        msg_id = self._msg_counter

        if text.strip() == "Thinking...":
            print(f"{_DIM}  ...{_RESET}", end="", flush=True)
            return SentMessage(chat_id=chat_id, message_id=msg_id, channel=self)

        print(f"\n{_GREEN}{_BOLD}pharoclaw{_RESET} {_DIM}›{_RESET} {text}")

        if buttons:
            for row in buttons:
                for btn in row:
                    print(f"  {_YELLOW}[{btn.label}]{_RESET}")

        return SentMessage(chat_id=chat_id, message_id=msg_id, channel=self)

    async def edit_message(
        self,
        chat_id: int | str,
        message_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        print(f"\r{' ' * 20}\r", end="")
        print(f"\n{_GREEN}{_BOLD}pharoclaw{_RESET} {_DIM}›{_RESET} {text}")

    async def delete_message(self, chat_id: int | str, message_id: int | str) -> None:
        print(f"\r{' ' * 20}\r", end="")

    async def send_typing(self, chat_id: int | str) -> None:
        pass


async def _init_server():
    """Run PharoClaw's core startup (DB, Claude, skills) without starting bots."""
    import server

    server.db_conn = server.init_db()
    server.autonomy = server.AutonomyController(server.db_conn)

    from learning import set_conn as set_learning_conn
    set_learning_conn(server.db_conn)

    row = server.db_conn.execute("SELECT value FROM settings WHERE key = 'owner_chat_id'").fetchone()
    if row:
        server.OWNER_CHAT_ID = int(row[0])

    from config import LLM_BACKEND
    if LLM_BACKEND == "claude":
        import anthropic
        api_key = server.get_secret("anthropic-api-key")
        if not api_key:
            print(f"{_YELLOW}No Anthropic API key. Set via keyring or ANTHROPIC_API_KEY env var.{_RESET}")
        else:
            server.claude = anthropic.AsyncAnthropic(api_key=api_key)

    from skills import get_registry
    registry = get_registry()
    print(f"{_DIM}  {len(registry.list_skills())} skills loaded{_RESET}")

    return server


async def _process_query(server_mod, channel: CLIChannel, query: str):
    """Send a query through the full pipeline: intent → action → LLM."""
    ctx = MessageContext(
        channel=channel,
        chat_id="cli",
        user_id="owner",
        channel_type=ChannelType.TELEGRAM,
        incoming=IncomingMessage(
            text=query, chat_id="cli", user_id="owner",
            channel_type=ChannelType.TELEGRAM,
        ),
    )
    start = time.monotonic()
    try:
        await server_mod.handle_message_generic(ctx)
    except Exception as e:
        print(f"\n{_YELLOW}Error: {e}{_RESET}")
        log.exception("Pipeline error")
    elapsed = time.monotonic() - start
    print(f"{_DIM}  ({elapsed:.1f}s){_RESET}")


async def _repl():
    print(f"\n{_CYAN}{_BOLD}PharoClaw CLI{_RESET} {_DIM}— full pipeline active{_RESET}")
    server_mod = await _init_server()
    channel = CLIChannel()
    print()

    # Single query mode
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        print(f"{_BOLD}you{_RESET} {_DIM}›{_RESET} {query}")
        await _process_query(server_mod, channel, query)
        return

    # Piped input
    if not sys.stdin.isatty():
        query = sys.stdin.read().strip()
        if query:
            print(f"{_BOLD}you{_RESET} {_DIM}›{_RESET} {query}")
            await _process_query(server_mod, channel, query)
        return

    # Interactive REPL
    while True:
        try:
            query = input(f"{_BOLD}you{_RESET} {_DIM}›{_RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{_DIM}Bye.{_RESET}")
            break
        if not query:
            continue
        if query.lower() in ("quit", "exit", "q"):
            print(f"{_DIM}Bye.{_RESET}")
            break
        await _process_query(server_mod, channel, query)
        print()


def main():
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(name)s: %(message)s")
    # Only show fatal errors — CLI output should be clean
    for name in ("pharoclaw", "httpx", "httpcore", "googleapiclient", "urllib3"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(_repl())
    except KeyboardInterrupt:
        print(f"\n{_DIM}Bye.{_RESET}")


if __name__ == "__main__":
    main()
