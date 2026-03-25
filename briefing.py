"""
Mimi Morning Briefing Generator.

Reads the latest pipeline_observations.json and an overnight run summary
and generates a clean markdown briefing for HJ to read with coffee.

Usage (standalone):
    python3 briefing.py                 # generate from current observations
    python3 briefing.py --output /path  # write to specific file

Called programmatically from overnight.py:
    from briefing import generate_briefing
    path = generate_briefing(summary_dict)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────

MIMI_DIR = Path(__file__).parent
OBSERVATIONS_FILE = MIMI_DIR / "pipeline_observations.json"
REVIEWS_DIR = MIMI_DIR / "reviews"

# Pipeline stage display names
STAGE_LABELS: dict[str, str] = {
    "01_download_xbrl": "01 Download XBRL",
    "02_download_csv": "02 Download CSV",
    "03_download_psc": "03 Download PSC",
    "04_verify_xbrl": "04 Verify XBRL",
    "05_seed_db": "05 Seed DB",
    "06_enrich_psc_bulk": "06 Enrich PSC (bulk)",
    "07_mine_financials": "07 Mine Financials",
    "08_enrich_officers_bulk": "08 Enrich Officers (bulk)",
    "09_enrich_charges_bulk": "09 Enrich Charges (bulk)",
    "10_refresh_financials": "10 Refresh Financials",
    "11_audit_financials": "11 Audit Financials",
    "12_audit_quality": "12 Audit Quality",
    "13_enrich_psc_api": "13 Enrich PSC (API)",
    "14_audit_ownership": "14 Audit Ownership",
    "15_enrich_charges": "15 Enrich Charges",
    "16_enrich_officers": "16 Enrich Officers",
}

# Status-to-emoji mapping
CLASSIFICATION_EMOJI: dict[str, str] = {
    "NEW": "🔵",
    "REGRESSION": "🔴",
    "ONGOING": "🟡",
    "RESOLVED": "✅",
}

ACTION_EMOJI: dict[str, str] = {
    "auto-fix": "🔧",
    "auto-cancel": "⛔",
    "pr-required": "📋",
    "suppress": "🔇",
    "monitor": "👁",
}

# Sonnet pricing for cost estimates
SONNET_INPUT_COST_PER_M = 3.0
SONNET_OUTPUT_COST_PER_M = 15.0


# ── Helpers ────────────────────────────────────────────────────────────

def _time_ago(iso_str: Optional[str]) -> str:
    """Return a human-readable time delta from an ISO timestamp."""
    if not iso_str:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        diff = int((datetime.now(timezone.utc) - dt).total_seconds())
        if diff < 60:
            return f"{diff}s ago"
        if diff < 3600:
            return f"{diff // 60}m ago"
        if diff < 86400:
            return f"{diff // 3600}h ago"
        return f"{diff // 86400}d ago"
    except Exception:
        return iso_str[:16]


def _stage_label(stage: str) -> str:
    """Return a display label for a stage name."""
    return STAGE_LABELS.get(stage, stage)


def _health_indicator(stage_data: dict) -> str:
    """Return a red/amber/green emoji for a stage."""
    if stage_data.get("resolved_at"):
        return "🟢"
    failures = stage_data.get("consecutive_failures", 0)
    if failures >= 3:
        return "🔴"
    if failures >= 1:
        return "🟡"
    return "🟢"


def _redact_secrets(text: str) -> str:
    """Remove anything that looks like a secret from a string."""
    import re
    # Redact common secret patterns
    text = re.sub(r"secret=[^&\s\"']+", "secret=***", text)
    text = re.sub(r"(admin_secret|ADMIN_STATUS_SECRET)['\"]?\s*[:=]\s*['\"]?[^\s\"',}]+",
                  r"\1=***", text)
    # Redact long hex-like strings (likely tokens)
    text = re.sub(r"\b[0-9a-f]{32,}\b", "***", text, flags=re.IGNORECASE)
    return text


# ── Observations reader ────────────────────────────────────────────────

def load_observations() -> dict:
    """Load pipeline_observations.json or return empty structure."""
    if not OBSERVATIONS_FILE.exists():
        return {
            "version": 1,
            "last_check": None,
            "stages": {},
            "actions_log": [],
            "not_started": True,
        }
    try:
        return json.loads(OBSERVATIONS_FILE.read_text())
    except Exception as exc:
        return {
            "version": 1,
            "last_check": None,
            "stages": {},
            "actions_log": [],
            "read_error": str(exc),
        }


# ── Briefing sections ──────────────────────────────────────────────────

def _section_pipeline_health(observations: dict) -> list[str]:
    """Pipeline Health section — green/amber/red per known stage."""
    lines: list[str] = [
        "## Pipeline Health",
        "",
    ]

    stages = observations.get("stages", {})
    last_check = observations.get("last_check")

    if not last_check:
        lines.append("*Watcher has never run. Start with:*")
        lines.append("```bash")
        lines.append("python pipeline_watcher.py --once")
        lines.append("```")
        return lines

    lines.append(f"*Last check: {_time_ago(last_check)}*")
    lines.append("")

    if not stages:
        lines.append("✅ No issues tracked — all stages healthy or not yet seen.")
        return lines

    # Show all tracked stages with health indicator
    lines.append("| Stage | Health | Failures | Last Action | Last Seen |")
    lines.append("|-------|--------|----------|-------------|-----------|")

    for stage, data in sorted(stages.items()):
        indicator = _health_indicator(data)
        label = _stage_label(stage)
        failures = data.get("consecutive_failures", 0)
        last_action = data.get("last_action", "—")
        action_emoji = ACTION_EMOJI.get(last_action, "")
        last_seen = _time_ago(data.get("last_seen_at"))
        resolved = "✅ resolved" if data.get("resolved_at") else ""

        lines.append(
            f"| {indicator} {label} | "
            f"{'resolved' if resolved else ('failing' if failures > 0 else 'ok')} | "
            f"{failures if not resolved else '—'} | "
            f"{action_emoji} {last_action} | "
            f"{last_seen} |"
        )

    return lines


def _section_overnight_activity(summary: dict, observations: dict) -> list[str]:
    """Overnight Activity section — what Mimi did and estimated cost."""
    lines: list[str] = [
        "## Overnight Activity",
        "",
    ]

    mode = summary.get("mode", "observe")
    watcher_ran = summary.get("watcher_ran", False)
    server_reachable = summary.get("server_reachable", False)
    started_at = summary.get("started_at", "")
    completed_at = summary.get("completed_at", "")
    api_calls = summary.get("api_call_count", 0)

    lines.append(f"- **Mode:** {mode}")
    lines.append(f"- **Server reachable:** {'Yes ✅' if server_reachable else 'No ❌'}")
    lines.append(f"- **Watcher cycle ran:** {'Yes ✅' if watcher_ran else 'No ❌'}")

    if started_at:
        lines.append(f"- **Started:** {_time_ago(started_at)}")
    if completed_at:
        lines.append(f"- **Completed:** {_time_ago(completed_at)}")

    # Count actions from the log
    action_log = observations.get("actions_log", [])
    recent_actions = action_log[:20]  # actions from this cycle approximately

    auto_fixed = sum(1 for a in recent_actions if a.get("action") == "auto-fix")
    prs_opened = sum(1 for a in recent_actions if a.get("action") == "pr-required")
    monitored = sum(1 for a in recent_actions if a.get("action") == "monitor")

    lines.extend([
        "",
        f"**Actions taken this cycle:**",
        f"- Auto-fix triggers: {auto_fixed}",
        f"- PR-required dispatches: {prs_opened}",
        f"- Monitor (no action): {monitored}",
        f"- Claude API calls: ~{api_calls}",
    ])

    # Cost estimate (rough — based on classifier call only)
    # One classifier call processes all issues together, estimate ~2K tokens input + ~1K output
    estimated_input_tokens = api_calls * 2_000
    estimated_output_tokens = api_calls * 1_000
    cost_usd = (
        estimated_input_tokens / 1_000_000 * SONNET_INPUT_COST_PER_M
        + estimated_output_tokens / 1_000_000 * SONNET_OUTPUT_COST_PER_M
    )
    lines.append(
        f"- Estimated cost: ~${cost_usd:.4f} "
        f"(~{estimated_input_tokens:,} input / ~{estimated_output_tokens:,} output tokens, "
        f"Sonnet pricing)"
    )

    # Fix mode summary
    if mode == "fix":
        fix_attempts = summary.get("fix_attempts", 0)
        fix_results = summary.get("fix_results", [])
        successful = sum(1 for r in fix_results if r.get("success"))
        lines.extend([
            "",
            f"**Fix mode results:** {successful}/{fix_attempts} successful",
        ])
        for result in fix_results:
            icon = "✅" if result.get("success") else "❌"
            branch = result.get("branch", "—")
            lines.append(f"  {icon} `{result['stage']}` → branch: `{branch}`")

    return lines


def _section_issues_detected(summary: dict, observations: dict) -> list[str]:
    """Issues Detected section."""
    lines: list[str] = [
        "## Issues Detected",
        "",
    ]

    stages = observations.get("stages", {})
    active = {
        stage: data
        for stage, data in stages.items()
        if not data.get("resolved_at")
    }

    if not active:
        lines.append("✅ **No active issues.** All tracked stages are healthy or resolved.")
        return lines

    for stage, data in sorted(
        active.items(),
        key=lambda x: x[1].get("consecutive_failures", 0),
        reverse=True
    ):
        classification = data.get("status", "UNKNOWN")
        emoji = CLASSIFICATION_EMOJI.get(classification, "⚪")
        failures = data.get("consecutive_failures", 0)
        total = data.get("total_failures", 0)
        last_action = data.get("last_action", "—")
        first_seen = _time_ago(data.get("first_seen_at"))
        pr_branch = data.get("pr_branch")

        lines.extend([
            f"### {emoji} `{_stage_label(stage)}`",
            "",
            f"- **Classification:** {classification}",
            f"- **Consecutive failures:** {failures} (total: {total})",
            f"- **Last action:** {ACTION_EMOJI.get(last_action, '')} {last_action}",
            f"- **First seen:** {first_seen}",
        ])

        if pr_branch:
            lines.append(f"- **PR branch:** `{pr_branch}`")

        # Show investigation context if available
        investigated = summary.get("investigated_issues", [])
        for inv in investigated:
            if inv.get("stage") == stage:
                if inv.get("context", {}).get("file_path"):
                    lines.append(
                        f"- **Primary file:** `{inv['context']['file_path']}`"
                    )
                if inv.get("context", {}).get("git_log"):
                    lines.extend([
                        "",
                        "**Recent git history:**",
                        "```",
                        inv["context"]["git_log"],
                        "```",
                    ])
                if inv.get("suggestion"):
                    lines.extend([
                        "",
                        "**Suggested investigation:**",
                        f"> {inv['suggestion'].split(chr(10))[0]}",
                    ])
                break

        lines.append("")

    return lines


def _section_branches_for_review(observations: dict) -> list[str]:
    """Branches for Review section — ai/* branches to inspect."""
    lines: list[str] = [
        "## Branches for Review",
        "",
    ]

    stages = observations.get("stages", {})
    pr_branches = [
        (stage, data["pr_branch"])
        for stage, data in stages.items()
        if data.get("pr_branch") and not data.get("resolved_at")
    ]

    if not pr_branches:
        lines.append("*No branches pending review.*")
        return lines

    for stage, branch in pr_branches:
        lines.extend([
            f"### `{branch}`",
            f"For stage: `{_stage_label(stage)}`",
            "",
            "```bash",
            f"git diff main..{branch}   # review changes",
            f"git checkout main && git merge {branch}  # merge if happy",
            f"git branch -D {branch}   # clean up after merge",
            "```",
            "",
        ])

    return lines


def _section_human_required(summary: dict, observations: dict) -> list[str]:
    """Human Required section — issues Mimi couldn't handle."""
    lines: list[str] = [
        "## Human Required",
        "",
    ]

    items: list[str] = []

    # Server not reachable
    if not summary.get("server_reachable", True):
        items.append(
            "❌ **Pipeline server not reachable.** The Next.js server was offline "
            "during the overnight check. Start it with `cd frontend && npm run dev` "
            "and run `python3 overnight.py` again."
        )

    # Watcher didn't complete
    if summary.get("server_reachable") and not summary.get("watcher_ran"):
        items.append(
            "⚠️ **Watcher cycle did not complete.** Check logs for errors. "
            "The pipeline_watcher.py script may have crashed or timed out."
        )

    # Stages with very high failure counts (probable systemic issue)
    stages = observations.get("stages", {})
    for stage, data in stages.items():
        if data.get("resolved_at"):
            continue
        failures = data.get("consecutive_failures", 0)
        if failures >= 5 and data.get("last_action") not in ("pr-required",):
            items.append(
                f"🚨 **`{_stage_label(stage)}` has failed {failures} consecutive times.** "
                f"No PR has been opened. This may require manual investigation — "
                f"check the stage script and recent CH API / data source changes."
            )

    # Any generic error from the overnight run
    if summary.get("error"):
        items.append(f"❌ **Overnight run error:** {summary['error']}")

    if not items:
        lines.append("✅ Nothing requires immediate human attention.")
    else:
        lines.extend(items)

    return lines


def _section_recommendation(summary: dict, observations: dict) -> list[str]:
    """Recommendation section — what HJ should focus on today."""
    lines: list[str] = [
        "## Recommendation",
        "",
    ]

    stages = observations.get("stages", {})
    active = {
        stage: data
        for stage, data in stages.items()
        if not data.get("resolved_at")
    }

    regressions = [
        stage for stage, data in active.items()
        if data.get("status") == "REGRESSION"
    ]
    new_issues = [
        stage for stage, data in active.items()
        if data.get("status") == "NEW"
    ]
    pr_branches = [
        (stage, data["pr_branch"])
        for stage, data in active.items()
        if data.get("pr_branch")
    ]

    if not active:
        lines.extend([
            "**Pipeline is healthy.** No active issues.",
            "",
            "Today's focus options:",
            "- Review any open `ai/*` branches from previous Mimi runs",
            "- Progress tasks in `tasks.yaml` if the batch runner hasn't run recently",
            "- Check the punchlist for any unblocked items",
        ])
        return lines

    # Prioritize: regressions > new > ongoing
    if regressions:
        lines.extend([
            f"🔴 **Priority: {len(regressions)} regression(s) detected.**",
            "",
            "These stages were working before and broke again — likely a recent "
            "code change or external dependency shift. Check git log for recent "
            "commits to these files:",
            "",
        ])
        for stage in regressions:
            lines.append(f"- `{_stage_label(stage)}`")
        lines.append("")

    if new_issues:
        lines.extend([
            f"🔵 **{len(new_issues)} new issue(s) detected.**",
            "",
        ])
        for stage in new_issues:
            lines.append(f"- `{_stage_label(stage)}`")
        lines.append("")

    if pr_branches:
        lines.extend([
            f"📋 **{len(pr_branches)} branch(es) waiting for review:**",
            "",
        ])
        for stage, branch in pr_branches:
            lines.append(f"- `{branch}` (for {_stage_label(stage)})")
        lines.append("")
        lines.extend([
            "Review and merge if the fixes look correct, then re-run the "
            "watcher to confirm resolution.",
        ])

    if not regressions and not new_issues and not pr_branches:
        lines.extend([
            f"🟡 **{len(active)} ongoing issue(s) — no immediate action needed.**",
            "",
            "Mimi is monitoring these. If they persist beyond 5 cycles, "
            "escalate to manual investigation.",
        ])

    return lines


# ── Main generator ─────────────────────────────────────────────────────

def generate_briefing(
    summary: Optional[dict] = None,
    output_path: Optional[str] = None,
) -> str:
    """
    Generate the morning briefing markdown file.

    Args:
        summary: Run summary dict from overnight.py. If None, uses empty summary.
        output_path: Override output file path. Defaults to reviews/morning_briefing_YYYYMMDD.md

    Returns:
        Absolute path to the generated briefing file.
    """
    if summary is None:
        summary = {
            "mode": "standalone",
            "server_reachable": True,
            "watcher_ran": True,
            "api_call_count": 0,
            "active_issues": [],
            "investigated_issues": [],
            "fix_attempts": 0,
            "fix_results": [],
        }

    observations = load_observations()
    now = datetime.now()

    # Build output path
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    if output_path:
        dest = Path(output_path)
    else:
        date_str = now.strftime("%Y%m%d")
        dest = REVIEWS_DIR / f"morning_briefing_{date_str}.md"

    # Assemble sections
    header = [
        f"# Mimi Morning Briefing — {now.strftime('%A, %d %B %Y')}",
        "",
        f"*Generated {now.strftime('%H:%M')} | Mode: {summary.get('mode', 'standalone')}*",
        "",
        "---",
        "",
    ]

    sections = [
        header,
        _section_pipeline_health(observations),
        [""],
        ["---", ""],
        _section_overnight_activity(summary, observations),
        [""],
        ["---", ""],
        _section_issues_detected(summary, observations),
        ["---", ""],
        _section_branches_for_review(observations),
        ["---", ""],
        _section_human_required(summary, observations),
        [""],
        ["---", ""],
        _section_recommendation(summary, observations),
        [""],
        ["---", ""],
        [
            "*Briefing generated by Mimi — Perpetua's operational agent.*",
            "*All pipeline data sourced from `pipeline_observations.json`.*",
        ],
    ]

    all_lines: list[str] = []
    for section in sections:
        all_lines.extend(section)

    content = "\n".join(all_lines)

    # Safety: redact any secrets that may have leaked through
    content = _redact_secrets(content)

    dest.write_text(content)
    return str(dest)


# ── CLI entry point ────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Mimi morning briefing from current observations"
    )
    parser.add_argument(
        "--output",
        help="Output file path (default: reviews/morning_briefing_YYYYMMDD.md)",
        default=None,
    )
    args = parser.parse_args()

    path = generate_briefing(output_path=args.output)
    print(f"Briefing written to: {path}")


if __name__ == "__main__":
    main()
