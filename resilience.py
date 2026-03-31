"""Resilience utilities — retry, error classification, user-friendly messages.

Shared across all skills to handle transient failures consistently.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Callable

log = logging.getLogger("khalil.resilience")

# Exceptions that are worth retrying (transient)
TRANSIENT_EXCEPTIONS = (
    TimeoutError,
    asyncio.TimeoutError,
    ConnectionError,
    ConnectionResetError,
    OSError,  # covers network-level errors
)

# Exception substrings that indicate transient issues
_TRANSIENT_STRINGS = (
    "429",
    "rate limit",
    "too many requests",
    "503",
    "service unavailable",
    "timeout",
    "timed out",
    "connection reset",
    "temporary failure",
)


def is_transient(exc: Exception) -> bool:
    """Check if an exception is likely transient and worth retrying."""
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in _TRANSIENT_STRINGS)


def format_user_error(exc: Exception, skill_name: str = "") -> str:
    """Format an exception into a user-friendly message."""
    msg = str(exc)
    prefix = f"{skill_name}: " if skill_name else ""

    if isinstance(exc, asyncio.TimeoutError) or "timeout" in msg.lower():
        return f"⚠️ {prefix}Timed out. Try again in a moment."
    if "429" in msg or "rate limit" in msg.lower():
        return f"⚠️ {prefix}Rate limited. Try again in a minute."
    if "403" in msg or "forbidden" in msg.lower():
        return f"⚠️ {prefix}Access denied. Check /health for auth status."
    if "401" in msg or "unauthorized" in msg.lower():
        return f"⚠️ {prefix}Not authenticated. Check /health for auth status."
    if isinstance(exc, ConnectionError) or "connection" in msg.lower():
        return f"⚠️ {prefix}Connection failed. Service may be down."
    if isinstance(exc, ImportError):
        return f"⚠️ {prefix}Missing dependency. Check setup."
    # Truncate raw messages
    short = msg[:150] + "..." if len(msg) > 150 else msg
    return f"⚠️ {prefix}{short}"


def retry(
    max_attempts: int = 3,
    backoff_factor: float = 1.0,
    max_backoff: float = 10.0,
    on_transient_only: bool = True,
):
    """Decorator for retrying async functions on transient failures.

    Usage:
        @retry(max_attempts=3)
        async def fetch_data():
            ...
    """
    def decorator(fn: Callable):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if on_transient_only and not is_transient(e):
                        raise
                    if attempt == max_attempts:
                        raise
                    delay = min(backoff_factor * (2 ** (attempt - 1)), max_backoff)
                    log.debug(
                        "Retry %d/%d for %s after %.1fs: %s",
                        attempt, max_attempts, fn.__name__, delay, e,
                    )
                    await asyncio.sleep(delay)
            raise last_exc  # unreachable, but makes type checker happy
        return wrapper
    return decorator
