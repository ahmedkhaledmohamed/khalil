"""Structured LLM output parsing via Pydantic models.

Provides typed models for all structured LLM responses and a robust
parse_llm_json() helper that replaces fragile manual fence-stripping
and json.loads() calls scattered across the codebase.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional, TypeVar

from pydantic import BaseModel, Field

log = logging.getLogger("khalil.llm")

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Pydantic models for structured LLM responses
# ---------------------------------------------------------------------------


class ActionIntent(BaseModel):
    """Intent extracted from user message (server.py detect_intent)."""
    action: str
    text: str = ""
    time: str = ""
    to: str = ""
    subject: str = ""
    context_query: str = ""
    command: str = ""
    description: str = ""


class TaskStepModel(BaseModel):
    """A single step in a decomposed multi-step plan (orchestrator.py)."""
    id: str
    action: str
    description: str = ""
    params: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class WeeklyInsight(BaseModel):
    """A single insight from weekly reflection (learning.py)."""
    category: str
    summary: str
    evidence: str = ""
    recommendation: str = ""
    auto_apply: bool = False


class MonthlyMetaInsight(BaseModel):
    """A meta-insight from monthly reflection (learning.py)."""
    category: str = "meta"
    summary: str
    recommendation: str = ""


class WorkflowProposal(BaseModel):
    """LLM-generated workflow proposal (workflows.py)."""
    name: str = ""
    trigger_type: str = "signal"
    trigger_config: dict = Field(default_factory=dict)
    condition: Optional[dict] = None
    actions: list[dict] = Field(default_factory=list)


class DailyActionModel(BaseModel):
    """A daily action plan item (scheduler/planning.py)."""
    description: str
    time_estimate: str = ""
    linked_goal: str = ""
    priority: int = 1


class CapabilityGapResult(BaseModel):
    """Classification of a capability gap (actions/extend.py)."""
    type: str  # "capability_gap", "knowledge_gap", or "conversation"
    name: str = ""
    command: str = ""
    description: str = ""


class ReceiptItem(BaseModel):
    """A single item on a receipt."""
    name: str = ""
    price: float = 0.0


class ReceiptData(BaseModel):
    """Extracted receipt data (actions/doc_extract.py)."""
    vendor: str = ""
    date: str = ""
    total: float = 0.0
    currency: str = "CAD"
    items: list[ReceiptItem] = Field(default_factory=list)
    tax: float = 0.0
    payment_method: str = ""


# ---------------------------------------------------------------------------
# Robust JSON extraction
# ---------------------------------------------------------------------------

# Matches ```json ... ``` or ``` ... ```
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)
# Matches a top-level JSON object or array
_JSON_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def extract_json_str(text: str) -> Optional[str]:
    """Extract a JSON string from an LLM response.

    Handles:
    - Clean JSON (no wrapping)
    - Markdown fenced code blocks (```json ... ```)
    - JSON embedded in prose
    """
    text = text.strip()
    if not text:
        return None

    # 1. Try direct parse first (clean response)
    if text.startswith(("{", "[")):
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass

    # 2. Try extracting from markdown fences
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # 3. Try regex extraction of JSON object/array
    json_match = _JSON_RE.search(text)
    if json_match:
        candidate = json_match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return None


def parse_llm_json(text: str, model: type[T]) -> Optional[T]:
    """Parse an LLM response into a Pydantic model.

    Handles markdown fences, embedded JSON, and validation.
    Returns None if parsing fails (never raises).
    """
    json_str = extract_json_str(text)
    if json_str is None:
        log.debug("No JSON found in LLM response: %s", text[:100])
        return None

    try:
        data = json.loads(json_str)
        return model.model_validate(data)
    except (json.JSONDecodeError, Exception) as e:
        log.debug("Failed to parse LLM JSON into %s: %s", model.__name__, e)
        return None


def parse_llm_json_list(text: str, model: type[T]) -> list[T]:
    """Parse an LLM response into a list of Pydantic models.

    Handles markdown fences, embedded JSON arrays, and validation.
    Returns empty list if parsing fails (never raises).
    """
    json_str = extract_json_str(text)
    if json_str is None:
        log.debug("No JSON found in LLM response: %s", text[:100])
        return []

    try:
        data = json.loads(json_str)
        if not isinstance(data, list):
            log.debug("Expected JSON array, got %s", type(data).__name__)
            return []
        return [model.model_validate(item) for item in data]
    except (json.JSONDecodeError, Exception) as e:
        log.debug("Failed to parse LLM JSON list into %s: %s", model.__name__, e)
        return []
