"""Intent classification — determines how to handle each message.

Classifies before any LLM call so the pipeline knows:
- Whether to search the KB (skip for continuations)
- What context to assemble
- What execution strategy to use

No LLM needed — pure heuristics.
"""

import re
from enum import Enum

# Continuation cues — short replies that mean "keep going with current task"
_CONTINUATION_CUES = {
    "yes", "ok", "okay", "continue", "proceed", "go", "sure", "sounds good",
    "do it", "go ahead", "keep going", "yep", "yeah", "go on", "perfect",
    "great", "good", "right", "correct", "exactly", "that works",
    "sounds right", "let's do it", "make it so", "approved", "lgtm",
    "finish it", "complete it", "keep it", "do the work",
}

# Action verbs that indicate a task (build, create, write, send, etc.)
_ACTION_VERBS = re.compile(
    r"\b(build|create|write|generate|make|send|draft|schedule|set|run|"
    r"delete|remove|open|close|merge|push|deploy|install|update|fix|"
    r"move|copy|rename|search|find|analyze|summarize|prepare|prep)\b",
    re.IGNORECASE,
)

# Artifact signals (subset of action verbs + artifact nouns)
_ARTIFACT_SIGNALS = re.compile(
    r"\b(build|create|write|generate|make)\s+.{0,40}"
    r"\b(presentation|html|page|script|file|document|deck|slides?|report|"
    r"template|website|summary|readme)\b",
    re.IGNORECASE,
)

# Question starters
_QUESTION_STARTERS = (
    "what", "how", "when", "where", "who", "which", "why",
    "is", "are", "do", "does", "can", "could", "will", "would",
    "tell me", "show me", "explain",
)


class Intent(Enum):
    CONTINUATION = "continuation"  # "Yes", "Ok" — inherit active task
    QUESTION = "question"          # "What's the weather?" — KB search + tools
    TASK = "task"                  # "Build a presentation" — full pipeline
    CHAT = "chat"                  # "Hello", "Thanks" — conversational only


def classify_intent(query: str, has_active_task: bool = False) -> Intent:
    """Classify user message intent. No LLM needed — pure heuristics.

    Args:
        query: The user's message text.
        has_active_task: Whether there's an active task in the TaskManager.

    Returns:
        Intent enum value.
    """
    stripped = query.strip().rstrip("!.,")
    lower = stripped.lower()
    words = lower.split()

    # Very short messages with active task → continuation
    if has_active_task and len(words) <= 5:
        if lower in _CONTINUATION_CUES:
            return Intent.CONTINUATION
        # Partial matches for phrases like "yes please", "ok do it"
        if words and words[0] in ("yes", "ok", "okay", "sure", "yep", "yeah", "go", "proceed"):
            return Intent.CONTINUATION

    # With active task: short non-task messages are continuations
    # (catches "what's the status", "keep it short", etc.)
    if has_active_task and len(words) <= 6 and not _ARTIFACT_SIGNALS.search(query):
        # Only break out for clear NEW task requests
        if _ACTION_VERBS.search(query) and len(words) >= 5:
            pass  # Fall through to task detection below
        else:
            return Intent.CONTINUATION

    # Artifact creation → task
    if _ARTIFACT_SIGNALS.search(query):
        return Intent.TASK

    # Action verbs → task
    if _ACTION_VERBS.search(query) and len(words) >= 3:
        return Intent.TASK

    # Question patterns
    if stripped.endswith("?"):
        return Intent.QUESTION
    if any(lower.startswith(s) for s in _QUESTION_STARTERS):
        return Intent.QUESTION

    # If there's an active task and message is substantive, treat as continuation
    if has_active_task and len(words) >= 2:
        return Intent.CONTINUATION

    return Intent.CHAT


def is_artifact_request(query: str) -> bool:
    """Check if query is specifically asking to create a file/artifact."""
    return bool(_ARTIFACT_SIGNALS.search(query))
