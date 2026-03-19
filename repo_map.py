"""
Repo map generator — produces a compact structural overview of a codebase.
Uses only stdlib. Deterministic (sorted). Depth- and count-limited.
"""

import os
from pathlib import Path
import fnmatch

_SKIP_DIRS = frozenset({
    ".git", ".github", ".claude",
    "node_modules", ".next", "__pycache__", ".pytest_cache",
    "dist", "build", "venv", ".venv", "htmlcov", ".eggs", "coverage",
    ".DS_Store",
})

_SKIP_SUFFIXES = frozenset({".egg-info"})


def _truncate_to_lines(text: str, max_chars: int) -> str:
    """Truncate text at a line boundary so no entry is cut mid-line."""
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    if cut == -1:
        cut = max_chars
    return text[:cut] + "\n... (truncated)"


def _should_skip_dir(name: str) -> bool:
    if name in _SKIP_DIRS:
        return True
    if any(name.endswith(suf) for suf in _SKIP_SUFFIXES):
        return True
    return False


def _tree(
    root: Path,
    max_depth: int,
    max_files_per_dir: int,
    current_depth: int = 0,
    prefix: str = "",
) -> list[str]:
    """Recursively build a tree listing. Returns lines."""
    if current_depth > max_depth:
        return []

    try:
        entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return []

    dirs = [e for e in entries if e.is_dir() and not _should_skip_dir(e.name)]
    # Skip dotfiles — hidden/config/env files add noise without navigation value.
    # The agent can still read specific dotfiles via read_file if needed.
    files = [e for e in entries if e.is_file() and not e.name.startswith(".")]

    lines: list[str] = []

    # Emit dirs first (they were already filtered above via sorted key)
    all_items = dirs + files
    file_count = 0
    truncated = False

    for i, entry in enumerate(all_items):
        if entry.is_file():
            file_count += 1
            if file_count > max_files_per_dir:
                truncated = True
                continue

        is_last = (i == len(all_items) - 1) and not truncated
        connector = "└── " if is_last else "├── "
        suffix = "/" if entry.is_dir() else ""
        lines.append(f"{prefix}{connector}{entry.name}{suffix}")

        if entry.is_dir() and current_depth < max_depth:
            extension = "    " if is_last else "│   "
            child_lines = _tree(
                entry,
                max_depth=max_depth,
                max_files_per_dir=max_files_per_dir,
                current_depth=current_depth + 1,
                prefix=prefix + extension,
            )
            lines.extend(child_lines)

    if truncated:
        remaining = len(files) - max_files_per_dir
        lines.append(f"{prefix}└── ... ({remaining} more files)")

    return lines


def _match_scope(workspace: Path, scope: str) -> list[Path]:
    """
    Return the set of top-level subtree roots that match a scope glob.
    e.g. "backend/**" -> workspace/backend
         "frontend/src/**" -> workspace/frontend/src
    """
    if not scope:
        return []

    # Strip trailing /**  or /* to get the directory root
    clean = scope.rstrip("/").rstrip("*").rstrip("/")
    if not clean:
        return [workspace]

    # Try direct match first
    candidate = workspace / clean
    if candidate.exists():
        return [candidate]

    # Try fnmatch against top-level dirs
    results = []
    try:
        for child in sorted(workspace.iterdir()):
            if fnmatch.fnmatch(child.name, clean) or fnmatch.fnmatch(
                str(child.relative_to(workspace)), scope.rstrip("*").rstrip("/")
            ):
                results.append(child)
    except PermissionError:
        pass

    return results  # empty list = no match; caller omits the scope section


def build_repo_map(
    workspace: str,
    scope: str | None = None,
    max_chars: int = 3000,
    scope_max_chars: int = 4000,
) -> str:
    """
    Build a repo map for the given workspace.

    Returns two sections:
    1. Root overview: shallow tree (depth 2, max 15 files/dir), capped at max_chars
    2. Scope subtree: deeper tree (depth 4) for each scope token, capped at scope_max_chars total

    Parameters
    ----------
    workspace : str
        Absolute path to the project root.
    scope : str | None
        Optional glob pattern like "backend/**" or comma-separated "backend/,api/" to zoom in.
    max_chars : int
        Character cap for the root overview section.
    scope_max_chars : int
        Total character cap across all scope subtree sections combined.
    """
    root = Path(workspace).resolve()
    if not root.is_dir():
        return f"(workspace not found: {workspace})"

    # ── Section 1: Root overview (depth 2) ──────────────────
    root_lines = _tree(root, max_depth=2, max_files_per_dir=15)
    root_section = f"{root.name}/\n" + "\n".join(root_lines)

    root_section = _truncate_to_lines(root_section, max_chars)

    parts = [
        "### Root Overview (depth 2)\n",
        root_section,
    ]

    # ── Section 2: Scope subtree (depth 4) ───────────────────
    if scope:
        # Handle comma-separated scopes like "backend/,api/"
        scope_tokens = [s.strip() for s in scope.split(",") if s.strip()]

        scope_parts: list[str] = []
        seen_roots: set[Path] = set()
        for token in scope_tokens:
            scope_roots = _match_scope(root, token)
            for sr in scope_roots:
                if not sr.is_dir() or sr in seen_roots:
                    continue
                seen_roots.add(sr)
                rel = sr.relative_to(root)
                scope_lines = _tree(sr, max_depth=4, max_files_per_dir=15)
                scope_parts.append(f"{rel}/\n" + "\n".join(scope_lines))

        if scope_parts:
            combined = _truncate_to_lines("\n\n".join(scope_parts), scope_max_chars)
            parts.append(f"\n\n### Scope Subtree: `{scope}` (depth 4)\n")
            parts.append(combined)

    return "".join(parts)


if __name__ == "__main__":
    import sys

    ws = sys.argv[1] if len(sys.argv) > 1 else "."
    sc = sys.argv[2] if len(sys.argv) > 2 else None
    print(build_repo_map(ws, sc))
