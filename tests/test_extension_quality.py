"""Tests for improved gap detection (semantic gate) and smoke test."""

import asyncio
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from actions.extend import detect_capability_gap, GAP_GATE_PATTERNS, smoke_test_module
from actions.claude_code import (
    WorktreeValidationError,
    cleanup_worktree,
    create_worktree,
    run_codex,
    resolve_worktree_path,
    validate_worktree_changes,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "origin.git"
    repo = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Khalil Test")
    _git(repo, "config", "user.email", "khalil@example.com")
    (repo / "README.md").write_text("main\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return repo, remote


class TestGeneratedWorktreeIsolation:
    def test_remote_main_isolated_from_ambient_head_and_dirty_files(self, tmp_path):
        repo, _ = _repo_with_remote(tmp_path)
        _git(repo, "checkout", "-b", "ambient-feature")
        (repo / "ambient.txt").write_text("unrelated commit\n")
        _git(repo, "add", "ambient.txt")
        _git(repo, "commit", "-m", "ambient work")
        (repo / "dirty.txt").write_text("uncommitted user work\n")

        worktrees_dir = tmp_path / "worktrees"
        worktree = create_worktree(
            "generated/test",
            repo_dir=repo,
            worktrees_dir=worktrees_dir,
        )
        try:
            assert _git(worktree, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")
            assert not (worktree / "ambient.txt").exists()
            assert not (worktree / "dirty.txt").exists()
            assert _git(repo, "branch", "--show-current") == "ambient-feature"
            assert "dirty.txt" in _git(repo, "status", "--short")
        finally:
            cleanup_worktree(
                "generated/test",
                repo_dir=repo,
                worktrees_dir=worktrees_dir,
            )

    def test_rejects_changes_outside_expected_file_set(self, tmp_path):
        repo, _ = _repo_with_remote(tmp_path)
        worktrees_dir = tmp_path / "worktrees"
        worktree = create_worktree(
            "generated/test",
            repo_dir=repo,
            worktrees_dir=worktrees_dir,
        )
        try:
            action_file = worktree / "actions" / "demo.py"
            action_file.parent.mkdir()
            action_file.write_text("async def handle():\n    pass\n")
            (worktree / "UNRELATED.md").write_text("unexpected\n")

            with pytest.raises(WorktreeValidationError, match="UNRELATED.md"):
                validate_worktree_changes(worktree, {"actions/demo.py"})

            (worktree / "UNRELATED.md").unlink()
            assert validate_worktree_changes(
                worktree,
                {"actions/demo.py"},
            ) == ["actions/demo.py"]
        finally:
            cleanup_worktree(
                "generated/test",
                repo_dir=repo,
                worktrees_dir=worktrees_dir,
            )

    def test_rejects_target_path_outside_worktree(self, tmp_path):
        with pytest.raises(ValueError, match="inside the worktree"):
            resolve_worktree_path(tmp_path, "../outside.py")

    def test_simple_extension_pr_does_not_mutate_source_worktree(self, tmp_path, monkeypatch):
        repo, _ = _repo_with_remote(tmp_path)
        _git(repo, "checkout", "-b", "ambient-feature")
        (repo / "dirty.txt").write_text("uncommitted user work\n")
        worktrees_dir = tmp_path / "worktrees"

        monkeypatch.setattr("actions.claude_code.KHALIL_DIR", repo)
        monkeypatch.setattr("actions.claude_code.WORKTREES_DIR", worktrees_dir)

        def fake_gh(*args, **kwargs):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="https://github.com/example/khalil/pull/1\n",
                stderr="",
            )

        monkeypatch.setattr("actions.extend._run_gh", fake_gh)

        from actions.extend import create_extension_pr

        result = asyncio.run(create_extension_pr(
            "demo",
            "async def handle():\n    pass\n",
            {"name": "demo", "command": "demo", "description": "Demo"},
        ))

        assert result.endswith("/pull/1")
        assert _git(repo, "branch", "--show-current") == "ambient-feature"
        assert "dirty.txt" in _git(repo, "status", "--short")
        assert not (repo / "actions" / "demo.py").exists()
        assert _git(
            repo,
            "diff",
            "--name-only",
            "origin/main...origin/khalil-extend/demo",
        ).splitlines() == ["actions/demo.py", "extensions/demo.json"]

    def test_healing_pr_does_not_mutate_source_worktree(self, tmp_path, monkeypatch):
        repo, _ = _repo_with_remote(tmp_path)
        _git(repo, "checkout", "-b", "ambient-feature")
        (repo / "dirty.txt").write_text("uncommitted user work\n")
        worktrees_dir = tmp_path / "worktrees"

        monkeypatch.setattr("actions.claude_code.KHALIL_DIR", repo)
        monkeypatch.setattr("actions.claude_code.WORKTREES_DIR", worktrees_dir)

        def fake_gh(*args, **kwargs):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="https://github.com/example/khalil/pull/2\n",
                stderr="",
            )

        monkeypatch.setattr("actions.extend._run_gh", fake_gh)

        from healing import create_healing_pr

        result = asyncio.run(create_healing_pr(
            "README.md",
            "healed\n",
            {
                "fingerprint": "failure:shell",
                "summary": "repair shell routing",
                "failure_count": 3,
                "sample_queries": ["open localhost"],
                "source_context": [{"file": "README.md"}],
            },
        ))

        assert result.endswith("/pull/2")
        assert _git(repo, "branch", "--show-current") == "ambient-feature"
        assert "dirty.txt" in _git(repo, "status", "--short")
        assert (repo / "README.md").read_text() == "main\n"
        assert _git(
            repo,
            "diff",
            "--name-only",
            "origin/main...origin/khalil-heal/failure-shell",
        ).splitlines() == ["README.md"]


class TestCodexCodingAgent:
    def test_runs_sdk_in_explicit_worktree(self, tmp_path, monkeypatch):
        calls = {}

        class FakeThread:
            async def run(self, prompt, **options):
                calls["run"] = (prompt, options)
                return types.SimpleNamespace(
                    error=None,
                    final_response="Implemented the requested change.",
                )

        class FakeCodex:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def thread_start(self, **options):
                calls["thread_start"] = options
                return FakeThread()

        fake_sdk = types.SimpleNamespace(
            AsyncCodex=FakeCodex,
            ApprovalMode=types.SimpleNamespace(auto_review="auto-review"),
            Sandbox=types.SimpleNamespace(workspace_write="workspace-write"),
        )
        monkeypatch.setitem(sys.modules, "openai_codex", fake_sdk)
        monkeypatch.setattr("actions.claude_code.CODEX_MODEL", "")
        monkeypatch.setattr("actions.claude_code.CODEX_REASONING_EFFORT", "high")

        success, output = asyncio.run(run_codex("Fix the bug", tmp_path))

        assert success is True
        assert output == "Implemented the requested change."
        assert calls["thread_start"] == {
            "cwd": str(tmp_path.resolve()),
            "sandbox": "workspace-write",
            "approval_mode": "auto-review",
        }
        assert calls["run"] == (
            "Fix the bug",
            {
                "cwd": str(tmp_path.resolve()),
                "sandbox": "workspace-write",
                "effort": "high",
            },
        )

    def test_does_not_fall_back_from_codex_to_claude(self, tmp_path, monkeypatch):
        import actions.claude_code as coding_runtime

        async def failed_codex(*args, **kwargs):
            return False, "Codex auth failed"

        async def unexpected_claude(*args, **kwargs):
            raise AssertionError("Claude fallback must be explicit")

        monkeypatch.setattr(coding_runtime, "CODING_AGENT_BACKEND", "codex")
        monkeypatch.setattr(coding_runtime, "run_codex", failed_codex)
        monkeypatch.setattr(coding_runtime, "run_claude_code", unexpected_claude)

        success, output = asyncio.run(
            coding_runtime.run_coding_agent("Fix the bug", tmp_path)
        )

        assert success is False
        assert output == "Codex auth failed"


class TestSemanticGate:
    """The semantic gate should catch all known refusal patterns plus novel variants."""

    @pytest.mark.parametrize("response", [
        # Original phrase list entries
        "Sorry, I can't do that right now.",
        "I don't have the ability to read Slack.",
        "That capability isn't available yet.",
        "I can't currently access your device.",
        "Not something I can do yet.",
        "I don't have a feature for that.",
        "I don't have that capability.",
        "That's not something I support.",
        "I'm not able to check that.",
        "No built-in support for Jira integration.",
        "I would need direct access to your device.",
        "I don't have real-time monitoring.",
        "I can't determine the exact number.",
        "I don't have access to your Slack.",
        "I can't access your calendar directly.",
        "I'm unable to perform that action.",
        "That's beyond my current capabilities.",
        "Please check your Mac manually.",
        # NOVEL patterns the old phrase list would miss
        "I cannot read your Slack messages directly.",
        "I won't be able to do that without an API key.",
        "I couldn't access that service.",
        "Unfortunately, I do not have the ability to track Jira issues.",
        "This is not possible with my current setup.",
    ])
    def test_catches_refusals(self, response):
        assert detect_capability_gap(response) is True, f"Should detect gap in: {response!r}"

    @pytest.mark.parametrize("response", [
        "Here are your emails from last week.",
        "The meeting is at 3pm tomorrow.",
        "Your portfolio is up 3% this month.",
        "I found 5 matching documents.",
        "Reminder created for tomorrow at 9am.",
        "",
        "Sure, I can help with that!",
        "The weather today is sunny.",
        "Done. File has been saved.",
    ])
    def test_no_false_positives(self, response):
        assert detect_capability_gap(response) is False, f"False positive on: {response!r}"


class TestSmokeTest:
    def test_valid_module_passes(self, tmp_path):
        module = tmp_path / "good_module.py"
        module.write_text(textwrap.dedent("""\
            async def cmd_test(update, context):
                pass
        """))
        ok, err = smoke_test_module(module, "test")
        assert ok, f"Should pass but got: {err}"

    def test_missing_handler_fails(self, tmp_path):
        module = tmp_path / "bad_handler.py"
        module.write_text(textwrap.dedent("""\
            async def cmd_wrong_name(update, context):
                pass
        """))
        ok, err = smoke_test_module(module, "test")
        assert not ok
        assert "Missing handler" in err

    def test_import_error_fails(self, tmp_path):
        module = tmp_path / "bad_import.py"
        module.write_text(textwrap.dedent("""\
            import nonexistent_module_xyz
            async def cmd_test(update, context):
                pass
        """))
        ok, err = smoke_test_module(module, "test")
        assert not ok
        assert "ModuleNotFoundError" in err or "No module named" in err

    def test_syntax_error_fails(self, tmp_path):
        module = tmp_path / "bad_syntax.py"
        module.write_text("def broken(\n")
        ok, err = smoke_test_module(module, "test")
        assert not ok
