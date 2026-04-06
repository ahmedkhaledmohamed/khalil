"""AI media generation skill — generate images, videos, and audio via Replicate.

Usage:
    "generate an image of a sunset over Toronto"
    "create a video of waves on a beach"
    "make some chill lo-fi music"
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH, MEDIA_PROVIDER
from actions.ai_media_providers import get_provider, DEFAULT_MODELS

log = logging.getLogger("khalil.actions.ai_media")

SKILL = {
    "name": "ai_media",
    "description": "AI media generation — create images, videos, and audio using open-source models",
    "category": "creative",
    "patterns": [
        (r"\b(?:generate|create|make|draw|paint|render)\b.*\b(?:image|photo|picture|illustration|artwork)\b", "generate_image"),
        (r"\b(?:generate|create|make|render)\b.*\b(?:video|clip|animation)\b", "generate_video"),
        (r"\b(?:generate|create|make|compose)\b.*\b(?:music|audio|song|sound|beat|melody)\b", "generate_audio"),
        (r"\b(?:image|photo|picture)\b.*\b(?:of|showing|with)\b", "generate_image"),
    ],
    "actions": [
        {
            "type": "generate_image",
            "handler": "handle_media_intent",
            "keywords": "generate create make image photo picture draw paint render illustration artwork",
            "description": "Generate an AI image from a text prompt",
            "parameters": {
                "prompt": {"type": "string", "description": "Description of the image to generate"},
            },
        },
        {
            "type": "generate_video",
            "handler": "handle_media_intent",
            "keywords": "generate create make video clip animation render",
            "description": "Generate an AI video from a text prompt",
            "parameters": {
                "prompt": {"type": "string", "description": "Description of the video to generate"},
            },
        },
        {
            "type": "generate_audio",
            "handler": "handle_media_intent",
            "keywords": "generate create make music audio song sound beat melody compose",
            "description": "Generate AI music or audio from a text prompt",
            "parameters": {
                "prompt": {"type": "string", "description": "Description of the music/audio to generate"},
            },
        },
    ],
    "examples": [
        "generate an image of a sunset over Toronto",
        "create a short video of ocean waves",
        "make some chill lo-fi background music",
        "draw a cat wearing a top hat",
        "generate a 15-second ambient track",
    ],
}

# Map action types to media types
_ACTION_TO_MEDIA = {
    "generate_image": "image",
    "generate_video": "video",
    "generate_audio": "audio",
}

_MEDIA_TYPE_LABELS = {
    "image": "image",
    "video": "video",
    "audio": "audio",
}


def _ensure_table():
    """Create media_generations table if it doesn't exist."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            media_type TEXT NOT NULL,
            prompt TEXT NOT NULL,
            model TEXT,
            file_path TEXT,
            url TEXT,
            duration_s REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


_ensure_table()


def _save_generation(chat_id, media_type, prompt, model, file_path, url, duration_s):
    """Record a generation to history."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT INTO media_generations (chat_id, media_type, prompt, model, file_path, url, duration_s) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, media_type, prompt, model, str(file_path), url, duration_s),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Failed to save media generation: %s", e)


import re

# Patterns to strip from the query to extract the actual prompt
_STRIP_PREFIXES = re.compile(
    r"^(?:please\s+)?(?:generate|create|make|draw|paint|render|compose)\s+"
    r"(?:a\s+|an\s+|some\s+|me\s+)?(?:short\s+|quick\s+|simple\s+)?"
    r"(?:ai\s+)?(?:image|photo|picture|illustration|artwork|video|clip|animation|music|audio|song|sound|beat|melody)"
    r"\s*(?:of|showing|with|about|for)?\s*",
    re.IGNORECASE,
)


def _extract_prompt(query: str, media_type: str) -> str:
    """Extract the generation prompt from the user's query."""
    prompt = _STRIP_PREFIXES.sub("", query).strip()
    # If stripping removed everything, use the original query
    if not prompt or len(prompt) < 3:
        prompt = query.strip()
    return prompt


async def handle_media_intent(query: str, ctx, *, action_type: str = "generate_image", **kwargs):
    """Handle media generation requests."""
    media_type = _ACTION_TO_MEDIA.get(action_type, "image")
    from actions.ai_media_providers import LOCAL_MODELS

    label = _MEDIA_TYPE_LABELS[media_type]

    # Extract the prompt — use the query directly, stripping the command prefix
    prompt = _extract_prompt(query, media_type)
    if not prompt:
        await ctx.reply(f"Please provide a description for the {label} you want to generate.")
        return

    # Resolve display model name
    if MEDIA_PROVIDER == "local":
        model_name = LOCAL_MODELS.get(media_type, "local")
    else:
        model_name = DEFAULT_MODELS.get(media_type, "unknown")

    # Send progress message
    progress = await ctx.reply(f"Generating {label}... (model: {model_name.split('/')[-1]})")

    try:
        provider = get_provider(MEDIA_PROVIDER)
        result = await provider.generate(media_type, prompt)

        # Save to history
        _save_generation(ctx.chat_id, media_type, prompt, result.model, result.file_path, result.url, result.duration_s)

        # Send the result
        caption = f"{prompt}\n\n({result.model.split('/')[-1]} | {result.duration_s:.1f}s)"

        if media_type == "image":
            await ctx.reply_photo(str(result.file_path), caption=caption)
        elif media_type == "video":
            await ctx.reply_video(str(result.file_path), caption=caption)
        elif media_type == "audio":
            await ctx.reply_document(str(result.file_path), caption=caption)

    except Exception as e:
        log.error("Media generation failed: %s", e, exc_info=True)
        error_msg = str(e)
        if "token not found" in error_msg.lower():
            await ctx.reply(f"Replicate API token not set. Run:\n`python -c \"import keyring; keyring.set_password('khalil-assistant', 'replicate-api-token', 'r8_...')\"`")
        else:
            await ctx.reply(f"Failed to generate {label}: {error_msg}")
