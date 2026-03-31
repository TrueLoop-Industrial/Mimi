"""
Pipeline issue classifier for Mimi watcher.

Enriches the admin status snapshot with git history and source context,
then uses Claude to classify each issue as NEW / REGRESSION / ONGOING
and decide the appropriate action.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

import anthropic
import yaml

MIMI_DIR = Path(__file__).parent
CONFIG_FILE = MIMI_DIR / "config.yaml"


def _load_workspace() -> str:
    config = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    workspace = config.get("workspace")
    if not workspace:
        raise ValueError("Missing workspace in config.yaml")
    return str(Path(workspace).expanduser())


WORKSPACE = _load_workspace()
PIPELINE_DIR = os.path.join(WORKSPACE, "backend", "pipeline")

# Module-level singleton — avoid re-initialising the client on every call
_anthropic_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client

# Stage code → Python filename
STAGE_FILES: dict[str, str] = {
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
    "17": "17_mine_pdf_accounts.py",
}

TERMINAL_STATUSES = {"failed", "partial", "stale", "cancelled"}

import re as _re

_SCHEMA_MISMATCH_RE = _re.compile(
    r'column "([^"]+)" of relation "([^"]+)" does not exist', _re.IGNORECASE
)


# ── Helpers ────────────────────────────────────────────────────────────

def _stage_code(workflow_name: str) -> str:
    """Extract stage code. '13_enrich_psc_api' → '13', 'webhook-13_...' → '13'."""
    name = workflow_name.lower()
    if name.startswith("webhook-"):
        name = name[8:]
    return name.split("_")[0]


def _stage_source(stage_code: str) -> Optional[str]:
    """First 80 lines of the stage Python file, or None if not found."""
    filename = STAGE_FILES.get(stage_code)
    if not filename:
        return None
    path = os.path.join(PIPELINE_DIR, filename)
    try:
        with open(path) as f:
            lines = f.readlines()[:80]
        return "".join(lines)
    except FileNotFoundError:
        return None


def _git_log(stage_code: str) -> str:
    """Last 5 commits touching the stage file."""
    filename = STAGE_FILES.get(stage_code)
    if not filename:
        return ""
    rel = f"backend/pipeline/{filename}"
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-5", "--", rel],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _error_hash(runs: list[dict]) -> str:
    """Stable identifier for the current failure pattern.

    Uses the first_error message text (normalised, first 60 chars) rather than
    error count — two different errors on the same stage should hash differently,
    and the same error with a different count should hash the same.
    Falls back to error count if no error text is available.
    """
    import re as _re2
    failed = [r for r in runs if r.get("execution_status") in TERMINAL_STATUSES]
    if not failed:
        return ""
    stage_name = failed[0].get("workflow_name", "")
    first_error = (failed[0].get("first_error") or "").strip()
    if first_error:
        normalized = _re2.sub(r"\s+", " ", first_error)[:60]
        return f"{stage_name}:{normalized}"
    total_errors = sum(r.get("total_errors") or 0 for r in failed)
    return f"{stage_name}:{total_errors}"


def _fingerprint_error(runs: list[dict]) -> dict:
    """Extract structured error metadata from recent failed runs.

    Reads the `first_error` field added by the admin/status API.
    Currently detects schema_mismatch (missing DB column) so Claude
    knows to write a migration rather than a code fix.
    """
    for run in runs:
        first_error: str = run.get("first_error") or ""
        if not first_error:
            continue
        m = _SCHEMA_MISMATCH_RE.search(first_error)
        if m:
            return {
                "error_type": "schema_mismatch",
                "missing_column": m.group(1),
                "affected_table": m.group(2),
                "raw": first_error[:200],
            }
    return {}


# ── Enrichment ─────────────────────────────────────────────────────────

def enrich_issues(status: dict, observations: dict) -> list[dict]:
    """
    Build an enriched issue list from the status snapshot.
    Includes only stages with recent failures and coverage problems below threshold.
    """
    issues: list[dict] = []
    recent_runs: list[dict] = status.get("recent_runs", [])
    pipeline: dict = status.get("pipeline", {})

    # Group runs by normalised stage name
    by_stage: dict[str, list[dict]] = {}
    for run in recent_runs:
        stage = run.get("workflow_name", "")
        by_stage.setdefault(stage, []).append(run)

    for stage, runs in by_stage.items():
        # Skip self-healed: if the most recent run succeeded, the stage is healthy.
        # _resolve_self_healed in the watcher already marks these resolved, but
        # this guard ensures they never reach Claude even if called standalone.
        if runs and runs[0].get("execution_status") == "success":
            continue

        failed = [r for r in runs if r.get("execution_status") in TERMINAL_STATUSES]
        if not failed:
            continue

        code = _stage_code(stage)
        obs = observations.get("stages", {}).get(stage, {})

        issues.append({
            "stage": stage,
            "stage_code": code,
            "recent_runs": runs[:5],
            "failure_count": len(failed),
            "consecutive_failures": obs.get("consecutive_failures", 0),
            "last_status": runs[0].get("execution_status") if runs else None,
            "total_errors_last_run": runs[0].get("total_errors") or 0 if runs else 0,
            "observation_status": obs.get("status", "NEW"),
            "pr_branch": obs.get("pr_branch"),
            "last_error_hash": obs.get("last_error_hash", ""),
            "current_error_hash": _error_hash(runs),
            "error_fingerprint": _fingerprint_error(runs),
            "source_snippet": _stage_source(code),
            "git_log": _git_log(code),
        })

    # Coverage gaps
    coverage_checks = [
        ("psc_coverage",      pipeline.get("has_psc_pct", 100),      15, "13_enrich_psc_api"),
        ("officers_coverage", pipeline.get("has_officers_pct", 100),  10, "16_enrich_officers"),
        ("charges_coverage",  pipeline.get("has_charges_pct", 100),   10, "15_enrich_charges"),
    ]
    for check_id, current_pct, threshold, stage in coverage_checks:
        if current_pct < threshold:
            obs = observations.get("stages", {}).get(stage, {})
            issues.append({
                "stage": stage,
                "type": "coverage",
                "check_id": check_id,
                "current_pct": current_pct,
                "threshold": threshold,
                "observation_status": obs.get("status", "NEW"),
                "last_action": obs.get("last_action"),
                "consecutive_failures": obs.get("consecutive_failures", 0),
            })

    return issues


# ── Classification prompt ──────────────────────────────────────────────

_PROMPT = """\
You are Mimi, a pipeline reliability agent for Perpetua — a UK private company \
intelligence platform. You monitor pipeline stages and decide whether to fix \
issues autonomously or escalate to a code PR.

