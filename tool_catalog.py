"""Tool catalog — generate OpenAI-format tool schemas from the skill registry.

Each skill becomes one tool with an `action` enum parameter listing its
action types. This keeps the tool count manageable (~50 tools) while
exposing all ~120 action types to the LLM.

Usage:
    from tool_catalog import generate_tool_schemas
    from skills import get_registry
    tools = generate_tool_schemas(get_registry())
    # Pass `tools` to chat.completions.create(tools=tools, tool_choice="auto")
"""

import logging

log = logging.getLogger("khalil.tool_catalog")

# Skills to exclude from tool catalog (internal/meta, not user-facing)
_EXCLUDE_SKILLS = {"extend", "guardian"}

# Only these skills get exposed as tools to the LLM.
# Others are still available via regex fast-path and direct dispatch.
# Keeping this small (~15) prevents the LLM from over-triggering tools.
_INCLUDE_SKILLS = {
    "calendar", "gmail", "reminders", "weather", "shell", "spotify",
    "web", "pomodoro", "synthesis", "slack", "clipboard",
    "apple_reminders", "github_api", "workflows", "summarize",
    "machine",
}


def generate_tool_schemas(registry) -> list[dict]:
    """Generate OpenAI-format tool schemas from the skill registry.

    Each skill becomes one tool. Action types become an enum parameter.
    The LLM picks the skill + action, and the existing handle_intent
    handler executes it.

    Returns list of tool dicts in OpenAI tools format.
    """
    tools = []
    for skill in registry.list_skills():
        if skill.name in _EXCLUDE_SKILLS:
            continue
        if skill.name not in _INCLUDE_SKILLS:
            continue
        if not skill.actions:
            continue

        # Build action enum from action types
        action_types = sorted(skill.actions.keys())
        action_descriptions = []
        for atype in action_types:
            info = skill.actions[atype]
            desc = info.get("description", atype)
            action_descriptions.append(f"{atype}: {desc}")

        # Build parameters schema
        properties = {
            "action": {
                "type": "string",
                "enum": action_types,
                "description": " | ".join(action_descriptions),
            },
        }

        # Add skill-level parameters if any action declares them
        extra_params = _collect_skill_parameters(skill)
        properties.update(extra_params)

        # Build tool description from skill description + examples
        description = skill.description
        if skill.examples:
            description += " Examples: " + "; ".join(skill.examples[:3])

        tool = {
            "type": "function",
            "function": {
                "name": skill.name,
                "description": description[:1024],  # OpenAI limit
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": ["action"],
                },
            },
        }
        tools.append(tool)

    log.info("Generated %d tool schemas from skill registry", len(tools))
    return tools


def _collect_skill_parameters(skill) -> dict:
    """Collect additional parameters declared across a skill's actions.

    Actions can declare parameters in their SKILL dict:
        "parameters": {"text": {"type": "string", "description": "..."}}

    These get merged into the skill-level tool schema. Shared parameter
    names (e.g., "query") are merged; conflicting types use the first seen.
    """
    params = {}
    for atype, info in skill.actions.items():
        action_params = info.get("parameters", {})
        for pname, pdef in action_params.items():
            if pname == "action":
                continue  # Reserved
            if pname not in params:
                schema = {"type": pdef.get("type", "string")}
                if "description" in pdef:
                    schema["description"] = pdef["description"]
                if "enum" in pdef:
                    schema["enum"] = pdef["enum"]
                params[pname] = schema
    return params


def generate_tool_summary(registry) -> str:
    """Generate a compact text summary of all tools for system prompt injection.

    This is cheaper than sending full tool schemas — useful when the LLM
    backend doesn't support tool-use natively (e.g., older Ollama models).
    """
    lines = ["Available tools:"]
    for skill in sorted(registry.list_skills(), key=lambda s: s.name):
        if skill.name in _EXCLUDE_SKILLS or not skill.actions:
            continue
        actions = ", ".join(sorted(skill.actions.keys()))
        lines.append(f"  {skill.name}: {skill.description[:80]} [{actions}]")
    return "\n".join(lines)
