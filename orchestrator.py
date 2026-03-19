"""
Core orchestrator — runs LLM agents in an agentic loop against your codebase.

v2 changes (from Codex review):
  - Git worktrees instead of branch switching (parallel-safe, no stashing)
  - Multi-model support (Claude, Groq, Codex) via providers.py
  - Golden ratio validation gates that run before commit
  - Task templates for common Perpetua operations
"""

import json
import shutil
import subprocess
import sys
import yaml
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from tools import TOOL_DEFINITIONS, ToolExecutor
from providers import create_provider, ProviderError
from gates import GateRunner, GateResult
from repo_map import build_repo_map


@dataclass
class TaskMetrics:
    total_turns: int = 0
    tool_sequence: list[str] = field(default_factory=list)
    explore_calls: int = 0
    explore_before_first_edit: int = 0
    first_read_turn: int | None = None
    first_edit_turn: int | None = None
    first_run_turn: int | None = None
    task_complete_reached: bool = False
    token_usage: int = 0

    # Internal tracking
    _first_edit_done: bool = field(default=False, repr=False)

    _ABBREV: dict = field(default_factory=lambda: {
        "list_directory": "ld",
        "read_file": "rf",
        "search_codebase": "sc",
        "edit_file": "ef",
        "write_file": "wf",
        "run_command": "rc",
        "task_complete": "tc",
    }, repr=False)

    def compact_sequence(self) -> str:
        return ", ".join(self._ABBREV.get(t, t) for t in self.tool_sequence)


