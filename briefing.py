"""
Mimi Morning Briefing Generator.

Called by overnight.py after each run cycle:
    from briefing import generate_briefing
    path = generate_briefing(summary)

The summary dict shape (both observe and fix modes):
    mode, started_at, completed_at, watcher_ran, server_reachable,
    active_issues, investigated_issues, api_call_count, estimated_cost, error
    # fix mode only:
    fix_attempts, fix_results, budget_exceeded, estimated_cost_usd
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

_MIMI_DIR = Path(__file__).parent
_REVIEWS_DIR = _MIMI_DIR / "reviews"


def generate_briefing(summary: dict) -> Path:
    """
    Write a morning briefing markdown file and return its path.
    Never raises — all errors are caught and embedded in the output.
    """
    _REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    path = _REVIEWS_DIR / f"morning_briefing_{today}.md"

    try:
        content = _render(summary)
    except Exception as exc:
        content = f"# Morning Briefing — {today}\n\n**Briefing generation failed:** {exc}\n"

    path.write_text(content)
    return path


# ── Rendering ──────────────────────────────────────────────────────────

def _render(summary: dict) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    mode = summary.get("mode", "observe")
    server_ok = summary.get("server_reachable", False)
    watcher_ran = summary.get("watcher_ran", False)
    error = summary.get("error")
    active_issues = summary.get("active_issues", [])
    investigated = summary.get("investigated_issues", [])
    api_calls = summary.get("api_call_count", 0)
    cost = summary.get("estimated_cost", "$0.00")

    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────────
    lines += [
        f"# Mimi Morning Briefing — {now_str}",
        "",
        f"**Mode:** {mode}  |  "
        f"**Server:** {'✅ reachable' if server_ok else '❌ offline'}  |  "
        f"**Watcher:** {'ran' if watcher_ran else 'skipped'}",
        "",
    ]

    if error:
        lines += [f"> ⚠️ **{error}**", ""]

    # ── Pipeline health summary ────────────────────────────────────────
    if server_ok:
        if not active_issues:
            lines += ["## Pipeline Health", "", "✅ All monitored stages healthy.", ""]
        else:
            lines += [
                "## Pipeline Health",
                "",
                f"**{len(active_issues)} active issue(s):**",
                "",
            ]
            for item in active_issues:
                stage = item.get("stage", "?")
                data = item.get("data", {})
                classification = data.get("status", "?")
                consec = data.get("consecutive_failures", 0)
                action = data.get("last_action", "?")
                pr = data.get("pr_branch")
                pr_note = f" → `{pr}`" if pr else ""
                lines.append(
                    f"- **{stage}** — {classification}, "
                    f"{consec} consecutive failure(s), last action: `{action}`{pr_note}"
                )
            lines.append("")

    # ── Deep investigations (observe mode) ────────────────────────────
    if investigated:
        lines += ["## Investigations", ""]
        for inv in investigated:
            stage = inv.get("stage", "?")
            cls = inv.get("classification", "?")
            consec = inv.get("consecutive_failures", 0)
            suggestion = inv.get("suggestion", "")
            lines += [
                f"### {stage}",
                f"**Classification:** {cls}  |  **Consecutive failures:** {consec}",
                "",
            ]
            if suggestion:
                lines += [f"{suggestion}", ""]
            ctx = inv.get("context", {})
            snippets = ctx.get("snippets", [])
            if snippets:
                lines += ["**Relevant code:**", ""]
                for snip in snippets[:2]:
                    lines += [f"```python", snip[:500], "```", ""]

    # ── Fix results (fix mode) ─────────────────────────────────────────
    if mode == "fix":
        fix_results: list[dict] = summary.get("fix_results", [])
        fix_attempts = summary.get("fix_attempts", 0)
        budget_exceeded = summary.get("budget_exceeded", False)
        cost_usd = summary.get("estimated_cost_usd", 0.0)

        lines += ["## Fix Attempts", ""]
        if not fix_results:
            lines += ["No fix candidates dispatched.", ""]
        else:
            success_count = sum(1 for r in fix_results if r.get("success"))
            lines += [
                f"**{fix_attempts} dispatched** — "
                f"{success_count} branch(es) created, "
                f"${cost_usd:.4f} estimated cost",
                "",
            ]
            for r in fix_results:
                stage = r.get("stage", "?")
                success = r.get("success", False)
                branch = r.get("branch", "")
                err = r.get("error", "")
                if success:
                    lines.append(f"- ✅ **{stage}** → `{branch}`")
                else:
                    lines.append(f"- ❌ **{stage}** — {err or 'dispatch failed'}")
            lines.append("")

        if budget_exceeded:
            lines += [
                "> ⚠️ **Budget cap reached** — remaining candidates skipped.",
                "",
            ]

    # ── PR outcomes (recent) ──────────────────────────────────────────
    try:
        import json
        obs_file = _MIMI_DIR / "pipeline_observations.json"
        if obs_file.exists():
            obs = json.loads(obs_file.read_text())
            open_prs = [
                p for p in obs.get("pr_outcomes", [])
                if p.get("merged") is None and not p.get("closed_at")
            ]
            if open_prs:
                lines += ["## Open PRs", ""]
                for pr in open_prs:
                    lines.append(
                        f"- **{pr['stage']}** → `{pr['pr_branch']}` "
                        f"(opened {pr['opened_at'][:10]})"
                    )
                lines.append("")
    except Exception:
        pass

    # ── Cost / API usage ──────────────────────────────────────────────
    lines += [
        "## API Usage",
        "",
        f"**Classifier calls:** {api_calls}  |  **Estimated cost:** {cost}",
        "",
    ]

    # ── Footer ────────────────────────────────────────────────────────
    completed = summary.get("completed_at", "")
    if completed:
        lines += [f"---", f"*Generated at {completed[:19].replace('T', ' ')} UTC*", ""]

    return "\n".join(lines)
