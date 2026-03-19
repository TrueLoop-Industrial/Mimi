"""
Validation gates — configurable checks that run AFTER the agent's changes
but BEFORE the commit. If any gate fails, the task is marked as failed
and the branch is not committed.

Gates are defined in config.yaml under `gates:`. Each gate has:
  - name: human-readable label
  - command: shell command to run (exit 0 = pass)
  - scope: optional glob — only run this gate if changed files match

Built-in gate types:
  - command gates: run any shell command, check exit code
  - golden_ratio gates: assert specific numeric values in the output
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from fnmatch import fnmatch


@dataclass
class GateResult:
    name: str
    passed: bool
    output: str
    expected: str | None = None
    actual: str | None = None


@dataclass
class GateConfig:
    name: str
    command: str
    scope: str = ""         # glob pattern, e.g. "backend/pipeline/**"
    type: str = "command"    # "command" or "golden_ratio"
    # For golden_ratio type:
    metric: str = ""         # regex to extract value from command output
    expected: float = 0.0
    tolerance: float = 0.01  # acceptable drift


class GateRunner:
    """Runs validation gates against the workspace."""

    def __init__(self, workspace: str, gates: list[dict], timeout: int = 120) -> None:
        self.workspace = Path(workspace)
        self.timeout = timeout
        self.gates = [self._parse_gate(g) for g in gates]

    def _parse_gate(self, raw: dict) -> GateConfig:
        return GateConfig(
            name=raw["name"],
            command=raw["command"],
            scope=raw.get("scope", ""),
            type=raw.get("type", "command"),
            metric=raw.get("metric", ""),
            expected=float(raw.get("expected", 0)),
            tolerance=float(raw.get("tolerance", 0.01)),
        )

    def run_gates(self, changed_files: list[str]) -> list[GateResult]:
        """Run gates applicable to changed_files (respects scope filtering)."""
        results = []
        for gate in self.gates:
            if gate.scope and not self._scope_matches(gate.scope, changed_files):
                continue
            if gate.type == "golden_ratio":
                results.append(self._run_golden_ratio_gate(gate))
            else:
                results.append(self._run_command_gate(gate))
        return results

    def run_all_gates(self) -> list[GateResult]:
        """Run every configured gate regardless of scope. Used for baseline capture."""
        results = []
        for gate in self.gates:
            if gate.type == "golden_ratio":
                results.append(self._run_golden_ratio_gate(gate))
            else:
                results.append(self._run_command_gate(gate))
        return results

    def _scope_matches(self, scope: str, changed_files: list[str]) -> bool:
        """Check if any changed file matches the scope glob."""
        for f in changed_files:
            if fnmatch(f, scope):
                return True
        return False

    def _run_command_gate(self, gate: GateConfig) -> GateResult:
        """Run a command gate. Pass if exit code is 0."""
        try:
            result = subprocess.run(
                gate.command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(self.workspace),
                timeout=self.timeout,
            )
            output = (result.stdout + result.stderr).strip()
            if len(output) > 5000:
                output = output[:2500] + "\n...(truncated)...\n" + output[-1000:]

            return GateResult(
                name=gate.name,
                passed=result.returncode == 0,
                output=output,
            )
        except subprocess.TimeoutExpired:
            return GateResult(
                name=gate.name,
                passed=False,
                output=f"Gate timed out after {self.timeout}s",
            )
        except Exception as e:
            return GateResult(
                name=gate.name,
                passed=False,
                output=f"Gate error: {e}",
            )

    def _run_golden_ratio_gate(self, gate: GateConfig) -> GateResult:
        """
        Run a command, extract a numeric metric from stdout,
        and assert it's within tolerance of the expected value.

        Example config:
            name: "Current ratio check"
            type: golden_ratio
            command: "python -c \"from backend.pipeline.lib.ratios import check; print(check())\""
            metric: "current_ratio:\\s*([\\d.]+)"
            expected: 1.5
            tolerance: 0.05
        """
        try:
            result = subprocess.run(
                gate.command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(self.workspace),
                timeout=self.timeout,
            )
            output = (result.stdout + result.stderr).strip()

            if result.returncode != 0:
                return GateResult(
                    name=gate.name,
                    passed=False,
                    output=output,
                    expected=str(gate.expected),
                    actual="(command failed)",
                )

            # Extract metric value
            if gate.metric:
                match = re.search(gate.metric, output)
                if not match:
                    return GateResult(
                        name=gate.name,
                        passed=False,
                        output=output,
                        expected=str(gate.expected),
                        actual="(metric pattern not found in output)",
                    )
                actual_val = float(match.group(1))
            else:
                # If no metric pattern, try to parse the entire output as a number
                try:
                    actual_val = float(output.strip().split()[-1])
                except (ValueError, IndexError):
                    return GateResult(
                        name=gate.name,
                        passed=False,
                        output=output,
                        expected=str(gate.expected),
                        actual="(could not parse numeric value from output)",
                    )

            drift = abs(actual_val - gate.expected)
            passed = drift <= gate.tolerance

            return GateResult(
                name=gate.name,
                passed=passed,
                output=output,
                expected=f"{gate.expected} (±{gate.tolerance})",
                actual=f"{actual_val} (drift: {drift:.4f})",
            )

        except subprocess.TimeoutExpired:
            return GateResult(
                name=gate.name,
                passed=False,
                output=f"Gate timed out after {self.timeout}s",
            )
        except Exception as e:
            return GateResult(
                name=gate.name,
                passed=False,
                output=f"Gate error: {e}",
            )
