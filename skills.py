"""Skill registry — auto-discovers action modules and provides intent matching.

Each action module can declare a SKILL dict at module level. The registry
scans all modules at startup, builds a lookup table, and provides:
- Pattern-based intent matching (replaces _ACTION_PATTERNS)
- Keyword-based gap detection (replaces ACTION_REGISTRY)
- Selective context injection for LLM prompts
"""

import importlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("khalil.skills")

_ACTIONS_DIR = Path(__file__).parent / "actions"
_EXTENSIONS_DIR = Path(__file__).parent / "extensions"


@dataclass
class VoiceConfig:
    """Voice-specific metadata for a skill."""

    confirm_before_execute: bool = False  # require verbal confirmation before running
    response_style: str = "brief"  # "brief" or "full" — controls response verbosity


@dataclass
class SensorConfig:
    """Configuration for a skill's background sensor."""

    name: str  # sensor name (e.g. "reminders", "health")
    function: object  # async callable returning dict
    interval_min: int = 5  # how often to run (minutes)
    identify_opportunities: object | None = None  # optional: (state, last_state) -> list[Opportunity]


@dataclass
class Skill:
    """A registered skill backed by an action module."""

    name: str
    description: str
    module_name: str  # e.g. "weather", "spotify"
    actions: dict  # action_type -> {"handler": callable, "description": str}
    patterns: list[tuple[re.Pattern, str]]  # (compiled regex, action_type)
    keywords: dict[str, str]  # action_type -> keyword string (for gap detection)
    category: str = "general"
    examples: list[str] = field(default_factory=list)
    command: str | None = None  # Telegram /command
    command_handler: str | None = None  # function name for /command
    sensor: SensorConfig | None = None  # optional background sensor
    voice: VoiceConfig | None = None  # optional voice-specific config

    def match(self, text: str) -> str | None:
        """Return the first matching action_type for the given text, or None."""
        text_lower = text.lower()
        for pattern, action_type in self.patterns:
            if pattern.search(text_lower):
                return action_type
        return None


