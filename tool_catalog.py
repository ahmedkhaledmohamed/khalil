"""Tool catalog — generate OpenAI-format tool schemas from the skill registry.

Each action becomes its own tool with dedicated parameters. No more action
enum — the tool name IS the action. This eliminates the two-decision problem
(which tool + which action) that caused most tool-use failures.

Dynamic filtering (#62): instead of exposing all ~50 tools, we select the
top 8 most relevant tools per query based on keyword/pattern matching,
plus a small set of always-available core tools.

Usage:
    from tool_catalog import generate_tool_schemas, filter_tools_for_query
    from skills import get_registry
    all_tools = generate_tool_schemas(get_registry())
    relevant = filter_tools_for_query(query, get_registry(), all_tools)
    # Pass `relevant` to chat.completions.create(tools=relevant, tool_choice="auto")
"""

import logging
import re
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

# Core tools always included regardless of query relevance
_CORE_TOOLS = {
    "shell", "reminder", "web_search",
}

# Maximum tools to expose per query (core + filtered)
_MAX_TOOLS_PER_QUERY = 12

# Cache for generated schemas (invalidated on registry reload)
_schema_cache: list[dict] | None = None
_schema_cache_ts: float = 0.0
_SCHEMA_CACHE_TTL = 300.0  # 5 minutes

# Tool descriptions: few-shot examples and negative examples (#13, #14)
# Keyed by action_type. Overrides the generic description builder.
_TOOL_DESCRIPTIONS = {
    "send_to_claude": (
        "Send a text prompt to a running Claude Code session. "
        'Example: send_to_claude(target="/dev/ttys057", command="refactor the auth module") '
        "Returns confirmation of delivery. "
        "DO NOT use for shell commands — use send_to_terminal or shell instead."
    ),
    "send_to_terminal": (
        "Send a shell command to a specific terminal session by TTY path. "
        'Example: send_to_terminal(target="/dev/ttys042", command="git status") '
        "DO NOT use for Claude Code sessions — use send_to_claude instead."
    ),
    "shell": (
        "Execute a shell command on the local machine. "
        'Example: shell(command="df -h") for disk space, shell(command="open -a Safari") to launch apps. '
        "Returns stdout/stderr. Commands are safety-classified before execution."
    ),
    "calendar": (
        "Check today's calendar events and schedule. "
        'Example: "What meetings do I have today?" → calendar() '
        "Returns formatted list of events with times. "
        "For creating events use calendar_create. For multi-day view use calendar_upcoming."
    ),
    "calendar_create": (
        "Create a new Google Calendar event. "
        'Example: calendar_create(summary="1:1 with Sarah", start_time="2026-04-07T14:00:00") '
        "Requires summary and start_time. End time defaults to 1 hour after start."
    ),
    "calendar_upcoming": (
        "Check upcoming events for the next N days. "
        'Example: calendar_upcoming(days=7) for this week\'s schedule. '
        "Default is 7 days."
    ),
    "email": (
        "Send or draft an email via Gmail. "
        'Example: email(to="sarah@example.com", subject="Meeting follow-up") '
        "Requires recipient and subject."
    ),
    "email_work": (
        "Search work email (Spotify Gmail). "
        'Example: "Check my work inbox" → email_work() '
        "For personal email use email_personal."
    ),
    "email_personal": (
        "Search personal email. "
        'Example: "Any personal emails about the mortgage?" → email_personal() '
        "For work email use email_work."
    ),
    "reminder": (
        "Create a one-time reminder delivered via Telegram. "
        'Example: reminder(text="Call dentist", time="tomorrow 9am") '
        "Requires text. Time supports natural language."
    ),
    "reminder_list": (
        "List all active/pending reminders. "
        'Example: "What are my reminders?" → reminder_list()'
    ),
    "weather": (
        "Get current weather conditions (Toronto default). "
        'Example: "What\'s the weather?" → weather()'
    ),
    "weather_forecast": (
        "Get multi-day weather forecast. "
        'Example: weather_forecast(days=5) for 5-day forecast.'
    ),
    "spotify_now": (
        "Show currently playing track on Spotify. "
        'Example: "What\'s playing?" → spotify_now()'
    ),
    "spotify_recent": (
        "Show recently played tracks on Spotify. "
        'Example: "What did I listen to?" → spotify_recent()'
    ),
    "spotify_top": (
        "Show top tracks or artists on Spotify. "
        'Example: "My top artists" → spotify_top()'
    ),
    "meeting_prep": (
        "Prepare for a meeting — pulls calendar, emails, and relevant context. "
        'Example: meeting_prep(meeting_title="1:1 with manager") '
        "Returns comprehensive brief."
    ),
    "daily_focus": (
        "Generate daily focus plan from calendar, reminders, emails, and goals. "
        'Example: "What should I focus on today?" → daily_focus()'
    ),
    "weekly_review": (
        "Summarize the week — calendar, tasks completed, email activity. "
        'Example: "Weekly review" → weekly_review()'
    ),
    "web_search": (
        "Search the web via DuckDuckGo. "
        'Example: web_search() for general queries. '
        "Use when the answer isn't in the user's personal archives."
    ),
    "github_notifications": (
        "Check unread GitHub notifications. "
        'Example: "GitHub notifications" → github_notifications()'
    ),
    "github_prs": (
        "List open pull requests across repos. "
        'Example: "Check my PRs" → github_prs()'
    ),
    "github_create_issue": (
        "Create a new GitHub issue. "
        'Example: github_create_issue() '
        "Will prompt for repo, title, and body."
    ),
    "list_sessions": (
        "List all terminal sessions (iTerm2 + tmux) with running processes and TTY paths. "
        'Example: "What terminals are open?" → list_sessions()'
    ),
    "read_terminal": (
        "Read recent output from a terminal session. "
        'Example: read_terminal(target="/dev/ttys057", lines=50) '
        "Requires target TTY path or tmux session name."
    ),
    "claude_code_status": (
        "Show all running Claude Code processes with CWD, TTY, and state. "
        'Example: "What Claude sessions are running?" → claude_code_status()'
    ),
    "system_info": (
        "Get system info: battery, storage, CPU, running apps. "
        'Example: "What\'s running on my machine?" → system_info()'
    ),
    "summarize_url": (
        "Summarize a web page into key points. "
        'Example: "Summarize this article: https://..." → summarize_url()'
    ),
    "summarize_youtube": (
        "Summarize a YouTube video from its transcript. "
        'Example: "TLDR of this video: https://youtube.com/..." → summarize_youtube()'
    ),
    "summarize_pdf": (
        "Summarize a PDF document. "
        'Example: "Summarize this PDF" → summarize_pdf()'
    ),
}

