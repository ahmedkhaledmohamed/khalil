"""Claude Code CLI wrapper for complex code generation.

Spawns Claude Code in a git worktree for isolated, multi-file code generation.
Used by the self-extension engine for capabilities that need external APIs,
multi-step flows, or complex integrations.
"""

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

from config import CLAUDE_CODE_BIN, KHALIL_DIR, WORKTREES_DIR

log = logging.getLogger("khalil.actions.claude_code")


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


def create_worktree(branch_name: str) -> Path:
    """Create a git worktree for isolated code generation.

    Returns the worktree path.
    """
    WORKTREES_DIR.mkdir(exist_ok=True)
    wt_path = WORKTREES_DIR / branch_name.replace("/", "-")
    if wt_path.exists():
        shutil.rmtree(wt_path)

    subprocess.run(
        ["git", "worktree", "add", str(wt_path), "-b", branch_name],
        cwd=str(KHALIL_DIR),
        check=True,
        capture_output=True,
    )
    return wt_path


def cleanup_worktree(branch_name: str):
    """Remove a worktree after use."""
    wt_path = WORKTREES_DIR / branch_name.replace("/", "-")
    try:
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=str(KHALIL_DIR),
            capture_output=True,
        )
    except Exception as e:
        log.warning("Failed to clean up worktree %s: %s", branch_name, e)

    # Also try to delete the branch if it wasn't pushed
    try:
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=str(KHALIL_DIR),
            capture_output=True,
        )
    except Exception:
        pass
