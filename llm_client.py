"""Shared LLM client factory — routes through Taskforce proxy when configured.

All subsystems (extend, guardian, healing) should use these factories instead
of creating raw anthropic.Anthropic() clients, which bypass the Taskforce proxy.
"""

import logging
import os

from config import (
    CLAUDE_BASE_URL, CLAUDE_API_KEY_HEADER, KEYRING_SERVICE,
)

log = logging.getLogger("khalil.llm_client")


def _get_api_key() -> str | None:
    """Get API key from keyring or environment."""
    try:
        import keyring
        key = keyring.get_password(KEYRING_SERVICE, "anthropic-api-key")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY")


def get_llm_client():
    """Get a sync LLM client (for guardian, healing).

    Returns an OpenAI client if Taskforce is configured, else Anthropic.
    """
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("No API key found for LLM client")

    if CLAUDE_BASE_URL:
        from openai import OpenAI
        return OpenAI(
            api_key=api_key,
            base_url=CLAUDE_BASE_URL,
            default_headers={CLAUDE_API_KEY_HEADER: api_key} if CLAUDE_API_KEY_HEADER else {},
        ), "openai"
    else:
        import anthropic
        return anthropic.Anthropic(api_key=api_key), "anthropic"


def get_async_llm_client():
    """Get an async LLM client (for extend, test generation).

    Returns an AsyncOpenAI client if Taskforce is configured, else AsyncAnthropic.
    """
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("No API key found for LLM client")

    if CLAUDE_BASE_URL:
        from openai import AsyncOpenAI
        return AsyncOpenAI(
            api_key=api_key,
            base_url=CLAUDE_BASE_URL,
            default_headers={CLAUDE_API_KEY_HEADER: api_key} if CLAUDE_API_KEY_HEADER else {},
        ), "openai"
    else:
        import anthropic
        return anthropic.AsyncAnthropic(api_key=api_key), "anthropic"


def call_llm_sync(client, client_type: str, model: str, system: str, user_msg: str, max_tokens: int = 1500) -> str:
    """Make a sync LLM call, handling both OpenAI and Anthropic formats."""
    if client_type == "openai":
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
        )
        return response.choices[0].message.content
    else:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        return response.content[0].text


async def call_llm_async(client, client_type: str, model: str, system: str, user_msg: str, max_tokens: int = 1500) -> str:
    """Make an async LLM call, handling both OpenAI and Anthropic formats."""
    if client_type == "openai":
        response = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
        )
        return response.choices[0].message.content
    else:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        return response.content[0].text
