"""1Password integration — look up credentials, OTPs, and secure notes.

Wraps the 1Password CLI (op). Requires 1Password desktop app + CLI auth.
Security: never displays full passwords in Telegram — copies to clipboard instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

log = logging.getLogger("khalil.actions.onepassword")

SKILL = {
    "name": "onepassword",
    "description": "Look up credentials, OTPs, and secure notes via 1Password",
    "category": "productivity",
    "patterns": [
        (r"\b(?:1password|one\s*password)\b", "op_search"),
        (r"\bpassword\s+for\s+\w+\b", "op_get"),
        (r"\bget\s+(?:my\s+)?(?:password|credentials?|login)\s+for\b", "op_get"),
        (r"\botp\s+(?:for|code)\b", "op_otp"),
        (r"\b(?:two\s*factor|2fa|totp)\s+(?:for|code)\b", "op_otp"),
        (r"\bsecure\s+note\b", "op_search"),
        (r"\bsearch\s+(?:1password|passwords?)\b", "op_search"),
    ],
    "actions": [
        {"type": "op_get", "handler": "handle_intent", "keywords": "password credentials login get 1password onepassword", "description": "Get a credential from 1Password"},
        {"type": "op_search", "handler": "handle_intent", "keywords": "search 1password onepassword secure note find", "description": "Search 1Password items"},
        {"type": "op_otp", "handler": "handle_intent", "keywords": "otp 2fa totp two factor code 1password", "description": "Get OTP code from 1Password"},
    ],
    "examples": [
        "Password for GitHub",
        "OTP for AWS",
        "Search 1password for Spotify",
    ],
    "voice": {"confirm_before_execute": True, "response_style": "brief"},
}


async def _run_op(*args: str, timeout: float = 15) -> tuple[str, int]:
    """Run a 1Password CLI command."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "op", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            err = stderr.decode().strip()
            if "not signed in" in err.lower() or "session expired" in err.lower():
                return "1Password CLI not authenticated. Run: eval $(op signin)", 1
            return err, proc.returncode
        return stdout.decode().strip(), 0
    except FileNotFoundError:
        return "1Password CLI not installed. Install: brew install 1password-cli", 1
    except asyncio.TimeoutError:
        return "1Password CLI timed out", 1


async def _copy_to_clipboard(text: str) -> None:
    """Copy text to macOS clipboard."""
    proc = await asyncio.create_subprocess_exec(
        "pbcopy",
        stdin=asyncio.subprocess.PIPE,
    )
    await proc.communicate(input=text.encode())


async def get_credential(item_name: str) -> str:
    """Get a credential and copy password to clipboard."""
    output, rc = await _run_op("item", "get", item_name, "--format", "json")
    if rc != 0:
        return f"Could not find '{item_name}': {output}"

    try:
        item = json.loads(output)
    except json.JSONDecodeError:
        return f"Could not parse 1Password response for '{item_name}'."

    title = item.get("title", item_name)
    category = item.get("category", "LOGIN")

    # Extract username and password
    username = None
    password = None
    for field in item.get("fields", []):
        if field.get("purpose") == "USERNAME" or field.get("id") == "username":
            username = field.get("value", "")
        if field.get("purpose") == "PASSWORD" or field.get("id") == "password":
            password = field.get("value", "")

    if password:
        await _copy_to_clipboard(password)
        masked = password[:2] + "•" * (len(password) - 2) if len(password) > 2 else "••"
        lines = [f"🔐 **{title}** ({category})"]
        if username:
            lines.append(f"  Username: {username}")
        lines.append(f"  Password: {masked} (copied to clipboard)")
        return "\n".join(lines)

    return f"Found '{title}' but no password field."


async def search_items(query: str) -> str:
    """Search 1Password items by name."""
    output, rc = await _run_op("item", "list", "--format", "json")
    if rc != 0:
        return f"Search failed: {output}"

    try:
        items = json.loads(output)
    except json.JSONDecodeError:
        return "Could not parse 1Password item list."

    query_lower = query.lower()
    matches = [
        item for item in items
        if query_lower in item.get("title", "").lower()
        or query_lower in item.get("additional_information", "").lower()
    ]

    if not matches:
        return f"No items matching '{query}' found in 1Password."

    lines = [f"🔍 Found {len(matches)} items matching '{query}':"]
    for item in matches[:10]:
        title = item.get("title", "Untitled")
        cat = item.get("category", "")
        vault = item.get("vault", {}).get("name", "")
        lines.append(f"  • {title} ({cat}) — {vault}")
    if len(matches) > 10:
        lines.append(f"  ...and {len(matches) - 10} more")
    return "\n".join(lines)


async def get_otp(item_name: str) -> str:
    """Get the current TOTP code for an item."""
    output, rc = await _run_op("item", "get", item_name, "--otp")
    if rc != 0:
        return f"Could not get OTP for '{item_name}': {output}"

    otp = output.strip()
    await _copy_to_clipboard(otp)
    return f"🔑 OTP for **{item_name}**: `{otp}` (copied to clipboard)"


def _extract_item_name(query: str, action: str) -> str:
    """Extract the item/service name from a natural language query."""
    if action == "op_otp":
        m = re.search(r"(?:otp|2fa|totp|two\s*factor)\s+(?:for|code\s+for)\s+(.+)", query, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    if action == "op_get":
        m = re.search(r"(?:password|credentials?|login)\s+for\s+(.+)", query, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m = re.search(r"get\s+(?:my\s+)?(.+?)(?:\s+password|\s+credentials?|\s+login)?$", query, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    # Fallback: strip command words
    cleaned = re.sub(
        r"\b(?:1password|onepassword|password|credentials?|login|otp|2fa|totp|get|my|for|search|find|the|show|secure|note)\b",
        "", query, flags=re.IGNORECASE,
    ).strip()
    return cleaned


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle 1Password intents."""
    query = intent.get("query", "") or intent.get("user_query", "")
    item_name = _extract_item_name(query, action)

    if action == "op_get":
        if not item_name:
            await ctx.reply("Which credential should I look up?")
            return True
        result = await get_credential(item_name)
        await ctx.reply(result)
        return True

    if action == "op_search":
        if not item_name:
            await ctx.reply("What should I search for in 1Password?")
            return True
        result = await search_items(item_name)
        await ctx.reply(result)
        return True

    if action == "op_otp":
        if not item_name:
            await ctx.reply("Which service do you need an OTP for?")
            return True
        result = await get_otp(item_name)
        await ctx.reply(result)
        return True

    return False
