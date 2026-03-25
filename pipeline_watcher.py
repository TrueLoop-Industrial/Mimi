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

import requests
import yaml

# ── Paths ──────────────────────────────────────────────────────────────

MIMI_DIR = Path(__file__).parent
WORKSPACE = Path("/Users/herbert-johnignacio/Desktop/Project Succession")
OBSERVATIONS_FILE = MIMI_DIR / "pipeline_observations.json"
CONFIG_FILE = MIMI_DIR / "config.yaml"

# ── Logging ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mimi] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mimi.watcher")


# ── Observations state ─────────────────────────────────────────────────

def load_observations() -> dict:
    if OBSERVATIONS_FILE.exists():
        return json.loads(OBSERVATIONS_FILE.read_text())
    return {"version": 1, "last_check": None, "stages": {}, "actions_log": []}


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
        obs["stages"][stage]["pr_branch"] = None


# ── Status fetch ───────────────────────────────────────────────────────

def fetch_status(base_url: str, secret: str) -> dict:
    url = f"{base_url.rstrip('/')}/api/admin/status?secret={secret}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── Admin API actions ──────────────────────────────────────────────────

def _post(base_url: str, secret: str, path: str, body: dict, timeout: int = 30) -> bool:
    url = f"{base_url.rstrip('/')}{path}?secret={secret}"
    try:
        resp = requests.post(url, json=body, timeout=timeout)
        return resp.ok
    except Exception as exc:
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


# ── Mimi orchestrator dispatch ─────────────────────────────────────────

def dispatch_to_mimi(result: dict) -> str:
    """
    Dispatch a pr-required issue to Mimi's TaskOrchestrator.
    Creates an isolated worktree, runs Claude agent, returns branch name.
    Returns empty string on failure.
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
        return ""

    stage = result["stage"]
    safe = stage.replace("_", "-")
    ts = datetime.now(timezone.utc).strftime("%m%d%H%M")
    task_id = f"mimi-fix-{safe}-{ts}"

    description = result.get("pr_description") or (
        f"Pipeline stage {stage} has been failing. "
        f"Investigate the root cause and apply a minimal fix. "
        f"Read the stage script, check git history, look at the error pattern."
    )

    task = {
        "id": task_id,
        "description": description,
        "provider": "claude",
        "scope": "backend/pipeline/",
        "context_files": [
            f"backend/pipeline/{stage.split('_')[0]}*.py",
            "backend/pipeline/lib/uk_ingestor.py",
        ],
    }

    try:
        orch = TaskOrchestrator(str(CONFIG_FILE))
        task_result = orch.run_task(task)
        branch = task_result.get("branch", "")
        log.info(f"    Orchestrator finished — branch: {branch or '(none)'}")
        return branch
    except Exception as exc:
        log.error(f"Orchestrator dispatch failed: {exc}")
        return ""


# ── Main check ─────────────────────────────────────────────────────────

def run_check(config: dict) -> None:
    # Import here so the watcher can still show --status without anthropic installed
    from pipeline_classifier import classify_issues, enrich_issues  # type: ignore

    watcher_cfg = config["watcher"]
    base_url: str = watcher_cfg["base_url"]
    secret: str = watcher_cfg["admin_secret"]
    max_auto_retries: int = watcher_cfg.get("auto_fix_max_retries", 2)

    log.info("Fetching status snapshot...")
    try:
        status = fetch_status(base_url, secret)
    except Exception as exc:
        log.error(f"Status fetch failed: {exc}")
        return

    observations = load_observations()
    observations["last_check"] = datetime.now(timezone.utc).isoformat()

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
            stage_obs = observations["stages"].get(stage, {})
            retries = stage_obs.get("auto_retried_count", 0)
            if retries >= max_auto_retries:
                log.warning(f"    Max auto-retries ({max_auto_retries}) reached — escalating to pr-required")
                action = "pr-required"
            else:
                ok = action_run_stage(base_url, secret, stage)
                outcome = "triggered" if ok else "failed"
                log.info(f"    run-stage → {outcome}")
                if ok:
                    observations["stages"].setdefault(stage, {})
                    observations["stages"][stage]["auto_retried_count"] = retries + 1

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
                pr_branch = dispatch_to_mimi(result)
                outcome = f"branch:{pr_branch}" if pr_branch else "dispatch_failed"

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
    args = parser.parse_args()

    if args.status:
        show_status()
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
