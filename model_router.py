"""Smart model router — heuristic query classification, optimal model selection."""

import logging
import re
import sqlite3
from enum import Enum

from config import CLAUDE_MODEL, CLAUDE_MODEL_COMPLEX, DB_PATH

log = logging.getLogger("khalil.model_router")


class ModelTier(Enum):
    FAST = "fast"
    STANDARD = "standard"
    COMPLEX = "complex"


# Default tier → model mapping
MODEL_MAP: dict[ModelTier, str] = {
    ModelTier.FAST: CLAUDE_MODEL,         # Taskforce is free — use best model for all tiers
    ModelTier.STANDARD: CLAUDE_MODEL,
    ModelTier.COMPLEX: CLAUDE_MODEL_COMPLEX,
}

# Settings keys for DB overrides
_SETTINGS_KEYS = {
    ModelTier.FAST: "model_map_fast",
    ModelTier.STANDARD: "model_map_standard",
    ModelTier.COMPLEX: "model_map_complex",
}

# --- Heuristic patterns (compiled once) ---

_GREETING_PATTERNS = re.compile(
    r"^(hi|hello|hey|thanks|thank you|ok|okay|yes|no|yep|nope|sure|bye|gm|gn)[\s!.,?]*$",
    re.IGNORECASE,
)

_COMPLEX_KEYWORDS = re.compile(
    r"\b(generate|create|build|write\s+code|analyze\s+in\s+depth|analyze|compare|refactor|implement|architect|design\s+a)\b",
    re.IGNORECASE,
)

_COMPLEX_ACTION_HINTS = {"extend", "heal"}


def classify_complexity(
    query: str,
    *,
    action_hint: str | None = None,
    is_explicit: bool = False,
) -> ModelTier:
    """Classify query complexity using fast heuristics (no LLM call).

    Args:
        query: The user's query text.
        action_hint: Optional hint from the action system (e.g. "extend", "heal").
        is_explicit: If True, caller explicitly needs COMPLEX tier.

    Returns:
        The appropriate ModelTier for this query.
    """
    if is_explicit:
        return ModelTier.COMPLEX

    stripped = query.strip()

    # FAST signals
    if len(stripped) < 20:
        return ModelTier.FAST
    if _GREETING_PATTERNS.match(stripped):
        return ModelTier.FAST
    if stripped.startswith("/") and stripped in ("/health", "/stats", "/status", "/help"):
        return ModelTier.FAST

    # COMPLEX signals
    if action_hint and action_hint in _COMPLEX_ACTION_HINTS:
        return ModelTier.COMPLEX
    if len(stripped) > 500:
        return ModelTier.COMPLEX
    if _COMPLEX_KEYWORDS.search(stripped):
        return ModelTier.COMPLEX

    # Default
    return ModelTier.STANDARD


def get_model(tier: ModelTier) -> str:
    """Return model ID for the given tier, with optional DB overrides."""
    settings_key = _SETTINGS_KEYS[tier]
    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (settings_key,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass  # DB not available or table missing — use default
    return MODEL_MAP[tier]


def route_query(query: str, **kwargs) -> tuple[ModelTier, str]:
    """Classify query and return (tier, model_id) in one call."""
    tier = classify_complexity(query, **kwargs)
    model_id = get_model(tier)
    return tier, model_id
