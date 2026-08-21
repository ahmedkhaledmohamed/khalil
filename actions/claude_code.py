"""Coding-agent runtime and isolated worktree utilities.

Codex is the default executor. Claude Code remains available as an explicit
compatibility backend for existing installations.
"""

import asyncio
import importlib.util
import logging
import subprocess
from pathlib import Path

from config import (
    CLAUDE_CODE_BIN,
    CODEX_MODEL,
    CODEX_REASONING_EFFORT,
    CODING_AGENT_BACKEND,
    KHALIL_DIR,
    WORKTREES_DIR,
)

log = logging.getLogger("khalil.actions.claude_code")


class WorktreeValidationError(RuntimeError):
    """Raised when generated work escapes its expected file boundary."""


def coding_agent_available(backend: str | None = None) -> bool:
    """Return whether the configured coding-agent runtime is installed."""
    selected = (backend or CODING_AGENT_BACKEND).lower()
    if selected == "codex":
        return importlib.util.find_spec("openai_codex") is not None
    if selected == "claude":
        return Path(CLAUDE_CODE_BIN).exists()
    return False


async def run_codex(
    prompt: str,
    worktree_path: Path,
    timeout: int = 300,
) -> tuple[bool, str]:
    """Run one Codex SDK turn inside an isolated worktree."""
    worktree_path = Path(worktree_path).resolve()
    if not worktree_path.is_dir():
        return False, f"Codex worktree not found: {worktree_path}"

    try:
        from openai_codex import ApprovalMode, AsyncCodex, Sandbox
    except ImportError:
        return False, "Codex SDK is not installed. Install requirements.txt first."

    async def _run() -> tuple[bool, str]:
        async with AsyncCodex() as codex:
            thread_options = {
                "cwd": str(worktree_path),
                "sandbox": Sandbox.workspace_write,
                "approval_mode": ApprovalMode.auto_review,
            }
            if CODEX_MODEL:
                thread_options["model"] = CODEX_MODEL

            thread = await codex.thread_start(**thread_options)
            run_options = {
                "cwd": str(worktree_path),
                "sandbox": Sandbox.workspace_write,
            }
            if CODEX_REASONING_EFFORT:
                run_options["effort"] = CODEX_REASONING_EFFORT

            result = await thread.run(prompt, **run_options)
            if result.error:
                return False, str(result.error)
            return True, result.final_response or "Codex completed without a final response."

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        return False, f"Codex timed out after {timeout}s"
    except Exception as exc:
        log.exception("Codex coding task failed")
        return False, str(exc)


async def run_coding_agent(
    prompt: str,
    worktree_path: Path,
    timeout: int = 300,
) -> tuple[bool, str]:
    """Run the explicitly configured coding-agent backend."""
    if CODING_AGENT_BACKEND == "codex":
        return await run_codex(prompt, worktree_path, timeout)
    if CODING_AGENT_BACKEND == "claude":
        return await run_claude_code(prompt, worktree_path, timeout)
    return False, f"Unsupported coding-agent backend: {CODING_AGENT_BACKEND}"


def _run_git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run git in a repository and raise with its stderr on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result


def _normalize_expected_path(path: str) -> str:
    """Return a safe repository-relative path using git's path separator."""
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"Expected path must stay inside the worktree: {path}")
    normalized = candidate.as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        raise ValueError("Expected path cannot be empty")
    return normalized


def resolve_worktree_path(worktree_path: Path, relative_path: str) -> Path:
    """Resolve a validated repository-relative path inside a worktree."""
    worktree_path = Path(worktree_path).resolve()
    normalized = _normalize_expected_path(relative_path)
    resolved = (worktree_path / normalized).resolve()
    if not resolved.is_relative_to(worktree_path):
        raise ValueError(f"Path escapes worktree: {relative_path}")
    return resolved


async def run_claude_code(
    prompt: str,
    worktree_path: Path,
    timeout: int = 300,
) -> tuple[bool, str]:
    """Run Claude Code CLI with a prompt in the given directory.

    Returns (success, output_text).
    """
    if not Path(CLAUDE_CODE_BIN).exists():
        return False, f"Claude Code CLI not found at {CLAUDE_CODE_BIN}"

    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_CODE_BIN,
            "--print",
            "--dangerously-skip-permissions",
            "--output-format", "text",
            prompt,
            cwd=str(worktree_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )

        if proc.returncode == 0:
            return True, stdout.decode()
        else:
            return False, stderr.decode() or stdout.decode()

    except asyncio.TimeoutError:
        proc.kill()
        return False, f"Claude Code timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def create_worktree(
    branch_name: str,
    *,
    repo_dir: Path | None = None,
    worktrees_dir: Path | None = None,
    base_ref: str = "origin/main",
) -> Path:
    """Create a git worktree for isolated code generation.

    The branch always starts from a freshly fetched remote main commit, never
    from the caller's current branch or working-tree state.

    Returns the worktree path.
    """
    repo_dir = Path(repo_dir or KHALIL_DIR).resolve()
    worktrees_dir = Path(worktrees_dir or WORKTREES_DIR).resolve()
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    wt_path = worktrees_dir / branch_name.replace("/", "-")
    if wt_path.exists():
        raise RuntimeError(f"Worktree path already exists: {wt_path}")

    _run_git(repo_dir, "fetch", "origin", "main")
    base_sha = _run_git(repo_dir, "rev-parse", f"{base_ref}^{{commit}}").stdout.strip()
    _run_git(repo_dir, "worktree", "add", str(wt_path), "-b", branch_name, base_sha)
    return wt_path


def validate_worktree_changes(
    worktree_path: Path,
    expected_paths: set[str] | list[str] | tuple[str, ...],
    *,
    base_ref: str = "origin/main",
    require_changes: bool = True,
) -> list[str]:
    """Return changed paths, rejecting anything outside ``expected_paths``."""
    worktree_path = Path(worktree_path).resolve()
    allowed = {_normalize_expected_path(path) for path in expected_paths}

    tracked = _run_git(
        worktree_path,
        "diff",
        "--name-only",
        "--diff-filter=ACDMRTUXB",
        base_ref,
        "--",
    ).stdout.splitlines()
    untracked = _run_git(
        worktree_path,
        "ls-files",
        "--others",
        "--exclude-standard",
    ).stdout.splitlines()
    changed = sorted({path for path in tracked + untracked if path})

    if require_changes and not changed:
        raise WorktreeValidationError("Generated worktree contains no changes")

    unexpected = sorted(set(changed) - allowed)
    if unexpected:
        raise WorktreeValidationError(
            "Generated worktree changed unexpected files: " + ", ".join(unexpected)
        )

    return changed


def cleanup_worktree(
    branch_name: str,
    *,
    repo_dir: Path | None = None,
    worktrees_dir: Path | None = None,
):
    """Remove a worktree after use."""
    repo_dir = Path(repo_dir or KHALIL_DIR).resolve()
    worktrees_dir = Path(worktrees_dir or WORKTREES_DIR).resolve()
    wt_path = worktrees_dir / branch_name.replace("/", "-")
    try:
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=str(repo_dir),
            capture_output=True,
        )
    except Exception as e:
        log.warning("Failed to clean up worktree %s: %s", branch_name, e)

    # Also try to delete the branch if it wasn't pushed
    try:
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=str(repo_dir),
            capture_output=True,
        )
    except Exception:
        pass
