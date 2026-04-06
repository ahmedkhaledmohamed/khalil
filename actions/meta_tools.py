"""Meta-tools — tools that control the tool-use loop itself.

clarify: LLM asks the user a clarifying question instead of guessing.
         This prevents wrong tool calls when the query is ambiguous.
"""

import logging

log = logging.getLogger("khalil.actions.meta_tools")

SKILL = {
    "name": "meta_tools",
    "description": "Meta-tools for the tool-use loop (clarification, search)",
    "category": "system",
    "patterns": [],  # No regex patterns — these are LLM-only tools
    "actions": [
        {
            "type": "clarify",
            "handler": "handle_intent",
            "keywords": "",
            "description": "Ask the user a clarifying question when the request is ambiguous",
            "parameters": {
                "question": {
                    "type": "string",
                    "description": "The clarifying question to ask the user",
                    "required": True,
                },
            },
        },
    ],
    "examples": [],
}


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle meta-tool actions."""

    if action == "clarify":
        question = intent.get("question", "Could you clarify what you'd like me to do?")
        await ctx.reply(f"Before I proceed — {question}")
        return True

    return False