class SkillRegistry:
    """Central registry for all discovered skills."""

    def __init__(self):
        self._skills: dict[str, Skill] = {}  # name -> Skill
        self._action_index: dict[str, Skill] = {}  # action_type -> Skill

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill
        for action_type in skill.actions:
            self._action_index[action_type] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def get_by_action(self, action_type: str) -> Skill | None:
        return self._action_index.get(action_type)

    def match_intent(self, text: str) -> tuple[str | None, Skill | None]:
        """Match text against all skill patterns.

        Returns (action_type, skill) or (None, None).
        """
        text_lower = text.lower()
        for skill in self._skills.values():
            for pattern, action_type in skill.patterns:
                if pattern.search(text_lower):
                    return action_type, skill
        return None, None

    def find_keyword_match(self, query: str) -> str | None:
        """Keyword overlap match for gap detection (replaces find_matching_action).

        Returns action_type or None.
        """
        query_words = set(re.findall(r"\b\w+\b", query.lower()))
        best_action = None
        best_score = 0
        for skill in self._skills.values():
            for action_type, keywords in skill.keywords.items():
                keyword_set = set(keywords.split())
                score = len(query_words & keyword_set)
                if score > best_score and score >= 2:
                    best_score = score
                    best_action = action_type
        return best_action

    def get_handler(self, action_type: str):
        """Get the intent handler callable for an action_type, or None."""
        skill = self._action_index.get(action_type)
        if not skill:
            return None
        action_info = skill.actions.get(action_type, {})
        return action_info.get("handler")

    def get_context_for_intent(self, text: str, max_skills: int = 5) -> str:
        """Build selective LLM context based on intent.

        Returns a compact description of relevant skills for prompt injection.
        """
        text_lower = text.lower()
        scored: list[tuple[int, Skill]] = []

        for skill in self._skills.values():
            score = 0
            # Pattern match = high score
            for pattern, _ in skill.patterns:
                if pattern.search(text_lower):
                    score += 10
                    break
            # Keyword overlap
            for keywords in skill.keywords.values():
                overlap = len(
                    set(re.findall(r"\b\w+\b", text_lower))
                    & set(keywords.split())
                )
                score += overlap
            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [s for _, s in scored[:max_skills]]

        if not top:
            return self.format_full_capabilities()

        lines = ["Available capabilities:"]
        for skill in top:
            lines.append(f"- **{skill.name}**: {skill.description}")
            if skill.examples:
                lines.append(f"  Examples: {'; '.join(skill.examples[:2])}")
        return "\n".join(lines)

    def format_category_summary(self) -> str:
        """One-liner per category for fallback context injection."""
        cats: dict[str, list[str]] = {}
        for skill in self._skills.values():
            cats.setdefault(skill.category, []).append(skill.name)
        lines = ["Available capability categories:"]
        for cat, names in sorted(cats.items()):
            lines.append(f"- {cat}: {', '.join(names)}")
        return "\n".join(lines)

    def format_full_capabilities(self) -> str:
        """Full capability manifest for system prompt injection.

        Groups skills by category with action counts. Used to ensure the LLM
        knows everything Khalil can do.
        """
        cats: dict[str, list[Skill]] = {}
        for skill in self._skills.values():
            cats.setdefault(skill.category, []).append(skill)

        lines = [f"Khalil has {len(self._skills)} skills and {len(self._action_index)} action types:\n"]
        for cat in sorted(cats):
            skills = cats[cat]
            lines.append(f"**{cat}** ({len(skills)} skills):")
            for skill in sorted(skills, key=lambda s: s.name):
                action_count = len(skill.actions)
                lines.append(f"  - {skill.name}: {skill.description} ({action_count} actions)")
            lines.append("")
        return "\n".join(lines)

    def get_all_keywords(self) -> set[str]:
        """All trigger keywords across all skills (for gap detection overlap check)."""
        words = set()
        for skill in self._skills.values():
            for keywords in skill.keywords.values():
                words.update(keywords.split())
            words.update(skill.name.split("_"))
        return words

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def get_sensors(self) -> list[SensorConfig]:
        """Return all registered sensors from skills that have them."""
        return [s.sensor for s in self._skills.values() if s.sensor is not None]

    def needs_voice_confirmation(self, action_type: str) -> bool:
        """Check if an action requires voice confirmation before execution."""
        skill = self._action_index.get(action_type)
        if not skill:
            return False
        if skill.voice and skill.voice.confirm_before_execute:
            return True
        return False

    def get_voice_config(self, action_type: str) -> VoiceConfig | None:
        """Get voice config for an action's parent skill."""
        skill = self._action_index.get(action_type)
        if skill:
            return skill.voice
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _build_skill(module_name: str, mod) -> Skill | None:
    """Build a Skill from a module's SKILL dict, or return None."""
    raw = getattr(mod, "SKILL", None)
    if not raw or not isinstance(raw, dict):
        return None

    name = raw.get("name", module_name)
    patterns = []
    for entry in raw.get("patterns", []):
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            patterns.append((re.compile(entry[0], re.IGNORECASE), entry[1]))
        elif isinstance(entry, str):
            # Pattern string without action_type — defaults to skill name
            patterns.append((re.compile(entry, re.IGNORECASE), name))

    actions = {}
    for action_def in raw.get("actions", []):
        atype = action_def.get("type")
        handler_name = action_def.get("handler")
        handler = getattr(mod, handler_name, None) if handler_name else None
        actions[atype] = {
            "handler": handler,
            "description": action_def.get("description", ""),
        }

    keywords = {}
    for action_def in raw.get("actions", []):
        atype = action_def.get("type")
        kw = action_def.get("keywords", "")
        if kw:
            keywords[atype] = kw

    # Build sensor config if present
    sensor = None
    sensor_raw = raw.get("sensor")
    if sensor_raw and isinstance(sensor_raw, dict):
        sensor_fn_name = sensor_raw.get("function")
        sensor_fn = getattr(mod, sensor_fn_name, None) if sensor_fn_name else None
        opp_fn_name = sensor_raw.get("identify_opportunities")
        opp_fn = getattr(mod, opp_fn_name, None) if opp_fn_name else None
        if sensor_fn:
            sensor = SensorConfig(
                name=sensor_raw.get("name", name),
                function=sensor_fn,
                interval_min=sensor_raw.get("interval_min", 5),
                identify_opportunities=opp_fn,
            )

    # Build voice config if present
    voice = None
    voice_raw = raw.get("voice")
    if voice_raw and isinstance(voice_raw, dict):
        voice = VoiceConfig(
            confirm_before_execute=voice_raw.get("confirm_before_execute", False),
            response_style=voice_raw.get("response_style", "brief"),
        )

    return Skill(
        name=name,
        description=raw.get("description", ""),
        module_name=module_name,
        actions=actions,
        patterns=patterns,
        keywords=keywords,
        category=raw.get("category", "general"),
        examples=raw.get("examples", []),
        command=raw.get("command"),
        command_handler=raw.get("command_handler"),
        sensor=sensor,
        voice=voice,
    )


def discover_skills() -> SkillRegistry:
    """Scan actions/*.py for SKILL dicts and build the registry."""
    registry = SkillRegistry()

    # Built-in action modules
    for py_file in sorted(_ACTIONS_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = py_file.stem
        try:
            mod = importlib.import_module(f"actions.{module_name}")
            skill = _build_skill(module_name, mod)
            if skill:
                registry.register(skill)
                log.debug("Registered skill: %s (%d patterns, %d actions)",
                          skill.name, len(skill.patterns), len(skill.actions))
        except Exception as e:
            log.warning("Failed to load skill from actions.%s: %s", module_name, e)

    # Extension modules (from manifests)
    for manifest_path in sorted(_EXTENSIONS_DIR.glob("*.json")):
        if manifest_path.name == "extensions.json":
            continue
        try:
            import json
            manifest = json.loads(manifest_path.read_text())
            mod_path = manifest.get("action_module", "")
            if not mod_path:
                continue
            mod = importlib.import_module(mod_path)
            skill = _build_skill(manifest.get("name", manifest_path.stem), mod)
            if skill:
                registry.register(skill)
        except Exception as e:
            log.debug("Extension skill load failed for %s: %s", manifest_path.name, e)

    log.info("Skill registry: %d skills, %d action types",
             len(registry._skills), len(registry._action_index))
    return registry


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_registry: SkillRegistry | None = None


def get_registry() -> SkillRegistry:
    """Get or create the global skill registry."""
    global _registry
    if _registry is None:
        _registry = discover_skills()
    return _registry


def reload_registry() -> SkillRegistry:
    """Force re-discovery (e.g. after hot-reload of extensions)."""
    global _registry
    _registry = discover_skills()
    return _registry
