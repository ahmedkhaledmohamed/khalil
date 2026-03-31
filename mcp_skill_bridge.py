"""MCP-to-Skill bridge — auto-register MCP server tools as Khalil skills.

Fetches tools from all connected MCP servers and registers them in the
SkillRegistry with auto-generated patterns, keywords, and a handler that
routes to MCPClientManager.call_tool().

Usage:
    from mcp_skill_bridge import register_mcp_skills
    await register_mcp_skills(registry)
"""

import logging
import re
from typing import Any

from config import ActionType

log = logging.getLogger("khalil.mcp_skill_bridge")

# Words too generic to use as standalone patterns
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "it", "of", "to", "in", "for", "on", "and", "or",
})

# Classify MCP tool safety by name/description heuristics
_DANGEROUS_KEYWORDS = frozenset({
    "delete", "remove", "drop", "destroy", "purge", "kill", "terminate",
    "send", "post", "publish", "push", "deploy", "transfer", "pay",
})

_WRITE_KEYWORDS = frozenset({
    "create", "write", "update", "modify", "edit", "set", "put", "add",
    "insert", "append", "move", "rename", "copy",
})


def classify_tool_safety(name: str, description: str) -> ActionType:
    """Classify an MCP tool's safety level based on name and description."""
    combined = f"{name} {description}".lower()
    if any(kw in combined for kw in _DANGEROUS_KEYWORDS):
        return ActionType.DANGEROUS
    if any(kw in combined for kw in _WRITE_KEYWORDS):
        return ActionType.WRITE
    return ActionType.READ


def _tool_name_to_words(name: str) -> list[str]:
    """Split a tool name like 'search_files' or 'searchFiles' into words."""
    # Handle snake_case
    words = re.sub(r"[_\-.]", " ", name).split()
    # Handle camelCase
    expanded = []
    for w in words:
        expanded.extend(re.sub(r"([a-z])([A-Z])", r"\1 \2", w).lower().split())
    return [w for w in expanded if w and w not in _STOP_WORDS]


def _generate_patterns(tool_name: str, description: str, server_name: str) -> list[tuple[str, str]]:
    """Generate regex patterns for an MCP tool.

    Returns list of (pattern_string, action_type) tuples.
    """
    action_type = f"mcp_{server_name}_{tool_name}"
    patterns = []

    # Pattern 1: Exact tool name (with underscores/hyphens as spaces)
    readable_name = re.sub(r"[_\-.]", r"\\s+", tool_name)
    patterns.append((rf"\b{readable_name}\b", action_type))

    # Pattern 2: Key words from tool name (if 2+ meaningful words)
    words = _tool_name_to_words(tool_name)
    if len(words) >= 2:
        # Build a pattern that matches the words in order with flexible spacing
        word_pattern = r"\b" + r"\b.*\b".join(re.escape(w) for w in words) + r"\b"
        patterns.append((word_pattern, action_type))

    # Pattern 3: Extract key nouns from description (first sentence)
    if description:
        first_sentence = description.split(".")[0].lower()
        desc_words = re.findall(r"\b[a-z]{4,}\b", first_sentence)
        # Keep meaningful, uncommon words
        desc_keywords = [w for w in desc_words if w not in _STOP_WORDS and len(w) > 4]
        if desc_keywords:
            # Use the top 2-3 keywords as an OR pattern
            unique_kws = list(dict.fromkeys(desc_keywords))[:3]
            if len(unique_kws) >= 2:
                kw_pattern = r"\b(?:" + "|".join(re.escape(w) for w in unique_kws) + r")\b"
                patterns.append((kw_pattern, action_type))

    return patterns


def _generate_keywords(tool_name: str, description: str, server_name: str) -> str:
    """Generate keyword string for gap detection."""
    words = _tool_name_to_words(tool_name)
    if description:
        desc_words = re.findall(r"\b[a-z]{3,}\b", description.lower())
        desc_filtered = [w for w in desc_words if w not in _STOP_WORDS][:5]
        words.extend(desc_filtered)
    words.append(server_name)
    return " ".join(dict.fromkeys(words))  # dedupe, preserve order


