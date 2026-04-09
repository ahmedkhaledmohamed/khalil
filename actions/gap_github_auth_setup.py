"""Automated GitHub authentication setup — store, verify, and manage GitHub PATs.

Manages Personal Access Tokens for github.com and GHE (ghe.spotify.net) via
the system keyring. Verifies tokens against the GitHub API and logs actions.

Token type: GitHub Personal Access Token (classic or fine-grained)
  - Classic: Settings > Developer settings > Personal access tokens > Tokens (classic)
  - Fine-grained: Settings > Developer settings > Personal access tokens > Fine-grained
  Minimum scopes: (none — /user works with any valid token)
  Recommended scopes: repo, read:org

Setup: /ghauth setup <token> (github.com) or /ghauth setup_ghe <token> (GHE)
"""
import asyncio
import logging
import re
import sqlite3

import httpx
import keyring

from config import DB_PATH, KEYRING_SERVICE, TIMEZONE

log = logging.getLogger("khalil.actions.gap_github_auth_setup")

_KEY_GITHUB = "github-token"
_KEY_GHE = "github-enterprise-token"
_API_GITHUB = "https://api.github.com"
_API_GHE = "https://ghe.spotify.net/api/v3"
_INSTANCES = [("github.com", "github"), ("GHE (ghe.spotify.net)", "ghe")]
_tables_ensured = False