## Current pipeline status
{status_summary}

## Issues to classify
{issues_json}

## Observation history (what you have seen before)
{observations_json}

---

For each issue classify:

**status** — one of:
- NEW: first occurrence (no prior observation, or was previously resolved)
- REGRESSION: was resolved, broke again
- ONGOING: same failure, still unresolved

**action** — one of:
- auto-fix: safe to trigger via API. Criteria: failed once or twice, last \
  success < 48h ago, error count reasonable, no structural change needed
- auto-cancel: stage is stuck (running > 30 min)
- pr-required: needs code or schema investigation. Criteria: 3+ consecutive \
  failures, never-seen error pattern, data integrity risk, touches fragile \
  files (xbrl_parsing.py, uk_ingestor.py)
- suppress: ONGOING and PR already open, or issue not actionable
- monitor: watch another cycle before acting

**schema_mismatch handling** — if an issue has \
`"error_fingerprint": {"error_type": "schema_mismatch"}`:
- action MUST be `pr-required` (not auto-fix — retrying will always fail)
- risk is always `high` (seeding stages blocked = entire pipeline stalled)
- pr_description MUST say: "Write a migration file in /migrations/ to add \
  column `<missing_column>` to `<affected_table>`. Check the highest-numbered \
  existing migration for naming convention and column type. Do not alter \
  application code — the column name in uk_ingestor.py is correct."

**risk** — low | medium | high

**pr_description** — only if action is pr-required: one paragraph describing \
what to investigate and what a good fix looks like

Return ONLY a JSON array with no markdown wrapping:
[
  {{
    "stage": "...",
    "classification": "NEW|REGRESSION|ONGOING",
    "action": "auto-fix|auto-cancel|pr-required|suppress|monitor",
    "risk": "low|medium|high",
    "reason": "one sentence",
    "pr_description": "paragraph or null"
  }}
]"""


def classify_issues(issues: list[dict], status: dict, observations: dict) -> list[dict]:
    """
    Call Claude to classify each issue and decide the action.
    Returns the parsed JSON list.
    """
    if not issues:
        return []

    client = _get_client()

    activity = status.get("activity", {})
    sat = status.get("saturation", {})
    status_summary = {
        "failures_24h": f"{activity.get('failures_24h', 0)}/{activity.get('total_runs_24h', 0)} runs",
        "api_utilization_pct": sat.get("api_utilization_pct", 0),
        "stale_healed": status.get("stale_count", 0),
        "active_runs": sat.get("active_runs", 0),
    }

    involved = {iss["stage"] for iss in issues}
    relevant_obs = {
        k: v for k, v in observations.get("stages", {}).items()
        if k in involved
    }

    prompt = _PROMPT.format(
        status_summary=json.dumps(status_summary, indent=2),
        issues_json=json.dumps(issues, indent=2, default=str),
        observations_json=json.dumps(relevant_obs, indent=2, default=str),
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown fences if present — handle nested/multiple fences robustly
    if "```" in raw:
        # Find the first JSON array inside any code fence
        import re
        match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
        if match:
            raw = match.group(1)
        else:
            # Fallback: extract first [...] block
            match2 = re.search(r"(\[.*\])", raw, re.DOTALL)
            if match2:
                raw = match2.group(1)

    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude returned invalid JSON: {exc}\nRaw response:\n{raw[:500]}") from exc
