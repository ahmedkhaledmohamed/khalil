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
import shlex
import subprocess

from config import ActionType

log = logging.getLogger("khalil.actions.shell")

SHELL_TIMEOUT = 30
MAX_OUTPUT_LENGTH = 3000

# --- Path hallucination detection ---

# Regex to find absolute paths in commands (skip flags like -/--xxx)
_ABS_PATH_RE = re.compile(r'(?<!\w)(/(?:Users|home|tmp|var|opt|etc|usr|Applications|Volumes|Library|System)[/\w.\-~]+)')


def _check_paths_exist(cmd: str, cwd: str | None = None) -> str | None:
    """Check if absolute paths referenced in a command actually exist.

    Returns an error message if a hallucinated path is found, None if all paths exist
    or no absolute paths are referenced.
    """
    paths = _ABS_PATH_RE.findall(cmd)
    if not paths:
        return None

    missing = []
    for p in paths:
        # Expand ~ and resolve relative to cwd
        expanded = os.path.expanduser(p)
        if not os.path.exists(expanded):
            # Check parent dir — if the parent exists, the command might be creating the file
            parent = os.path.dirname(expanded)
            if os.path.isdir(parent):
                continue  # Parent exists, file might be intended to be created
            missing.append(p)

    if not missing:
        return None

    # Build helpful error with suggestions
    lines = [f"Path does not exist: {missing[0]}"]
    # Try fuzzy match: check siblings of the parent
    parent = os.path.dirname(missing[0])
    grandparent = os.path.dirname(parent)
    if os.path.isdir(grandparent):
        try:
            siblings = os.listdir(grandparent)
            base = os.path.basename(parent)
            close = [s for s in siblings if base.lower() in s.lower() or s.lower() in base.lower()]
            if close:
                suggestions = [os.path.join(grandparent, s) for s in close[:3]]
                lines.append(f"Did you mean: {', '.join(suggestions)}?")
        except OSError:
            pass

    return "\n".join(lines)

# --- Per-chat working directory tracking ---

_chat_cwd: dict[int, str] = {}


def get_chat_cwd(chat_id: int) -> str:
    """Get the tracked working directory for a chat, defaulting to ~."""
    return _chat_cwd.get(chat_id, os.path.expanduser("~"))


def set_chat_cwd(chat_id: int, path: str) -> bool:
    """Set the working directory for a chat. Returns True if path is valid."""
    resolved = os.path.expanduser(path)
    if not os.path.isabs(resolved):
        resolved = os.path.join(get_chat_cwd(chat_id), resolved)
    resolved = os.path.realpath(resolved)
    if os.path.isdir(resolved):
        _chat_cwd[chat_id] = resolved
        return True
    return False