class TaskOrchestrator:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self.workspace = Path(self.config["workspace"]).resolve()
        if not self.workspace.is_dir():
            raise FileNotFoundError(f"Workspace not found: {self.workspace}")

        # Worktree directory — sibling to workspace, not inside it
        self.worktree_root = Path(self.config.get(
            "worktree_root",
            str(self.workspace.parent / ".mimi-worktrees")
        ))
        self.worktree_root.mkdir(parents=True, exist_ok=True)

        # Default provider (can be overridden per-task)
        self.default_provider = self.config.get("provider", "claude")
        self.default_model = self.config.get("model", None)  # None = provider default

        self.max_turns = self.config.get("max_turns", 30)
        self.base_branch = self.config.get("base_branch", "main")
        self.test_commands = self.config.get("test_commands", {})

        # Validation gates
        self.gate_configs = self.config.get("gates", [])

        # Task templates
        self.templates = self.config.get("templates", {})

        self.results: list[dict] = []
        self.total_tokens = {"input": 0, "output": 0}

    # ── Project context ──────────────────────────────────────

    def _load_project_context(self, workspace_path: Path) -> str:
        """Load CLAUDE.md from the (worktree) workspace for the system prompt."""
        claude_md = workspace_path / "CLAUDE.md"
        if claude_md.exists():
            content = claude_md.read_text(encoding="utf-8", errors="replace")
            if len(content) > 15_000:
                content = content[:15_000] + "\n\n...(CLAUDE.md truncated at 15k chars)"
            return content
        return "(No CLAUDE.md found — read relevant files before making changes.)"

    def _build_system_prompt(self, workspace_path: Path, scope: str | None = None) -> str:
        ctx = self._load_project_context(workspace_path)
        test_block = ""
        if self.test_commands:
            lines = [f"- {name}: `{cmd}`" for name, cmd in self.test_commands.items()]
            test_block = "## Test Commands\n" + "\n".join(lines)

        # Build repo map from the worktree, not self.workspace.
        # The live workspace may be on a different branch with local changes;
        # the worktree is branched from main so it reflects what the agent actually sees.
        repo_map_content = build_repo_map(str(workspace_path), scope)
        repo_map_block = f"""## Repo Map
The following is a structural overview of the codebase. Use it to navigate directly — do not call list_directory on directories already shown here.

<repo_map>
{repo_map_content}
</repo_map>

## Navigation Rules
- Do NOT call list_directory on directories already visible in the repo map above
- Read the context files listed in the task prompt — they are your entry points, already identified
- Make your first edit by turn 8. Do not wait until you fully understand every callsite.
- After reading the context files, use at most 2 additional searches only if a specific gap
  blocks you from knowing exactly where to write code. Do not search to confirm what you already know.
- If you are past turn 6 and have not made an edit yet, make one now based on what you know.

"""

        return f"""{repo_map_block}You are a senior software engineer completing a specific task on this codebase.
Follow these rules exactly.

## Workflow
1. Read the context files listed in the task prompt — do not search for files that are already provided
2. Make your first edit. You do not need to understand every callsite before acting.
3. Run the relevant tests
4. Fix any failures (up to 3 attempts), then call task_complete

## Project Context
{ctx}

{test_block}

## Hard Rules
- NEVER modify test files unless the task explicitly requires it
- NEVER create or modify database migrations
- NEVER modify CLAUDE.md, ARCHITECTURE.md, or any CI workflow files
- NEVER delete files
- Prefer edit_file over write_file for existing files
- If you cannot complete the task, call task_complete with confidence "low" and explain why
- If tests fail after your changes, try to fix them (up to 3 attempts), then report honestly
"""

    # ── Git + worktree operations ────────────────────────────

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.workspace),
            capture_output=True,
            text=True,
            check=check,
        )

    def _create_worktree(self, task_id: str) -> Path | None:
        """
        Create an isolated git worktree for this task.
        Returns the worktree path, or None on failure.

        Worktree approach (vs branch switching):
        - No stashing needed — main workspace stays untouched
        - Parallel-safe — could run multiple tasks at once in future
        - Clean isolation — each task has its own working directory
        """
        branch = f"ai/{task_id}"
        worktree_path = self.worktree_root / task_id

        try:
            # Clean up stale worktree if exists (re-run scenario)
            if worktree_path.exists():
                self._git("worktree", "remove", str(worktree_path), "--force", check=False)
                if worktree_path.exists():
                    shutil.rmtree(worktree_path, ignore_errors=True)

            # Delete old branch if exists
            self._git("branch", "-D", branch, check=False)

            # Pull latest on base branch (ok to fail for local-only)
            self._git("fetch", "--quiet", check=False)

            # Create worktree on a new branch from base
            self._git("worktree", "add", "-b", branch, str(worktree_path), self.base_branch)

            # Symlink gitignored dependency dirs from main workspace into the worktree
            # so gates (lint, tsc, pytest) work without re-installing everything.
            for dep_dir in ("frontend/node_modules", "backend/.venv", "api/.venv"):
                src = self.workspace / dep_dir
                dst = worktree_path / dep_dir
                if src.exists() and not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.symlink_to(src)

            return worktree_path

        except subprocess.CalledProcessError as e:
            print(f"  ✗ Worktree error: {e.stderr.strip()}")
            print(f"  ✗ Tried to branch from '{self.base_branch}' — does that branch exist?")
            print(f"  ✗ Run: cd {self.workspace} && git branch -a")
            return None

    def _commit_in_worktree(self, worktree_path: Path, task_id: str, summary: str) -> str:
        try:
            # Remove dependency symlinks created by _create_worktree before staging.
            # They point outside the worktree and must never be committed.
            for path in worktree_path.rglob("*"):
                if path.is_symlink():
                    try:
                        path.resolve().relative_to(worktree_path)
                    except ValueError:
                        path.unlink()

            self._git("add", "-A", cwd=worktree_path)
            result = self._git("diff", "--cached", "--stat", cwd=worktree_path, check=False)
            if not result.stdout.strip():
                return "(no changes to commit)"

            msg = f"ai({task_id}): {summary[:72]}"
            self._git("commit", "-m", msg, cwd=worktree_path)

            stat = self._git("diff", f"{self.base_branch}..HEAD", "--stat", cwd=worktree_path, check=False)
            return stat.stdout.strip()
        except subprocess.CalledProcessError as e:
            return f"Commit error: {e.stderr.strip()}"

    def _get_diff_from_worktree(self, worktree_path: Path) -> str:
        result = self._git("diff", f"{self.base_branch}..HEAD", cwd=worktree_path, check=False)
        diff = result.stdout
        if len(diff) > 20_000:
            diff = diff[:20_000] + "\n\n...(diff truncated at 20k chars)"
        return diff

    def _cleanup_worktree(self, worktree_path: Path):
        """Remove the worktree directory (branch is preserved in main repo)."""
        try:
            self._git("worktree", "remove", str(worktree_path), "--force", check=False)
        except Exception:
            pass
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

    # ── Validation gates ─────────────────────────────────────

    def _run_gates(self, worktree_path: Path, changed_files: list[str]) -> list[GateResult]:
        """Run gates scoped to changed_files."""
        if not self.gate_configs:
            return []
        runner = GateRunner(str(worktree_path), self.gate_configs)
        return runner.run_gates(changed_files)

    def _run_baseline_gates(self, worktree_path: Path) -> list[GateResult]:
        """
        Run ALL gates before the agent makes any changes.
        Captures which gates are already failing on the base branch so we can
        distinguish pre-existing failures from regressions introduced by the task.
        """
        if not self.gate_configs:
            return []
        runner = GateRunner(str(worktree_path), self.gate_configs)
        return runner.run_all_gates()

    # ── Task template expansion ──────────────────────────────

    def _expand_task(self, task: dict) -> dict:
        """If task uses a template, expand it."""
        template_name = task.get("template")
        if not template_name or template_name not in self.templates:
            return task

        template = self.templates[template_name].copy()

        # Task fields override template fields
        expanded = {**template, **task}

        # Interpolate {variables} in description
        desc = expanded.get("description", "")
        for key, val in task.items():
            desc = desc.replace(f"{{{key}}}", str(val))
        expanded["description"] = desc

        return expanded

    # ── Agent loop ───────────────────────────────────────────

    def _execute_tool(self, tools: ToolExecutor, name: str, inputs: dict) -> str:
        dispatch = {
            "read_file": lambda: tools.read_file(inputs["path"]),
            "write_file": lambda: tools.write_file(inputs["path"], inputs["content"]),
            "edit_file": lambda: tools.edit_file(inputs["path"], inputs["old_str"], inputs["new_str"]),
            "list_directory": lambda: tools.list_directory(inputs.get("path", "."), inputs.get("max_depth", 2)),
            "search_codebase": lambda: tools.search_codebase(
                inputs["pattern"], inputs.get("path", "."), inputs.get("file_pattern", "")
            ),
            "run_command": lambda: tools.run_command(inputs["command"]),
            "task_complete": lambda: "TASK_COMPLETE",
        }
        fn = dispatch.get(name)
        if fn is None:
            return f"ERROR: Unknown tool '{name}'"
        try:
            return fn()
        except Exception as e:
            return f"ERROR in {name}: {e}"

    def run_task(self, task: dict) -> dict:
        """Run a single task through the agentic loop in an isolated worktree."""
        task = self._expand_task(task)
        task_id = task["id"]
        description = task["description"]
        scope = task.get("scope", "")
        branch = f"ai/{task_id}"

        # Per-task provider override
        provider_name = task.get("provider", self.default_provider)
        model_override = task.get("model", self.default_model)

        # Per-task max_turns override
        task_max_turns = task.get("max_turns", self.max_turns)

        print(f"\n{'━' * 60}")
        print(f"  Task:     {task_id}")
        print(f"  Desc:     {description[:80]}{'...' if len(description) > 80 else ''}")
        print(f"  Provider: {provider_name}" + (f" ({model_override})" if model_override else ""))
        if scope:
            print(f"  Scope:    {scope}")
        print(f"{'━' * 60}")

        # Create isolated worktree
        worktree_path = self._create_worktree(task_id)
        if not worktree_path:
            result = {"task_id": task_id, "branch": branch, "status": "failed",
                      "error": "Could not create worktree — check base_branch in config.yaml",
                      "gate_report": []}
            self.results.append(result)
            return result

        # Set up provider and tools pointed at worktree
        try:
            provider = create_provider(provider_name, model_override)
        except (ValueError, Exception) as e:
            self._cleanup_worktree(worktree_path)
            result = {"task_id": task_id, "branch": branch, "status": "failed",
                      "error": f"Provider error: {e}", "gate_report": []}
            self.results.append(result)
            return result

        tools = ToolExecutor(str(worktree_path))

        # Capture baseline gate state BEFORE the agent makes any changes.
        # Pre-existing failures won't block the commit — only new ones will.
        baseline_results = self._run_baseline_gates(worktree_path)
        baseline_failing: set[str] = {r.name for r in baseline_results if not r.passed}
        if baseline_failing:
            print(f"  ⚠ Baseline gate failures (pre-existing, won't block): {', '.join(baseline_failing)}")

        # Build prompt
        scope_hint = f"\n\nFocus area: `{scope}`" if scope else ""
        context_hint = ""
        context_paths = task.get("context", [])
        if context_paths:
            path_list = "\n".join(f"- {p}" for p in context_paths)
            context_hint = (
                f"\n\nBefore starting, read these likely-relevant files first:\n{path_list}"
            )
        task_prompt = (
            f"## Task\n{description}{scope_hint}{context_hint}\n\n"
            "Start by reading the relevant files to understand the current code, "
            "then make the necessary changes. Run tests when done."
        )

        messages = [{"role": "user", "content": task_prompt}]
        system = self._build_system_prompt(worktree_path, scope or None)
        completion_data = None
        turns = 0
        metrics = TaskMetrics()

        while turns < task_max_turns:
            turns += 1
            tool_names = []

            try:
                response = provider.send(system, messages, TOOL_DEFINITIONS)
            except ProviderError as e:
                print(f"  ✗ API error on turn {turns}: {e}")
                break

            self.total_tokens["input"] += response.input_tokens
            self.total_tokens["output"] += response.output_tokens
            metrics.token_usage += response.input_tokens + response.output_tokens

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                print(f"  Turn {turns}: agent finished (no task_complete called)")
                break

            tool_results = []
            for block in response.content:
                if not hasattr(block, "type") or block.type != "tool_use":
                    continue

                tool_names.append(block.name)
                metrics.tool_sequence.append(block.name)

                # Track first-occurrence turns
                if block.name == "read_file" and metrics.first_read_turn is None:
                    metrics.first_read_turn = turns
                if block.name in ("edit_file", "write_file") and metrics.first_edit_turn is None:
                    metrics.first_edit_turn = turns
                    metrics._first_edit_done = True
                if block.name == "run_command" and metrics.first_run_turn is None:
                    metrics.first_run_turn = turns

                # Exploration tracking
                if block.name in ("list_directory", "search_codebase"):
                    metrics.explore_calls += 1
                    if not metrics._first_edit_done:
                        metrics.explore_before_first_edit += 1

                if block.name == "task_complete":
                    metrics.task_complete_reached = True
                    completion_data = block.input
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Task complete acknowledged.",
                    })
                else:
                    result = self._execute_tool(tools, block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result),
                    })

            print(f"  Turn {turns}: {', '.join(tool_names)}")

            if completion_data:
                break

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

        # Finalize metrics
        metrics.total_turns = turns

        # ── Post-completion: gates, commit, cleanup ──────────

        if completion_data:
            summary = completion_data.get("summary", "(no summary)")
            files_changed = completion_data.get("files_changed", [])

            # Run validation gates BEFORE committing
            gate_results = self._run_gates(worktree_path, files_changed)
            # A gate blocks the commit only if it's a NEW failure — i.e. it passed
            # at baseline but fails now. Pre-existing failures are annotated but
            # do not block the commit.
            new_failures = [g for g in gate_results if not g.passed and g.name not in baseline_failing]
            gates_passed = len(new_failures) == 0
            gate_report = []
            if baseline_failing:
                gate_report.append("Baseline (pre-existing failures on base branch):")
                for r in baseline_results:
                    status = "✅" if r.passed else "⚠️ pre-existing"
                    gate_report.append(f"  {status} {r.name}")
                gate_report.append("")
                gate_report.append("Post-task gate results:")
            for g in gate_results:
                pre_existing = g.name in baseline_failing
                if g.passed:
                    status = "✅"
                elif pre_existing:
                    status = "⚠️ pre-existing"
                else:
                    status = "❌ NEW"
                line = f"  {status} {g.name}"
                if g.expected:
                    line += f" (expected: {g.expected}, actual: {g.actual})"
                gate_report.append(line)
                if not g.passed and g.output:
                    for out_line in g.output.splitlines()[:50]:
                        gate_report.append(f"    {out_line}")
                if g.passed:
                    print(f"  ✓ Gate: {g.name}")
                elif pre_existing:
                    print(f"  ⚠ Gate pre-existing (not blocking): {g.name}")
                else:
                    print(f"  ✗ Gate FAILED (new regression): {g.name}")

            # Only commit if gates pass
            if gates_passed:
                diff_stat = self._commit_in_worktree(worktree_path, task_id, summary)
                full_diff = self._get_diff_from_worktree(worktree_path)
            else:
                diff_stat = "(not committed — gate failure)"
                full_diff = ""

            result = {
                "task_id": task_id,
                "branch": branch,
                "status": "complete" if gates_passed else "gate_failed",
                "summary": summary,
                "files_changed": files_changed,
                "tests_passed": completion_data.get("tests_passed", False),
                "confidence": completion_data.get("confidence", "unknown"),
                "diff_stat": diff_stat,
                "diff": full_diff,
                "turns": turns,
                "provider": provider_name,
                "gate_report": gate_report,
                "worktree_path": str(worktree_path) if not gates_passed else "",
                "baseline_failing": sorted(baseline_failing),
                "metrics": metrics,
            }

            if gates_passed:
                status_icon = "✓" if completion_data.get("tests_passed") else "⚠"
                print(f"  {status_icon} Complete ({result['confidence']} confidence, {turns} turns)")
            else:
                print(f"  ✗ Gates failed — changes NOT committed")
        else:
            result = {
                "task_id": task_id,
                "branch": branch,
                "status": "incomplete",
                "error": f"Agent did not call task_complete after {turns} turns",
                "turns": turns,
                "provider": provider_name,
                "gate_report": [],
                "metrics": metrics,
            }
            print(f"  ✗ Incomplete after {turns} turns")

        # On gate failure, preserve the worktree so the diff can be inspected.
        # On success or incomplete, clean up immediately.
        if result["status"] == "gate_failed":
            print(f"  ━ Worktree preserved: {worktree_path}")
            print(f"    Inspect: cd {worktree_path} && git diff HEAD")
            print(f"    Clean up when done: python3 run.py --cleanup")
        else:
            self._cleanup_worktree(worktree_path)
        self.results.append(result)
        return result

    # ── Batch runner ─────────────────────────────────────────

    def run_batch(self, tasks_path: str = "tasks.yaml") -> str:
        with open(tasks_path) as f:
            tasks_config = yaml.safe_load(f)

        tasks = tasks_config.get("tasks", [])
        if not tasks:
            print("No tasks found.")
            return ""

        print(f"\n  Batch: {len(tasks)} tasks")
        print(f"  Workspace: {self.workspace}")
        print(f"  Worktrees: {self.worktree_root}")
        print(f"  Default provider: {self.default_provider}")
        print(f"  Max turns/task: {self.max_turns}")
        if self.gate_configs:
            print(f"  Gates: {len(self.gate_configs)}")

        for task in tasks:
            try:
                self.run_task(task)
            except Exception as e:
                print(f"  ✗ Fatal error on {task['id']}: {e}")
                self.results.append({
                    "task_id": task["id"],
                    "status": "error",
                    "error": str(e),
                    "gate_report": [],
                })

        return self._generate_review()

    # ── Metrics rendering ─────────────────────────────────────

    def _render_metrics(self, m: TaskMetrics) -> list[str]:
        """Render a TaskMetrics object as markdown lines for the review report."""
        lines: list[str] = ["## Metrics", ""]
        seq = m.compact_sequence()
        lines.append(f"**Tool sequence:** `{seq}`" if seq else "**Tool sequence:** (none)")
        lines.append("")

        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Total turns | {m.total_turns} |")
        lines.append(f"| Explore calls (ld + sc) | {m.explore_calls} |")
        lines.append(f"| Explore before first edit | {m.explore_before_first_edit} |")
        lines.append(f"| First read turn | {m.first_read_turn if m.first_read_turn is not None else '—'} |")
        lines.append(f"| First edit turn | {m.first_edit_turn if m.first_edit_turn is not None else '—'} |")
        lines.append(f"| First run turn | {m.first_run_turn if m.first_run_turn is not None else '—'} |")
        lines.append(f"| task_complete reached | {'yes' if m.task_complete_reached else 'no'} |")
        lines.append(f"| Tokens (in+out) | {m.token_usage:,} |")
        lines.append("")

        warnings: list[str] = []
        if m.first_edit_turn is not None and m.first_edit_turn > 12:
            warnings.append(
                f"⚠️ **Slow to edit:** first edit happened on turn {m.first_edit_turn} (threshold: 12)"
            )
        if m.explore_before_first_edit > 5:
            warnings.append(
                f"⚠️ **Excessive exploration:** {m.explore_before_first_edit} explore calls before first edit (threshold: 5)"
            )
        for w in warnings:
            lines.append(w)
        if warnings:
            lines.append("")

        return lines

    # ── Review report ────────────────────────────────────────

    def _generate_review(self) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        completed = sum(1 for r in self.results if r["status"] == "complete")
        gate_failed = sum(1 for r in self.results if r["status"] == "gate_failed")
        other_failed = len(self.results) - completed - gate_failed

        lines = [
            f"# Morning Review — {now}",
            "",
            f"**Tasks:** {len(self.results)} | "
            f"**Complete:** {completed} | "
            f"**Gate failures:** {gate_failed} | "
            f"**Other failures:** {other_failed}",
            "",
            f"**Tokens used:** {self.total_tokens['input']:,} in / {self.total_tokens['output']:,} out",
            "",
            "---",
            "",
        ]

        for r in self.results:
            lines.append(f"## `{r['task_id']}`")

            if r["status"] == "complete":
                emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(r.get("confidence", ""), "⚪")
                tests = "✅ pass" if r.get("tests_passed") else "❌ fail"
                lines.append(
                    f"**Branch:** `{r['branch']}` | "
                    f"**Provider:** {r.get('provider', '?')} | "
                    f"**Confidence:** {emoji} {r.get('confidence')} | "
                    f"**Tests:** {tests} | "
                    f"**Turns:** {r.get('turns')}"
                )
                lines.append("")
                lines.append(f"**Summary:** {r.get('summary', '—')}")
                lines.append("")

                files = r.get("files_changed", [])
                if files:
                    lines.append(f"**Files:** `{'`, `'.join(files)}`")
                    lines.append("")

                # Gate results
                if r.get("gate_report"):
                    lines.append("**Gates:**")
                    for gl in r["gate_report"]:
                        lines.append(gl)
                    lines.append("")

                if r.get("diff_stat"):
                    lines.append("```")
                    lines.append(r["diff_stat"])
                    lines.append("```")
                    lines.append("")

                lines.append("**Review & merge:**")
                lines.append("```bash")
                lines.append(f"git diff {self.base_branch}..{r['branch']}   # review changes")
                lines.append(f"git checkout {self.base_branch} && git merge {r['branch']}  # merge if happy")
                lines.append(f"git branch -D {r['branch']}                  # clean up after merge")
                lines.append("```")

            elif r["status"] == "gate_failed":
                lines.append(f"**Status:** 🚫 Gate failure — changes NOT committed")
                lines.append(f"**Summary:** {r.get('summary', '—')}")
                lines.append("")
                if r.get("gate_report"):
                    lines.append("**Gate output:**")
                    lines.append("```")
                    for gl in r["gate_report"]:
                        lines.append(gl)
                    lines.append("```")
                    lines.append("")
                if r.get("worktree_path"):
                    lines.append("**Inspect the uncommitted diff:**")
                    lines.append("```bash")
                    lines.append(f"cd {r['worktree_path']} && git diff HEAD")
                    lines.append("```")
                    lines.append("")
                lines.append(
                    "*Fix the underlying issue and re-run. "
                    "Run `python3 run.py --cleanup` to remove the preserved worktree.*"
                )

            else:
                lines.append(f"**Status:** ❌ {r['status']}")
                if r.get("error"):
                    lines.append(f"**Error:** {r['error']}")

            # Metrics block (all statuses)
            metrics_obj = r.get("metrics")
            if metrics_obj is not None:
                lines.append("")
                lines.extend(self._render_metrics(metrics_obj))

            lines.append("")
            lines.append("---")
            lines.append("")

        # Full diffs
        has_diffs = any(r.get("diff") for r in self.results)
        if has_diffs:
            lines.append("# Full Diffs")
            lines.append("")
            for r in self.results:
                if r.get("diff"):
                    lines.append(f"## `{r['task_id']}`")
                    lines.append("```diff")
                    lines.append(r["diff"])
                    lines.append("```")
                    lines.append("")

        return "\n".join(lines)
