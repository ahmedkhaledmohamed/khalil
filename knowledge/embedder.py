"""Embedding abstraction layer (#68). Routes to configured provider.

Default provider is Ollama (nomic-embed-text). Personal data never leaves machine.
Switch providers by setting EMBED_PROVIDER in config.py (e.g., "openai").
"""

import logging

import httpx
from config import OLLAMA_URL, EMBED_MODEL, EMBED_PROVIDER

log = logging.getLogger("khalil.embedder")

EMBED_TIMEOUT = 10.0  # seconds per single embed call
EMBED_BATCH_TIMEOUT = 120.0  # seconds for batch operations


# --- Ollama provider ---

async def _ollama_embed_text(text: str) -> list[float] | None:
    """Generate embedding for a single text chunk via Ollama."""
    try:
        async with httpx.AsyncClient(timeout=EMBED_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": EMBED_MODEL, "input": text},
            )
            response.raise_for_status()
            return response.json()["embeddings"][0]
    except httpx.TimeoutException:
        log.warning("Embedding call timed out after %.0fs", EMBED_TIMEOUT)
        return None
    except (httpx.ConnectError, httpx.HTTPError) as e:
        log.warning("Embedding call failed: %s", e)
        return None


async def _ollama_embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Generate embeddings for multiple texts via Ollama."""
    all_embeddings = []
    try:
        async with httpx.AsyncClient(timeout=EMBED_BATCH_TIMEOUT) as client:
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                response = await client.post(
                    f"{OLLAMA_URL}/api/embed",
                    json={"model": EMBED_MODEL, "input": batch},
                )
                response.raise_for_status()
                all_embeddings.extend(response.json()["embeddings"])
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as e:
        log.warning("Batch embedding failed at index %d: %s", len(all_embeddings), e)
    return all_embeddings


async def _ollama_check() -> bool:
    """Check if Ollama is running and model is available."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            return any(EMBED_MODEL in m for m in models)
    except (httpx.ConnectError, httpx.HTTPError):
        return False


# --- Provider registry (#68) ---

_PROVIDERS = {
    "ollama": {
        "embed_text": _ollama_embed_text,
        "embed_batch": _ollama_embed_batch,
        "check": _ollama_check,
    },
    # Future: "openai", "cohere", etc. — add provider functions and register here.
}


def _get_provider() -> dict:
    """Get the active embedding provider functions."""
    provider = _PROVIDERS.get(EMBED_PROVIDER)
    if provider is None:
        raise ValueError(f"Unknown EMBED_PROVIDER: {EMBED_PROVIDER!r}. Available: {list(_PROVIDERS.keys())}")
    return provider


# --- Public API (delegates to configured provider) ---

async def embed_text(text: str) -> list[float] | None:
    """Generate embedding for a single text chunk. Routes to configured provider."""
    return await _get_provider()["embed_text"](text)


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Generate embeddings for multiple texts. Routes to configured provider."""
    return await _get_provider()["embed_batch"](texts, batch_size)


async def check_ollama() -> bool:
    """Check if the configured embedding provider is available."""
    return await _get_provider()["check"]()
