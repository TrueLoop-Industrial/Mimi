# SKILL.md — Mimi Skill Registry

> A registry of things Mimi knows how to do. Each skill lists its status,
> where it lives in the codebase, and what it does.
>
> Status: ✅ Implemented | 🔧 Partial | TODO Not yet built

---

## Implemented Skills

### `monitor_pipeline` ✅
**Location:** `pipeline_watcher.py` → `run_check()`

Fetches the current pipeline status snapshot from `/api/admin/status`, parses
the recent run history, and identifies stages with failures or coverage gaps
below threshold. The entry point for every watcher cycle.

**Inputs:** admin API base URL + secret (from config)
**Outputs:** list of enriched issue dicts passed to `classify_issue`

---

### `classify_issue` ✅
**Location:** `pipeline_classifier.py` → `classify_issues()`

Calls Claude (`claude-sonnet-4-6`) with a structured prompt containing the
pipeline status summary, the enriched issue list, and the observation history.
Returns a classification for each issue:
- **NEW** — first occurrence; no prior observation or was previously resolved
- **REGRESSION** — was resolved, broke again
- **ONGOING** — same failure, still unresolved

Also decides the action: `auto-fix`, `auto-cancel`, `pr-required`, `suppress`,
or `monitor`.

**Inputs:** list of enriched issues, status snapshot, observations dict
**Outputs:** list of classification dicts with `stage`, `classification`,
`action`, `risk`, `reason`, `pr_description`

---

### `find_code_context` ✅
**Location:** `pipeline_classifier.py` → `_stage_source()`, `_git_log()`

Locates the Python source file for a given pipeline stage code (e.g. `"07"` →
`07_mine_financials.py`) and reads the first 80 lines. Also fetches the last 5
git commits touching that file to surface recent changes.

**Inputs:** stage code string (e.g. `"07"`)
**Outputs:** source snippet string + git log string, both included in the
classification prompt for richer context

---

### `track_observations` ✅
**Location:** `pipeline_watcher.py` → `update_observation()`, `load_observations()`,
`save_observations()`

Maintains `pipeline_observations.json` — a persistent state file that tracks
every stage Mimi has seen fail. Records classification, action, timestamps,
consecutive failure count, total failures, and any associated PR branch.
The action log (last 100 entries) is prepended on each update.

**Inputs:** stage name, classification, action, reason, optional pr_branch and outcome
**Outputs:** mutates the in-memory observations dict; saves to disk

---

### `resolve_stage` ✅
**Location:** `pipeline_watcher.py` → `mark_resolved()`

Marks a stage as healthy when it stops appearing in the failure list. Sets
`resolved_at` to the current UTC timestamp, resets `consecutive_failures` to 0,
and clears `pr_branch`. Called automatically in the resolution pass at the end
of each watcher cycle.

**Inputs:** observations dict, stage name
**Outputs:** mutates the stage entry in the observations dict

---

### `run_task` ✅
**Location:** `orchestrator.py` → `TaskOrchestrator.run_task()`

Runs an LLM agent in an isolated git worktree to implement a code change.
Creates an `ai/<task-id>` branch from `main`, gives the agent 7 sandboxed tools
(read/write/edit files, list directory, search codebase, run command,
task_complete), and runs validation gates after the agent signals completion.
Only commits if gates pass.

**Inputs:** task dict with `id`, `description`, optional `scope`, `context`,
`provider`, `model`, `max_turns`
**Outputs:** result dict with `status`, `branch`, `summary`, `diff_stat`,
`gate_report`, `metrics`

---

### `auto_retry_stage` ✅
**Location:** `pipeline_watcher.py` → `auto_retry_stage(stage, config, observations)`

Re-run a failed pipeline stage via the admin API with the same parameters
(`batch_size: 500`, `country: GB`). Checks `auto_retried_count` against
`auto_fix_max_retries` from config before triggering. If max retries reached,
returns `escalated: True` so the caller can fall through to `pr-required`.

**Inputs:** `stage: str`, `config: dict`, `observations: dict`
**Outputs:** `AutoRetryResult` TypedDict — `success: bool`, `outcome: str`, `escalated: bool`

---

### `draft_fix` ✅
**Location:** `pipeline_watcher.py` → `draft_fix(issue, config)`

Given a classified issue with `action: pr-required`, builds a task using the
`pipeline_fix_reactive` template and dispatches it to the TaskOrchestrator.
Retries once on failure. Records the resulting branch name in observations as
`pr_branch`.

**Inputs:** `issue: dict` (from `classify_issues()`), `config: dict`
**Outputs:** `DraftFixResult` TypedDict — `branch: str`, `success: bool`, `error: str | None`

---

## TODO Skills

### `deduplicate_issues` TODO
Before creating a new observation, check if one already exists for the same
stage + error signature (`current_error_hash`). If the error hash matches the
last recorded hash, treat it as ONGOING rather than NEW. Prevents false REGRESSION
signals when the same error persists across watcher cycles.

**Planned location:** `pipeline_classifier.py` → `enrich_issues()`. Add a check:
if `obs.last_error_hash == current_error_hash` and `obs.resolved_at is None`,
force classification to ONGOING.

---

### `summarize_overnight` TODO
Generate a morning briefing markdown summarising overnight activity:
- Pipeline stage health (green/amber/red)
- Issues detected and classified
- Actions taken (auto-fixes, PRs opened)
- API call cost estimate (Sonnet: $3/M input, $15/M output)
- Items requiring human attention

**Planned location:** `briefing.py` (standalone generator). The `overnight.py`
loop calls this at the end of each run.

---

### `validate_financial_data` TODO
Run the golden ratio checks and balance sheet identity checks (`Total Assets ==
Total Liabilities + Equity`) across a sample of recently-processed companies.
Surface any discrepancies in the morning briefing.

**Planned location:** New script `validate_financials.py`. Called from `overnight.py`
as an optional validation step after the watcher cycle.

Dependencies: requires DB read access via Supabase client (currently not in Mimi).
Defer until Mimi has a read-only DB connection.

---

### `alert_on_drift` TODO
Detect when financial metrics (coverage percentages, success rates, error rates)
drift beyond acceptable thresholds across consecutive pipeline runs. Compare
the current status snapshot to the previous one and flag significant deltas.

**Planned location:** `pipeline_watcher.py` → add a post-classification drift
check comparing `obs.consecutive_failures` trajectory against a rolling baseline.

---

## Skill Dependency Graph

```
monitor_pipeline
    └── find_code_context
    └── classify_issue
            └── track_observations
            └── resolve_stage
            └── auto_retry_stage ✅
            └── draft_fix ✅
                    └── run_task
                    └── deduplicate_issues (TODO — prevents duplicate PRs)

overnight.py
    └── monitor_pipeline
    └── find_code_context (deeper search in observe mode)
    └── summarize_overnight (TODO → briefing.py)
    └── validate_financial_data (TODO, optional)
    └── alert_on_drift (TODO)
```
