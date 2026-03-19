"""
Unit tests for tools.py

Covers:
  - Path traversal check (including the /tmp/foo vs /tmp/foobar prefix bug)
  - Protected-file enforcement for write_file and edit_file
  - Command blocklist in run_command
  - Normal happy-path for read/write/edit/list/search/run
"""

import sys
from pathlib import Path

import pytest

# Allow importing from the parent directory without installing a package.
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import ToolExecutor


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def ws(tmp_path: Path) -> ToolExecutor:
    """A ToolExecutor whose workspace is an isolated tmp directory."""
    return ToolExecutor(str(tmp_path))


# ── _safe_path / path traversal ──────────────────────────────


def test_safe_path_normal(ws: ToolExecutor, tmp_path: Path) -> None:
    p = ws._safe_path("src/foo.py")
    assert str(p).startswith(str(tmp_path))


def test_safe_path_workspace_itself(ws: ToolExecutor, tmp_path: Path) -> None:
    # Resolving "." should return the workspace root without error.
    assert ws._safe_path(".") == tmp_path.resolve()


def test_safe_path_dotdot_blocked(ws: ToolExecutor) -> None:
    with pytest.raises(ValueError, match="Path traversal blocked"):
        ws._safe_path("../escape.py")


def test_safe_path_deep_dotdot_blocked(ws: ToolExecutor) -> None:
    with pytest.raises(ValueError, match="Path traversal blocked"):
        ws._safe_path("a/b/../../../../../../etc/passwd")


def test_safe_path_sibling_dir_prefix_not_confused(tmp_path: Path) -> None:
    """
    Regression: /tmp/pytest-abc should NOT match as a subpath of /tmp/pytest-a
    even though the string 'pytest-a' is a prefix of 'pytest-abc'.
    """
    workspace = tmp_path / "myproject"
    workspace.mkdir()
    sibling = tmp_path / "myprojectevil"
    sibling.mkdir()

    ex = ToolExecutor(str(workspace))
    # A path that would resolve to the sibling should be blocked.
    with pytest.raises(ValueError, match="Path traversal blocked"):
        ex._safe_path("../myprojectevil/secret.py")


# ── _is_protected ────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "CLAUDE.md",
    "ARCHITECTURE.md",
    "test_models.py",
    "models_test.py",
    "App.test.ts",
    "App.spec.tsx",
    "Component.test.js",
    "Component.spec.jsx",
    "migrations/0001_initial.py",
    "backend/migrations/add_column.sql",
    ".github/workflows/ci.yml",
])
def test_is_protected_true(ws: ToolExecutor, path: str) -> None:
    assert ws._is_protected(path) is True


@pytest.mark.parametrize("path", [
    "src/models.py",
    "app.ts",
    "Component.tsx",
    "README.md",
    "backend/pipeline/lib/ratios.py",
])
def test_is_protected_false(ws: ToolExecutor, path: str) -> None:
    assert ws._is_protected(path) is False


# ── read_file ────────────────────────────────────────────────


