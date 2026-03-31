"""Document extraction — extract structured data from images.

Receipt scanning, business card parsing, table extraction, and OCR.
Uses Claude's vision API for image understanding. Accepts Telegram photos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path

log = logging.getLogger("khalil.actions.doc_extract")

SKILL = {
    "name": "doc_extract",
    "description": "Extract structured data from images — receipts, business cards, tables, OCR",
    "category": "productivity",
    "patterns": [
        (r"\b(?:extract|scan|parse)\s+(?:this\s+)?receipt\b", "extract_receipt"),
        (r"\breceipt\s+(?:scan|extract|read)\b", "extract_receipt"),
        (r"\b(?:extract|scan|parse)\s+(?:this\s+)?(?:business\s+)?card\b", "extract_text"),
        (r"\b(?:read|extract|ocr)\s+(?:this\s+)?(?:image|screenshot|photo|picture)\b", "extract_text"),
        (r"\bwhat\s+does\s+(?:this\s+)?(?:image|screenshot|photo)\s+say\b", "extract_text"),
        (r"\b(?:extract|read)\s+(?:the\s+)?table\b", "extract_table"),
        (r"\bparse\s+(?:this\s+)?(?:image|photo)\b", "extract_text"),
    ],
    "actions": [
        {"type": "extract_receipt", "handler": "handle_intent", "keywords": "extract scan parse receipt expense amount vendor", "description": "Extract data from a receipt image"},
        {"type": "extract_text", "handler": "handle_intent", "keywords": "extract read ocr image screenshot photo text card parse", "description": "OCR / extract text from an image"},
        {"type": "extract_table", "handler": "handle_intent", "keywords": "extract read table data image screenshot spreadsheet", "description": "Extract tabular data from an image"},
    ],
    "examples": [
        "Extract the data from this receipt",
        "What does this screenshot say?",
        "Read the table in this image",
    ],
    "voice": {"response_style": "brief"},
}


async def _analyze_image_with_llm(image_path: str, prompt: str) -> str:
    """Send an image to Claude's vision API for analysis."""
    import base64

    # Read and encode image
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode()

    # Detect media type
    ext = Path(image_path).suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = media_types.get(ext, "image/jpeg")

    try:
        import anthropic
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt,
                        },
                    ],
                }
            ],
        )
        return message.content[0].text
    except ImportError:
        return "anthropic library not installed. Run: pip install anthropic"
    except Exception as e:
        log.error("Vision API failed: %s", e)
        return f"Image analysis failed: {e}"


async def extract_receipt(image_path: str) -> dict:
    """Extract structured data from a receipt image."""
    prompt = (
        "Extract the following from this receipt image and return as JSON:\n"
        "{\n"
        '  "vendor": "store/restaurant name",\n'
        '  "date": "YYYY-MM-DD",\n'
        '  "total": 0.00,\n'
        '  "currency": "CAD",\n'
        '  "items": [{"name": "item", "price": 0.00}],\n'
        '  "tax": 0.00,\n'
        '  "payment_method": "card/cash"\n'
        "}\n"
        "Return ONLY the JSON, no explanation."
    )
    result = await _analyze_image_with_llm(image_path, prompt)
    try:
        # Try to parse as JSON
        json_match = re.search(r"\{.*\}", result, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except json.JSONDecodeError:
        pass
    return {"raw": result}


async def extract_text(image_path: str) -> str:
    """OCR / extract all text from an image."""
    prompt = (
        "Extract ALL text from this image. Preserve the layout as much as possible. "
        "If it's a business card, format as: Name, Title, Company, Email, Phone."
    )
    return await _analyze_image_with_llm(image_path, prompt)


async def extract_table(image_path: str) -> str:
    """Extract tabular data from an image."""
    prompt = (
        "Extract the table data from this image. "
        "Format as a markdown table with headers. "
        "If there are multiple tables, extract all of them."
    )
    return await _analyze_image_with_llm(image_path, prompt)


def _format_receipt(data: dict) -> str:
    """Format extracted receipt data for display."""
    if "raw" in data:
        return f"Receipt extraction (raw):\n{data['raw']}"

    lines = ["🧾 **Receipt Extracted**"]
    if data.get("vendor"):
        lines.append(f"  Vendor: {data['vendor']}")
    if data.get("date"):
        lines.append(f"  Date: {data['date']}")
    if data.get("total"):
        currency = data.get("currency", "")
        lines.append(f"  Total: {currency} {data['total']}")
    if data.get("items"):
        lines.append("  Items:")
        for item in data["items"][:10]:
            lines.append(f"    • {item.get('name', '?')} — {item.get('price', '?')}")
    if data.get("tax"):
        lines.append(f"  Tax: {data['tax']}")
    if data.get("payment_method"):
        lines.append(f"  Payment: {data['payment_method']}")
    return "\n".join(lines)


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle document extraction intents."""
    # Check if there's an image attached
    image_path = intent.get("image_path")

    if not image_path:
        await ctx.reply(
            "Send me a photo along with your request. For example:\n"
            "• Send a receipt photo with caption 'extract receipt'\n"
            "• Send a screenshot with 'what does this say?'"
        )
        return True

    if action == "extract_receipt":
        await ctx.reply("🧾 Scanning receipt...")
        data = await extract_receipt(image_path)
        text = _format_receipt(data)
        await ctx.reply(text)

        # Offer to log as expense
        if isinstance(data, dict) and data.get("total") and data.get("vendor"):
            await ctx.reply(
                f"Log this as an expense? Say: "
                f"'log expense {data.get('total')} {data.get('vendor')}'"
            )
        return True

    if action == "extract_text":
        await ctx.reply("🔍 Extracting text...")
        text = await extract_text(image_path)
        await ctx.reply(f"📄 Extracted text:\n\n{text}")
        return True

    if action == "extract_table":
        await ctx.reply("📊 Extracting table...")
        table = await extract_table(image_path)
        await ctx.reply(f"📊 Extracted table:\n\n{table}")
        return True

    return False
