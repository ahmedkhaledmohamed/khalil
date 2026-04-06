"""Tool catalog — generate OpenAI-format tool schemas from the skill registry.

Each action becomes its own tool with dedicated parameters. No more action
enum — the tool name IS the action. This eliminates the two-decision problem
(which tool + which action) that caused most tool-use failures.

Usage:
    from tool_catalog import generate_tool_schemas
    from skills import get_registry
    tools = generate_tool_schemas(get_registry())
    # Pass `tools` to chat.completions.create(tools=tools, tool_choice="auto")
"""

import logging
import time

log = logging.getLogger("khalil.tool_catalog")

# Skills to exclude from tool catalog (internal/meta, not user-facing)
_EXCLUDE_SKILLS = {"extend", "guardian"}

# Only actions from these skills get exposed as tools to the LLM.
# Others are still available via regex fast-path and direct dispatch.
_INCLUDE_SKILLS = {
    "calendar", "gmail", "reminders", "weather", "shell", "spotify",
    "web", "pomodoro", "synthesis", "slack", "clipboard",
    "apple_reminders", "github_api", "workflows", "summarize",
    "machine",
}

# Cache for generated schemas (invalidated on registry reload)
_schema_cache: list[dict] | None = None
_schema_cache_ts: float = 0.0
_SCHEMA_CACHE_TTL = 300.0  # 5 minutes


def generate_tool_schemas(registry) -> list[dict]:
    """Generate OpenAI-format tool schemas from the skill registry.

    Each action becomes its own tool. The tool name is the action_type,
    so the LLM only makes ONE decision: which tool to call.

    Returns list of tool dicts in OpenAI tools format.
    """
    global _schema_cache, _schema_cache_ts
    now = time.monotonic()
    if _schema_cache is not None and (now - _schema_cache_ts) < _SCHEMA_CACHE_TTL:
        return _schema_cache

    tools = []
    for skill in registry.list_skills():
        if skill.name in _EXCLUDE_SKILLS:
            continue
        if skill.name not in _INCLUDE_SKILLS:
            continue
        if not skill.actions:
            continue

        for action_type, action_info in skill.actions.items():
            tool = _build_action_tool(skill, action_type, action_info)
            if tool:
                tools.append(tool)

    log.info("Generated %d tool schemas (one per action) from %d skills",
             len(tools), len(_INCLUDE_SKILLS))

    _schema_cache = tools
    _schema_cache_ts = now
    return tools


def invalidate_cache():
    """Invalidate the schema cache (call after registry reload)."""
    global _schema_cache
    _schema_cache = None


def _build_action_tool(skill, action_type: str, action_info: dict) -> dict | None:
    """Build a single tool schema for one action.

    Tool name = action_type (e.g., "send_to_claude", "calendar_create")
    Parameters = action-specific params only (no action enum)
    """
    description = action_info.get("description", action_type)

    # Enrich description with skill context and examples
    desc_parts = [description]

    # Add skill category for disambiguation
    desc_parts.append(f"(Part of: {skill.name})")

    # Add relevant examples from the skill
    skill_examples = _get_action_examples(skill, action_type)
    if skill_examples:
        desc_parts.append(f"Examples: {'; '.join(skill_examples)}")

    full_description = " ".join(desc_parts)

    # Build parameters from action-specific declarations
    properties = {}
    required = []
    action_params = action_info.get("parameters", {})
    for pname, pdef in action_params.items():
        schema = {"type": pdef.get("type", "string")}
        if "description" in pdef:
            schema["description"] = pdef["description"]
        if "enum" in pdef:
            schema["enum"] = pdef["enum"]
        properties[pname] = schema
        # Mark as required if flagged, or if it's a core param
        if pdef.get("required", False):
            required.append(pname)

    # Infer required fields for critical actions
    required = _infer_required_fields(action_type, properties, required)

    params_schema = {"type": "object", "properties": properties}
    if required:
        params_schema["required"] = required

    return {
        "type": "function",
        "function": {
            "name": action_type,
            "description": full_description[:1024],  # OpenAI limit
            "parameters": params_schema,
        },
    }


def _get_action_examples(skill, action_type: str) -> list[str]:
    """Get examples relevant to a specific action from the skill's examples."""
    # Simple heuristic: match examples containing action-related keywords
    if not skill.examples:
        return []
    keywords = skill.actions.get(action_type, {}).get("keywords", "").split()
    if not keywords:
        return skill.examples[:1]

    relevant = []
    for ex in skill.examples:
        ex_lower = ex.lower()
        if any(kw in ex_lower for kw in keywords[:3]):
            relevant.append(ex)
    return relevant[:2] if relevant else skill.examples[:1]


def _infer_required_fields(action_type: str, properties: dict, existing: list) -> list:
    """Infer required fields for actions based on known patterns.

    This prevents the LLM from making empty tool calls.
    """
    required = list(existing)

    # Actions that MUST have specific params to work
    _REQUIRED_MAP = {
        "calendar_create": ["summary", "start_time"],
        "email": ["to", "subject"],
        "reminder": ["text"],
        "send_to_terminal": ["command"],
        "send_to_claude": ["command", "target"],
        "shell": ["command"],
        "slack_send": [],
        "type_text": ["command"],
        "click": ["command"],
        "read_terminal": ["target"],
        "summarize_url": [],
        "summarize_youtube": [],
        "workflow_run": [],
        "meeting_prep": ["meeting_title"],
        "context_brief": ["topic"],
    }

    inferred = _REQUIRED_MAP.get(action_type, [])
    for field in inferred:
        if field in properties and field not in required:
            required.append(field)

    return required


def generate_tool_summary(registry) -> str:
    """Generate a compact text summary of all tools for system prompt injection.

    This is cheaper than sending full tool schemas — useful when the LLM
    backend doesn't support tool-use natively (e.g., older Ollama models).
    """
    lines = ["Available tools:"]
    for skill in sorted(registry.list_skills(), key=lambda s: s.name):
        if skill.name in _EXCLUDE_SKILLS or not skill.actions:
            continue
        for action_type, info in sorted(skill.actions.items()):
            desc = info.get("description", action_type)
            lines.append(f"  {action_type}: {desc}")
    return "\n".join(lines)
