"""Objective checker for ``continuity.decision``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from benchmarks.productivity_v1.fixtures.continuity_decision import SCENARIO_ID


def _workspace_files(workspace: Path) -> list[str]:
    return sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file()
    )


def load_control(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("scenario_id") != SCENARIO_ID:
        raise ValueError("fixture control has the wrong scenario_id")
    return value


def check_workspace(
    *,
    workspace: Path,
    control: dict[str, Any],
    additional_baseline_files: Iterable[str] = (),
) -> dict[str, Any]:
    workspace = workspace.resolve()
    baseline_files = set(control["baseline_files"]) | set(additional_baseline_files)
    allowed_output = str(control["allowed_output"])
    current_files = set(_workspace_files(workspace))
    expected_files = baseline_files | {allowed_output}
    output_path = workspace / allowed_output
    output_exists = output_path.is_file()
    output_is_valid_json = False
    decision_state_is_exact = False
    actual: Any = None

    if output_exists:
        try:
            actual = json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            actual = None
        else:
            output_is_valid_json = isinstance(actual, dict)
            decision_state_is_exact = actual == control["expected"]

    checks = {
        "initial_workspace_unchanged": baseline_files <= current_files,
        "only_expected_output_changed": current_files <= expected_files,
        "output_exists": output_exists,
        "output_is_valid_json": output_is_valid_json,
        "decision_state_is_exact": decision_state_is_exact,
    }
    return {
        "assessment_type": "deterministic",
        "authoritative": True,
        "scenario_id": SCENARIO_ID,
        "checks": checks,
        "passed": all(checks.values()),
        "actual": actual,
        "expected": control["expected"],
        "workspace_files": sorted(current_files),
    }
