"""AI media generation providers — abstraction layer for image/video/audio generation.

Providers:
  - "local": Fully free, runs on Apple Silicon via torch MPS (SDXL Turbo + MusicGen)
  - "replicate": Paid cloud API (Flux, Minimax, MusicGen)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import keyring

from config import KEYRING_SERVICE, MEDIA_DOWNLOAD_DIR, MEDIA_MAX_FILE_SIZE

log = logging.getLogger("khalil.actions.ai_media_providers")

# Default models per media type (used by Replicate provider)
DEFAULT_MODELS = {
    "image": "black-forest-labs/flux-1.1-pro",
    "video": "minimax/video-01-live",
    "audio": "meta/musicgen",
}

# Local models
LOCAL_MODELS = {
    "image": "stabilityai/sdxl-turbo",
    "audio": "facebook/musicgen-small",
}


@dataclass
class MediaResult:
    """Result of a media generation request."""
    url: str
    file_path: Path
    media_type: str        # "image", "video", "audio"
    model: str
    duration_s: float      # wall-clock generation time
    prompt: str


class MediaProvider(ABC):
    """Abstract base for media generation providers."""

    @abstractmethod
    async def generate(self, media_type: str, prompt: str, **kwargs) -> MediaResult:
        """Generate media and return result with downloaded file."""
        ...


class ReplicateProvider(MediaProvider):
    """Replicate.com provider — runs open-source models via REST API."""

    BASE_URL = "https://api.replicate.com/v1"
    POLL_INTERVAL = 2.0   # seconds between status checks
    MAX_WAIT = 600        # 10 min max wait

    def __init__(self):
        self._api_token: str | None = None

    def _get_token(self) -> str:
        if not self._api_token:
            self._api_token = keyring.get_password(KEYRING_SERVICE, "replicate-api-token")
        if not self._api_token:
            raise RuntimeError(
                "Replicate API token not found. Set it with:\n"
                "python -c \"import keyring; keyring.set_password('khalil-assistant', 'replicate-api-token', 'r8_...')\""
            )
        return self._api_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def _build_input(self, media_type: str, prompt: str, **kwargs) -> dict[str, Any]:
        """Build model-specific input params."""
        if media_type == "image":
            return {"prompt": prompt, "num_outputs": 1, **kwargs}
        elif media_type == "video":
            return {"prompt": prompt, **kwargs}
        elif media_type == "audio":
            return {"prompt": prompt, "duration": kwargs.get("duration", 8), **kwargs}
        return {"prompt": prompt, **kwargs}

    async def generate(self, media_type: str, prompt: str, **kwargs) -> MediaResult:
        model = kwargs.pop("model", None) or DEFAULT_MODELS.get(media_type)
        if not model:
            raise ValueError(f"No default model for media type: {media_type}")

        start = time.monotonic()
        MEDIA_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

        async with aiohttp.ClientSession() as session:
            # Create prediction
            payload = {
                "version": None,  # use latest
                "input": self._build_input(media_type, prompt, **kwargs),
            }
            # Replicate's official models use the /models/{owner}/{name}/predictions endpoint
            create_url = f"{self.BASE_URL}/models/{model}/predictions"

            async with session.post(create_url, json=payload, headers=self._headers()) as resp:
                if resp.status != 201:
                    body = await resp.text()
                    raise RuntimeError(f"Replicate create failed ({resp.status}): {body}")
                prediction = await resp.json()

            prediction_id = prediction["id"]
            poll_url = f"{self.BASE_URL}/predictions/{prediction_id}"

            # Poll until complete
            elapsed = 0.0
            while elapsed < self.MAX_WAIT:
                await asyncio.sleep(self.POLL_INTERVAL)
                elapsed = time.monotonic() - start

                async with session.get(poll_url, headers=self._headers()) as resp:
                    prediction = await resp.json()

                status = prediction.get("status")
                if status == "succeeded":
                    break
                elif status in ("failed", "canceled"):
                    error = prediction.get("error", "unknown error")
                    raise RuntimeError(f"Replicate prediction {status}: {error}")
                # else: "starting" or "processing" — keep polling
            else:
                raise TimeoutError(f"Replicate prediction timed out after {self.MAX_WAIT}s")

            # Extract output URL
            output = prediction.get("output")
            if isinstance(output, list):
                output_url = output[0]
            elif isinstance(output, str):
                output_url = output
            else:
                raise RuntimeError(f"Unexpected output format: {output}")

            # Download file
            ext = _guess_extension(media_type, output_url)
            file_path = MEDIA_DOWNLOAD_DIR / f"{prediction_id}{ext}"

            async with session.get(output_url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Failed to download media: HTTP {resp.status}")
                content_length = resp.content_length or 0
                if content_length > MEDIA_MAX_FILE_SIZE:
                    raise RuntimeError(f"File too large ({content_length} bytes), max {MEDIA_MAX_FILE_SIZE}")
                data = await resp.read()
                if len(data) > MEDIA_MAX_FILE_SIZE:
                    raise RuntimeError(f"File too large ({len(data)} bytes)")
                file_path.write_bytes(data)

        duration_s = time.monotonic() - start
        log.info("Generated %s in %.1fs: %s", media_type, duration_s, file_path.name)
        return MediaResult(
            url=output_url,
            file_path=file_path,
            media_type=media_type,
            model=model,
            duration_s=duration_s,
            prompt=prompt,
        )


# --- Local provider (free, Apple Silicon MPS) ---

# Lazy-loaded model singletons — stay in memory after first use
_image_pipeline = None
_audio_model = None
_audio_processor = None


def _load_image_pipeline():
    """Load SDXL Turbo pipeline (lazy, ~6GB first download)."""
    global _image_pipeline
    if _image_pipeline is not None:
        return _image_pipeline

    import torch
    from diffusers import AutoPipelineForText2Image

    log.info("Loading image model %s (first time may download ~6GB)...", LOCAL_MODELS["image"])
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    _image_pipeline = AutoPipelineForText2Image.from_pretrained(
        LOCAL_MODELS["image"],
        torch_dtype=torch.float16,
        variant="fp16",
    ).to(device)
    log.info("Image model loaded on %s", device)
    return _image_pipeline


def _load_audio_model():
    """Load MusicGen Small model (lazy, ~2GB first download)."""
    global _audio_model, _audio_processor
    if _audio_model is not None:
        return _audio_model, _audio_processor

    import torch
    from transformers import AutoProcessor, MusicgenForConditionalGeneration

    log.info("Loading audio model %s (first time may download ~2GB)...", LOCAL_MODELS["audio"])
    _audio_processor = AutoProcessor.from_pretrained(LOCAL_MODELS["audio"])
    _audio_model = MusicgenForConditionalGeneration.from_pretrained(LOCAL_MODELS["audio"])
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    _audio_model = _audio_model.to(device)
    log.info("Audio model loaded on %s", device)
    return _audio_model, _audio_processor


class LocalProvider(MediaProvider):
    """Fully local provider — runs open-source models on Apple Silicon via MPS."""

    SUPPORTED_TYPES = ("image", "audio")

    async def generate(self, media_type: str, prompt: str, **kwargs) -> MediaResult:
        if media_type not in self.SUPPORTED_TYPES:
            raise ValueError(
                f"Local provider doesn't support '{media_type}' generation. "
                f"Supported: {', '.join(self.SUPPORTED_TYPES)}. "
                f"Set MEDIA_PROVIDER='replicate' for video."
            )

        MEDIA_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        loop = asyncio.get_event_loop()

        if media_type == "image":
            file_path = await loop.run_in_executor(
                None, functools.partial(self._generate_image_sync, prompt, **kwargs)
            )
            model = LOCAL_MODELS["image"]
        elif media_type == "audio":
            file_path = await loop.run_in_executor(
                None, functools.partial(self._generate_audio_sync, prompt, **kwargs)
            )
            model = LOCAL_MODELS["audio"]

        duration_s = time.monotonic() - start
        log.info("Local generated %s in %.1fs: %s", media_type, duration_s, file_path.name)
        return MediaResult(
            url="",  # local file, no URL
            file_path=file_path,
            media_type=media_type,
            model=model,
            duration_s=duration_s,
            prompt=prompt,
        )

    @staticmethod
    def _generate_image_sync(prompt: str, **kwargs) -> Path:
        """Generate image synchronously (runs in executor thread)."""
        import torch

        pipe = _load_image_pipeline()
        with torch.no_grad():
            result = pipe(
                prompt=prompt,
                num_inference_steps=kwargs.get("steps", 4),
                guidance_scale=0.0,  # SDXL Turbo doesn't use guidance
                width=kwargs.get("width", 512),
                height=kwargs.get("height", 512),
            )
        image = result.images[0]
        file_path = MEDIA_DOWNLOAD_DIR / f"{uuid.uuid4().hex[:12]}.png"
        image.save(file_path)
        return file_path

    @staticmethod
    def _generate_audio_sync(prompt: str, **kwargs) -> Path:
        """Generate audio synchronously (runs in executor thread)."""
        import torch
        import scipy.io.wavfile

        model, processor = _load_audio_model()
        duration = kwargs.get("duration", 8)
        # MusicGen generates ~256 tokens/s of audio; max_new_tokens controls length
        max_tokens = int(duration * 256 / 5)  # ~50 tokens per second of audio

        inputs = processor(text=[prompt], padding=True, return_tensors="pt")
        device = next(model.parameters()).device
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            audio_values = model.generate(**inputs, max_new_tokens=max_tokens)

        # Output shape: (batch, channels, samples)
        audio_data = audio_values[0, 0].cpu().numpy()
        sample_rate = model.config.audio_encoder.sampling_rate

        file_path = MEDIA_DOWNLOAD_DIR / f"{uuid.uuid4().hex[:12]}.wav"
        scipy.io.wavfile.write(str(file_path), rate=sample_rate, data=audio_data)
        return file_path


def _guess_extension(media_type: str, url: str) -> str:
    """Guess file extension from media type and URL."""
    # Try URL path first
    from urllib.parse import urlparse
    path = urlparse(url).path
    if "." in path.split("/")[-1]:
        return "." + path.split(".")[-1].split("?")[0][:5]
    # Fallback by media type
    return {"image": ".webp", "video": ".mp4", "audio": ".wav"}.get(media_type, ".bin")


def get_provider(name: str = "local") -> MediaProvider:
    """Factory for media providers."""
    if name == "local":
        return LocalProvider()
    if name == "replicate":
        return ReplicateProvider()
    raise ValueError(f"Unknown media provider: {name}")
