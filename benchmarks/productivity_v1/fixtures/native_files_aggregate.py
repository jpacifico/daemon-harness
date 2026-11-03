"""Deterministic fixture for ``native.files.aggregate``.

The generator deliberately avoids ``random`` so the same public seed produces
byte-identical CSV files across supported Python versions.
"""

from __future__ import annotations

import csv
from hashlib import sha256
import io
import json
from pathlib import Path
from typing import Any

from benchmarks.productivity_v1 import LANGUAGE, PROTOCOL_VERSION


SCENARIO_ID = "native.files.aggregate"
OFFICIAL_SEEDS = (1101, 1102, 1103, 1104, 1105, 1106)
CATEGORIES = (
    "books",
    "games",
    "music",
    "office",
    "plants",
    "tools",
)
WAREHOUSES = ("north", "south")
OUTPUT_PATH = "out/stock_summary.json"
FIXTURE_VARIANTS = ("left", "right")


def _stable_number(
    seed: int,
    variant: str,
    label: str,
    low: int,
    high: int,
) -> int:
    if low > high:
        raise ValueError("low must not exceed high")
    payload = f"{SCENARIO_ID}:{seed}:{variant}:{label}".encode("utf-8")
    value = int.from_bytes(sha256(payload).digest()[:8], "big")
    return low + value % (high - low + 1)


def _selected_categories(seed: int) -> tuple[str, ...]:
    ranked = sorted(
        CATEGORIES,
        key=lambda category: sha256(
            f"{SCENARIO_ID}:{seed}:category:{category}".encode("utf-8")
        ).hexdigest(),
    )
    return tuple(sorted(ranked[:4]))


def _csv_text(rows: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=("sku", "category", "units", "unit_price_cents"),
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_fixture(seed: int, variant: str) -> dict[str, Any]:
    """Return the English prompt, workspace files, and private control data."""

    if seed not in OFFICIAL_SEEDS:
        raise ValueError(f"seed {seed} is not registered for {SCENARIO_ID}")
    if variant not in FIXTURE_VARIANTS:
        raise ValueError(f"unknown fixture variant: {variant}")

    categories = _selected_categories(seed)
    workspace_files: dict[str, str] = {}
    category_totals = {
        category: {"units": 0, "value_cents": 0}
        for category in categories
    }
    line_count = 0

    for warehouse in WAREHOUSES:
        rows: list[dict[str, Any]] = []
        for index, category in enumerate(categories, start=1):
            units = _stable_number(
                seed,
                variant,
                f"{warehouse}:{category}:units",
                1,
                9,
            )
            unit_price = _stable_number(
                seed,
                variant,
                f"{warehouse}:{category}:price",
                250,
                4800,
            )
            rows.append(
                {
                    "sku": (
                        f"{category[:3].upper()}-{warehouse[0].upper()}-"
                        f"{index:02d}"
                    ),
                    "category": category,
                    "units": units,
                    "unit_price_cents": unit_price,
                }
            )
            category_totals[category]["units"] += units
            category_totals[category]["value_cents"] += units * unit_price
            line_count += 1

        relative_path = f"input/warehouse_{warehouse}.csv"
        workspace_files[relative_path] = _csv_text(rows)

    expected = {
        "categories": category_totals,
        "line_count": line_count,
        "total_units": sum(
            item["units"] for item in category_totals.values()
        ),
        "total_value_cents": sum(
            item["value_cents"] for item in category_totals.values()
        ),
    }
    input_sha256 = {
        path: sha256(content.encode("utf-8")).hexdigest()
        for path, content in sorted(workspace_files.items())
    }
    fixture_ref = "nf-" + sha256(
        f"{SCENARIO_ID}:{seed}:{variant}".encode("utf-8")
    ).hexdigest()[:16]
    prompt = (
        "Read both CSV files in input/. Aggregate every row by category and "
        "write out/stock_summary.json. The JSON object must contain exactly "
        "these top-level keys: categories, line_count, total_units, and "
        "total_value_cents. Each category value must contain integer units "
        "and value_cents, where value_cents is units multiplied by "
        "unit_price_cents for every row. Keep category keys alphabetical. "
        "Do not modify the input files and do not create any other file. "
        "Validate the JSON and the totals before answering."
    )
    control = {
        "schema_version": "daemon.public-productivity.fixture-control.v1",
        "protocol_version": PROTOCOL_VERSION,
        "language": LANGUAGE,
        "scenario_id": SCENARIO_ID,
        "seed": seed,
        "variant": variant,
        "fixture_ref": fixture_ref,
        "baseline_files": sorted(workspace_files),
        "input_sha256": input_sha256,
        "allowed_output": OUTPUT_PATH,
        "expected": expected,
    }
    return {
        "prompt": prompt,
        "workspace_files": workspace_files,
        "control": control,
    }


def materialize_fixture(
    *,
    seed: int,
    variant: str,
    workspace: Path,
    control_path: Path,
) -> dict[str, Any]:
    """Write a fresh workspace and its out-of-workspace control record."""

    workspace = workspace.resolve()
    control_path = control_path.resolve()
    if control_path == workspace or workspace in control_path.parents:
        raise ValueError("control_path must remain outside the agent workspace")
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"workspace is not empty: {workspace}")

    fixture = build_fixture(seed, variant)
    workspace.mkdir(parents=True, exist_ok=True)
    for relative_path, content in fixture["workspace_files"].items():
        destination = workspace / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="")

    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.write_text(
        _canonical_json(fixture["control"]),
        encoding="utf-8",
    )
    return {
        "scenario_id": SCENARIO_ID,
        "seed": seed,
        "variant": variant,
        "fixture_ref": fixture["control"]["fixture_ref"],
        "language": LANGUAGE,
        "prompt": fixture["prompt"],
        "workspace": str(workspace),
        "control": str(control_path),
    }
