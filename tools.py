"""
Tool definitions and executor for the AI task runner.
These tools are what Claude can use to interact with your codebase.
All file operations are sandboxed to the workspace directory.
"""

import os
import subprocess
from fnmatch import fnmatch
from pathlib import Path

# Files / directories the agent must never write to, even if they exist in the workspace.
_PROTECTED_EXACT: frozenset[str] = frozenset({"CLAUDE.md", "ARCHITECTURE.md"})
_PROTECTED_PATTERNS: tuple[str, ...] = (
    "test_*.py", "*_test.py",
    "*.test.ts", "*.test.tsx", "*.spec.ts", "*.spec.tsx",
    "*.test.js", "*.test.jsx", "*.spec.js", "*.spec.jsx",
)
_PROTECTED_DIRS: frozenset[str] = frozenset({"migrations", ".github"})


class ToolExecutor:
    """Executes tool calls from Claude, sandboxed to the workspace directory."""

    def __init__(self, workspace: str, timeout: int = 120) -> None:
        self.workspace = Path(workspace).resolve()
        self.timeout = timeout

    def _safe_path(self, rel_path: str) -> Path:
        """Resolve a relative path and block any traversal outside workspace.

        Uses a separator-aware prefix check so that /tmp/foo never
        accidentally matches /tmp/foobar.
        """
        full = (self.workspace / rel_path).resolve()
        ws = str(self.workspace)
        if full != self.workspace and not str(full).startswith(ws + os.sep):
            raise ValueError(f"Path traversal blocked: {rel_path}")
        return full

    def _is_protected(self, rel_path: str) -> bool:
        """Return True if this path must not be written or edited."""
        p = Path(rel_path)
        name = p.name
        if name in _PROTECTED_EXACT:
            return True
        if any(fnmatch(name, pat) for pat in _PROTECTED_PATTERNS):
            return True
        if any(part in _PROTECTED_DIRS for part in p.parts):
            return True
        return False

    # ── File operations ──────────────────────────────────────

    def read_file(self, path: str) -> str:
        p = self._safe_path(path)
        if not p.exists():
            return f"ERROR: File not found: {path}"
        size = p.stat().st_size
        if size > 100_000:
            return (
                f"ERROR: File too large ({size:,} bytes). "
                "Use search_codebase to find the specific section you need."
            )
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"ERROR reading {path}: {e}"

    def write_file(self, path: str, content: str) -> str:
        if self._is_protected(path):
            return f"PROTECTED: Writing to '{path}' is not allowed (test file, migration, CI config, or protected doc)."
        p = self._safe_path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"OK: Written {len(content)} chars to {path}"
        except Exception as e:
            return f"ERROR writing {path}: {e}"

    def edit_file(self, path: str, old_str: str, new_str: str) -> str:
        if self._is_protected(path):
            return f"PROTECTED: Editing '{path}' is not allowed (test file, migration, CI config, or protected doc)."
        p = self._safe_path(path)
        if not p.exists():
            return f"ERROR: File not found: {path}"
        content = p.read_text(encoding="utf-8", errors="replace")
        count = content.count(old_str)
        if count == 0:
            return f"ERROR: String not found in {path}. Read the file first to get the exact text."
        if count > 1:
            return f"ERROR: String found {count} times in {path}. Use a longer, more unique snippet."
        content = content.replace(old_str, new_str, 1)
        p.write_text(content, encoding="utf-8")
        return f"OK: Edited {path}"

    # ── Discovery operations ─────────────────────────────────

    def list_directory(self, path: str = ".", max_depth: int = 2) -> str:
        p = self._safe_path(path)
        if not p.is_dir():
            return f"ERROR: Not a directory: {path}"
        skip = {".git", "node_modules", "__pycache__", ".next", "data", "htmlcov", ".pytest_cache"}
        lines = []
        for item in sorted(p.rglob("*")):
            rel = item.relative_to(p)
            if len(rel.parts) > max_depth:
                continue
            if any(part in skip for part in rel.parts):
                continue
            indent = "  " * (len(rel.parts) - 1)
            suffix = "/" if item.is_dir() else ""
            lines.append(f"{indent}{rel.parts[-1]}{suffix}")
            if len(lines) >= 200:
                lines.append("...(truncated at 200 entries)")
                break
        return "\n".join(lines) if lines else "(empty directory)"

    def search_codebase(self, pattern: str, path: str = ".", file_pattern: str = "") -> str:
        p = self._safe_path(path)
        cmd = ["grep", "-rn", "--color=never"]
        if file_pattern:
            cmd.append(f"--include={file_pattern}")
        else:
            # Default: search common source files only
            for ext in ("*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.sql", "*.md", "*.yaml", "*.yml"):
                cmd.append(f"--include={ext}")
        # Always exclude heavy directories
        for excl in ("node_modules", ".git", "__pycache__", "data", ".next", "htmlcov"):
            cmd.append(f"--exclude-dir={excl}")
        cmd.extend([pattern, str(p)])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = result.stdout.strip()
            if not output:
                return "No results found."
            lines = output.split("\n")
            if len(lines) > 50:
                return "\n".join(lines[:50]) + f"\n\n... ({len(lines) - 50} more results, narrow your search)"
            return output
        except subprocess.TimeoutExpired:
            return "ERROR: Search timed out after 30s. Use a narrower path or pattern."

    # ── Command execution ────────────────────────────────────

    def run_command(self, command: str) -> str:
        """Run a shell command in the workspace. Blocks destructive operations."""
        blocked = [
            "rm -rf", "git push", "git checkout main", "git checkout master",
            "git merge", "git rebase", "drop table", "truncate", "DELETE FROM",
            "rm -r /", "sudo",
        ]
        cmd_lower = command.lower()
        for b in blocked:
            if b.lower() in cmd_lower:
                return f"BLOCKED: '{b}' is not allowed. This runner never pushes, merges, or deletes."
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(self.workspace),
                timeout=self.timeout,
                env={**os.environ, "FORCE_COLOR": "0"},  # clean output
            )
            output = (result.stdout + result.stderr).strip()
            if len(output) > 10_000:
                output = output[:5000] + "\n\n...(truncated)...\n\n" + output[-2000:]
            return f"Exit code: {result.returncode}\n{output}"
        except subprocess.TimeoutExpired:
            return f"ERROR: Command timed out after {self.timeout}s"