# Tool co-selection groups (#65): when one tool is relevant, include its companions
_TOOL_GROUPS = {
    "calendar": {"calendar", "calendar_create", "calendar_upcoming"},
    "email": {"email", "email_work", "email_personal"},
    "reminders": {"reminder", "reminder_list"},
    "weather": {"weather", "weather_forecast"},
    "spotify": {"spotify_now", "spotify_recent", "spotify_top"},
    "terminal": {"list_sessions", "read_terminal", "send_to_terminal", "send_to_claude", "claude_code_status", "create_terminal"},
    "github": {"github_notifications", "github_prs", "github_create_issue"},
    "summarize": {"summarize_url", "summarize_youtube", "summarize_pdf"},
    "focus": {"pomodoro_start", "pomodoro_stop", "pomodoro_status", "daily_focus"},
    "workflows": {"workflow_run", "workflow_list"},
}


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


def filter_tools_for_query(query: str, registry, all_tools: list[dict]) -> list[dict]:
    """Select the most relevant tools for a given query (#62).

    Returns core tools + top relevant tools, capped at _MAX_TOOLS_PER_QUERY.
    This prevents overwhelming the LLM with 50+ tools when only 5-8 are relevant.
    """
    if not all_tools:
        return all_tools

    # Build a name→tool index
    tool_index = {t["function"]["name"]: t for t in all_tools}

    # Start with core tools (always available)
    selected = set()
    for name in _CORE_TOOLS:
        if name in tool_index:
            selected.add(name)

    # Score each tool by relevance to the query
    query_lower = query.lower()
    query_words = set(re.findall(r"\b\w+\b", query_lower))
    scored: list[tuple[float, str]] = []

    for skill in registry.list_skills():
        if skill.name in _EXCLUDE_SKILLS or skill.name not in _INCLUDE_SKILLS:
            continue

        for action_type, action_info in skill.actions.items():
            if action_type not in tool_index:
                continue

            score = 0.0

            # Pattern match from skill = strong signal
            for pattern, at in skill.patterns:
                if at == action_type and pattern.search(query_lower):
                    score += 10.0
                    break

            # Keyword overlap
            keywords = action_info.get("keywords", "").split()
            if keywords:
                overlap = len(query_words & set(keywords))
                score += overlap * 1.5

            # Description word overlap
            desc_words = set(action_info.get("description", "").lower().split())
            score += len(query_words & desc_words) * 0.5

            if score > 0:
                scored.append((score, action_type))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    # Add top-scoring tools and their group companions
    for _score, action_type in scored:
        if len(selected) >= _MAX_TOOLS_PER_QUERY:
            break
        selected.add(action_type)
        # Include group companions
        for group_tools in _TOOL_GROUPS.values():
            if action_type in group_tools:
                for companion in group_tools:
                    if companion in tool_index:
                        selected.add(companion)
                break

    # If nothing matched, include a broader set
    if len(selected) <= len(_CORE_TOOLS):
        # Fall back to all tools (let the LLM decide)
        return all_tools

    result = [tool_index[name] for name in selected if name in tool_index]
    log.info("Filtered %d → %d tools for query: %s", len(all_tools), len(result), query[:50])
    return result


def invalidate_cache():
    """Invalidate the schema cache (call after registry reload)."""
    global _schema_cache
    _schema_cache = None


def _build_action_tool(skill, action_type: str, action_info: dict) -> dict | None:
    """Build a single tool schema for one action.

    Tool name = action_type (e.g., "send_to_claude", "calendar_create")
    Parameters = action-specific params only (no action enum)
    """
    # Use curated description if available (#13, #14)
    if action_type in _TOOL_DESCRIPTIONS:
        full_description = _TOOL_DESCRIPTIONS[action_type]
    else:
        description = action_info.get("description", action_type)
        desc_parts = [description]
        desc_parts.append(f"(Part of: {skill.name})")
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
    """Infer required fields for actions based on known patterns."""
    required = list(existing)

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
    """Generate a compact text summary of all tools for system prompt injection."""
    lines = ["Available tools:"]
    for skill in sorted(registry.list_skills(), key=lambda s: s.name):
        if skill.name in _EXCLUDE_SKILLS or not skill.actions:
            continue
        for action_type, info in sorted(skill.actions.items()):
            desc = info.get("description", action_type)
            lines.append(f"  {action_type}: {desc}")
    return "\n".join(lines)
