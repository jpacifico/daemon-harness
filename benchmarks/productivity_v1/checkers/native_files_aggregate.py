"""Objective checker for ``native.files.aggregate``."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable

from benchmarks.productivity_v1.fixtures.native_files_aggregate import (
    SCENARIO_ID,
)


def _workspace_files(workspace: Path) -> list[str]:
    return sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file()
    )


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


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
    """Return deterministic checks without trusting the assistant response."""

    workspace = workspace.resolve()
    baseline_files = set(control["baseline_files"]) | set(
        additional_baseline_files
    )
    allowed_output = str(control["allowed_output"])
    current_files = set(_workspace_files(workspace))
    expected_files = baseline_files | {allowed_output}

    input_sources_unchanged = all(
        (workspace / relative_path).is_file()
        and _file_sha256(workspace / relative_path) == expected_hash
        for relative_path, expected_hash in control["input_sha256"].items()
    )
    only_expected_output_changed = current_files <= expected_files
    output_path = workspace / allowed_output
    output_exists = output_path.is_file()
    output_is_valid_json = False
    category_keys_alphabetical = False
    summary_is_exact = False
    actual: Any = None

    if output_exists:
        try:
            actual = json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            actual = None
        else:
            output_is_valid_json = isinstance(actual, dict)
            if output_is_valid_json:
                categories = actual.get("categories")
                if isinstance(categories, dict):
                    keys = list(categories)
                    category_keys_alphabetical = keys == sorted(keys)
                summary_is_exact = actual == control["expected"]

    checks = {
        "input_sources_unchanged": input_sources_unchanged,
        "only_expected_output_changed": only_expected_output_changed,
        "output_exists": output_exists,
        "output_is_valid_json": output_is_valid_json,
        "category_keys_alphabetical": category_keys_alphabetical,
        "summary_is_exact": summary_is_exact,
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
