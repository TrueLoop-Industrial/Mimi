"""
Mimi Pipeline Watcher — reactive pipeline health agent.

Polls the Perpetua admin/status endpoint on a schedule, classifies issues
using Claude (NEW / REGRESSION / ONGOING), and takes action:
  - auto-fix    → calls admin API endpoints directly (run-stage, cancel-stage)
  - pr-required → dispatches a task to Mimi's orchestrator (creates branch + PR)
  - suppress    → records observation, no action
  - monitor     → records observation, waits for next cycle

Usage:
    python pipeline_watcher.py              # run continuously (default 15 min interval)
    python pipeline_watcher.py --once       # single check and exit
    python pipeline_watcher.py --status     # print current observations and exit
    python pipeline_watcher.py --interval 300  # override interval (seconds)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

import httpx
import yaml

# ── Paths ──────────────────────────────────────────────────────────────

MIMI_DIR = Path(__file__).parent
OBSERVATIONS_FILE = MIMI_DIR / "pipeline_observations.json"
CONFIG_FILE = MIMI_DIR / "config.yaml"

# Load .env so ADMIN_STATUS_SECRET (and other vars) are available
# even when the script isn't launched from a shell that sourced .env.
_env_file = MIMI_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#"):
            _line = _line.removeprefix("export ").strip()
            _key, _, _val = _line.partition("=")
            if _key and _val:
                os.environ.setdefault(_key.strip(), _val.strip().strip('"'))


def _configured_workspace() -> Path:
    try:
        config = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    except FileNotFoundError:
        return Path.cwd()
    workspace = config.get("workspace")
    return Path(workspace).expanduser() if workspace else Path.cwd()


WORKSPACE = _configured_workspace()

# ── Logging ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mimi] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mimi.watcher")


# ── Skill return types ─────────────────────────────────────────────────

class AutoRetryResult(TypedDict):
    success: bool
    outcome: str
    escalated: bool


class DraftFixResult(TypedDict):
    branch: str
    success: bool
    error: str | None


class PrOutcome(TypedDict):
    stage: str
    pr_branch: str
    opened_at: str
    merged: bool | None
    required_edits: bool | None
    closed_at: str | None
    notes: str | None


# ── Observations state ─────────────────────────────────────────────────

def load_observations() -> dict:
    if OBSERVATIONS_FILE.exists():
        obs = json.loads(OBSERVATIONS_FILE.read_text())
        if "pr_outcomes" not in obs:
            obs["pr_outcomes"] = []
        return obs
    return {"version": 1, "last_check": None, "stages": {}, "actions_log": [], "pr_outcomes": []}


def save_observations(obs: dict) -> None:
    OBSERVATIONS_FILE.write_text(json.dumps(obs, indent=2, default=str))


def update_observation(
    obs: dict,
    stage: str,
    classification: str,
    action: str,
    reason: str,
    pr_branch: str | None = None,
    outcome: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    stage_obs = obs["stages"].get(stage, {})

    was_resolved = bool(stage_obs.get("resolved_at"))
    is_first = not stage_obs or was_resolved

    stage_obs["status"] = classification
    stage_obs["last_seen_at"] = now
    stage_obs["last_action"] = action
    stage_obs["last_action_at"] = now
    stage_obs["consecutive_failures"] = (
        1 if is_first else stage_obs.get("consecutive_failures", 0) + 1
    )
    stage_obs["total_failures"] = stage_obs.get("total_failures", 0) + 1

    if is_first:
        stage_obs["first_seen_at"] = now
        stage_obs["resolved_at"] = None
        stage_obs["pr_branch"] = None

    if pr_branch:
        stage_obs["pr_branch"] = pr_branch
        stage_obs["resolved_at"] = None

    obs["stages"][stage] = stage_obs

    # Prepend to action log, keep last 100 entries
    obs["actions_log"] = (
        [{
            "ts": now,
            "stage": stage,
            "action": action,
            "classification": classification,
            "reason": reason,
            "outcome": outcome,
        }]
        + obs.get("actions_log", [])
    )[:100]


def mark_resolved(obs: dict, stage: str) -> None:
    if stage in obs["stages"]:
        obs["stages"][stage]["resolved_at"] = datetime.now(timezone.utc).isoformat()
        obs["stages"][stage]["consecutive_failures"] = 0
        obs["stages"][stage]["auto_retried_count"] = 0
        obs["stages"][stage]["pr_branch"] = None


def record_pr_outcome(
    obs: dict,
    stage: str,
    pr_branch: str,
    merged: bool | None = None,
    required_edits: bool | None = None,
    notes: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    outcomes: list = obs.setdefault("pr_outcomes", [])

    for entry in outcomes:
        if entry["pr_branch"] == pr_branch:
            entry["merged"] = merged
            entry["required_edits"] = required_edits
            entry["notes"] = notes
            if merged is not None:
                entry["closed_at"] = now
            return

    outcomes.append({
        "stage": stage,
        "pr_branch": pr_branch,
        "opened_at": now,
        "merged": merged,
        "required_edits": required_edits,
        "closed_at": None,
        "notes": notes,
    })

    if len(outcomes) > 50:
        obs["pr_outcomes"] = outcomes[-50:]


# ── Status fetch ───────────────────────────────────────────────────────

def fetch_status(base_url: str, secret: str) -> dict:
    url = f"{base_url.rstrip('/')}/api/admin/status?secret={secret}"
    resp = httpx.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


# ── Admin API actions ──────────────────────────────────────────────────

def _post(base_url: str, secret: str, path: str, body: dict, timeout: int = 30) -> bool:
    url = f"{base_url.rstrip('/')}{path}?secret={secret}"
    try:
        resp = httpx.post(url, json=body, timeout=float(timeout), follow_redirects=True)
        return resp.is_success
    except httpx.HTTPError as exc:
        log.warning(f"POST {path} failed: {exc}")
        return False


def action_run_stage(base_url: str, secret: str, stage: str) -> bool:
    return _post(
        base_url, secret,
        "/api/admin/run-stage",
        {"stage_name": stage, "batch_size": 500, "country": "GB"},
    )


def action_cancel_stage(base_url: str, secret: str, stage: str) -> bool:
    return _post(
        base_url, secret,
        "/api/admin/cancel-stage",
        {"stage_name": stage},
    )


def action_pause_jobs(base_url: str, secret: str, job_ids: list[str]) -> bool:
    return all(
        _post(base_url, secret, "/api/admin/scheduler",
              {"action": "pause", "job_id": jid})
        for jid in job_ids
    )


# ── Skill functions ────────────────────────────────────────────────────

def auto_retry_stage(stage: str, config: dict, observations: dict) -> AutoRetryResult:
    """
    Re-run a failed pipeline stage via the admin API.
    Checks auto_retried_count against max_auto_retries before triggering.
    If max retries reached, returns escalated=True for pr-required handling.
    """
    watcher_cfg = config["watcher"]
    base_url: str = watcher_cfg["base_url"]
    secret: str = watcher_cfg["admin_secret"]
    max_auto_retries: int = watcher_cfg.get("auto_fix_max_retries", 2)

    stage_obs = observations["stages"].get(stage, {})
    retries = stage_obs.get("auto_retried_count", 0)

    if retries >= max_auto_retries:
        log.warning(f"    Max auto-retries ({max_auto_retries}) reached for {stage} — escalating to pr-required")
        return {"success": False, "outcome": "max_retries_reached", "escalated": True}

    ok = action_run_stage(base_url, secret, stage)
    if ok:
        observations["stages"].setdefault(stage, {})
        observations["stages"][stage]["auto_retried_count"] = retries + 1
        log.info(f"    run-stage → triggered")
        return {"success": True, "outcome": "triggered", "escalated": False}

    log.warning(f"    run-stage → failed")
    return {"success": False, "outcome": "failed", "escalated": False}


# ── Mimi orchestrator dispatch ─────────────────────────────────────────

def draft_fix(issue: dict, config: dict) -> DraftFixResult:
    """
    Dispatch a pr-required issue to Mimi's TaskOrchestrator using the
    pipeline_fix_reactive template. Retries once on failure.
    Returns DraftFixResult with branch, success, and error.
    """
    mimi_dir = str(MIMI_DIR)
    if mimi_dir not in sys.path:
        sys.path.insert(0, mimi_dir)

    try:
        spec = importlib.util.spec_from_file_location(
            "orchestrator", MIMI_DIR / "orchestrator.py"
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        TaskOrchestrator = mod.TaskOrchestrator
    except Exception as exc:
        log.error(f"Could not load orchestrator: {exc}")
        return {"branch": "", "success": False, "error": str(exc)}

    stage = issue["stage"]
    safe = stage.replace("_", "-")
    ts = datetime.now(timezone.utc).strftime("%m%d%H%M")
    task_id = f"mimi-fix-{safe}-{ts}"

    task = {
        "id": task_id,
        "template": "pipeline_fix_reactive",
        "stage": stage,
        "issue_description": issue.get("reason", ""),
        "failure_pattern": issue.get("pr_description", f"Pipeline stage {stage} has been failing repeatedly."),
        "provider": "claude",
        "scope": "backend/pipeline/",
        "context": [
            f"backend/pipeline/{stage.split('_')[0]}*.py",
            "backend/pipeline/lib/uk_ingestor.py",
        ],
    }

    max_attempts = 2
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            orch = TaskOrchestrator(str(CONFIG_FILE))
            task_result = orch.run_task(task)
            branch = task_result.get("branch", "")
            if branch:
                log.info(f"    Orchestrator finished — branch: {branch}")
                return {"branch": branch, "success": True, "error": None}
            last_error = "orchestrator returned empty branch"
            log.warning(
                f"    Attempt {attempt}: empty branch — "
                f"{'retrying' if attempt < max_attempts else 'giving up'}"
            )
        except Exception as exc:
            last_error = str(exc)
            log.warning(
                f"    Attempt {attempt}: orchestrator error: {exc} — "
                f"{'retrying' if attempt < max_attempts else 'giving up'}"
            )

    log.error(f"Orchestrator dispatch failed after {max_attempts} attempts: {last_error}")
    return {"branch": "", "success": False, "error": last_error}


def dispatch_to_mimi(result: dict) -> str:
    """Thin alias for backward compatibility. Calls draft_fix() and returns branch string."""
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)
    fix_result = draft_fix(result, cfg)
    return fix_result["branch"]


# ── Self-heal detection ─────────────────────────────────────────────────

def _resolve_self_healed(status: dict, observations: dict) -> list[str]:
    """Auto-resolve stages whose most recent run succeeded after prior failures.

    Runs before Claude classification so self-healed stages never reach the
    classifier — avoids false ONGOING alerts after a fix lands.

    The admin/status endpoint returns recent_runs ordered desc by start_time,
    so runs[0] is always the latest.
    """
    recent_runs: list[dict] = status.get("recent_runs", [])
    by_stage: dict[str, list[dict]] = {}
    for run in recent_runs:
        stage = run.get("workflow_name", "")
        by_stage.setdefault(stage, []).append(run)

    resolved: list[str] = []
    for stage, obs_data in list(observations.get("stages", {}).items()):
        if obs_data.get("resolved_at"):
            continue  # Already resolved
        runs = by_stage.get(stage, [])
        if not runs:
            continue
        latest = runs[0]  # Most recent run (desc order)
        if latest.get("execution_status") == "success":
            mark_resolved(observations, stage)
            resolved.append(stage)
            log.info(
                f"  {stage}: self-healed — latest run succeeded "
                f"({latest.get('total_companies_processed', 0):,} processed, "
                f"0 errors) — auto-resolved"
            )
    return resolved


# ── Main check ─────────────────────────────────────────────────────────

def run_check(config: dict) -> None:
    # Import here so the watcher can still show --status without anthropic installed
    from pipeline_classifier import classify_issues, enrich_issues  # type: ignore

    watcher_cfg = config["watcher"]
    base_url: str = watcher_cfg["base_url"]
    secret: str = watcher_cfg["admin_secret"]

    log.info("Fetching status snapshot...")
    try:
        status = fetch_status(base_url, secret)
    except Exception as exc:
        log.error(f"Status fetch failed: {exc}")
        return

    observations = load_observations()
    observations["last_check"] = datetime.now(timezone.utc).isoformat()

    # Self-heal pass: resolve stages whose latest run succeeded without calling Claude
    healed = _resolve_self_healed(status, observations)
    if healed:
        log.info(f"Self-healed: {', '.join(healed)}")

    log.info("Enriching issues...")
    issues = enrich_issues(status, observations)

    if not issues:
        log.info("No issues detected — pipeline healthy. Resolving any stale observations...")
        for obs_stage, obs_data in observations.get("stages", {}).items():
            if not obs_data.get("resolved_at"):
                log.info(f"  {obs_stage}: resolved (no longer flagged)")
                mark_resolved(observations, obs_stage)
        save_observations(observations)
        return

    log.info(f"Found {len(issues)} issue(s). Classifying with Claude...")
    try:
        classified = classify_issues(issues, status, observations)
    except Exception as exc:
        log.error(f"Classification failed: {exc}")
        save_observations(observations)
        return

    for result in classified:
        stage = result["stage"]
        action = result["action"]
        classification = result["classification"]
        reason = result["reason"]
        pr_branch: str | None = None
        outcome: str | None = None

        log.info(f"  {stage}: {classification} → {action}")
        log.info(f"    reason: {reason}")

        if action == "auto-fix":
            retry_result = auto_retry_stage(stage, config, observations)
            outcome = retry_result["outcome"]
            if retry_result["escalated"]:
                action = "pr-required"

        if action == "auto-cancel":
            ok = action_cancel_stage(base_url, secret, stage)
            outcome = "cancelled" if ok else "failed"
            log.info(f"    cancel-stage → {outcome}")

        if action == "pr-required":
            existing_branch = observations.get("stages", {}).get(stage, {}).get("pr_branch")
            if existing_branch:
                log.info(f"    PR already open: {existing_branch} — suppressing")
                action = "suppress"
                outcome = f"existing_pr:{existing_branch}"
            else:
                log.info(f"    Dispatching to Mimi orchestrator...")
                fix_result = draft_fix(result, config)
                pr_branch = fix_result["branch"] if fix_result["success"] else None
                outcome = f"branch:{fix_result['branch']}" if fix_result["success"] else f"dispatch_failed:{fix_result['error']}"
                if fix_result["success"]:
                    record_pr_outcome(observations, stage, fix_result["branch"])

        if action in ("suppress", "monitor"):
            log.info(f"    No action taken ({action})")

        update_observation(
            observations, stage, classification, action, reason, pr_branch, outcome
        )

    # Resolution pass: any previously-active stage not in this cycle's issues is healthy
    flagged_stages = {r["stage"] for r in classified}
    for obs_stage, obs_data in observations.get("stages", {}).items():
        if obs_data.get("resolved_at"):
            continue  # Already resolved
        if obs_stage not in flagged_stages:
            log.info(f"  {obs_stage}: no longer flagged — marking resolved")
            mark_resolved(observations, obs_stage)

    save_observations(observations)
    log.info("Check complete.")


# ── Status display ─────────────────────────────────────────────────────

def show_status() -> None:
    if not OBSERVATIONS_FILE.exists():
        print("No observations file found. Run the watcher first.")
        return

    obs = json.loads(OBSERVATIONS_FILE.read_text())
    last = obs.get("last_check") or "never"
    print(f"Last check : {last}")
    print()

    stages = obs.get("stages", {})
    if not stages:
        print("No issues tracked.")
    else:
        print(f"{'Stage':<35} {'Status':<12} {'Fails':<7} {'Action':<14} {'PR'}")
        print("-" * 85)
        for stage, data in stages.items():
            pr = data.get("pr_branch") or ""
            print(
                f"{stage:<35} "
                f"{data.get('status','?'):<12} "
                f"{data.get('consecutive_failures', 0):<7} "
                f"{data.get('last_action','?'):<14} "
                f"{pr}"
            )

    recent = obs.get("actions_log", [])[:8]
    if recent:
        print()
        print("Recent actions:")
        for entry in recent:
            ts = entry["ts"][:16]
            print(
                f"  {ts}  {entry['stage']:<30}  "
                f"{entry['action']:<12}  {entry.get('reason','')}"
            )


# ── Entry point ────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Mimi Pipeline Watcher")
    parser.add_argument("--once", action="store_true", help="Single check then exit")
    parser.add_argument("--status", action="store_true", help="Show observations then exit")
    parser.add_argument(
        "--interval", type=int, default=None,
        help="Poll interval in seconds (overrides config)"
    )
    parser.add_argument("--pr-outcome", metavar="STAGE", help="Record a PR outcome for a stage")
    parser.add_argument("--branch", help="PR branch name (required with --pr-outcome)")
    parser.add_argument("--merged", action="store_true", help="Mark PR as merged")
    parser.add_argument("--closed", action="store_true", help="Mark PR as closed without merge")
    parser.add_argument("--edits-required", action="store_true", help="Mark that edits were required")
    parser.add_argument("--notes", help="Optional notes about the PR outcome")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.pr_outcome:
        if not args.branch:
            parser.error("--branch is required when using --pr-outcome")
        obs = load_observations()
        merged: bool | None = True if args.merged else (False if args.closed else None)
        record_pr_outcome(obs, args.pr_outcome, args.branch, merged, args.edits_required or None, args.notes)
        save_observations(obs)
        status_str = "merged" if args.merged else ("closed" if args.closed else "pending")
        print(f"Recorded outcome for {args.pr_outcome} / {args.branch}: {status_str}")
        return

    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f)

    # Allow env var to override the YAML value (YAML can't interpolate env vars)
    env_secret = os.environ.get("ADMIN_STATUS_SECRET")
    if env_secret and "watcher" in config:
        config["watcher"]["admin_secret"] = env_secret

    if "watcher" not in config:
        log.error(
            "No 'watcher' section in config.yaml.\n"
            "Add:\n  watcher:\n    base_url: http://localhost:3000\n"
            "    admin_secret: <your secret>"
        )
        sys.exit(1)

    interval = args.interval or config["watcher"].get("interval_seconds", 900)

    if args.once:
        run_check(config)
        return

    log.info(f"Starting — interval {interval}s — workspace: {WORKSPACE}")
    while True:
        try:
            run_check(config)
        except Exception as exc:
            log.error(f"Unhandled error in check: {exc}", exc_info=True)
        log.info(f"Next check in {interval}s...")
        time.sleep(interval)


if __name__ == "__main__":
    main()
