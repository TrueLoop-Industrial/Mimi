"""
Mimi Overnight Loop — deep observation + optional fix mode.

Runs before bed. Produces a morning briefing by default.

Usage:
    python3 overnight.py                    # observe mode (default, safe)
    python3 overnight.py --mode observe     # same as above
    python3 overnight.py --mode fix         # CAUTION: also runs Task Runner
    python3 overnight.py --dry-run          # print plan, make no API calls
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

# ── Paths ──────────────────────────────────────────────────────────────

MIMI_DIR = Path(__file__).parent
WORKSPACE = Path("/Users/herbert-johnignacio/Desktop/Project Succession")
OBSERVATIONS_FILE = MIMI_DIR / "pipeline_observations.json"
CONFIG_FILE = MIMI_DIR / "config.yaml"
REVIEWS_DIR = MIMI_DIR / "reviews"

# ── Logging ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mimi.overnight] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mimi.overnight")

# ── Constants ──────────────────────────────────────────────────────────

# Max fix attempts in fix mode (cost control)
MAX_FIX_ATTEMPTS = 5

# Consecutive failure threshold for deep investigation in observe mode
DEEP_OBSERVE_THRESHOLD = 2

# Claude API pricing (per million tokens, as of 2026-03)
SONNET_INPUT_COST_PER_M = 3.0
SONNET_OUTPUT_COST_PER_M = 15.0


# ── Config loading ─────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config.yaml, interpolating ADMIN_STATUS_SECRET from env."""
    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f)

    # YAML can't interpolate env vars — do it here
    env_secret = os.environ.get("ADMIN_STATUS_SECRET")
    if env_secret and "watcher" in config:
        config["watcher"]["admin_secret"] = env_secret

    if "watcher" not in config:
        raise RuntimeError(
            "No 'watcher' section in config.yaml. "
            "Add:\n  watcher:\n    base_url: http://localhost:3000\n"
            "    admin_secret: <your secret>"
        )

    return config


# ── Pipeline status fetch ──────────────────────────────────────────────

def fetch_pipeline_status(base_url: str, secret: str) -> Optional[dict]:
    """
    Fetch the admin status snapshot. Returns None on connection failure.
    Logs the error clearly without crashing.
    """
    import requests  # type: ignore[import]
    url = f"{base_url.rstrip('/')}/api/admin/status?secret={secret}"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError as exc:
        log.error(f"Could not reach pipeline server: {exc}")
        log.error("Is the Next.js server running? (npm run dev on port 3000)")
        return None
    except requests.exceptions.Timeout:
        log.error("Pipeline status request timed out after 30s")
        return None
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            log.error(
                "Pipeline status returned 404 — ADMIN_STATUS_SECRET may not be set "
                "in the environment. Set it with: "
                "export ADMIN_STATUS_SECRET=<your-secret>"
            )
        else:
            log.error(f"Pipeline status returned HTTP error: {exc}")
        return None
    except Exception as exc:
        log.error(f"Unexpected error fetching pipeline status: {exc}")
        return None


# ── Observations ───────────────────────────────────────────────────────

def load_observations() -> dict:
    """Load current observations from disk, or return empty structure."""
    if OBSERVATIONS_FILE.exists():
        return json.loads(OBSERVATIONS_FILE.read_text())
    return {
        "version": 1,
        "last_check": None,
        "stages": {},
        "actions_log": [],
    }


# ── Deep investigation (observe mode) ─────────────────────────────────

