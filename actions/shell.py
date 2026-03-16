"""Shell command execution with safety classification.

Three-tier classification:
- SAFE (READ): auto-execute — open apps, read-only commands, system info
- RISKY (WRITE): needs approval — installs, file modifications, etc.
- BLOCKED (DANGEROUS): never execute — sudo, rm -rf /, disk formatting, etc.
"""

import asyncio
import logging
import os
import re
import subprocess

from config import ActionType

log = logging.getLogger("khalil.actions.shell")

SHELL_TIMEOUT = 30
MAX_OUTPUT_LENGTH = 3000

# --- Classification ---

BLOCKED_PATTERNS = [
    r"\bsudo\b",
    r"\brm\s+-rf\s+/\s*$",
    r"\brm\s+-rf\s+/[^.]",       # rm -rf /anything (not relative)
    r"\brm\s+-rf\s+~\s*$",       # rm -rf ~ (home dir)
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bdiskutil\s+erase",
    r"\bnewfs\b",
    r"\bchmod\s+-r\s+777\s+/",
    r"\bcurl.*\|\s*(ba)?sh",
    r"\bwget.*\|\s*(ba)?sh",
    r">\s*/dev/sd",
    r">\s*/dev/disk",
    r":\(\)\s*\{",                # fork bomb
    r"\blaunchctl\s+unload\b",
    r"\bkillall\s+Finder\b",
    r"\bkillall\s+Dock\b",
    r"\bkillall\s+SystemUIServer\b",
]

SAFE_PREFIXES = [
    # macOS app/URL opening
    "open ",
    "open -a ",
    # Read-only file operations
    "ls",
    "cat ",
    "head ",
    "tail ",
    "wc ",
    "file ",
    "stat ",
    "find ",
    "tree ",
    # System info
    "date",
    "cal",
    "uptime",
    "whoami",
    "pwd",
    "hostname",
    "sw_vers",
    "uname",
    "arch",
    "sysctl ",
    "system_profiler ",
    # Disk/network info
    "df",
    "du ",
    "ifconfig",
    "networksetup ",
    "diskutil list",
    "diskutil info",
    # Process info
    "ps ",
    "top -l ",
    "pgrep ",
    # Package info (read-only)
    "brew list",
    "brew info",
    "brew search",
    "pip list",
    "pip show",
    "pip3 list",
    "pip3 show",
    "npm list",
    "npm info",
    # Version checks
    "python --version",
    "python3 --version",
    "node --version",
    "npm --version",
    "ruby --version",
    "swift --version",
    "git --version",
    "java -version",
    # Git read-only
    "git status",
    "git log",
    "git diff",
    "git branch",
    "git remote",
    "git show",
    "git stash list",
    # Other read-only
    "echo ",
    "which ",
    "where ",
    "type ",
    "printenv",
    "env",
    "defaults read",
    "pmset -g",
    "ioreg ",
    "screencapture ",
    "pbpaste",
    "osascript ",
]


def classify_command(cmd: str) -> ActionType:
    """Classify a shell command into READ (safe), WRITE (risky), or DANGEROUS (blocked)."""
    cmd_stripped = cmd.strip()
    cmd_lower = cmd_stripped.lower()

    # Check blocked patterns first
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, cmd_lower):
            return ActionType.DANGEROUS

    # Check safe prefixes
    for prefix in SAFE_PREFIXES:
        if cmd_lower.startswith(prefix) or cmd_lower == prefix.strip():
            return ActionType.READ

    # Everything else is WRITE (risky)
    return ActionType.WRITE


# --- Execution ---

def _sanitize_env() -> dict:
    """Return env with sensitive variables removed."""
    env = os.environ.copy()
    sensitive_keywords = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL", "API_KEY")
    for key in list(env.keys()):
        if any(s in key.upper() for s in sensitive_keywords):
            del env[key]
    return env


