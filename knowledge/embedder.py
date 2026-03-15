"""Local embedding via Ollama (nomic-embed-text). Personal data never leaves machine."""

import logging

import httpx
from config import OLLAMA_URL, EMBED_MODEL

log = logging.getLogger("khalil.embedder")

EMBED_TIMEOUT = 10.0  # seconds per single embed call
EMBED_BATCH_TIMEOUT = 120.0  # seconds for batch operations


async def embed_text(text: str) -> list[float] | None:
    """Generate embedding for a single text chunk via Ollama.

    Returns None if Ollama is unreachable or the call fails.
    """
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


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Generate embeddings for multiple texts. Skips failed batches."""
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


async def check_ollama() -> bool:
    """Check if Ollama is running and model is available."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            return any(EMBED_MODEL in m for m in models)
    except (httpx.ConnectError, httpx.HTTPError):
        return False
