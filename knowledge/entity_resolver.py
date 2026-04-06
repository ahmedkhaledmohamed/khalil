"""M4: Entity pre-resolution — resolve names/references before LLM sees the query.

Given a query like "Email John about the proposal", resolves:
- "John" → full name, email address, relationship context
- "the proposal" → recent documents/conversations about proposals

Uses email archives + conversation memories + knowledge base.
Caches results per session to avoid repeated lookups.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("khalil.entity_resolver")

# Common non-name words to filter out
_STOP_WORDS = {
    "the", "a", "an", "my", "your", "our", "their", "this", "that", "about",
    "with", "from", "for", "and", "but", "or", "not", "all", "any", "some",
    "it", "its", "i", "me", "we", "us", "you", "he", "she", "they", "them",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "shall", "should", "may", "might",
    "can", "could", "must", "need", "to", "of", "in", "on", "at", "by",
    "up", "out", "off", "over", "under", "again", "further", "then", "once",
}

# Pattern to extract capitalized names from text
_NAME_PATTERN = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b")


@dataclass
class ResolvedEntity:
    """A resolved entity with its context."""
    name: str
    entity_type: str  # "person", "project", "document"
    email: str | None = None
    context: str = ""  # brief context about the entity
    confidence: float = 0.0


class EntityResolver:
    """Resolves entity references in queries against knowledge base."""

    def __init__(self):
        self._cache: dict[str, ResolvedEntity | None] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 3600  # 1 hour

    def _is_cache_valid(self) -> bool:
        return (time.monotonic() - self._cache_ts) < self._cache_ttl

    def clear_cache(self):
        self._cache.clear()
        self._cache_ts = 0.0

    def extract_names(self, query: str) -> list[str]:
        """Extract potential person names from query text."""
        matches = _NAME_PATTERN.findall(query)
        return [m for m in matches if m.lower() not in _STOP_WORDS and len(m) > 1]

    async def resolve_contact(self, name: str) -> ResolvedEntity | None:
        """Resolve a person's name to their full identity and context.

        Searches:
        1. Email archives (From: headers)
        2. Conversation memories
        3. Knowledge base documents
        """
        cache_key = name.lower()
        if self._is_cache_valid() and cache_key in self._cache:
            return self._cache[cache_key]

        result = None

        # Search email archives for this person
        try:
            from knowledge.search import keyword_search
            email_results = keyword_search(f"From: {name}", limit=5, category="email")
            if email_results:
                # Extract email address from results
                email_addr = None
                context_bits = []
                for doc in email_results[:3]:
                    content = doc.get("content", "")
                    # Try to extract email from "From: Name <email>" pattern
                    email_match = re.search(
                        rf"{re.escape(name)}.*?<([^>]+@[^>]+)>", content, re.IGNORECASE
                    )
                    if email_match and not email_addr:
                        email_addr = email_match.group(1)
                    # Extract subject for context
                    subj_match = re.search(r"Subject:\s*(.+)", content)
                    if subj_match:
                        context_bits.append(subj_match.group(1).strip()[:60])

                if email_addr or context_bits:
                    result = ResolvedEntity(
                        name=name,
                        entity_type="person",
                        email=email_addr,
                        context=f"Recent topics: {'; '.join(context_bits[:3])}" if context_bits else "",
                        confidence=0.8 if email_addr else 0.5,
                    )
        except Exception as e:
            log.debug("Email search for %s failed: %s", name, e)

        # Search conversation memories
        if not result or not result.email:
            try:
                from knowledge.search import search_memories
                memories = await search_memories(name, limit=3)
                if memories:
                    mem_context = "; ".join(m["content"][:80] for m in memories[:2])
                    if result:
                        result.context += f" | Memories: {mem_context}"
                    else:
                        result = ResolvedEntity(
                            name=name,
                            entity_type="person",
                            context=f"From memories: {mem_context}",
                            confidence=0.4,
                        )
            except Exception as e:
                log.debug("Memory search for %s failed: %s", name, e)

        self._cache[cache_key] = result
        if not self._cache_ts:
            self._cache_ts = time.monotonic()

        return result

    async def resolve_entities_in_query(self, query: str) -> dict[str, ResolvedEntity]:
        """Extract and resolve all entities mentioned in a query.

        Returns dict of name -> ResolvedEntity for all resolved entities.
        """
        names = self.extract_names(query)
        if not names:
            return {}

        import asyncio
        tasks = [self.resolve_contact(name) for name in names[:5]]  # cap at 5
        results = await asyncio.gather(*tasks, return_exceptions=True)

        resolved = {}
        for name, result in zip(names, results):
            if isinstance(result, ResolvedEntity) and result:
                resolved[name] = result

        return resolved

    def format_entity_context(self, entities: dict[str, ResolvedEntity]) -> str:
        """Format resolved entities as context string for LLM injection."""
        if not entities:
            return ""
        lines = ["[Resolved entities]"]
        for name, entity in entities.items():
            parts = [f"- {name}"]
            if entity.email:
                parts.append(f"({entity.email})")
            if entity.context:
                parts.append(f"— {entity.context[:150]}")
            lines.append(" ".join(parts))
        return "\n".join(lines)


# Singleton
_resolver: EntityResolver | None = None


def get_entity_resolver() -> EntityResolver:
    global _resolver
    if _resolver is None:
        _resolver = EntityResolver()
    return _resolver