# ── Tool schema for Claude API ───────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "Read a file's contents. Path is relative to workspace root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path, e.g. 'backend/pipeline/lib/uk_ingestor.py'",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file. For targeted changes to existing files, prefer edit_file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path"},
                "content": {"type": "string", "description": "Full file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace a unique string in a file. old_str must appear exactly once. "
            "Always read_file first to get the exact text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path"},
                "old_str": {"type": "string", "description": "Exact text to find (must be unique)"},
                "new_str": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files/dirs up to max_depth. Skips node_modules, .git, data/, __pycache__.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative dir, e.g. 'src/'. Omit to list workspace root."},
                "max_depth": {"type": "integer", "description": "Depth limit (1–4). Omit for default of 2."},
            },
            "required": [],
        },
    },
    {
        "name": "search_codebase",
        "description": "Grep the codebase for a pattern. Returns file paths, line numbers, and matching lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern (regex supported)"},
                "path": {"type": "string", "description": "Subdirectory to search. Omit to search entire workspace."},
                "file_pattern": {
                    "type": "string",
                    "description": "Glob filter, e.g. '*.py'. Omit to search all source files.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command in the workspace. Use for tests, linting, type checking. "
            "Destructive commands (rm -rf, git push/merge, SQL drops) are blocked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "task_complete",
        "description": "Signal that you have finished the task. Always call this when done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "What you changed and why. Written for a human reviewer.",
                },
                "files_changed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of modified/created file paths",
                },
                "tests_passed": {
                    "type": "boolean",
                    "description": "Did the relevant tests pass after your changes?",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "Your confidence the changes are correct and complete",
                },
            },
            "required": ["summary", "files_changed", "tests_passed", "confidence"],
        },
    },
]
