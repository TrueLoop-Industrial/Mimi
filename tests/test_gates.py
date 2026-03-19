"""
Unit tests for gates.py

Covers:
  - GateConfig / GateResult dataclasses
  - Scope glob matching (gate skipped when no files match)
  - command gate: pass (exit 0), fail (exit 1), timeout, exception
  - golden_ratio gate: pass within tolerance, fail outside tolerance,
    metric pattern not found, command failure, no metric pattern fallback
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from gates import GateConfig, GateResult, GateRunner


# ── Helpers ───────────────────────────────────────────────────


def make_runner(gates: list[dict], tmp_path: Path, timeout: int = 30) -> GateRunner:
    return GateRunner(str(tmp_path), gates, timeout=timeout)


# ── Dataclass sanity ─────────────────────────────────────────


def test_gate_result_defaults() -> None:
    r = GateResult(name="test", passed=True, output="ok")
    assert r.expected is None
    assert r.actual is None


def test_gate_config_defaults() -> None:
    cfg = GateConfig(name="g", command="true")
    assert cfg.scope == ""
    assert cfg.type == "command"
    assert cfg.tolerance == 0.01


def test_parse_gate_minimal(tmp_path: Path) -> None:
    runner = make_runner([{"name": "G", "command": "true"}], tmp_path)
    assert len(runner.gates) == 1
    assert runner.gates[0].name == "G"
    assert runner.gates[0].type == "command"


def test_parse_gate_golden_ratio(tmp_path: Path) -> None:
    runner = make_runner([{
        "name": "R",
        "command": "echo 1.5",
        "type": "golden_ratio",
        "metric": r"([\d.]+)",
        "expected": 1.5,
        "tolerance": 0.05,
    }], tmp_path)
    cfg = runner.gates[0]
    assert cfg.type == "golden_ratio"
    assert cfg.expected == 1.5
    assert cfg.tolerance == 0.05


# ── Scope matching ────────────────────────────────────────────


def test_scope_matches_simple(tmp_path: Path) -> None:
    runner = make_runner([], tmp_path)
    assert runner._scope_matches("backend/**", ["backend/models.py"])
    assert runner._scope_matches("backend/**", ["other.py", "backend/views.py"])


def test_scope_not_matches(tmp_path: Path) -> None:
    runner = make_runner([], tmp_path)
    assert not runner._scope_matches("frontend/**", ["backend/models.py"])


def test_scope_empty_means_gate_always_runs(tmp_path: Path) -> None:
    """A gate with no scope should run regardless of changed files."""
    runner = make_runner([{"name": "Always", "command": "true"}], tmp_path)
    results = runner.run_gates(["any/file.py"])
    assert len(results) == 1


def test_scoped_gate_skipped_on_no_match(tmp_path: Path) -> None:
    runner = make_runner([{
        "name": "Frontend only",
        "command": "true",
        "scope": "frontend/**",
    }], tmp_path)
    results = runner.run_gates(["backend/models.py"])
    assert results == []


def test_scoped_gate_runs_on_match(tmp_path: Path) -> None:
    runner = make_runner([{
        "name": "Frontend only",
        "command": "true",
        "scope": "frontend/**",
    }], tmp_path)
    results = runner.run_gates(["frontend/App.tsx"])
    assert len(results) == 1
    assert results[0].passed


def test_no_changed_files_skips_scoped_gate(tmp_path: Path) -> None:
    runner = make_runner([{
        "name": "Scoped",
        "command": "true",
        "scope": "backend/**",
    }], tmp_path)
    results = runner.run_gates([])
    assert results == []


# ── command gates ─────────────────────────────────────────────


def test_command_gate_pass(tmp_path: Path) -> None:
    runner = make_runner([{"name": "Pass", "command": "true"}], tmp_path)
    results = runner.run_gates([])
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].name == "Pass"


def test_command_gate_fail(tmp_path: Path) -> None:
    runner = make_runner([{"name": "Fail", "command": "false"}], tmp_path)
    results = runner.run_gates([])
    assert results[0].passed is False


def test_command_gate_captures_output(tmp_path: Path) -> None:
    runner = make_runner([{"name": "Output", "command": "echo hello_marker"}], tmp_path)
    results = runner.run_gates([])
    assert "hello_marker" in results[0].output


def test_command_gate_timeout(tmp_path: Path) -> None:
    runner = make_runner([{"name": "Slow", "command": "sleep 30"}], tmp_path, timeout=1)
    results = runner.run_gates([])
    assert results[0].passed is False
    assert "timed out" in results[0].output.lower()


def test_multiple_gates_all_run(tmp_path: Path) -> None:
    runner = make_runner([
        {"name": "A", "command": "true"},
        {"name": "B", "command": "false"},
    ], tmp_path)
    results = runner.run_gates([])
    assert len(results) == 2
    assert results[0].passed is True
    assert results[1].passed is False


# ── golden_ratio gates ────────────────────────────────────────


def test_golden_ratio_pass(tmp_path: Path) -> None:
    runner = make_runner([{
        "name": "Ratio",
        "type": "golden_ratio",
        "command": "echo 'current_ratio: 1.5'",
        "metric": r"current_ratio:\s*([\d.]+)",
        "expected": 1.5,
        "tolerance": 0.05,
    }], tmp_path)
    results = runner.run_gates([])
    assert results[0].passed is True
    assert "1.5" in results[0].actual


def test_golden_ratio_within_tolerance(tmp_path: Path) -> None:
    runner = make_runner([{
        "name": "Ratio",
        "type": "golden_ratio",
        "command": "echo 'val: 1.52'",
        "metric": r"val:\s*([\d.]+)",
        "expected": 1.5,
        "tolerance": 0.05,
    }], tmp_path)
    results = runner.run_gates([])
    assert results[0].passed is True


def test_golden_ratio_outside_tolerance(tmp_path: Path) -> None:
    runner = make_runner([{
        "name": "Ratio",
        "type": "golden_ratio",
        "command": "echo 'val: 2.0'",
        "metric": r"val:\s*([\d.]+)",
        "expected": 1.5,
        "tolerance": 0.05,
    }], tmp_path)
    results = runner.run_gates([])
    assert results[0].passed is False
    assert results[0].expected is not None
    assert "1.5" in results[0].expected


def test_golden_ratio_metric_not_found(tmp_path: Path) -> None:
    runner = make_runner([{
        "name": "Ratio",
        "type": "golden_ratio",
        "command": "echo 'no numbers here'",
        "metric": r"current_ratio:\s*([\d.]+)",
        "expected": 1.5,
        "tolerance": 0.05,
    }], tmp_path)
    results = runner.run_gates([])
    assert results[0].passed is False
    assert results[0].actual is not None
    assert "not found" in results[0].actual


def test_golden_ratio_command_fails(tmp_path: Path) -> None:
    runner = make_runner([{
        "name": "Ratio",
        "type": "golden_ratio",
        "command": "false",
        "metric": r"([\d.]+)",
        "expected": 1.5,
        "tolerance": 0.05,
    }], tmp_path)
    results = runner.run_gates([])
    assert results[0].passed is False
    assert "command failed" in results[0].actual


def test_golden_ratio_no_metric_fallback_pass(tmp_path: Path) -> None:
    """When metric is empty, the last whitespace-separated token is parsed."""
    runner = make_runner([{
        "name": "Ratio",
        "type": "golden_ratio",
        "command": "echo '1.5'",
        "metric": "",
        "expected": 1.5,
        "tolerance": 0.05,
    }], tmp_path)
    results = runner.run_gates([])
    assert results[0].passed is True


def test_golden_ratio_no_metric_fallback_unparseable(tmp_path: Path) -> None:
    runner = make_runner([{
        "name": "Ratio",
        "type": "golden_ratio",
        "command": "echo 'not a number'",
        "metric": "",
        "expected": 1.5,
        "tolerance": 0.05,
    }], tmp_path)
    results = runner.run_gates([])
    assert results[0].passed is False
    assert results[0].actual is not None
    assert "could not parse" in results[0].actual


def test_golden_ratio_timeout(tmp_path: Path) -> None:
    runner = make_runner([{
        "name": "Ratio",
        "type": "golden_ratio",
        "command": "sleep 30",
        "metric": r"([\d.]+)",
        "expected": 1.5,
        "tolerance": 0.05,
    }], tmp_path, timeout=1)
    results = runner.run_gates([])
    assert results[0].passed is False
    assert "timed out" in results[0].output.lower()