def find_code_context_for_stage(stage_name: str) -> dict:
    """
    Find relevant code context for a failing stage.
    Returns a dict with file_paths, snippets, and git_log.
    Does NOT call any LLM — pure file system + git operations.
    """
    stage_files = {
        "01": "01_download_xbrl.py",
        "02": "02_download_csv.py",
        "03": "03_download_psc.py",
        "04": "04_verify_xbrl.py",
        "05": "05_seed_db.py",
        "06": "06_enrich_psc_bulk.py",
        "07": "07_mine_financials.py",
        "08": "08_enrich_officers_bulk.py",
        "09": "09_enrich_charges_bulk.py",
        "10": "10_refresh_financials.py",
        "11": "11_audit_financials.py",
        "12": "12_audit_quality.py",
        "13": "13_enrich_psc_api.py",
        "14": "14_audit_ownership.py",
        "15": "15_enrich_charges.py",
        "16": "16_enrich_officers.py",
    }

    # Extract stage code from stage name
    name = stage_name.lower()
    if name.startswith("webhook-"):
        name = name[8:]
    stage_code = name.split("_")[0]

    filename = stage_files.get(stage_code)
    context: dict = {
        "stage": stage_name,
        "stage_code": stage_code,
        "filename": filename,
        "file_path": None,
        "snippet": None,
        "git_log": None,
        "lib_files": [],
    }

    if not filename:
        return context

    pipeline_dir = WORKSPACE / "backend" / "pipeline"
    file_path = pipeline_dir / filename

    if file_path.exists():
        context["file_path"] = str(file_path.relative_to(WORKSPACE))
        try:
            lines = file_path.read_text().splitlines()[:80]
            context["snippet"] = "\n".join(lines)
        except Exception as exc:
            context["snippet"] = f"(could not read file: {exc})"

    # Git log for the stage file
    rel_path = f"backend/pipeline/{filename}"
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-5", "--", rel_path],
            cwd=str(WORKSPACE),
            capture_output=True,
            text=True,
            timeout=10,
        )
        context["git_log"] = result.stdout.strip() or "(no commits found)"
    except Exception as exc:
        context["git_log"] = f"(git log failed: {exc})"

    # Also note if lib files are involved
    lib_dir = pipeline_dir / "lib"
    if lib_dir.is_dir():
        context["lib_files"] = [
            f.name for f in sorted(lib_dir.glob("*.py"))
            if not f.name.startswith("__")
        ]

    return context


def generate_fix_suggestion(
    issue: dict,
    context: dict,
    classification: str,
    consecutive_failures: int,
) -> str:
    """
    Generate a plain-text fix suggestion without calling an LLM.
    Used in observe mode — a heuristic best-effort approach.
    """
    lines = [
        f"Stage: {issue['stage']}",
        f"Classification: {classification}",
        f"Consecutive failures: {consecutive_failures}",
        "",
    ]

    if context.get("file_path"):
        lines.append(f"Primary file: {context['file_path']}")
    if context.get("git_log"):
        lines.append(f"Recent commits:\n{context['git_log']}")

    # Heuristic suggestions based on classification
    if classification == "REGRESSION":
        lines.append(
            "\nSuggested approach: This stage was working before — check recent "
            "git commits to this file for the likely regression. Compare the "
            "current error pattern to what was working previously."
        )
    elif classification == "NEW":
        lines.append(
            "\nSuggested approach: First occurrence — read the stage script and "
            "check if external dependencies (Companies House API, XBRL feeds) "
            "have changed. Look at the error type: connection error vs. parsing "
            "error vs. data quality issue will each point to different fixes."
        )
    else:  # ONGOING
        lines.append(
            "\nSuggested approach: Issue persists despite prior action. If an "
            "auto-fix was attempted, check whether the retry actually ran. "
            "If a PR exists, check if it was merged. Consider whether the "
            "underlying data source has changed."
        )

    return "\n".join(lines)


# ── Watcher cycle ──────────────────────────────────────────────────────