def test_read_file_ok(ws: ToolExecutor, tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('hello')", encoding="utf-8")
    assert ws.read_file("hello.py") == "print('hello')"


def test_read_file_not_found(ws: ToolExecutor) -> None:
    result = ws.read_file("nope.py")
    assert result.startswith("ERROR: File not found")


def test_read_file_too_large(ws: ToolExecutor, tmp_path: Path) -> None:
    (tmp_path / "big.bin").write_bytes(b"x" * 100_001)
    result = ws.read_file("big.bin")
    assert result.startswith("ERROR: File too large")


def test_read_file_exactly_at_limit(ws: ToolExecutor, tmp_path: Path) -> None:
    # Exactly 100 000 bytes should be allowed.
    (tmp_path / "ok.bin").write_bytes(b"x" * 100_000)
    result = ws.read_file("ok.bin")
    assert "ERROR" not in result


# ── write_file ───────────────────────────────────────────────


def test_write_file_ok(ws: ToolExecutor, tmp_path: Path) -> None:
    result = ws.write_file("new_module.py", "x = 1")
    assert result.startswith("OK:")
    assert (tmp_path / "new_module.py").read_text() == "x = 1"


def test_write_file_creates_parents(ws: ToolExecutor, tmp_path: Path) -> None:
    result = ws.write_file("deep/nested/file.py", "pass")
    assert result.startswith("OK:")
    assert (tmp_path / "deep/nested/file.py").exists()


@pytest.mark.parametrize("protected_path", [
    "test_foo.py",
    "CLAUDE.md",
    "ARCHITECTURE.md",
    "migrations/0001_add_table.py",
    ".github/workflows/ci.yml",
])
def test_write_file_protected_blocked(ws: ToolExecutor, protected_path: str) -> None:
    result = ws.write_file(protected_path, "content")
    assert result.startswith("PROTECTED:")


# ── edit_file ────────────────────────────────────────────────


def test_edit_file_ok(ws: ToolExecutor, tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("hello world", encoding="utf-8")
    result = ws.edit_file("app.py", "world", "there")
    assert result.startswith("OK:")
    assert (tmp_path / "app.py").read_text() == "hello there"


def test_edit_file_not_found(ws: ToolExecutor) -> None:
    result = ws.edit_file("missing.py", "old", "new")
    assert result.startswith("ERROR: File not found")


def test_edit_file_string_not_found(ws: ToolExecutor, tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("abc", encoding="utf-8")
    result = ws.edit_file("mod.py", "xyz", "abc")
    assert "not found" in result.lower()


def test_edit_file_not_unique(ws: ToolExecutor, tmp_path: Path) -> None:
    (tmp_path / "dup.py").write_text("foo foo foo", encoding="utf-8")
    result = ws.edit_file("dup.py", "foo", "bar")
    assert "3 times" in result


def test_edit_file_protected_blocked(ws: ToolExecutor, tmp_path: Path) -> None:
    (tmp_path / "test_models.py").write_text("pass", encoding="utf-8")
    result = ws.edit_file("test_models.py", "pass", "fail")
    assert result.startswith("PROTECTED:")


# ── list_directory ───────────────────────────────────────────


def test_list_directory_ok(ws: ToolExecutor, tmp_path: Path) -> None:
    (tmp_path / "a.py").touch()
    (tmp_path / "b.py").touch()
    result = ws.list_directory(".")
    assert "a.py" in result
    assert "b.py" in result


def test_list_directory_not_a_dir(ws: ToolExecutor, tmp_path: Path) -> None:
    (tmp_path / "file.py").touch()
    result = ws.list_directory("file.py")
    assert result.startswith("ERROR: Not a directory")


def test_list_directory_skips_gitdir(ws: ToolExecutor, tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").touch()
    result = ws.list_directory(".")
    assert ".git" not in result


# ── run_command ──────────────────────────────────────────────


def test_run_command_ok(ws: ToolExecutor) -> None:
    result = ws.run_command("echo hello")
    assert "hello" in result
    assert "Exit code: 0" in result


def test_run_command_nonzero_exit(ws: ToolExecutor) -> None:
    result = ws.run_command("exit 1")
    assert "Exit code: 1" in result


@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "rm -rf .",
    "git push origin main",
    "git checkout main",
    "git checkout master",
    "git merge feature",
    "git rebase main",
    "DROP TABLE users",
    "drop table sessions",
    "DELETE FROM logs",
    "sudo apt-get install evil",
    "truncate table foo",
])
def test_run_command_blocklist(ws: ToolExecutor, cmd: str) -> None:
    result = ws.run_command(cmd)
    assert result.startswith("BLOCKED:"), f"Expected BLOCKED for: {cmd!r}"


def test_run_command_timeout(tmp_path: Path) -> None:
    short_timeout = ToolExecutor(str(tmp_path), timeout=1)
    result = short_timeout.run_command("sleep 10")
    assert "timed out" in result.lower()