SKILL = {
    "name": "shell",
    "description": "Execute shell commands with safety classification (safe/risky/blocked)",
    "category": "system",
    "patterns": [
        (r"\bopen\s+(?:the\s+)?(?:Safari|Chrome|Slack|Finder|Terminal|Music|Notes|Calendar|Spotify|Mail)\b", "shell"),
        (r"\bopen\s+https?://", "shell"),
        (r"\bcheck\s+(?:disk\s+)?(?:space|storage)\b", "shell"),
        (r"\brun\s+(?:the\s+)?command\b", "shell"),
        (r"\bbrew\s+(?:list|info|search|install|upgrade|uninstall|cleanup)\b", "shell"),
        (r"\blist\s+(?:my\s+)?brew\s+packages?\b", "shell"),
        (r"\b(?:arrange|tile|put)\s+windows?\s+(?:side\s+by\s+side|split)\b", "shell"),
        (r"\bresize\s+(?:the\s+)?window\b", "shell"),
        (r"\bminimize\s+(?:all\s+)?windows?\b", "shell"),
        (r"\b(?:check|test)\s+(?:my\s+)?(?:network|internet|connection)\b", "shell"),
        (r"\bping\s+\w+", "shell"),
        (r"\b(?:list|show|get)\s+(?:my\s+)?(?:login|startup)\s+items?\b", "shell"),
        (r"\b(?:disk\s+space|storage\s+usage)\b", "shell"),
        (r"\b(?:large|biggest)\s+files?\b", "shell"),
        (r"\bclean\s+cache[s]?\b", "shell"),
        (r"\b(?:play|resume)\s+music\b", "shell"),
        (r"\b(?:pause|stop)\s+music\b", "shell"),
        (r"\b(?:next|skip)\s+(?:song|track)\b", "shell"),
    ],
    "actions": [
        {"type": "shell", "handler": "handle_intent", "keywords": "run command shell open app brew install network disk space", "description": "Execute shell commands",
         "parameters": {
             "command": {"type": "string", "description": "Shell command to execute"},
         }},
    ],
    "examples": ["Open Safari", "Check disk space", "Run brew update"],
    "voice": {"confirm_before_execute": True, "response_style": "brief"},
}

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
    r"\bkillall\s+finder\b",
    r"\bkillall\s+dock\b",
    r"\bkillall\s+systemuiserver\b",
    # Git safety — never push directly to main or force push
    r"\bgit\s+push\b.*\bmain\b",
    r"\bgit\s+push\b.*\bmaster\b",
    r"\bgit\s+push\s+-f\b",
    r"\bgit\s+push\s+--force\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+branch\s+-D\b",      # force-delete branches
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
    # Text processing (read-only)
    "grep ",
    "grep -",
    "awk ",
    "sed -n ",      # read-only sed (print mode)
    "sed -n'",
    "sort ",
    "uniq ",
    "cut ",
    "tr ",
    "diff ",
    # Spotlight / metadata search
    "mdls ",
    "mdfind ",
    "mdutil -s",
    # Network diagnostics (read-only)
    "lsof ",
    "lsof -i",
    "netstat ",
    # Software updates (list only)
    "softwareupdate --list",
    "softwareupdate -l",
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
    "pbcopy",
    "osascript ",
    # Clipboard read
    "pbpaste",
    # Cursor IDE (read-only)
    "cursor --status",
    "cursor --list-extensions",
    "cursor --version",
    "cursor -g ",
    "cursor --goto ",
    "cursor --diff ",
]


def _split_chained_command(cmd: str) -> list[str] | None:
    """Split a command on &&, ||, ; while respecting quoted strings.

    Returns list of segments, or None if the command has no chaining operators.
    """
    # Quick check: if no chaining operators, skip the expensive split
    if not re.search(r"[;&]|&&|\|\|", cmd):
        return None

    # Split respecting quotes: tokenize, then re-join on operators
    segments = []
    current = []
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        # Malformed quotes — fall back to simple regex split
        parts = re.split(r"\s*(?:&&|\|\||;)\s*", cmd)
        return [p.strip() for p in parts if p.strip()] or None

    # Re-scan the raw string for operator positions (shlex eats them)
    # Use a simpler approach: split raw string on unquoted operators
    result = []
    buf = ""
    i = 0
    in_single = False
    in_double = False
    while i < len(cmd):
        c = cmd[i]
        if c == "'" and not in_double:
            in_single = not in_single
            buf += c
        elif c == '"' and not in_single:
            in_double = not in_double
            buf += c
        elif not in_single and not in_double:
            if cmd[i:i+2] == "&&":
                if buf.strip():
                    result.append(buf.strip())
                buf = ""
                i += 2
                continue
            elif cmd[i:i+2] == "||":
                if buf.strip():
                    result.append(buf.strip())
                buf = ""
                i += 2
                continue
            elif c == ";":
                if buf.strip():
                    result.append(buf.strip())
                buf = ""
            else:
                buf += c
        else:
            buf += c
        i += 1
    if buf.strip():
        result.append(buf.strip())

    return result if len(result) > 1 else None


def sanitize_command(cmd: str) -> tuple[str | None, str]:
    """Sanitize a shell command for injection patterns.

    Returns (sanitized_cmd, rejection_reason). If rejected, cmd is None.
    For chained commands (&&, ;, ||), each segment is classified independently.
    The chain is rejected only if any segment is DANGEROUS.
    """
    # Reject null bytes
    if "\x00" in cmd:
        return None, "Command contains null bytes"

    # Reject backtick/subshell injection
    if "`" in cmd or "$(" in cmd:
        return None, "Command contains subshell expansion (backticks or $(...)). Rejected for safety."

    # For chained commands, classify each segment independently
    segments = _split_chained_command(cmd)
    if segments:
        for seg in segments:
            if classify_command(seg) == ActionType.DANGEROUS:
                return None, f"Chained command contains a dangerous segment: {seg[:80]}"
        # All segments are safe or write — allow the full chain
        return cmd.strip(), ""

    return cmd.strip(), ""


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


