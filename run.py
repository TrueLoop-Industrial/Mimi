#!/usr/bin/env python3
"""
Perpetua Task Runner v2 — batch AI tasks overnight, review branches in the morning.

Usage:
    python run.py                          # run all tasks in tasks.yaml
    python run.py --task fix-xbrl-edge     # run a single task by ID
    python run.py --dry-run                # show what would run
    python run.py --cleanup                # remove all worktrees
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

from orchestrator import TaskOrchestrator


def main():
    parser = argparse.ArgumentParser(
        description="Run AI tasks against your codebase overnight"
    )
    parser.add_argument("--config", default="config.yaml", help="Config file")
    parser.add_argument("--tasks", default="tasks.yaml", help="Tasks file")
    parser.add_argument("--task", action="append", dest="tasks_filter", metavar="TASK_ID", help="Run specific task(s) by ID (repeatable)")
    parser.add_argument("--output", default="reviews/", help="Review output directory")
    parser.add_argument("--dry-run", action="store_true", help="Show tasks without executing")
    parser.add_argument("--cleanup", action="store_true", help="Remove all worktree directories")

    args = parser.parse_args()

    # ── Cleanup mode ─────────────────────────────────────────
    if args.cleanup:
        config_path = Path(args.config)
        if not config_path.exists():
            print("✗ Config file not found")
            sys.exit(1)
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        workspace = Path(cfg["workspace"]).resolve()
        wt_root = Path(cfg.get("worktree_root", str(workspace.parent / ".mimi-worktrees")))
        if wt_root.exists():
            import subprocess
            shutil.rmtree(wt_root)
            # Prune AFTER rmtree so git sees the dirs are gone and clears its registry
            subprocess.run(["git", "worktree", "prune"], cwd=str(workspace), check=False)
            print(f"✓ Removed worktree directory: {wt_root}")
        else:
            print("Nothing to clean up.")
        sys.exit(0)

    # ── Load tasks ───────────────────────────────────────────
    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f"✗ Tasks file not found: {tasks_path}")
        sys.exit(1)

    with open(tasks_path) as f:
        all_tasks = yaml.safe_load(f).get("tasks", [])

    if not all_tasks:
        print("✗ No tasks defined")
        sys.exit(1)

    if args.tasks_filter:
        matched = [t for t in all_tasks if t["id"] in args.tasks_filter]
        missing = [tid for tid in args.tasks_filter if tid not in {t["id"] for t in all_tasks}]
        if missing:
            print(f"✗ Task(s) not found: {', '.join(missing)}. Available:")
            for t in all_tasks:
                print(f"  - {t['id']}")
            sys.exit(1)
        all_tasks = matched

    # ── Dry run ──────────────────────────────────────────────
    if args.dry_run:
        config_path = Path(args.config)
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            default_provider = cfg.get("provider", "claude")
        else:
            default_provider = "claude"

        print(f"\nDry run — {len(all_tasks)} task(s):\n")
        for t in all_tasks:
            provider = t.get("provider", default_provider)
            scope = t.get("scope", "(full repo)")
            template = t.get("template", "")
            print(f"  [{t['id']}]")
            desc = t.get("description", t.get("issue", t.get("component", "(see template)")))
            if desc:
                print(f"    {str(desc).strip()[:80]}")
            if template:
                print(f"    Template: {template}")
            print(f"    Provider: {provider} | Scope: {scope}")
            print(f"    Branch: ai/{t['id']}")
            print()
        sys.exit(0)

    # ── Run ──────────────────────────────────────────────────
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"✗ Config file not found: {config_path}")
        sys.exit(1)

    orchestrator = TaskOrchestrator(str(config_path))

    if args.tasks_filter:
        for task in all_tasks:
            orchestrator.run_task(task)
        review = orchestrator._generate_review()
    else:
        review = orchestrator.run_batch(str(tasks_path))

    if not review:
        print("\nNo results to report.")
        sys.exit(0)

    # ── Save review ──────────────────────────────────────────
    output_dir = Path(args.output)
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    review_path = output_dir / f"review_{timestamp}.md"
    review_path.write_text(review, encoding="utf-8")

    completed = sum(1 for r in orchestrator.results if r["status"] == "complete")
    gate_failed = sum(1 for r in orchestrator.results if r["status"] == "gate_failed")
    total = len(orchestrator.results)
    tokens = orchestrator.total_tokens

    print(f"\n{'━' * 60}")
    print(f"  Done: {completed}/{total} tasks complete" +
          (f" ({gate_failed} blocked by gates)" if gate_failed else ""))
    print(f"  Tokens: {tokens['input']:,} in / {tokens['output']:,} out")
    print(f"  Review: {review_path}")
    print()
    print("  Branches created:")
    for r in orchestrator.results:
        if r["status"] == "complete":
            emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(r.get("confidence", ""), "⚪")
            tests = "✅" if r.get("tests_passed") else "❌"
            print(f"    {emoji} {tests}  {r.get('branch', '?')}  [{r.get('provider', '?')}]")
        elif r["status"] == "gate_failed":
            print(f"    🚫     {r.get('branch', '?')}  [gate failure]")
        else:
            print(f"    ❌     {r['task_id']} — {r.get('error', r['status'])[:50]}")
    print(f"{'━' * 60}")


if __name__ == "__main__":
    main()
