"""M7: Contextual pre-fetch — enrich tool call params with knowledge base context.

Before executing a tool call, automatically:
1. Resolve entity references (names → emails via entity resolver)
2. Inject relevant context from knowledge base
3. Enrich params with missing information

Example: email(to="john") → resolves "john" to "john@example.com" + recent thread context.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("khalil.prefetch")

# Rules defining what to pre-fetch for each tool type
PREFETCH_RULES: dict[str, dict] = {
    "email": {
        "resolve": ["to"],         # Resolve these params as person names
        "context": ["subject"],     # Use these params to search for context
        "inject": "body_context",   # Inject found context into this param
    },
    "email_draft": {
        "resolve": ["to"],
        "context": ["subject"],
        "inject": "body_context",
    },
    "calendar_create": {
        "resolve": ["attendees"],
        "context": ["summary"],
        "inject": "notes_context",
    },
    "meeting_prep": {
        "context": ["meeting_title", "title"],
        "inject": "knowledge_context",
    },
    "send_to_claude": {
        "context": ["command"],
        "inject": "project_context",
    },
}


async def prefetch_for_tool(tool_name: str, args: dict) -> dict:
    """Enrich tool call arguments with pre-fetched context.

    Returns the enriched args dict (original dict is not modified).
    """
    rules = PREFETCH_RULES.get(tool_name)
    if not rules:
        return args

    enriched = dict(args)

    # 1. Resolve entity references
    resolve_params = rules.get("resolve", [])
    if resolve_params:
        try:
            from knowledge.entity_resolver import get_entity_resolver
            resolver = get_entity_resolver()
            for param in resolve_params:
                value = enriched.get(param, "")
                if not value or "@" in value:  # Skip if already an email
                    continue
                # Handle comma-separated lists
                names = [n.strip() for n in value.split(",")]
                resolved_names = []
                for name in names:
                    entity = await resolver.resolve_contact(name)
                    if entity and entity.email:
                        resolved_names.append(entity.email)
                        log.info("Prefetch: resolved '%s' → '%s'", name, entity.email)
                    else:
                        resolved_names.append(name)  # Keep original
                enriched[param] = ", ".join(resolved_names)
        except Exception as e:
            log.debug("Prefetch entity resolution failed: %s", e)

    # 2. Inject knowledge context
    context_params = rules.get("context", [])
    inject_param = rules.get("inject", "")
    if context_params and inject_param and inject_param not in enriched:
        try:
            from knowledge.context import get_relevant_context
            search_terms = []
            for param in context_params:
                val = enriched.get(param, "")
                if val:
                    search_terms.append(val)
            if search_terms:
                query = " ".join(search_terms)
                context = get_relevant_context(query, max_chars=800)
                if context and len(context) > 20:
                    enriched[inject_param] = context
                    log.info("Prefetch: injected %d chars of context for %s", len(context), tool_name)
        except Exception as e:
            log.debug("Prefetch context injection failed: %s", e)

    return enriched