def ensure_tables(conn: sqlite3.Connection):
    """Create tables. Called once at startup."""
    global _tables_ensured
    conn.execute("""CREATE TABLE IF NOT EXISTS github_auth_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance TEXT NOT NULL, action TEXT NOT NULL, username TEXT,
        status TEXT NOT NULL, detail TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.commit()
    _tables_ensured = True


def _ensure_tables_lazy():
    global _tables_ensured
    if _tables_ensured:
        return
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_tables(conn)
    finally:
        conn.close()


def _log_action(instance: str, action: str, username: str | None,
                status: str, detail: str | None = None):
    _ensure_tables_lazy()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO github_auth_log (instance, action, username, status, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (instance, action, username, status, detail),
        )
        conn.commit()
    finally:
        conn.close()


# --- Core sync functions (called via asyncio.to_thread) ---

def _key_for(instance: str) -> str:
    return _KEY_GHE if instance == "ghe" else _KEY_GITHUB

def _get_token(instance: str) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, _key_for(instance))

def _store_token(instance: str, token: str):
    keyring.set_password(KEYRING_SERVICE, _key_for(instance), token)

def _delete_token(instance: str):
    keyring.delete_password(KEYRING_SERVICE, _key_for(instance))

def _verify_token_sync(instance: str) -> dict:
    """Verify a token by calling GET /user. Returns user info or error."""
    token = _get_token(instance)
    if not token:
        return {"ok": False, "error": "No token stored"}
    base_url = _API_GHE if instance == "ghe" else _API_GITHUB
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    with httpx.Client(timeout=15) as client:
        resp = client.get(f"{base_url}/user", headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        return {"ok": True, "username": data.get("login", "unknown"),
                "name": data.get("name", ""), "scopes": resp.headers.get("x-oauth-scopes", "")}
    if resp.status_code == 401:
        return {"ok": False, "error": "Token is invalid or expired (401)"}
    return {"ok": False, "error": f"API returned {resp.status_code}"}

def _get_auth_history(instance: str | None = None, limit: int = 10) -> list[dict]:
    _ensure_tables_lazy()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        if instance:
            rows = conn.execute(
                "SELECT * FROM github_auth_log WHERE instance = ? ORDER BY created_at DESC LIMIT ?",
                (instance, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM github_auth_log ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Async wrappers ---

async def _verify_token(instance: str) -> dict:
    return await asyncio.to_thread(_verify_token_sync, instance)

async def _async_get_token(instance: str) -> str | None:
    return await asyncio.to_thread(_get_token, instance)

def _mask(token: str) -> str:
    return token[:4] + "..." + token[-4:] if len(token) > 8 else "****"


# --- Command handler ---

async def handle_ghauth(update, context):
    """Handle /ghauth command."""
    args = context.args or []
    subcmd = args[0].lower() if args else "status"
    dispatch = {
        "status": lambda: _cmd_status(update),
        "setup": lambda: _cmd_setup(update, args[1:], "github"),
        "setup_ghe": lambda: _cmd_setup(update, args[1:], "ghe"),
        "verify": lambda: _cmd_verify(update),
        "history": lambda: _cmd_history(update),
        "remove": lambda: _cmd_remove(update, len(args) > 1 and args[1].lower() == "confirm"),
    }
    if subcmd in dispatch:
        await dispatch[subcmd]()
    else:
        await update.message.reply_text(
            "Usage:\n  /ghauth status — check configured tokens\n"
            "  /ghauth setup <token> — store github.com PAT\n"
            "  /ghauth setup_ghe <token> — store GHE PAT\n"
            "  /ghauth verify — verify stored tokens\n"
            "  /ghauth history — recent auth actions\n"
            "  /ghauth remove — preview removal\n"
            "  /ghauth remove confirm — delete tokens")


async def _cmd_status(update):
    lines = ["**GitHub Auth Status**\n"]
    for label, instance in _INSTANCES:
        token = await _async_get_token(instance)
        if not token:
            lines.append(f"❌ {label}: no token configured")
            continue
        result = await _verify_token(instance)
        if result["ok"]:
            lines.append(f"✅ {label}: **{result['username']}**\n"
                         f"   Token: `{_mask(token)}` | Scopes: {result['scopes'] or '(none)'}")
        else:
            lines.append(f"⚠️ {label}: token stored but invalid\n"
                         f"   Token: `{_mask(token)}` | Error: {result['error']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _cmd_setup(update, args: list[str], instance: str):
    label = "GHE" if instance == "ghe" else "github.com"
    if not args:
        cmd = "setup_ghe" if instance == "ghe" else "setup"
        url = "https://ghe.spotify.net/settings/tokens" if instance == "ghe" else "https://github.com/settings/tokens"
        await update.message.reply_text(
            f"Provide your {label} PAT:\n  /ghauth {cmd} ghp_xxxx\n\nGenerate at:\n  {url}")
        return
    token = args[0].strip()
    if not re.match(r'^(ghp_[A-Za-z0-9]{36,}|gho_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|[a-f0-9]{40})$', token):
        await update.message.reply_text(
            "Invalid token format. Expected: ghp_..., github_pat_..., or 40-char hex.")
        return
    await asyncio.to_thread(_store_token, instance, token)
    result = await _verify_token(instance)
    if result["ok"]:
        _log_action(instance, "setup", result["username"], "success")
        await update.message.reply_text(
            f"✅ {label} token stored and verified!\n\n"
            f"User: **{result['username']}** | Token: `{_mask(token)}`\n"
            f"Scopes: {result['scopes'] or '(none)'}", parse_mode="Markdown")
    else:
        _log_action(instance, "setup", None, "stored_unverified", result["error"])
        await update.message.reply_text(
            f"⚠️ Token stored but verification failed: {result['error']}\n"
            "Check the token and try /ghauth verify later.")
    await update.message.reply_text(
        "🔒 **Security tip**: delete your message containing the token."
        " It's now stored securely in the system keyring.", parse_mode="Markdown")


async def _cmd_verify(update):
    lines = ["**Token Verification**\n"]
    any_found = False
    for label, instance in _INSTANCES:
        token = await _async_get_token(instance)
        if not token:
            continue
        any_found = True
        result = await _verify_token(instance)
        if result["ok"]:
            _log_action(instance, "verify", result["username"], "success")
            lines.append(f"✅ {label}: valid — **{result['username']}**")
        else:
            _log_action(instance, "verify", None, "failed", result["error"])
            lines.append(f"❌ {label}: {result['error']}")
    if not any_found:
        lines.append("No tokens configured. Use /ghauth setup <token> to add one.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _cmd_history(update):
    history = await asyncio.to_thread(_get_auth_history, None, 15)
    if not history:
        await update.message.reply_text("No auth history recorded yet.")
        return
    lines = ["**Recent Auth Actions**\n"]
    for e in history:
        ts = e["created_at"][:16] if e.get("created_at") else "?"
        lines.append(f"  `{ts}` {e['instance']} | {e['action']} | {e['status']} | {e.get('username') or '-'}")
    text = "\n".join(lines)
    if len(text) > 4096:
        text = text[:4090] + "\n..."
    await update.message.reply_text(text, parse_mode="Markdown")


async def _cmd_remove(update, confirm: bool = False):
    found = []
    for label, instance in _INSTANCES:
        if await _async_get_token(instance):
            found.append((label, instance))
    if not found:
        await update.message.reply_text("No tokens stored — nothing to remove.")
        return
    if not confirm:
        labels = ", ".join(l for l, _ in found)
        await update.message.reply_text(
            f"**Dry run** — would remove tokens for: {labels}\n\n"
            "To proceed: /ghauth remove confirm", parse_mode="Markdown")
        return
    removed = []
    for label, instance in found:
        try:
            await asyncio.to_thread(_delete_token, instance)
            _log_action(instance, "remove", None, "success")
            removed.append(label)
        except Exception as exc:
            _log_action(instance, "remove", None, "failed", str(exc))
            log.warning("Failed to remove %s token: %s", instance, exc)
    if removed:
        await update.message.reply_text(f"🗑 Removed tokens for: {', '.join(removed)}")
    else:
        await update.message.reply_text("Failed to remove tokens. Check logs.")
