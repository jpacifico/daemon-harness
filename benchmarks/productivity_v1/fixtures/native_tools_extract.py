"""Deterministic controlled source for ``native.tools.extract``."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from benchmarks.productivity_v1 import LANGUAGE, PROTOCOL_VERSION


SCENARIO_ID = "native.tools.extract"
OFFICIAL_SEEDS = (1201, 1202, 1203, 1204, 1205, 1206)
OUTPUT_PATH = "out/action_register.json"
FIXTURE_VARIANTS = ("left", "right")
SOURCE_PATHS = (
    "/evidence.json",
    "/index.json",
    "/owners.json",
    "/records/page-1.json",
    "/records/page-2.json",
)
OWNER_NAMES = (
    "Avery Chen",
    "Jordan Blake",
    "Morgan Diaz",
    "Riley Evans",
    "Samira Khan",
    "Taylor Okafor",
)
RISK_LABELS = {
    "amber": "needs attention",
    "green": "on track",
    "red": "at risk",
}


def _stable_number(
    seed: int,
    variant: str,
    label: str,
    low: int,
    high: int,
) -> int:
    payload = f"{SCENARIO_ID}:{seed}:{variant}:{label}".encode()
    value = int.from_bytes(sha256(payload).digest()[:8], "big")
    return low + value % (high - low + 1)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_fixture(seed: int, variant: str) -> dict[str, Any]:
    if seed not in OFFICIAL_SEEDS:
        raise ValueError(f"seed {seed} is not registered for {SCENARIO_ID}")
    if variant not in FIXTURE_VARIANTS:
        raise ValueError(f"unknown fixture variant: {variant}")

    owners = [
        {"display_name": name, "owner_id": f"owner-{index + 1}"}
        for index, name in enumerate(OWNER_NAMES)
    ]
    statuses = ("open", "open", "closed", "open", "open", "closed", "open", "open")
    priorities = (4, 3, 4, 2, 3, 3, 1, 4)
    risks = tuple(RISK_LABELS)
    records: list[dict[str, Any]] = []
    evidence: list[dict[str, str]] = []
    for index in range(8):
        item_id = f"WI-{seed % 100:02d}-{index + 1:02d}"
        owner_index = _stable_number(seed, variant, f"owner:{index}", 0, 5)
        day = _stable_number(seed, variant, f"day:{index}", 2, 24)
        risk = risks[_stable_number(seed, variant, f"risk:{index}", 0, 2)]
        records.append(
            {
                "due_date": f"2026-09-{day:02d}",
                "id": item_id,
                "owner_id": owners[owner_index]["owner_id"],
                "priority": priorities[index],
                "risk_code": risk,
                "status": statuses[index],
            }
        )
        evidence_count = _stable_number(
            seed, variant, f"evidence-count:{index}", 1, 2
        )
        for evidence_index in range(evidence_count):
            evidence.append(
                {
                    "item_id": item_id,
                    "ref": f"EV-{seed % 100:02d}-{index + 1:02d}-{evidence_index + 1}",
                }
            )

    owner_by_id = {item["owner_id"]: item["display_name"] for item in owners}
    evidence_by_id: dict[str, list[str]] = {}
    for item in evidence:
        evidence_by_id.setdefault(item["item_id"], []).append(item["ref"])
    selected = [
        item
        for item in records
        if item["status"] == "open" and item["priority"] >= 3
    ]
    actions = [
        {
            "due_date": item["due_date"],
            "evidence_refs": sorted(evidence_by_id[item["id"]]),
            "id": item["id"],
            "owner": owner_by_id[item["owner_id"]],
            "risk": RISK_LABELS[item["risk_code"]],
        }
        for item in sorted(selected, key=lambda item: (item["due_date"], item["id"]))
    ]
    source_payloads = {
        "index.json": {
            "dataset": "synthetic work-item register",
            "evidence_path": "/evidence.json",
            "owner_path": "/owners.json",
            "record_pages": ["/records/page-1.json", "/records/page-2.json"],
            "risk_labels": RISK_LABELS,
        },
        "owners.json": {"owners": owners},
        "evidence.json": {"evidence": evidence},
        "records/page-1.json": {"items": records[:4], "page": 1},
        "records/page-2.json": {"items": records[4:], "page": 2},
    }
    expected = {"actions": actions, "source_paths": list(SOURCE_PATHS)}
    fixture_ref = "nt-" + sha256(
        f"{SCENARIO_ID}:{seed}:{variant}".encode()
    ).hexdigest()[:16]
    control = {
        "schema_version": "daemon.public-productivity.fixture-control.v1",
        "protocol_version": PROTOCOL_VERSION,
        "language": LANGUAGE,
        "scenario_id": SCENARIO_ID,
        "seed": seed,
        "variant": variant,
        "fixture_ref": fixture_ref,
        "baseline_files": [],
        "allowed_output": OUTPUT_PATH,
        "required_source_paths": list(SOURCE_PATHS),
        "source_sha256": {
            path: sha256(_canonical_json(payload).encode()).hexdigest()
            for path, payload in sorted(source_payloads.items())
        },
        "expected": expected,
    }
    return {
        "prompt_template": (
            "Inspect the controlled local source starting at {base_url}/index.json "
            "with the tools available in the host. Write out/action_register.json "
            "as a JSON object with exactly two top-level keys: actions and "
            "source_paths. Include only work items whose status is open and whose "
            "priority is at least 3. For every included item, emit exactly "
            "due_date, evidence_refs, id, owner, and risk; resolve owners and risk "
            "labels from the linked sources and include every matching evidence "
            "reference in alphabetical order. Sort actions by due_date and then id. "
            "List every consulted source path, including /index.json, in "
            "source_paths alphabetically. Do not create any other file. Validate "
            "the JSON and the selection before answering."
        ),
        "source_payloads": source_payloads,
        "control": control,
    }


def materialize_fixture(
    *,
    seed: int,
    variant: str,
    workspace: Path,
    control_path: Path,
    source_root: Path,
    base_url: str,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    control_path = control_path.resolve()
    source_root = source_root.resolve()
    if control_path == workspace or workspace in control_path.parents:
        raise ValueError("control_path must remain outside the agent workspace")
    if source_root == workspace or workspace in source_root.parents:
        raise ValueError("source_root must remain outside the agent workspace")
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"workspace is not empty: {workspace}")
    if source_root.exists() and any(source_root.iterdir()):
        raise FileExistsError(f"source root is not empty: {source_root}")

    fixture = build_fixture(seed, variant)
    workspace.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(parents=True, exist_ok=True)
    for relative_path, payload in fixture["source_payloads"].items():
        destination = source_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_canonical_json(payload), encoding="utf-8")
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.write_text(
        _canonical_json(fixture["control"]), encoding="utf-8"
    )
    return {
        "scenario_id": SCENARIO_ID,
        "seed": seed,
        "variant": variant,
        "fixture_ref": fixture["control"]["fixture_ref"],
        "language": LANGUAGE,
        "prompt": fixture["prompt_template"].format(base_url=base_url),
        "source_base_url": base_url,
        "workspace": str(workspace),
        "control": str(control_path),
    }