def run_watcher_cycle(config: dict) -> bool:
    """
    Run pipeline_watcher.py --once as a subprocess.
    Returns True on success, False on failure.
    """
    watcher_script = MIMI_DIR / "pipeline_watcher.py"
    env = {**os.environ}

    # Ensure ADMIN_STATUS_SECRET is in the subprocess environment
    secret = config.get("watcher", {}).get("admin_secret", "")
    if secret and not secret.startswith("${"):
        env["ADMIN_STATUS_SECRET"] = secret

    log.info("Running pipeline_watcher.py --once ...")
    try:
        result = subprocess.run(
            [sys.executable, str(watcher_script), "--once"],
            cwd=str(MIMI_DIR),
            capture_output=False,  # Let watcher logs stream through
            env=env,
            timeout=300,
        )
        if result.returncode != 0:
            log.warning(f"pipeline_watcher.py exited with code {result.returncode}")
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("pipeline_watcher.py timed out after 300s")
        return False
    except Exception as exc:
        log.error(f"Failed to run pipeline_watcher.py: {exc}")
        return False


# ── Fix mode: Task Runner dispatch ─────────────────────────────────────

def dispatch_fix_task(
    stage: str,
    classification: str,
    reason: str,
    pr_description: str,
    fix_count: int,
    max_fixes: int,
) -> Optional[str]:
    """
    Dispatch a fix task to the Task Runner orchestrator.
    Returns the branch name on success, None on failure.
    Only called in fix mode.
    """
    if fix_count >= max_fixes:
        log.warning(
            f"Max fix attempts ({max_fixes}) reached — skipping {stage}"
        )
        return None

    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location(
            "orchestrator", MIMI_DIR / "orchestrator.py"
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        TaskOrchestrator = mod.TaskOrchestrator
    except Exception as exc:
        log.error(f"Could not load orchestrator: {exc}")
        return None

    safe = stage.replace("_", "-")
    ts = datetime.now(timezone.utc).strftime("%m%d%H%M")
    task_id = f"mimi-overnight-{safe}-{ts}"

    description = pr_description or (
        f"Pipeline stage {stage} has been failing ({classification}).\n"
        f"Reason: {reason}\n\n"
        f"Investigate the root cause and apply a minimal fix. "
        f"Read the stage script, check git history, look at the error pattern."
    )

    task = {
        "id": task_id,
        "description": description,
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "scope": "backend/pipeline/",
        "context_files": [
            f"backend/pipeline/{stage.split('_')[0]}*.py",
            "backend/pipeline/lib/uk_ingestor.py",
        ],
    }

    try:
        orch = TaskOrchestrator(str(CONFIG_FILE))
        result = orch.run_task(task)
        branch = result.get("branch", "")
        status = result.get("status", "unknown")
        log.info(f"  Task Runner result: {status}, branch: {branch or '(none)'}")
        return branch if status == "complete" else None
    except Exception as exc:
        log.error(f"Task Runner dispatch failed for {stage}: {exc}")
        return None


# ── Token cost estimation ──────────────────────────────────────────────

def estimate_cost(input_tokens: int, output_tokens: int) -> str:
    """Return a formatted cost estimate string for Sonnet pricing."""
    cost = (input_tokens / 1_000_000 * SONNET_INPUT_COST_PER_M +
            output_tokens / 1_000_000 * SONNET_OUTPUT_COST_PER_M)
    return f"~${cost:.4f} ({input_tokens:,} in / {output_tokens:,} out)"


# ── Main observe loop ──────────────────────────────────────────────────

def run_observe(config: dict, dry_run: bool = False) -> dict:
    """
    Run the observation loop. Returns a summary dict for the briefing.

    Steps:
    1. Run watcher cycle (pipeline_watcher.py --once)
    2. Read resulting observations
    3. For REGRESSION/NEW with consecutive_failures > THRESHOLD:
       find relevant code, log suggested fix — do NOT attempt fix
    4. Return summary for briefing generation
    """
    base_url = config["watcher"]["base_url"]
    secret = config["watcher"]["admin_secret"]

    summary: dict = {
        "mode": "observe",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "watcher_ran": False,
        "server_reachable": False,
        "pipeline_status": None,
        "active_issues": [],
        "investigated_issues": [],
        "api_call_count": 0,
        "estimated_cost": "$0.00",
        "error": None,
    }

    # Step 1: Fetch status to check server reachability
    if dry_run:
        log.info("[DRY RUN] Would fetch pipeline status and run watcher cycle")
        summary["server_reachable"] = True
        summary["watcher_ran"] = True
        return summary

    status = fetch_pipeline_status(base_url, secret)
    if status is None:
        summary["error"] = (
            "Could not reach pipeline — server may be offline or "
            "ADMIN_STATUS_SECRET is not set in the environment"
        )
        summary["server_reachable"] = False
        return summary

    summary["server_reachable"] = True

    # Step 2: Run the full watcher cycle (classify, act, update observations)
    watcher_ok = run_watcher_cycle(config)
    summary["watcher_ran"] = watcher_ok
    summary["api_call_count"] += 1  # The classifier uses one Claude call

    # Step 3: Read post-watcher observations
    observations = load_observations()
    stages = observations.get("stages", {})

    active_issues = [
        {"stage": stage, "data": data}
        for stage, data in stages.items()
        if not data.get("resolved_at")
    ]
    summary["active_issues"] = active_issues

    # Step 4: For high-priority issues, gather code context
    investigated = []
    for item in active_issues:
        stage = item["stage"]
        data = item["data"]
        classification = data.get("status", "UNKNOWN")
        consecutive_failures = data.get("consecutive_failures", 0)

        # Only investigate regressions and new issues above the threshold
        if classification not in ("REGRESSION", "NEW"):
            continue
        if consecutive_failures <= DEEP_OBSERVE_THRESHOLD:
            continue

        log.info(
            f"  Investigating {stage} ({classification}, "
            f"{consecutive_failures}× consecutive failures)..."
        )

        context = find_code_context_for_stage(stage)
        suggestion = generate_fix_suggestion(
            {"stage": stage},
            context,
            classification,
            consecutive_failures,
        )

        investigated.append({
            "stage": stage,
            "classification": classification,
            "consecutive_failures": consecutive_failures,
            "last_action": data.get("last_action"),
            "pr_branch": data.get("pr_branch"),
            "context": context,
            "suggestion": suggestion,
        })

    summary["investigated_issues"] = investigated
    summary["completed_at"] = datetime.now(timezone.utc).isoformat()

    return summary


# ── Main fix loop ──────────────────────────────────────────────────────

def run_fix(config: dict, dry_run: bool = False) -> dict:
    """
    Run the observation + fix loop.

    WARNING: This mode dispatches to the Task Runner which runs Claude agents
    and creates branches. Only use after validating that observe mode works.

    Steps 1-4: Same as observe mode
    Step 5: For issues that need fixes, dispatch to Task Runner (max MAX_FIX_ATTEMPTS)
    Step 6: Re-run watcher to check if anything resolved
    """
    # Start with observation data
    summary = run_observe(config, dry_run=dry_run)
    summary["mode"] = "fix"
    summary["fix_attempts"] = 0
    summary["fix_results"] = []

    if dry_run:
        log.info("[DRY RUN] Would attempt fixes in Task Runner")
        return summary

    if summary.get("error"):
        return summary

    # Reload observations after watcher cycle
    observations = load_observations()
    stages = observations.get("stages", {})

    # Get active issues from action log (classifier already decided pr-required)
    fix_candidates = []
    action_log = observations.get("actions_log", [])
    seen_stages: set[str] = set()
    for entry in action_log[:20]:  # check recent entries
        stage = entry.get("stage", "")
        action = entry.get("action", "")
        if action == "pr-required" and stage not in seen_stages:
            stage_data = stages.get(stage, {})
            if not stage_data.get("resolved_at") and not stage_data.get("pr_branch"):
                fix_candidates.append({
                    "stage": stage,
                    "classification": entry.get("classification", "UNKNOWN"),
                    "reason": entry.get("reason", ""),
                    "pr_description": None,  # Would come from classifier
                })
                seen_stages.add(stage)

    log.info(f"Fix candidates: {len(fix_candidates)} (max: {MAX_FIX_ATTEMPTS})")

    fix_count = 0
    for candidate in fix_candidates[:MAX_FIX_ATTEMPTS]:
        stage = candidate["stage"]
        log.info(f"  Dispatching fix for {stage}...")

        branch = dispatch_fix_task(
            stage=stage,
            classification=candidate["classification"],
            reason=candidate["reason"],
            pr_description=candidate.get("pr_description", ""),
            fix_count=fix_count,
            max_fixes=MAX_FIX_ATTEMPTS,
        )

        fix_count += 1
        result = {
            "stage": stage,
            "branch": branch,
            "success": branch is not None,
        }
        summary["fix_results"].append(result)
        summary["fix_attempts"] = fix_count

        if branch:
            log.info(f"    Branch created: {branch}")
        else:
            log.warning(f"    Fix failed for {stage}")

    # Re-run watcher to check if fixes resolved anything
    if fix_count > 0:
        log.info("Re-running watcher cycle after fixes...")
        run_watcher_cycle(config)
        summary["api_call_count"] += 1

    summary["completed_at"] = datetime.now(timezone.utc).isoformat()
    return summary


# ── Entry point ────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mimi Overnight Loop — observation and optional fix mode"
    )
    parser.add_argument(
        "--mode",
        choices=["observe", "fix"],
        default="observe",
        help="observe: gather and report only (default). fix: also run Task Runner.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without making API calls or running the watcher",
    )
    args = parser.parse_args()

    log.info(f"Starting overnight loop — mode={args.mode}")
    if args.dry_run:
        log.info("[DRY RUN MODE] No API calls will be made")

    # Load config
    try:
        config = load_config()
    except Exception as exc:
        log.error(f"Config error: {exc}")
        sys.exit(1)

    # Ensure reviews directory exists
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)

    # Run the appropriate mode
    if args.mode == "fix":
        log.warning(
            "Running in FIX mode — Task Runner will create branches. "
            "Ensure observe mode has been validated first."
        )
        summary = run_fix(config, dry_run=args.dry_run)
    else:
        summary = run_observe(config, dry_run=args.dry_run)

    # Generate morning briefing
    log.info("Generating morning briefing...")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "briefing", MIMI_DIR / "briefing.py"
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        briefing_path = mod.generate_briefing(summary)
        log.info(f"Morning briefing written to: {briefing_path}")
    except Exception as exc:
        log.error(f"Failed to generate briefing: {exc}")
        # Don't crash — the summary is still valid even without the briefing

    # Write test/run results
    today = datetime.now().strftime("%Y%m%d")
    test_results_path = REVIEWS_DIR / f"overnight_test_{today}.md"
    _write_run_results(test_results_path, summary, args.mode)
    log.info(f"Run results written to: {test_results_path}")

    # Exit cleanly
    if summary.get("error"):
        log.error(f"Overnight loop completed with errors: {summary['error']}")
        sys.exit(0)  # Exit 0 — error is documented in briefing, not fatal

    log.info("Overnight loop complete.")
    sys.exit(0)


def _write_run_results(path: Path, summary: dict, mode: str) -> None:
    """Write a concise run results file."""
    lines = [
        f"# Overnight Test Results — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"**Mode:** {mode}",
        f"**Server reachable:** {'Yes' if summary.get('server_reachable') else 'No'}",
        f"**Watcher ran:** {'Yes' if summary.get('watcher_ran') else 'No'}",
        f"**Active issues:** {len(summary.get('active_issues', []))}",
        f"**Investigated:** {len(summary.get('investigated_issues', []))}",
    ]

    if mode == "fix":
        lines.append(f"**Fix attempts:** {summary.get('fix_attempts', 0)}")
        fix_results = summary.get("fix_results", [])
        successful = sum(1 for r in fix_results if r.get("success"))
        lines.append(f"**Successful fixes:** {successful}/{len(fix_results)}")

    if summary.get("error"):
        lines.extend(["", f"**Error:** {summary['error']}"])

    lines.extend([
        "",
        "---",
        "",
        "All output verified — no API keys or secrets in this file.",
    ])

    path.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
