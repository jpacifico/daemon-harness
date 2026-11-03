"""Deterministic longitudinal fixture for ``continuity.decision``."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from benchmarks.productivity_v1 import LANGUAGE, PROTOCOL_VERSION


SCENARIO_ID = "continuity.decision"
OFFICIAL_SEEDS = (1301, 1302, 1303, 1304, 1305, 1306)
OUTPUT_PATH = "out/continuity_decision.json"
FIXTURE_VARIANTS = ("left", "right")

PROJECTS = (
    "Project Cedar",
    "Project Harbor",
    "Project Lantern",
    "Project Meadow",
    "Project Quartz",
    "Project Trestle",
    "Project Willow",
    "Project Zephyr",
)
OBJECTIVES = (
    "replace the internal release checklist",
    "move the pilot team to the new intake flow",
    "simplify the weekly inventory handoff",
    "standardize the customer trial launch",
    "retire the duplicate reporting path",
    "prepare the regional onboarding wave",
    "consolidate the review workflow",
    "stabilize the supplier approval process",
)
OPTIONS = (
    "use a single staged rollout",
    "keep two parallel rollout tracks",
    "start with one regional pilot",
    "switch every team in the same window",
    "retain the manual approval checkpoint",
    "replace the checkpoint with peer review",
    "archive the legacy template immediately",
    "keep the legacy template read-only for one cycle",
)
DECISIONS = (
    "run one regional pilot before the wider rollout",
    "keep the legacy template read-only for one cycle",
    "replace the manual checkpoint with peer review",
    "use one staged rollout with a single owner",
    "archive the duplicate reporting path after validation",
    "move only the pilot team to the new intake flow",
    "consolidate reviews into the weekly handoff",
    "delay the wider switch until the trial closes",
)
COMPLETED_OPERATIONS = (
    "baseline export",
    "pilot workspace migration",
    "legacy template snapshot",
    "access-control audit",
    "supplier list import",
    "trial cohort assignment",
    "reporting archive backup",
    "regional permissions sync",
)
NEXT_ACTIONS = (
    "send the pilot brief to the regional leads",
    "collect peer-review sign-off from the two owners",
    "compare the pilot output with the baseline",
    "schedule the wider rollout decision meeting",
    "publish the read-only template location",
    "verify the trial cohort can access the new flow",
    "ask operations to approve the consolidated handoff",
    "record the validation outcome in the release note",
)


def _ranked_choice(values: tuple[str, ...], seed: int, variant: str, label: str) -> str:
    ranked = sorted(
        values,
        key=lambda value: sha256(
            f"{SCENARIO_ID}:{seed}:{label}:{value}".encode("utf-8")
        ).hexdigest(),
    )
    return ranked[0 if variant == "left" else 1]


def _distinct_options(seed: int, variant: str) -> tuple[str, str]:
    ranked = sorted(
        OPTIONS,
        key=lambda value: sha256(
            f"{SCENARIO_ID}:{seed}:option:{value}".encode("utf-8")
        ).hexdigest(),
    )
    offset = 0 if variant == "left" else 2
    return ranked[offset], ranked[offset + 1]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_fixture(seed: int, variant: str) -> dict[str, Any]:
    if seed not in OFFICIAL_SEEDS:
        raise ValueError(f"seed {seed} is not registered for {SCENARIO_ID}")
    if variant not in FIXTURE_VARIANTS:
        raise ValueError(f"unknown fixture variant: {variant}")

    project = _ranked_choice(PROJECTS, seed, variant, "project")
    objective = _ranked_choice(OBJECTIVES, seed, variant, "objective")
    option_one, option_two = _distinct_options(seed, variant)
    expected = {
        "project": project,
        "current_decision": _ranked_choice(DECISIONS, seed, variant, "decision"),
        "completed_operation_not_to_restart": _ranked_choice(
            COMPLETED_OPERATIONS, seed, variant, "completed"
        ),
        "next_action": _ranked_choice(NEXT_ACTIONS, seed, variant, "next"),
    }
    fixture_ref = (
        "cd-"
        + sha256(f"{SCENARIO_ID}:{seed}:{variant}".encode("utf-8")).hexdigest()[:16]
    )
    history_user_prompts = (
        (
            f"We are planning {project}. The objective is to {objective}. "
            f"The two options still under consideration are to {option_one} or "
            f"to {option_two}. Please acknowledge the project and the open choice "
            "in one short paragraph."
        ),
        (
            f"Decision update for {project}: the current decision is "
            f'"{expected["current_decision"]}". The operation '
            f'"{expected["completed_operation_not_to_restart"]}" has already completed '
            "successfully and must not be restarted. The next action is "
            f'"{expected["next_action"]}". Please confirm the current state in one '
            "short paragraph."
        ),
    )
    prompt = (
        "Continue the project we discussed in the earlier session. Write "
        "out/continuity_decision.json as a JSON object containing exactly these "
        "keys: project, current_decision, completed_operation_not_to_restart, "
        "and next_action. Recover the current values from our prior conversation. "
        "Do not restart any completed operation, do not ask me to repeat the "
        "history, and do not create or modify any other file. Validate the JSON "
        "before answering."
    )
    control = {
        "schema_version": "daemon.public-productivity.fixture-control.v1",
        "protocol_version": PROTOCOL_VERSION,
        "language": LANGUAGE,
        "scenario_id": SCENARIO_ID,
        "seed": seed,
        "variant": variant,
        "fixture_ref": fixture_ref,
        "baseline_files": [],
        "input_sha256": {},
        "allowed_output": OUTPUT_PATH,
        "expected": expected,
        "history_message_count": 4,
        "episode_turn_count": 3,
    }
    return {
        "prompt": prompt,
        "history_user_prompts": history_user_prompts,
        "workspace_files": {},
        "control": control,
    }


def materialize_fixture(
    *,
    seed: int,
    variant: str,
    workspace: Path,
    control_path: Path,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    control_path = control_path.resolve()
    if control_path == workspace or workspace in control_path.parents:
        raise ValueError("control_path must remain outside the agent workspace")
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"workspace is not empty: {workspace}")

    fixture = build_fixture(seed, variant)
    workspace.mkdir(parents=True, exist_ok=True)
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.write_text(_canonical_json(fixture["control"]), encoding="utf-8")
    return {
        "scenario_id": SCENARIO_ID,
        "seed": seed,
        "variant": variant,
        "fixture_ref": fixture["control"]["fixture_ref"],
        "language": LANGUAGE,
        "prompt": fixture["prompt"],
        "history_user_prompts": list(fixture["history_user_prompts"]),
        "history_message_count": fixture["control"]["history_message_count"],
        "episode_turn_count": fixture["control"]["episode_turn_count"],
        "workspace": str(workspace),
        "control": str(control_path),
    }
