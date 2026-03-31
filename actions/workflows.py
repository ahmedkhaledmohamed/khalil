"""Cross-skill workflow orchestration — compose multi-skill sequences.

Built-in workflows:
- "start focus" → pomodoro_start + apple_music_pause + macos_focus_dnd_on
- "morning routine" → weather + calendar + health_summary
- "wind down" → pomodoro_stop + apple_music_play + macos_focus_dnd_off

User-defined workflows stored in data/workflows.json.
"""

import json
import logging
from pathlib import Path

from config import DATA_DIR

log = logging.getLogger("khalil.actions.workflows")

WORKFLOWS_FILE = DATA_DIR / "workflows.json"

SKILL = {
    "name": "workflows",
    "description": "Cross-skill workflow orchestration — run multi-step routines",
    "category": "productivity",
    "patterns": [
        (r"\bstart\s+(?:my\s+)?focus\s+(?:routine|mode|session)\b", "workflow_run"),
        (r"\bfocus\s+routine\b", "workflow_run"),
        (r"\b(?:start|run)\s+(?:my\s+)?morning\s+routine\b", "workflow_run"),
        (r"\bmorning\s+routine\b", "workflow_run"),
        (r"\b(?:start|run)\s+(?:my\s+)?(?:wind\s*down|evening)\s+routine\b", "workflow_run"),
        (r"\bwind\s*down\s+routine\b", "workflow_run"),
        (r"\b(?:run|start|execute)\s+(?:my\s+)?(?:workflow|routine)\s+\w+", "workflow_run"),
        (r"\blist\s+(?:my\s+)?(?:workflows|routines)\b", "workflow_list"),
        (r"\bcreate\s+(?:a\s+)?(?:workflow|routine)\b", "workflow_create"),
    ],
    "actions": [
        {"type": "workflow_run", "handler": "handle_intent", "keywords": "workflow routine start run focus morning wind down", "description": "Run a workflow/routine"},
        {"type": "workflow_list", "handler": "handle_intent", "keywords": "workflow routine list show", "description": "List available workflows"},
        {"type": "workflow_create", "handler": "handle_intent", "keywords": "workflow routine create new add", "description": "Create a custom workflow"},
    ],
    "examples": [
        "Start my focus routine",
        "Run morning routine",
        "Wind down routine",
        "List my workflows",
    ],
}

# --- Built-in workflows ---

BUILTIN_WORKFLOWS = {
    "focus": {
        "name": "Focus Mode",
        "description": "Start a focus session — pomodoro + pause music + enable DND",
        "steps": [
            {"skill": "pomodoro", "action": "pomodoro_start", "intent": {}},
            {"skill": "apple_music", "action": "apple_music_pause", "intent": {}},
            {"skill": "macos_focus", "action": "macos_focus_dnd_on", "intent": {}},
        ],
    },
    "morning": {
        "name": "Morning Routine",
        "description": "Morning check-in — weather + calendar + health summary",
        "steps": [
            {"skill": "weather", "action": "weather", "intent": {}},
            {"skill": "calendar", "action": "calendar", "intent": {}},
            {"skill": "apple_health", "action": "health_summary", "intent": {}},
        ],
    },
    "wind_down": {
        "name": "Wind Down",
        "description": "End focus — stop pomodoro + resume music + disable DND",
        "steps": [
            {"skill": "pomodoro", "action": "pomodoro_stop", "intent": {}},
            {"skill": "apple_music", "action": "apple_music_play", "intent": {}},
            {"skill": "macos_focus", "action": "macos_focus_dnd_off", "intent": {}},
        ],
    },
}


def _load_user_workflows() -> dict:
    """Load user-defined workflows from data/workflows.json."""
    if not WORKFLOWS_FILE.exists():
        return {}
    try:
        return json.loads(WORKFLOWS_FILE.read_text())
    except Exception:
        return {}


def _save_user_workflows(workflows: dict):
    """Save user-defined workflows."""
    WORKFLOWS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORKFLOWS_FILE.write_text(json.dumps(workflows, indent=2))


def get_all_workflows() -> dict:
    """Get all workflows — built-in + user-defined."""
    all_wf = dict(BUILTIN_WORKFLOWS)
    all_wf.update(_load_user_workflows())
    return all_wf


def _resolve_workflow_name(query: str) -> str | None:
    """Match a user query to a workflow name."""
    query_lower = query.lower()
    all_wf = get_all_workflows()

    # Direct name match
    for key in all_wf:
        if key in query_lower:
            return key

    # Alias matching
    aliases = {
        "focus": ["focus", "concentrate", "deep work"],
        "morning": ["morning", "wake up", "start day"],
        "wind_down": ["wind down", "winddown", "evening", "relax", "end focus"],
    }
    for key, words in aliases.items():
        if key in all_wf and any(w in query_lower for w in words):
            return key

    return None


async def run_workflow(workflow_name: str, ctx) -> list[str]:
    """Execute a workflow — run each step's skill handler in sequence.

    Returns list of step result summaries.
    """
    all_wf = get_all_workflows()
    wf = all_wf.get(workflow_name)
    if not wf:
        return [f"Workflow '{workflow_name}' not found."]

    results = []
    for i, step in enumerate(wf.get("steps", []), 1):
        action = step.get("action", "")
        intent = step.get("intent", {})

        try:
            from skills import get_registry
            registry = get_registry()
            handler = registry.get_handler(action)
            if handler:
                # Run the handler — it will reply to ctx
                await handler(action, intent, ctx)
                results.append(f"Step {i}: {action} \u2714")
            else:
                results.append(f"Step {i}: {action} \u2014 no handler found")
        except Exception as e:
            results.append(f"Step {i}: {action} \u2014 error: {e}")
            log.warning("Workflow step %d (%s) failed: %s", i, action, e)

    return results


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle workflow-related intents."""
    query = intent.get("query", "") or intent.get("user_query", "")

    if action == "workflow_run":
        wf_name = _resolve_workflow_name(query)
        if not wf_name:
            # List available workflows as suggestion
            all_wf = get_all_workflows()
            names = [f"  \u2022 **{k}**: {v.get('description', '')}" for k, v in all_wf.items()]
            await ctx.reply(
                "I couldn't determine which workflow to run.\n\n"
                "Available workflows:\n" + "\n".join(names)
            )
            return True

        wf = get_all_workflows()[wf_name]
        await ctx.reply(f"\u25b6\ufe0f Running **{wf.get('name', wf_name)}**...")
        results = await run_workflow(wf_name, ctx)
        summary = "\n".join(results)
        await ctx.reply(f"\u2705 Workflow complete:\n{summary}")
        return True

    elif action == "workflow_list":
        all_wf = get_all_workflows()
        if not all_wf:
            await ctx.reply("No workflows configured.")
            return True
        lines = [f"\U0001f4cb **Workflows** ({len(all_wf)}):\n"]
        for key, wf in all_wf.items():
            step_count = len(wf.get("steps", []))
            lines.append(f"  \u2022 **{key}** \u2014 {wf.get('description', '')} ({step_count} steps)")
        await ctx.reply("\n".join(lines))
        return True

    elif action == "workflow_create":
        await ctx.reply(
            "To create a workflow, send a JSON definition:\n\n"
            '```\n{"name": "my_routine", "description": "...", '
            '"steps": [{"action": "weather"}, {"action": "calendar"}]}\n```\n\n'
            "Or tell me the steps and I'll build it."
        )
        return True

    return False
