# CLAUDE.md — Mimi (AI Task Runner)

## What this is

A CLI tool that runs AI coding tasks against a codebase overnight.
Each task gets an isolated git worktree. User reviews branches in the morning.

Separate repo from the target project. Points at any codebase via `workspace` config path.

## Tech stack

- Python 3.12+
- FastAPI: NOT used here (this is a CLI, not a server)
- Anthropic SDK for Claude API
- Groq SDK for Groq API (fast/cheap tasks)
- OpenAI SDK for Codex (optional)
- PyYAML for config
- No database, no frontend, no Docker — just Python scripts

## Architecture (6 files)

```
mimi/
├── run.py              # CLI entry point (argparse)
├── orchestrator.py     # Agentic loop, git worktrees, review generation
├── providers.py        # Multi-LLM abstraction (Claude, Groq, OpenAI/Codex)
├── gates.py            # Validation gates (command + golden_ratio types)
├── tools.py            # Tool definitions the LLM agent can call
├── config.yaml         # Workspace path, provider config, gates, templates
├── tasks.yaml          # Task definitions
├── requirements.txt    # anthropic, pyyaml, groq, (openai optional)
└── reviews/            # Generated markdown review reports
```

## How it works

1. User defines tasks in `tasks.yaml`
2. For each task:
   a. Create a git worktree at `~/.mimi-worktrees/<task-id>/` branching from `main`
   b. Instantiate the configured LLM provider (Claude/Groq/Codex)
   c. Run an agentic loop: LLM reads code, makes changes, runs tests via tool calls
   d. On task_complete, run validation gates (lint, golden ratio checks)
   e. If gates pass → commit to branch `ai/<task-id>` in the worktree
   f. If gates fail → no commit, flag in review
   g. Clean up worktree directory (branch preserved in main repo)
3. Generate a markdown review report in `reviews/`
4. User reviews diffs and merges branches they approve

## Key design decisions

### Git worktrees (not branch switching)
- Main workspace is NEVER touched — no stashing, no checkout conflicts
- Each task works in its own isolated directory
- Parallel-safe for future multi-task execution
- Worktree is deleted after task; branch is preserved for review

### Multi-provider
- Provider abstraction: all implement `send(system, messages, tools) -> LLMResponse`
- Anthropic uses native tool calling format
- Groq and OpenAI use OpenAI-compatible format with conversion layer
- Normalized response uses `_TextBlock` and `_ToolUseBlock` mimicking Anthropic types
- Per-task provider override via `provider:` field in tasks.yaml
- Factory function: `create_provider("claude")`, `create_provider("groq")`, etc.

### Validation gates
- Run AFTER agent finishes, BEFORE commit
- Two types:
  - `command`: pass if exit code == 0 (e.g., lint, typecheck)
  - `golden_ratio`: run command, extract numeric value via regex, assert within tolerance
- Scoped by glob pattern — gate only runs if changed files match scope
- Gate failure blocks the commit and flags it in the review report

### Task templates
- Defined in config.yaml under `templates:`
- Referenced in tasks.yaml with `template: template_name`
- Variable interpolation: `{variable}` in template description replaced by task fields

### Tool sandbox
- All file operations sandboxed to workspace via path traversal check
- Blocked commands: rm -rf, git push, git merge, git rebase, sudo, DROP TABLE, etc.
- Agent CANNOT modify: test files, migrations, CI workflows, CLAUDE.md
- File size limit on reads (100KB)
- Command timeout (120s default)
- Search results truncated at 50 lines

### Safety
- Max turn limit per task (default 30) prevents runaway API costs
- All work on `ai/*` branches, never on main
- Worktree cleanup on both success and failure paths
- Token tracking for cost awareness in review reports

## Tool definitions (what the LLM agent can do)

7 tools:
- `read_file` — read file contents (sandboxed, max 100KB)
- `write_file` — create/overwrite file
- `edit_file` — replace a unique string (must read first)
- `list_directory` — ls with depth limit, skips .git/node_modules/etc
- `search_codebase` — grep with source file filters
- `run_command` — shell command with blocklist
- `task_complete` — signal done with summary, files_changed, tests_passed, confidence

## CLI interface

```bash
python run.py                          # run all tasks
python run.py --task <id>              # run single task
python run.py --dry-run                # preview without executing
python run.py --cleanup                # remove all worktrees
python run.py --config alt.yaml        # custom config
python run.py --tasks tonight.yaml     # custom tasks file
python run.py --output reports/        # custom review output dir
```

## Config structure (config.yaml)

```yaml
workspace: /absolute/path/to/target/project
worktree_root: /path/to/.mimi-worktrees  # optional, default: sibling to workspace
provider: claude                          # default provider
model: null                               # optional model override
max_turns: 30
base_branch: main
test_commands:
  backend: "cd backend && python -m pytest -x -q"
  frontend: "cd frontend && npx tsc --noEmit"
gates:
  - name: "Lint clean"
    type: command
    command: "npx eslint src/ --quiet"
    scope: "frontend/**"
  - name: "Ratio check"
    type: golden_ratio
    command: "python check_ratios.py"
    metric: "current_ratio:\\s*([\\d.]+)"
    expected: 1.5
    tolerance: 0.05
    scope: "backend/**"
templates:
  fix_bug:
    description: "Find and fix: {bug_description}"
    scope: "{scope}"
```

## Conventions

- Type hints on all functions
- Dataclasses for structured data (GateResult, GateConfig, LLMResponse)
- subprocess for all git and shell operations
- No async (sequential execution by design — one LLM at a time for 16GB RAM)
- Error handling: catch provider errors, subprocess errors, file errors — never crash the batch
- Print-based progress logging with unicode status indicators (✓ ✗ ━ etc.)

## Testing approach

- Unit tests for: tools.py (path traversal, blocklist), gates.py (command + golden_ratio), providers.py (format conversion)
- Integration test: mock LLM responses, verify worktree creation → tool execution → gate → commit → cleanup flow
- No tests for actual LLM quality — that's evaluated by reviewing the output

## DO NOT

- Add a web UI, WebSocket server, or React frontend
- Add LangGraph, LangChain, or any agent framework
- Add a vector database or RAG system
- Add async/concurrent execution
- Make it a package or library — it's a script you run
- Over-engineer the provider abstraction — 3 providers is enough