def _extract_cd_target(cmd: str) -> str | None:
    """Extract the target directory from a cd command. Returns None if not a cd command."""
    m = re.match(r"^\s*cd\s+(.*)", cmd)
    if not m:
        return None
    target = m.group(1).strip()
    # Remove surrounding quotes
    if (target.startswith('"') and target.endswith('"')) or \
       (target.startswith("'") and target.endswith("'")):
        target = target[1:-1]
    return target or None


async def execute_shell(cmd: str, cwd: str = None, timeout: int = SHELL_TIMEOUT, chat_id: int = None) -> dict:
    """Execute a shell command in a subprocess.

    Args:
        cmd: Shell command to execute.
        cwd: Working directory override. If None and chat_id is provided, uses tracked cwd.
        timeout: Max execution time in seconds.
        chat_id: Chat ID for per-chat cwd tracking.

    Returns: {"returncode": int, "stdout": str, "stderr": str, "timed_out": bool}
    """
    # Sanitize command before execution
    sanitized, reason = sanitize_command(cmd)
    if sanitized is None:
        log.warning("Command rejected by sanitizer: %s — %s", cmd[:100], reason)
        return {
            "returncode": -2,
            "stdout": "",
            "stderr": f"Command rejected: {reason}",
            "timed_out": False,
        }
    cmd = sanitized

    # Check for hallucinated paths before execution
    path_error = _check_paths_exist(cmd)
    if path_error:
        log.warning("Path hallucination detected in command: %s — %s", cmd[:80], path_error)
        return {
            "returncode": -3,
            "stdout": "",
            "stderr": path_error,
            "timed_out": False,
        }

    # Resolve working directory
    if not cwd and chat_id is not None:
        cwd = get_chat_cwd(chat_id)
    if not cwd:
        cwd = os.path.expanduser("~")

    # Handle pure `cd` commands — update tracker, don't run subprocess
    cd_target = _extract_cd_target(cmd)
    if cd_target and not re.search(r"&&|\|\||;", cmd):
        # Pure cd command — just update the cwd tracker
        resolved = os.path.expanduser(cd_target)
        if not os.path.isabs(resolved):
            resolved = os.path.join(cwd, resolved)
        resolved = os.path.realpath(resolved)
        if os.path.isdir(resolved):
            if chat_id is not None:
                set_chat_cwd(chat_id, resolved)
            return {
                "returncode": 0,
                "stdout": f"Changed directory to {resolved}",
                "stderr": "",
                "timed_out": False,
            }
        else:
            return {
                "returncode": 1,
                "stdout": "",
                "stderr": f"cd: no such directory: {cd_target}",
                "timed_out": False,
            }

    # For chained commands starting with cd, extract cd and run the rest
    segments = _split_chained_command(cmd)
    if segments and _extract_cd_target(segments[0]):
        cd_seg = segments[0]
        cd_dir = _extract_cd_target(cd_seg)
        resolved = os.path.expanduser(cd_dir)
        if not os.path.isabs(resolved):
            resolved = os.path.join(cwd, resolved)
        resolved = os.path.realpath(resolved)
        if os.path.isdir(resolved):
            cwd = resolved
            if chat_id is not None:
                set_chat_cwd(chat_id, resolved)
            # Run the remaining segments with the new cwd
            remaining = cmd[cmd.index(segments[1]):]
            cmd = remaining
        else:
            # Reject the entire chain — running with wrong cwd is worse than failing
            return {
                "returncode": 1,
                "stdout": "",
                "stderr": f"cd: no such directory: {cd_dir}\nCommand aborted — target directory doesn't exist.",
                "timed_out": False,
            }

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


async def handle_intent(action: str, intent: dict, ctx) -> bool:
    """Handle shell intent from tool-use or direct dispatch."""
    cmd = intent.get("command", "")
    if not cmd:
        await ctx.reply("No command specified.")
        return True

    classification = classify_command(cmd)
    if classification == ActionType.DANGEROUS:
        await ctx.reply(f"Command blocked (dangerous): {cmd}")
        return True

    result = await execute_shell(cmd)
    output = format_output(result, cmd)
    await ctx.reply(output)
    return True
