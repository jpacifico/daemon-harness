"""Objective checker for ``native.tools.extract``."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable

from benchmarks.productivity_v1.fixtures.native_tools_extract import SCENARIO_ID


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
    source_root: Path,
    accessed_paths: Iterable[str],
    control: dict[str, Any],
    additional_baseline_files: Iterable[str] = (),
) -> dict[str, Any]:
    workspace = workspace.resolve()
    allowed_output = str(control["allowed_output"])
    baseline_files = set(control["baseline_files"]) | set(
        additional_baseline_files
    )
    current_files = set(_workspace_files(workspace))
    output_path = workspace / allowed_output
    output_exists = output_path.is_file()
    output_is_valid_json = False
    action_order_is_exact = False
    extraction_is_exact = False
    actual: Any = None
    if output_exists:
        try:
            actual = json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            actual = None
        else:
            output_is_valid_json = isinstance(actual, dict)
            actions = actual.get("actions") if output_is_valid_json else None
            if isinstance(actions, list):
                observed = [
                    (item.get("due_date"), item.get("id"))
                    for item in actions
                    if isinstance(item, dict)
                ]
                action_order_is_exact = (
                    len(observed) == len(actions) and observed == sorted(observed)
                )
            extraction_is_exact = actual == control["expected"]

    source_payloads_unchanged = all(
        (source_root / relative_path).is_file()
        and sha256((source_root / relative_path).read_bytes()).hexdigest()
        == expected_hash
        for relative_path, expected_hash in control["source_sha256"].items()
    )
    required_sources_accessed = set(control["required_source_paths"]) <= set(
        accessed_paths
    )
    checks = {
        "only_expected_output_changed": (
            current_files <= baseline_files | {allowed_output}
        ),
        "output_exists": output_exists,
        "output_is_valid_json": output_is_valid_json,
        "action_order_is_exact": action_order_is_exact,
        "extraction_is_exact": extraction_is_exact,
        "required_sources_accessed": required_sources_accessed,
        "source_payloads_unchanged": source_payloads_unchanged,
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
