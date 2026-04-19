"""
Mimi Pipeline Classifier — issue detection and Claude-based triage.

Exports two functions consumed by pipeline_watcher.py:
    enrich_issues(status, observations) -> list[dict]
    classify_issues(issues, status, observations) -> list[dict]
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("mimi.classifier")

# Statuses that indicate a problem
_FAILURE_STATUSES = {"failed", "partial", "stale"}

# Stages that are noise / not worth classifying
_SKIP_STAGES = {"webhook-stream_processor"}

# Default action when Claude is unavailable or returns garbage
_SAFE_DEFAULT_ACTION = "monitor"


def enrich_issues(status: dict, observations: dict) -> list[dict]:
    """
    Scan the status snapshot and return a list of issue dicts for stages
    that need attention. Returns an empty list when the pipeline is healthy.

    Each issue dict:
        stage, latest_status, total_errors, companies_processed,
        duration_seconds, first_error, start_time,
        consecutive_failures, total_failures, first_seen_at,
        last_action, prior_classification
    """
    recent_runs: list[dict] = status.get("recent_runs", [])
    obs_stages: dict = observations.get("stages", {})

    # Group runs by stage, preserving desc order (most recent first)
    by_stage: dict[str, list[dict]] = {}
    for run in recent_runs:
        stage = run.get("workflow_name", "")
        if not stage or stage in _SKIP_STAGES:
            continue
        by_stage.setdefault(stage, []).append(run)

    issues: list[dict] = []
    for stage, runs in by_stage.items():
        latest = runs[0]
        exec_status = latest.get("execution_status", "")
        if exec_status not in _FAILURE_STATUSES:
            continue

        # Already resolved in observations — skip unless it just re-appeared
        stage_obs = obs_stages.get(stage, {})
        if stage_obs.get("resolved_at") and exec_status not in _FAILURE_STATUSES:
            continue

        issues.append({
            "stage": stage,
            "latest_status": exec_status,
            "total_errors": latest.get("total_errors") or 0,
            "companies_processed": latest.get("total_companies_processed") or 0,
            "duration_seconds": latest.get("duration_seconds"),
            "first_error": latest.get("first_error"),
            "start_time": latest.get("start_time"),
            "consecutive_failures": stage_obs.get("consecutive_failures", 0),
            "total_failures": stage_obs.get("total_failures", 0),
            "first_seen_at": stage_obs.get("first_seen_at"),
            "last_action": stage_obs.get("last_action"),
            "prior_classification": stage_obs.get("status"),
        })

    return issues


def classify_issues(
    issues: list[dict],
    status: dict,
    observations: dict,
) -> list[dict]:
    """
    Call Claude to classify each issue and decide on an action.

    Returns a list of result dicts:
        stage, classification, action, reason

    Classifications: NEW | REGRESSION | ONGOING
    Actions: auto-fix | auto-cancel | pr-required | monitor | suppress

    Falls back to (ONGOING, monitor) for each issue if Claude is unavailable.
    """
    if not issues:
        return []

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set — falling back to safe defaults")
        return _safe_defaults(issues)

    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed — falling back to safe defaults")
        return _safe_defaults(issues)

    client = anthropic.Anthropic(api_key=api_key)

    activity = status.get("activity", {})
    pipeline = status.get("pipeline", {})

    system = (
        "You are Mimi, an autonomous pipeline operations agent for Perpetua, "
        "a UK company intelligence platform. Your job is to classify pipeline "
        "stage failures and decide what action to take.\n\n"
        "CLASSIFICATION:\n"
        "  NEW        — first time we've seen this failure (or first time after a resolve)\n"
        "  REGRESSION — previously worked, now failing again\n"
        "  ONGOING    — already failing, consecutive failures accumulating\n\n"
        "ACTIONS (in order of escalation):\n"
        "  suppress     — known flap, not worth tracking\n"
        "  monitor      — one data point; watch another cycle before acting\n"
        "  auto-fix     — safe to re-trigger the stage via admin API (no code change)\n"
        "  auto-cancel  — stage is hung/stale; cancel it so it can be re-triggered\n"
        "  pr-required  — persistent failure needs a code fix; dispatch Mimi agent\n\n"
        "RULES:\n"
        "- 'stale' execution_status = process hung → prefer auto-cancel\n"
        "- 0 consecutive_failures = first sight → prefer monitor unless obviously hung\n"
        "- consecutive_failures >= 2 and last_action was auto-fix → escalate to pr-required\n"
        "- webhook-* stages are trigger noise; prefer monitor or suppress\n"
        "- Never auto-fix the same stage more than twice in a row\n"
        "- Respond ONLY with a JSON array, no prose, no markdown fences.\n"
    )

    issues_json = json.dumps(issues, indent=2, default=str)
    context_json = json.dumps({
        "successes_24h": activity.get("successes_24h"),
        "failures_24h": activity.get("failures_24h"),
        "companies_processed_24h": activity.get("companies_processed_24h"),
        "total_errors_24h": activity.get("total_errors_24h"),
        "total_companies": pipeline.get("total"),
    }, default=str)

    prompt = (
        f"Pipeline context (last 24h):\n{context_json}\n\n"
        f"Failing stages to classify:\n{issues_json}\n\n"
        "Return a JSON array where each element is:\n"
        '{"stage": "...", "classification": "...", "action": "...", "reason": "..."}\n'
        "reason must be one concise sentence explaining the decision."
    )

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown fences if Claude ignored instructions
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        classified: list[dict] = json.loads(raw)
        # Validate shape and fill gaps
        result = []
        known_stages = {i["stage"] for i in issues}
        for item in classified:
            stage = item.get("stage", "")
            if stage not in known_stages:
                continue
            result.append({
                "stage": stage,
                "classification": item.get("classification", "ONGOING"),
                "action": item.get("action", _SAFE_DEFAULT_ACTION),
                "reason": item.get("reason", "Classification unavailable."),
            })
        # Any stage Claude missed gets a safe default
        returned_stages = {r["stage"] for r in result}
        for issue in issues:
            if issue["stage"] not in returned_stages:
                result.append(_default_for(issue))
        return result

    except json.JSONDecodeError as exc:
        log.error(f"Claude returned non-JSON: {exc} — falling back to safe defaults")
        return _safe_defaults(issues)
    except Exception as exc:
        log.error(f"Classification API error: {exc} — falling back to safe defaults")
        return _safe_defaults(issues)


# ── Helpers ────────────────────────────────────────────────────────────

def _safe_defaults(issues: list[dict]) -> list[dict]:
    return [_default_for(i) for i in issues]


def _default_for(issue: dict) -> dict:
    stage = issue["stage"]
    exec_status = issue.get("latest_status", "failed")
    consec = issue.get("consecutive_failures", 0)

    # Deterministic fallback rules (no LLM)
    if exec_status == "stale":
        action = "auto-cancel"
        classification = "NEW" if consec == 0 else "ONGOING"
        reason = f"Stage is stale (hung process); cancelling so it can be re-triggered."
    elif consec == 0:
        action = "monitor"
        classification = "NEW"
        reason = "First failure observed; watching one more cycle before acting."
    elif consec >= 2 and issue.get("last_action") == "auto-fix":
        action = "pr-required"
        classification = "ONGOING"
        reason = f"Auto-fix did not resolve the issue after {consec} consecutive failures."
    elif consec >= 1:
        action = "auto-fix"
        classification = "ONGOING" if issue.get("prior_classification") else "NEW"
        reason = f"Stage has failed {consec} consecutive time(s); retrying via admin API."
    else:
        action = "monitor"
        classification = "ONGOING"
        reason = "Monitoring until pattern is clearer."

    return {
        "stage": stage,
        "classification": classification,
        "action": action,
        "reason": reason,
    }
