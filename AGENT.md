# AGENT.md — Mimi: Perpetua's Operational Agent

> Architecture document. Describes what Mimi is, how it works, and what it is
> and is not allowed to do. Keep this file up to date as Mimi evolves.

---

## What Mimi Is

Mimi is Perpetua's operational agent — an autonomous system that monitors the
16-stage UK company intelligence pipeline, classifies failures, takes corrective
action, and surfaces health status in the admin dashboard.

Inspired by Airweave's "Donke" pattern: an LLM-powered agent that watches its
own product's operational health, clusters errors, finds relevant code context,
checks for duplicate issues, and auto-resolves problems without human
intervention wherever it's safe to do so.

Mimi is **not** a general-purpose AI assistant. It has a narrow, well-defined
operational scope: keep Perpetua's data pipeline healthy.

---

## Three Operating Modes

### 1. Watcher (`pipeline_watcher.py`)

The continuous monitor. Runs on a 15-minute schedule (or on-demand via the
admin dashboard).

- Fetches the pipeline status snapshot from `/api/admin/status`
- Enriches failures with code context (stage source, git history)
- Calls Claude (`claude-sonnet-4-6`) to classify each issue
- Takes action based on classification:
  - `auto-fix` → calls `/api/admin/run-stage` to retry the stage
  - `auto-cancel` → calls `/api/admin/cancel-stage` for stuck stages
  - `pr-required` → dispatches to the Task Runner
  - `suppress` → records observation, no action (PR already open)
  - `monitor` → watches another cycle before acting
- Writes all observations to `pipeline_observations.json`
- Resolves healthy stages when they stop appearing in the failure list

### 2. Task Runner (`orchestrator.py`)

The code change executor. Runs AI agents in isolated git worktrees to implement
fixes, bypassing no tests or gates.

- Creates a fresh `ai/<task-id>` branch from `main`
- Runs the LLM in an agentic loop with 7 sandboxed tools
- After task completion, runs validation gates (lint, typecheck)
- Only commits if gates pass — otherwise preserves the worktree for inspection
- Never touches `main` directly; all changes require human review and merge

### 3. Overnight Loop (`overnight.py`)

A scheduled deep-observation run designed to produce a morning briefing.

- **Mode `observe`** (default, safe for automation):
  - Runs one watcher cycle
  - For REGRESSION/NEW issues with ≥3 failures: finds relevant code, logs
    suggested fix approach — does NOT attempt the fix
  - Generates `~/Mimi/reviews/morning_briefing_YYYYMMDD.md`
  - Exits with code 0
- **Mode `fix`** (manual activation only — use with caution):
  - Same as observe, but additionally runs the Task Runner for qualifying issues
  - Records outcomes and re-checks pipeline state after fixes
  - Hard cap: 5 fix attempts per overnight run (cost control)

---

## The Observation → Classification → Action → Resolution Lifecycle

```
Pipeline stage fails
        │
        ▼
Watcher fetches status snapshot
        │
        ▼
Enrich with code context
(stage source, git log, error hash)
        │
        ▼
Claude classifies each issue:
  ┌─────────────────────────────┐
  │ NEW       — first occurrence │
  │ REGRESSION— recurred after  │
  │             resolution       │
  │ ONGOING   — same failure,   │
  │             still unresolved │
  └─────────────────────────────┘
        │
        ▼
Action decision:
  auto-fix    → re-run stage via API
  auto-cancel → cancel stuck stage
  pr-required → dispatch to Task Runner
  suppress    → PR already open, wait
  monitor     → wait another cycle
        │
        ▼
Update pipeline_observations.json:
  - classification, action, timestamps
  - consecutive_failures counter
  - pr_branch (if Task Runner was invoked)
        │
        ▼
Stage starts succeeding again?
        │
        ▼
mark_resolved() → resolved_at set,
consecutive_failures reset to 0,
pr_branch cleared

Next watcher cycle starts fresh for this stage.
```

---

## Codebase Interaction Model

Mimi interacts with the Perpetua codebase **only through git worktrees**.

- The main workspace (`~/Desktop/Project Succession`) is **read-only** from
  Mimi's perspective during automated operations.
- All code changes happen in isolated worktrees at `~/.mimi-worktrees/<task-id>/`
  branched from `main`.
- Worktrees are cleaned up after task completion. Branches are preserved.
- Mimi never runs `git checkout main`, `git push`, `git merge`, or `git rebase`.
- Symlinks are created for `frontend/node_modules` and `backend/.venv` so gates
  (lint, typecheck, pytest) can run without reinstalling dependencies.

---

## Admin Dashboard Integration

The `MimiWidget` component in the admin dashboard (`/admin/status`) reads Mimi's
state via `/api/admin/mimi`:

- **GET** — reads `pipeline_observations.json`, returns active issues, open PRs,
  recent actions. Returns `not_started: true` if the file doesn't exist yet.
- **POST** — fires `pipeline_watcher.py --once` as a background subprocess.
  A module-level concurrency guard prevents simultaneous runs.

The widget polls every 60 seconds and shows:
- Status pulse (green/amber/red based on time since last check)
- Active issue count with classification badges
- Expand/collapse detail with Issues and Actions tabs
- Stats footer: regression / new / ongoing / auto-fixed counts

---

## What Mimi Is NOT Allowed To Do

These are hard constraints, never to be circumvented:

1. **No direct DB writes.** Mimi never connects to the database directly.
   Pipeline stage scripts (which Mimi may trigger) have their own DB access.

2. **No deployment.** Mimi never deploys to production, pushes Docker images,
   or modifies infrastructure configs.

3. **No merging without human review for financial data changes.** Any code
   change touching `backend/pipeline/` must be committed to a branch and left
   for HJ to review. Even if gates pass.

4. **No auto-merge of any kind.** Mimi creates branches. Humans merge.

5. **No modification of fragile files without explicit task.** `xbrl_parsing.py`
   and `uk_ingestor.py` contain known workarounds — do not touch without a
   specific, directed task.

6. **No API key logging.** API keys and secrets must never appear in observation
   files, briefings, or log output.

7. **No fix mode without human sign-off.** `overnight.py --mode fix` must not
   be used until HJ has validated that observe mode produces correct briefings.

---

## File Map

```
~/Mimi/
├── AGENT.md                    # This file — architecture document
├── SKILL.md                    # Registry of Mimi's capabilities
├── CLAUDE.md                   # Original task runner context (keep in sync)
├── README.md                   # Quick-start guide
├── config.yaml                 # Workspace, provider, gates, watcher config
├── tasks.yaml                  # Batch task definitions for Task Runner
│
├── pipeline_watcher.py         # Watcher mode — poll, classify, act
├── pipeline_classifier.py      # Claude classification logic
├── overnight.py                # Overnight loop — observe/fix modes
├── briefing.py                 # Morning briefing generator
│
├── orchestrator.py             # Task Runner — agentic loop, worktrees
├── providers.py                # Multi-LLM abstraction
├── gates.py                    # Validation gates
├── tools.py                    # Agent tool sandbox
├── repo_map.py                 # Repo structure builder for system prompt
├── run.py                      # CLI entry point for batch task runner
│
├── pipeline_observations.json  # Live state file (gitignored)
├── requirements.txt            # Python dependencies
│
├── reviews/                    # Generated review and briefing files
└── tests/                      # Unit tests
```
