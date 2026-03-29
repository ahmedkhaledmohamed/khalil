"""Voice interaction — speech-to-text and text-to-speech."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import httpx

from config import OLLAMA_URL, VOICE_REPLY_ENABLED, TTS_VOICE

log = logging.getLogger("pharoclaw.actions.voice")


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