async def register_mcp_skills(registry) -> int:
    """Fetch tools from all MCP servers and register them as skills.

    Args:
        registry: SkillRegistry instance to register skills into.

    Returns:
        Number of MCP tools registered as skills.
    """
    from mcp_client import MCPClientManager
    from skills import Skill

    manager = MCPClientManager.get_instance()
    all_tools = await manager.get_all_tools()

    if not all_tools:
        log.info("No MCP tools available to register")
        return 0

    # Group tools by server
    by_server: dict[str, list[dict]] = {}
    for tool in all_tools:
        server = tool.get("server", "unknown")
        by_server.setdefault(server, []).append(tool)

    registered = 0
    for server_name, tools in by_server.items():
        skill_name = f"mcp_{server_name}"
        patterns = []
        actions = {}
        keywords = {}
        examples = []

        for tool in tools:
            tool_name = tool["name"]
            description = tool.get("description", "")
            action_type = f"mcp_{server_name}_{tool_name}"

            # Generate patterns
            tool_patterns = _generate_patterns(tool_name, description, server_name)
            patterns.extend(tool_patterns)

            # Build handler (closure over server_name + tool_name + schema)
            handler = _make_mcp_handler(server_name, tool_name, tool.get("input_schema", {}))
            safety = classify_tool_safety(tool_name, description)

            actions[action_type] = {
                "handler": handler,
                "description": description or tool_name,
                "safety": safety.value,
                "input_schema": tool.get("input_schema", {}),
            }

            keywords[action_type] = _generate_keywords(tool_name, description, server_name)

            # Generate example from description
            if description:
                examples.append(f"{description.split('.')[0].strip()}")

        # Compile patterns
        compiled_patterns = []
        for pat_str, action_type in patterns:
            try:
                compiled_patterns.append((re.compile(pat_str, re.IGNORECASE), action_type))
            except re.error as e:
                log.debug("Invalid pattern for MCP tool: %s (%s)", pat_str, e)

        skill = Skill(
            name=skill_name,
            description=f"MCP server: {server_name} ({len(tools)} tools)",
            module_name=f"mcp:{server_name}",
            actions=actions,
            patterns=compiled_patterns,
            keywords=keywords,
            category="mcp",
            examples=examples[:5],
        )

        registry.register(skill)
        registered += len(tools)
        log.info("Registered MCP skill '%s' with %d tools, %d patterns",
                 skill_name, len(tools), len(compiled_patterns))

    log.info("MCP skill bridge: registered %d tools from %d servers",
             registered, len(by_server))
    return registered


def _make_mcp_handler(server_name: str, tool_name: str, input_schema: dict):
    """Create an async handler function for an MCP tool.

    The handler extracts arguments from the intent dict and calls the tool
    via MCPClientManager. If the tool has required parameters that aren't
    in the intent, it asks the user.
    """
    required_params = input_schema.get("required", [])
    properties = input_schema.get("properties", {})

    async def handler(action: str, intent: dict, ctx) -> bool:
        from mcp_client import MCPClientManager

        query = intent.get("query", "") or intent.get("user_query", "")

        # Build arguments from intent fields + LLM-extracted params
        arguments = {}
        for key in properties:
            # Check intent dict for explicit values
            if key in intent:
                arguments[key] = intent[key]

        # For simple tools with a single string parameter, use the query as input
        if not arguments and len(properties) == 1:
            param_name = list(properties.keys())[0]
            param_type = properties[param_name].get("type", "string")
            if param_type == "string" and query:
                arguments[param_name] = query

        # Check required params
        missing = [p for p in required_params if p not in arguments]
        if missing:
            param_descriptions = []
            for p in missing:
                desc = properties.get(p, {}).get("description", p)
                param_descriptions.append(f"**{p}**: {desc}")
            await ctx.reply(
                f"To use **{tool_name}** ({server_name}), I need:\n"
                + "\n".join(f"  • {d}" for d in param_descriptions)
            )
            return True

        # Call the tool
        try:
            manager = MCPClientManager.get_instance()
            result = await manager.call_tool(server_name, tool_name, arguments)

            # Format response
            if result.startswith("Error:"):
                await ctx.reply(f"❌ {result}")
            else:
                # Truncate very long results
                if len(result) > 2000:
                    result = result[:2000] + "\n... (truncated)"
                await ctx.reply(f"**{tool_name}** ({server_name}):\n\n{result}")
            return True
        except Exception as e:
            await ctx.reply(f"❌ MCP tool '{tool_name}' failed: {e}")
            return True

    return handler
