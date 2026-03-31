"""Content summarizer — URLs, PDFs, and YouTube videos.

Accepts a URL, PDF path, or YouTube link and returns a structured summary
using the LLM. Reuses web.py's fetch_url() for page content extraction.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

log = logging.getLogger("khalil.actions.summarize")

SKILL = {
    "name": "summarize",
    "description": "Summarize web pages, PDFs, and YouTube videos into key points",
    "category": "information",
    "patterns": [
        # YouTube-specific patterns first (more specific than generic URL)
        (r"\bsummar(?:ize|y)\s+(?:this\s+|of\s+(?:this\s+)?)?(?:youtube|video)\b", "summarize_youtube"),
        (r"\bsummar(?:ize|y)\s+(?:this\s+)?https?://(?:www\.)?(?:youtube\.com|youtu\.be)/", "summarize_youtube"),
        (r"\btl;?dr\b.*https?://(?:www\.)?(?:youtube\.com|youtu\.be)/", "summarize_youtube"),
        (r"https?://(?:www\.)?(?:youtube\.com/watch|youtu\.be/).*\bsummar", "summarize_youtube"),
        # PDF
        (r"\bsummar(?:ize|y)\s+(?:this\s+)?(?:pdf|document)\b", "summarize_pdf"),
        # Generic URL (after YouTube so YT URLs don't match here)
        (r"\bsummar(?:ize|y)\s+(?:this\s+)?(?:article|page|link|url|site)\b", "summarize_url"),
        (r"\bsummar(?:ize|y)\s+(?:this\s+)?https?://", "summarize_url"),
        (r"\btl;?dr\b.*https?://", "summarize_url"),
        (r"\bkey\s+points\b.*https?://", "summarize_url"),
    ],
    "actions": [
        {"type": "summarize_url", "handler": "handle_intent", "keywords": "summarize summary tldr key points article page link url", "description": "Summarize a web page"},
        {"type": "summarize_pdf", "handler": "handle_intent", "keywords": "summarize summary pdf document file", "description": "Summarize a PDF document"},
        {"type": "summarize_youtube", "handler": "handle_intent", "keywords": "summarize summary youtube video tldr", "description": "Summarize a YouTube video"},
    ],
    "examples": [
        "Summarize this article: https://example.com/post",
        "TLDR of this YouTube video",
        "Key points from this PDF",
    ],
    "voice": {"response_style": "brief"},
}

_URL_RE = re.compile(r"https?://\S+")
_YT_RE = re.compile(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]+)")


def _extract_url(text: str) -> str | None:
    """Extract the first URL from text."""
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,;:)") if m else None


def _extract_youtube_id(url: str) -> str | None:
    """Extract YouTube video ID from URL."""
    m = _YT_RE.search(url)
    return m.group(1) if m else None


async def _fetch_youtube_transcript(video_id: str) -> str | None:
    """Fetch YouTube transcript using youtube-transcript-api."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        transcript = await asyncio.to_thread(
            YouTubeTranscriptApi.get_transcript, video_id
        )
        return " ".join(entry["text"] for entry in transcript)
    except ImportError:
        log.info("youtube-transcript-api not installed")
        return None
    except Exception as e:
        log.error("YouTube transcript fetch failed: %s", e)
        return None


async def _read_pdf(path: str, max_pages: int = 20) -> str | None:
    """Extract text from a PDF file."""
    try:
        import pdfplumber

        def _extract():
            text_parts = []
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages[:max_pages]:
                    t = page.extract_text()
                    if t:
                        text_parts.append(t)
            return "\n\n".join(text_parts)

        return await asyncio.to_thread(_extract)
    except ImportError:
        log.info("pdfplumber not installed")
        return None
    except Exception as e:
        log.error("PDF read failed for %s: %s", path, e)
        return None


async def _summarize_content(content: str, source_type: str, ask_claude_fn) -> str:
    """Send content to LLM for summarization."""
    prompt = (
        f"Summarize this {source_type} content. Provide:\n"
        "1. A one-line TL;DR\n"
        "2. 3-5 key points as bullet points\n"
        "3. Any action items if applicable\n\n"
        "Keep it concise and information-dense.\n\n"
        f"Content:\n{content[:8000]}"
    )
    return await ask_claude_fn(prompt, "")


async def summarize_url(url: str, ask_claude_fn) -> str:
    """Summarize a web page."""
    from actions.web import web_fetch
    content = await web_fetch(url, max_chars=8000)
    if content.startswith("Error"):
        return content
    return await _summarize_content(content, "web page", ask_claude_fn)


async def summarize_youtube(url: str, ask_claude_fn) -> str:
    """Summarize a YouTube video via its transcript."""
    video_id = _extract_youtube_id(url)
    if not video_id:
        return "Could not extract YouTube video ID from URL."
    transcript = await _fetch_youtube_transcript(video_id)
    if not transcript:
        return "Could not fetch transcript. The video may not have captions available."
    return await _summarize_content(transcript, "YouTube video", ask_claude_fn)


async def summarize_pdf(path: str, ask_claude_fn) -> str:
    """Summarize a PDF file."""
    if not Path(path).exists():
        return f"PDF not found: {path}"
    content = await _read_pdf(path)
    if not content:
        return "Could not extract text from PDF."
    return await _summarize_content(content, "PDF document", ask_claude_fn)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle summarization intents."""
    query = intent.get("query", "") or intent.get("user_query", "")
    server = intent.get("_server", {})

    # We need ask_claude from server — import it
    from server import ask_claude

    if action == "summarize_url":
        url = _extract_url(query)
        if not url:
            await ctx.reply("Please include a URL to summarize.")
            return True
        # Check if it's actually a YouTube URL
        if _extract_youtube_id(url):
            await ctx.reply("📹 Fetching YouTube transcript...")
            summary = await summarize_youtube(url, ask_claude)
        else:
            await ctx.reply("📄 Fetching and summarizing...")
            summary = await summarize_url(url, ask_claude)
        await ctx.reply(summary)
        return True

    if action == "summarize_youtube":
        url = _extract_url(query)
        if not url:
            await ctx.reply("Please include a YouTube URL to summarize.")
            return True
        await ctx.reply("📹 Fetching YouTube transcript...")
        summary = await summarize_youtube(url, ask_claude)
        await ctx.reply(summary)
        return True

    if action == "summarize_pdf":
        # Extract path from query
        path = re.sub(
            r"\b(?:summarize|summary|tldr|key\s+points|this|the|pdf|document)\b",
            "", query, flags=re.IGNORECASE,
        ).strip()
        if not path:
            await ctx.reply("Please provide a path to the PDF file.")
            return True
        await ctx.reply("📄 Reading PDF...")
        summary = await summarize_pdf(path, ask_claude)
        await ctx.reply(summary)
        return True

    return False
