"""Voice interaction — speech-to-text and text-to-speech."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import httpx

from config import OLLAMA_URL, VOICE_REPLY_ENABLED, TTS_VOICE

log = logging.getLogger("khalil.actions.voice")

SKILL = {
    "name": "voice",
    "description": "Voice interaction — transcribe speech and synthesize audio replies",
    "category": "system",
    "patterns": [
        (r"\b(?:say|speak|read\s+aloud)\b.*", "voice_tts"),
        (r"\bvoice\s+(?:mode|reply|response)\s+(?:on|off|enable|disable)\b", "voice_toggle"),
    ],
    "actions": [
        {"type": "voice_tts", "handler": "handle_intent", "keywords": "say speak read aloud voice tts", "description": "Synthesize speech from text"},
        {"type": "voice_toggle", "handler": "handle_intent", "keywords": "voice mode reply response toggle", "description": "Toggle voice reply mode"},
    ],
    "examples": ["Say hello", "Enable voice replies"],
    "voice": {"response_style": "brief"},
}


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle voice-related intents."""
    if action == "voice_tts":
        text = intent.get("query", "").strip()
        # Strip the "say" prefix
        import re
        text = re.sub(r"^(?:say|speak|read\s+aloud)\s+", "", text, flags=re.IGNORECASE).strip()
        if not text:
            await ctx.reply("What would you like me to say?")
            return True
        audio_path = await synthesize_speech(text)
        if audio_path:
            await ctx.reply_voice(audio_path)
            os.unlink(audio_path)
        else:
            await ctx.reply(f"Could not synthesize speech. Here's the text: {text}")
        return True

    if action == "voice_toggle":
        query = intent.get("query", "").lower()
        import config
        if "on" in query or "enable" in query:
            config.VOICE_REPLY_ENABLED = True
            await ctx.reply("Voice replies enabled. I'll respond with audio when you send voice messages.")
        else:
            config.VOICE_REPLY_ENABLED = False
            await ctx.reply("Voice replies disabled.")
        return True

    return False


async def _run(cmd: list[str], timeout: float = 30) -> tuple[str, str, int]:
    """Run a subprocess and return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode


async def convert_ogg_to_wav(ogg_path: str) -> str | None:
    """Convert .ogg voice file to .wav using ffmpeg. Returns wav path or None."""
    wav_path = ogg_path.replace(".ogg", ".wav")
    try:
        _, stderr, rc = await _run([
            "ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path,
        ], timeout=15)
        if rc != 0:
            log.error("ffmpeg conversion failed: %s", stderr[:200])
            return None
        return wav_path
    except FileNotFoundError:
        log.error("ffmpeg not installed — run: brew install ffmpeg")
        return None
    except asyncio.TimeoutError:
        log.error("ffmpeg conversion timed out")
        return None


async def transcribe_voice(audio_path: str) -> str | None:
    """Transcribe audio file using Ollama's Whisper model.

    Falls back to returning None if Ollama/Whisper not available.
    """
    # Try Ollama whisper endpoint
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            with open(audio_path, "rb") as f:
                response = await client.post(
                    f"{OLLAMA_URL}/api/transcribe",
                    files={"file": ("audio.wav", f, "audio/wav")},
                    data={"model": "whisper"},
                )
            if response.status_code == 200:
                data = response.json()
                return data.get("text", "").strip()

            # Ollama may not support /api/transcribe — try alternative
            log.info("Ollama transcribe returned %d, trying whisper CLI fallback", response.status_code)
    except (httpx.ConnectError, httpx.HTTPError) as e:
        log.info("Ollama transcribe not available: %s", e)

    # Fallback: try local whisper CLI if installed
    try:
        stdout, stderr, rc = await _run([
            "whisper", audio_path, "--model", "base", "--output_format", "txt",
            "--language", "en", "--output_dir", str(Path(audio_path).parent),
        ], timeout=60)
        if rc == 0:
            txt_path = audio_path.rsplit(".", 1)[0] + ".txt"
            if os.path.exists(txt_path):
                text = Path(txt_path).read_text().strip()
                os.unlink(txt_path)  # cleanup
                return text
    except FileNotFoundError:
        log.info("whisper CLI not installed")
    except asyncio.TimeoutError:
        log.error("whisper CLI timed out")

    return None


async def synthesize_speech(text: str, voice: str = "") -> str | None:
    """Generate speech audio using macOS `say` command.

    Returns path to .aiff file or None on failure.
    """
    voice = voice or TTS_VOICE
    output_path = tempfile.mktemp(suffix=".aiff")
    try:
        _, stderr, rc = await _run([
            "say", "-v", voice, "-o", output_path, text[:500],  # cap length
        ], timeout=15)
        if rc != 0:
            log.error("TTS failed: %s", stderr[:200])
            return None
        if os.path.exists(output_path):
            return output_path
        return None
    except asyncio.TimeoutError:
        log.error("TTS timed out")
        return None
    except Exception as e:
        log.error("TTS error: %s", e)
        return None
