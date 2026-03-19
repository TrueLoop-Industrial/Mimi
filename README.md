# Perpetua Task Runner v2

A minimal CLI that runs AI coding tasks against your codebase overnight.
Each task gets an isolated git worktree. You review branches in the morning.

## What changed in v2 (Codex review)

- **Git worktrees** instead of branch switching — your main workspace stays untouched, no stashing, parallel-safe
- **Multi-model** — Claude, Groq, OpenAI/Codex behind one interface. Route cheap tasks to Groq, hard tasks to Claude
- **Validation gates** — golden ratio checks and lint gates run BEFORE commit. If they fail, the branch is blocked
- **Task templates** — define reusable task shapes for common operations

## Setup

```bash
cd ~/tools/perpetua-runner

# Install (Groq is optional — comment it out if not using)
pip install -r requirements.txt

# Set API keys for the providers you're using
export ANTHROPIC_API_KEY="sk-ant-..."
export GROQ_API_KEY="gsk_..."            # optional
# export OPENAI_API_KEY="sk-..."         # optional

# Verify config points to your project
cat config.yaml
```

## Usage

```bash
# Run all tasks
python run.py

# Run a single task
python run.py --task fix-overnight-tests-index

# Preview without executing
python run.py --dry-run

# Clean up all worktree directories
python run.py --cleanup
```

## How worktrees work

Unlike v1 (which switched branches on your main repo), v2 creates a separate
directory for each task:

```
~/Desktop/Project Succession/          ← your workspace (untouched)
~/Desktop/.perpetua-worktrees/
  ├── fix-overnight-tests-index/       ← task 1 works here
  ├── add-node-engines-field/          ← task 2 works here
  └── standardize-pytest-version/      ← task 3 works here
```

After each task completes, the worktree directory is removed. The **branch**
(`ai/fix-overnight-tests-index`) is preserved in your main repo for review.

## Multi-model strategy

Set a default provider in `config.yaml`, override per-task in `tasks.yaml`:

```yaml
# config.yaml
provider: claude  # default for all tasks

# tasks.yaml
tasks:
  - id: simple-doc-fix
    provider: groq        # fast & cheap (~$0.59/M tokens)

  - id: complex-pipeline-fix
    provider: claude      # strongest reasoning

  - id: code-generation-task
    provider: codex       # OpenAI Codex
    model: gpt-5.3-codex  # explicit model override
```

**Cost guidance:**
- Groq (Llama 3.3 70B): ~$0.59/M input, $0.79/M output — use for docs, simple fixes, JSON edits
- Claude Sonnet: your existing subscription — use for complex code, financial logic, frontend
- Codex: if you have OpenAI credits — alternative for code generation

## Validation gates

Gates run after the agent finishes but before committing. Define in `config.yaml`:

```yaml
gates:
  # Command gate: pass if exit code == 0
  - name: "Frontend lint clean"
    type: command
    command: "cd frontend && npx eslint src/ --quiet"
    scope: "frontend/**"

  # Golden ratio gate: extract a number, check it hasn't drifted
  - name: "Current ratio invariant"
    type: golden_ratio
    command: "python -c \"from backend.pipeline.lib.ratios import current_ratio; print(current_ratio())\""
    metric: "([\\d.]+)"      # regex to extract the number
    expected: 1.5             # what it should be
    tolerance: 0.05           # acceptable drift
    scope: "backend/pipeline/**"
```

If a gate fails, the review report marks it as 🚫 and tells you what went wrong.

## Task templates

Define reusable patterns in `config.yaml`, reference them in `tasks.yaml`:

```yaml
# config.yaml
templates:
  pipeline_fix:
    description: >
      Fix the pipeline issue: {issue}.
      This touches financial data — be extra careful.
    scope: "backend/pipeline/"
    provider: claude

# tasks.yaml
tasks:
  - id: fix-xbrl-dormant
    template: pipeline_fix
    issue: "Dormant company filings crash the parser"
```

## Morning review workflow

```bash
# 1. Read the review
cat reviews/review_YYYYMMDD_HHMM.md

# 2. For each branch, review the diff
git diff main..ai/fix-overnight-tests-index

# 3. Merge if happy
git checkout main && git merge ai/fix-overnight-tests-index
git branch -D ai/fix-overnight-tests-index

# 4. Or discard
git branch -D ai/fix-overnight-tests-index
```

## Safety guarantees

- All work happens in isolated **worktrees** — main workspace is never modified
- Destructive commands blocked (`rm -rf`, `git push`, `git merge`, SQL drops, `sudo`)
- Agent cannot modify tests, migrations, CI workflows, or CLAUDE.md
- Validation gates block commits if financial invariants drift
- Maximum turn limit prevents runaway API costs (default: 30)

## File structure

```
perpetua-runner/
├── run.py              # CLI entry point
├── orchestrator.py     # Agentic loop, worktrees, review generation
├── providers.py        # Claude / Groq / OpenAI provider abstraction
├── gates.py            # Validation gates (golden ratios, lint, etc.)
├── tools.py            # Tool definitions (what the agent can do)
├── config.yaml         # Workspace, provider, gates, templates
├── tasks.yaml          # Tonight's tasks
├── requirements.txt    # Python deps
└── reviews/            # Generated review reports
```