async def execute_shell(cmd: str, cwd: str = None, timeout: int = SHELL_TIMEOUT) -> dict:
    """Execute a shell command in a subprocess.

    Returns: {"returncode": int, "stdout": str, "stderr": str, "timed_out": bool}
    """
    if not cwd:
        cwd = os.path.expanduser("~")

    def _run():
        try:
            result = subprocess.run(
                cmd, shell=True,
                capture_output=True, text=True,
                cwd=cwd, timeout=timeout,
                env=_sanitize_env(),
            )
            return {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
                "timed_out": True,
            }

    return await asyncio.to_thread(_run)


# --- Output Formatting ---

# --- Error Classification ---

_TRANSIENT_PATTERNS = [
    "timed out", "resource busy", "try again", "connection refused",
    "resource temporarily unavailable", "interrupted system call",
]

_PERMANENT_PATTERNS = [
    "permission denied", "operation not permitted", "command not found",
    "no such file or directory", "not a directory",
]

# Errors that look permanent but often have alternative approaches
_CORRECTABLE_OVERRIDES = [
    "not allowed assistive access",  # osascript can often avoid accessibility
]

# User-friendly hints for specific permanent errors
_ERROR_HINTS = {
    "not allowed assistive access": (
        "macOS requires accessibility permission for this command.\n"
        "Go to System Settings → Privacy & Security → Accessibility "
        "and enable access for the app running Khalil (Terminal, Cursor, etc.)."
    ),
    "permission denied": "The command needs elevated permissions. You may need to grant access in System Settings.",
    "command not found": "This command is not installed on your system.",
}

# Severity ordering for escalation check
_ACTION_SEVERITY = {ActionType.READ: 0, ActionType.WRITE: 1, ActionType.DANGEROUS: 2}


def classify_error(returncode: int, stderr: str) -> str:
    """Classify a shell error as 'transient', 'correctable', or 'permanent'."""
    stderr_lower = stderr.lower()
    if any(p in stderr_lower for p in _TRANSIENT_PATTERNS):
        return "transient"
    # Check correctable overrides before permanent — these errors have alternative approaches
    if any(p in stderr_lower for p in _CORRECTABLE_OVERRIDES):
        return "correctable"
    if any(p in stderr_lower for p in _PERMANENT_PATTERNS):
        return "permanent"
    return "correctable"


def would_escalate(original_cmd: str, corrected_cmd: str) -> bool:
    """Return True if the corrected command has higher severity than the original."""
    orig = _ACTION_SEVERITY[classify_command(original_cmd)]
    corr = _ACTION_SEVERITY[classify_command(corrected_cmd)]
    return corr > orig


def format_output(result: dict, cmd: str) -> str:
    """Format shell output for Telegram."""
    parts = [f"$ {cmd}"]

    if result["timed_out"]:
        parts.append(f"⏱ Timed out after {SHELL_TIMEOUT}s")
        return "\n".join(parts)

    stdout = result["stdout"].strip()
    stderr = result["stderr"].strip()

    if stdout:
        if len(stdout) > MAX_OUTPUT_LENGTH:
            stdout = stdout[:MAX_OUTPUT_LENGTH] + f"\n... (truncated, {len(result['stdout'])} total chars)"
        parts.append(stdout)

    if stderr:
        if len(stderr) > MAX_OUTPUT_LENGTH:
            stderr = stderr[:MAX_OUTPUT_LENGTH] + "\n... (truncated)"
        parts.append(f"stderr: {stderr}")

    if not stdout and not stderr:
        parts.append("(no output)")

    if result["returncode"] != 0:
        parts.append(f"Exit code: {result['returncode']}")
        # Add user-friendly hint for known error types
        stderr_lower = stderr.lower()
        for pattern, hint in _ERROR_HINTS.items():
            if pattern in stderr_lower:
                parts.append(f"\n💡 {hint}")
                break

    return "\n".join(parts)
