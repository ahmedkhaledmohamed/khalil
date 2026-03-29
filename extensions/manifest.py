"""Plugin manifest — enable/disable layer for PharoClaw extensions.

Manages extensions.json which tracks all extensions with metadata
and enabled/disabled state. New extensions register as disabled by default.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from config import EXTENSIONS_DIR

log = logging.getLogger("pharoclaw.manifest")

MANIFEST_PATH = EXTENSIONS_DIR / "extensions.json"

_DEFAULT_MANIFEST = {"extensions": {}}


def load_manifest() -> dict:
    """Load the manifest file. Returns empty default if missing or corrupt."""
    try:
        if MANIFEST_PATH.exists():
            data = json.loads(MANIFEST_PATH.read_text())
            if "extensions" in data:
                return data
            log.warning("Manifest missing 'extensions' key, returning default")
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load manifest: %s", e)
    return {"extensions": {}}


def save_manifest(manifest: dict):
    """Write manifest atomically (write to temp, then rename)."""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(MANIFEST_PATH.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(manifest, f, indent=4)
            f.write("\n")
        os.replace(tmp_path, str(MANIFEST_PATH))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def is_extension_enabled(name: str) -> bool:
    """Check if an extension is enabled. Returns False if not found."""
    manifest = load_manifest()
    entry = manifest["extensions"].get(name)
    if entry is None:
        return False
    return entry.get("enabled", False)


def register_extension(
    name: str,
    *,
    action_type: str,
    intent_patterns: list[str] | None = None,
    description: str = "",
    source_pr: str | None = None,
) -> dict:
    """Add a new extension entry with enabled=false. Returns the entry."""
    manifest = load_manifest()
    entry = {
        "enabled": False,
        "version": "1.0",
        "action_type": action_type,
        "intent_patterns": intent_patterns or [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_pr": source_pr,
        "description": description,
    }
    manifest["extensions"][name] = entry
    save_manifest(manifest)
    log.info("Registered extension '%s' (disabled)", name)
    return entry


def set_extension_enabled(name: str, enabled: bool) -> bool:
    """Enable or disable an extension. Returns True if found and updated."""
    manifest = load_manifest()
    if name not in manifest["extensions"]:
        return False
    manifest["extensions"][name]["enabled"] = enabled
    save_manifest(manifest)
    log.info("Extension '%s' %s", name, "enabled" if enabled else "disabled")
    return True


def list_extensions() -> list[dict]:
    """Return all extensions with their metadata. Includes 'name' key in each."""
    manifest = load_manifest()
    result = []
    for name, entry in manifest["extensions"].items():
        result.append({"name": name, **entry})
    return result


def bootstrap_manifest():
    """Create initial manifest from existing extension JSON files if needed.

    Scans extensions/ for *.json files (excluding extensions.json itself)
    and registers any that aren't already in the manifest.
    """
    manifest = load_manifest()
    changed = False

    for ext_file in sorted(EXTENSIONS_DIR.glob("*.json")):
        if ext_file.name == "extensions.json":
            continue
        try:
            ext_data = json.loads(ext_file.read_text())
            name = ext_data.get("name", ext_file.stem)
            if name not in manifest["extensions"]:
                manifest["extensions"][name] = {
                    "enabled": True,  # Existing extensions default to enabled
                    "version": "1.0",
                    "action_type": ext_data.get("command", name),
                    "intent_patterns": [],
                    "created_at": ext_data.get("generated_at", datetime.now(timezone.utc).isoformat()),
                    "source_pr": None,
                    "description": ext_data.get("description", ""),
                }
                changed = True
                log.info("Bootstrapped extension '%s' into manifest", name)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Skipping %s during bootstrap: %s", ext_file.name, e)

    if changed:
        save_manifest(manifest)
