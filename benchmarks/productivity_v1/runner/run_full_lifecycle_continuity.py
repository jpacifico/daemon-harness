"""Replay implemented scenarios with complete host and daemon lifecycle accounting."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import platform
from pathlib import Path
import sys
from typing import Any

from benchmarks.productivity_v1 import (
    LANGUAGE,
    MANIFEST_SCHEMA_VERSION,
    PROTOCOL_VERSION,
)
from benchmarks.productivity_v1.report.recalculate import recalculate
from benchmarks.productivity_v1.runner.common import (
    canonical_json,
    executable_path,
    file_sha256,
    source_revision,
    write_json,
)
from benchmarks.productivity_v1.runner.hermes import (
    DEFAULT_HERMES_BINARY,
    DEFAULT_HERMES_HOME,
    run_pair as run_hermes_pair,
)
from benchmarks.productivity_v1.runner.openclaw import (
    DEFAULT_DAEMON_ENV,
    DEFAULT_DAEMON_PYTHON,
    DEFAULT_OPENCLAW_BINARY,
    run_pair as run_openclaw_pair,
)
from benchmarks.productivity_v1.runner.run_dry_run import _checksums, _version
from benchmarks.productivity_v1.scenarios import SCENARIOS, ScenarioDefinition


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = ROOT / ".cache" / "benchmarks" / "productivity-v1"
DEFAULT_RUNTIME_PARENT = Path("/private/tmp/daemon-full-lifecycle-campaign-runtime")
DEFAULT_SCENARIOS = ("continuity.decision",)
DEFAULT_HOSTS = ("hermes", "openclaw")
ARM_ORDER = {
    seed: (("A", "C") if index % 2 == 0 else ("C", "A"))
    for index, seed in enumerate(SCENARIOS["continuity.decision"].official_seeds)
}


def _selected_scenarios(scenario_ids: tuple[str, ...]) -> tuple[ScenarioDefinition, ...]:
    if not scenario_ids:
        raise ValueError("at least one scenario is required")
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("full lifecycle scenarios must be unique")
    try:
        return tuple(SCENARIOS[scenario_id] for scenario_id in scenario_ids)
    except KeyError as exc:
        raise ValueError(f"unknown productivity scenario: {exc.args[0]}") from exc


def _selected_hosts(host_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not host_ids:
        raise ValueError("at least one host is required")
    if len(set(host_ids)) != len(host_ids):
        raise ValueError("full lifecycle hosts must be unique")
    unknown = tuple(host for host in host_ids if host not in DEFAULT_HOSTS)
    if unknown:
        raise ValueError(f"unknown productivity host: {unknown[0]}")
    return host_ids


def _ordered_cases(
    scenarios: tuple[ScenarioDefinition, ...],
    *,
    host_ids: tuple[str, ...] = DEFAULT_HOSTS,
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    pair_index = 0
    for scenario in scenarios:
        for seed_index, seed in enumerate(scenario.official_seeds):
            arm_order = ("A", "C") if seed_index % 2 == 0 else ("C", "A")
            alternating_hosts = (
                ("openclaw", "hermes")
                if pair_index % 2 == 0
                else ("hermes", "openclaw")
            )
            hosts = tuple(host for host in alternating_hosts if host in host_ids)
            cases.extend(
                {
                    "scenario_id": scenario.id,
                    "seed": seed,
                    "host": host,
                    "arms": arm_order,
                }
                for host in hosts
            )
            pair_index += 1
    return cases


def _manifest_attestations(
    records: list[dict[str, Any]],
    *,
    host: str,
) -> list[dict[str, str]]:
    first = next(record for record in records if record["host"] == host)["arms"]
    return [
        {
            "arm_id": arm,
            "context_sha256": first[arm]["context_sha256"],
            "tool_catalog_sha256": first[arm]["tool_catalog_sha256"],
        }
        for arm in ("A", "C")
    ]


def _attestations_stable(
    records: list[dict[str, Any]],
    *,
    host_ids: tuple[str, ...] = DEFAULT_HOSTS,
) -> bool:
    for host in host_ids:
        host_records = [record["arms"] for record in records if record["host"] == host]
        for arm in ("A", "C"):
            for field in (
                "context_sha256",
                "tool_catalog_sha256",
                "external_tool_catalog_sha256",
            ):
                if len({record[arm][field] for record in host_records}) != 1:
                    return False
    return True


def _hermes_memory_installations_attested(
    records: list[dict[str, Any]],
) -> bool:
    hermes_records = [record for record in records if record["host"] == "hermes"]
    return bool(hermes_records) and all(
        set(record["arms"]["A"].get("installation_memory_tools", ()))
        >= {"memory"}
        and set(record["arms"]["C"].get("installation_memory_tools", ()))
        >= {"daemon_read_souvenirs", "daemon_recall_l0"}
        and "memory"
        not in set(record["arms"]["C"].get("installation_memory_tools", ()))
        for record in hermes_records
    )


def _daemon_background_usage_complete(
    runs: list[dict[str, Any]],
    attestations: list[dict[str, Any]],
) -> bool:
    c_attestations = {
        (record["host"], record["scenario_id"], record["seed"]): record["arms"]["C"]
        for record in attestations
    }
    c_runs = [run for run in runs if run["arm_id"] == "C"]
    if len(c_runs) != len(c_attestations):
        return False
    for run in c_runs:
        key = (run["host_id"], run["scenario_id"], run["seed"])
        attestation = c_attestations.get(key)
        if attestation is None:
            return False
        completed_turns = attestation.get("completed_host_turn_count")
        background = run["tokens"]["daemon.background"]
        if background["total"]["value"] is None:
            return False
        if completed_turns == 0:
            if background["call_count"] != 0 or background["total"]["value"] != 0:
                return False
        elif not isinstance(completed_turns, int) or completed_turns < 1:
            return False
        elif background["call_count"] < 1:
            return False
    return True


def _hermes_native_background_usage_attributed(
    attestations: list[dict[str, Any]],
) -> bool:
    for record in attestations:
        if record.get("host") != "hermes":
            continue
        for arm in record.get("arms", {}).values():
            scheduled = arm.get("native_background_scheduled")
            call_count = arm.get("native_background_call_count")
            if not isinstance(scheduled, int) or not isinstance(call_count, int):
                return False
            if scheduled > 0 and call_count < 1:
                return False
    return True


def _gates(
    *,
    runs: list[dict[str, Any]],
    attestations: list[dict[str, Any]],
    summary: dict[str, Any],
    expected_run_count: int,
    host_ids: tuple[str, ...] = DEFAULT_HOSTS,
) -> dict[str, bool]:
    return {
        "completed_records": (
            len(runs) == expected_run_count
            and all(run["status"] == "completed" for run in runs)
        ),
        "authoritative_outcomes": all(
            run["outcome"]["authoritative"] is True for run in runs
        ),
        "episode_turn_counts_match_scenarios": all(
            arm["episode_turns_observed"]
            == SCENARIOS[record["scenario_id"]].episode_turns
            for record in attestations
            for arm in record["arms"].values()
        ),
        "persistent_conversation_counts_match_scenarios": all(
            arm.get("persistent_host_path") is True
            and arm.get("persistent_conversation_count")
            == (2 if SCENARIOS[record["scenario_id"]].history_messages else 1)
            for record in attestations
            for arm in record["arms"].values()
        ),
        "durable_daemon_cycles_closed": all(
            record["arms"]["C"].get("durable_lifecycle_complete") is True
            for record in attestations
        ),
        "daemon_background_usage_measured": _daemon_background_usage_complete(
            runs, attestations
        ),
        "no_daemon_critical_llm_call": all(
            run["tokens"]["daemon.critical"]["call_count"] == 0
            for run in runs
            if run["arm_id"] == "C"
        ),
        "host_usage_observed": all(
            run["tokens"]["host.visible"]["total"]["value"] is not None
            for run in runs
        ),
        "canonical_daemon_path_observed": all(
            record["arms"]["C"].get("canonical_path_observed") is True
            for record in attestations
        ),
        "external_tools_at_parity": all(
            record["arms"]["A"]["external_tool_catalog_sha256"]
            == record["arms"]["C"]["external_tool_catalog_sha256"]
            for record in attestations
        ),
        "openclaw_native_memory_active": all(
            set(record["arms"]["A"].get("installation_memory_tools", ()))
            >= {"memory_get", "memory_search"}
            and record["arms"]["A"].get("native_memory_sync_observed") is True
            for record in attestations
            if record["host"] == "openclaw"
        ),
        "hermes_installation_memory_active": (
            "hermes" not in host_ids
            or _hermes_memory_installations_attested(attestations)
        ),
        "native_background_closed": all(
            arm.get("native_background_complete") is True
            for record in attestations
            for arm in record["arms"].values()
        ),
        "hermes_native_background_usage_attributed": (
            _hermes_native_background_usage_attributed(attestations)
        ),
        "attestations_stable": _attestations_stable(
            attestations, host_ids=host_ids
        ),
        "offline_recalculation_complete": (
            summary["run_count"] == expected_run_count
        ),
    }


def run_campaign(
    *,
    campaign_id: str,
    scenario_ids: tuple[str, ...],
    host_ids: tuple[str, ...],
    output_root: Path,
    runtime_parent: Path,
    hermes_binary: Path,
    hermes_home: Path,
    openclaw_binary: Path,
    daemon_python: Path,
    daemon_env: Path,
    timeout_ms: int,
    lifecycle_timeout_seconds: float,
    keep_runtime: bool = False,
) -> Path:
    scenarios = _selected_scenarios(scenario_ids)
    hosts = _selected_hosts(host_ids)
    cases = _ordered_cases(scenarios, host_ids=hosts)
    expected_run_count = len(cases) * 2
    revision = source_revision(ROOT)
    if revision["dirty"]:
        raise RuntimeError("the full lifecycle replay requires a clean checkout")
    artifact_root = (output_root / campaign_id).resolve()
    if artifact_root.exists():
        raise FileExistsError(f"campaign directory already exists: {artifact_root}")
    artifact_root.mkdir(parents=True)
    write_json(
        artifact_root / "plan.json",
        {
            "schema_version": "daemon.full-lifecycle-plan.v2",
            "protocol_version": PROTOCOL_VERSION,
            "campaign_id": campaign_id,
            "campaign_kind": "corrective-dry-run",
            "report_eligible": False,
            "language": LANGUAGE,
            "source": revision,
            "scenarios": [
                {
                    "id": scenario.id,
                    "seeds": list(scenario.official_seeds),
                    "episode_turns": scenario.episode_turns,
                    "history_messages": scenario.history_messages,
                }
                for scenario in scenarios
            ],
            "hosts": list(hosts),
            "timeout_ms": timeout_ms,
            "lifecycle_timeout_seconds": lifecycle_timeout_seconds,
            "order": [
                {
                    **case,
                    "arms": list(case["arms"]),
                }
                for case in cases
            ],
            "persistent_host_paths": True,
            "durable_daemon_owner": True,
            "synthetic_isolation": True,
        },
    )

    runs: list[dict[str, Any]] = []
    attestations: list[dict[str, Any]] = []
    for sequence, case in enumerate(cases, start=1):
        scenario_id = case["scenario_id"]
        seed = case["seed"]
        host = case["host"]
        arm_order = case["arms"]
        print(
            f"[{sequence}/{len(cases)}] scenario={scenario_id} host={host} "
            f"seed={seed} arms={'/'.join(arm_order)}",
            flush=True,
        )
        if host == "hermes":
            pair_runs, pair_attestations = run_hermes_pair(
                campaign_id=campaign_id,
                scenario_id=scenario_id,
                seed=seed,
                artifact_root=artifact_root,
                runtime_parent=runtime_parent,
                hermes_binary=hermes_binary,
                hermes_home=hermes_home,
                model="gpt-5.6-terra",
                provider="openai-codex",
                timeout_ms=timeout_ms,
                arm_order=arm_order,
                full_lifecycle=True,
                lifecycle_timeout_seconds=lifecycle_timeout_seconds,
                keep_runtime=keep_runtime,
            )
        else:
            pair_runs, pair_attestations = run_openclaw_pair(
                campaign_id=campaign_id,
                scenario_id=scenario_id,
                seed=seed,
                artifact_root=artifact_root,
                runtime_parent=runtime_parent,
                openclaw_binary=openclaw_binary,
                daemon_python=daemon_python,
                daemon_env_path=daemon_env,
                model="openai/gpt-5.6-terra",
                thinking="medium",
                timeout_ms=timeout_ms,
                arm_order=arm_order,
                full_lifecycle=True,
                lifecycle_timeout_seconds=lifecycle_timeout_seconds,
                keep_runtime=keep_runtime,
            )
        runs.extend(pair_runs)
        attestations.append(
            {
                "scenario_id": scenario_id,
                "seed": seed,
                "host": host,
                "arms": pair_attestations,
            }
        )

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": campaign_id,
        "campaign_kind": "dry-run",
        "language": LANGUAGE,
        "report_eligible": False,
        "ordering_seed": 20260830,
        "analysis": {"percentile_method": "nearest-rank"},
        "environment": {
            "operating_system": platform.platform(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
        },
        "hosts": [
            *([{
                "id": "hermes",
                "version": _version(hermes_binary, first_line=True),
                "binary_sha256": file_sha256(hermes_binary),
                "model": {
                    "provider": "openai-codex",
                    "name": "gpt-5.6-terra",
                    "reasoning_effort": "host-default",
                },
                "arm_attestations": _manifest_attestations(
                    attestations, host="hermes"
                ),
            }] if "hermes" in hosts else []),
            *([{
                "id": "openclaw",
                "version": _version(openclaw_binary),
                "binary_sha256": file_sha256(openclaw_binary),
                "model": {
                    "provider": "openai",
                    "name": "gpt-5.6-terra",
                    "reasoning_effort": "medium",
                },
                "arm_attestations": _manifest_attestations(
                    attestations, host="openclaw"
                ),
            }] if "openclaw" in hosts else []),
        ],
        "arms": [
            {"id": "A", "label": "native_host"},
            {"id": "C", "label": "host_with_daemon"},
        ],
        "scenarios": [
            {
                "id": scenario.id,
                "seeds": list(scenario.official_seeds),
                "max_turns": scenario.episode_turns,
                "timeout_ms": timeout_ms * scenario.episode_turns,
            }
            for scenario in scenarios
        ],
    }
    write_json(artifact_root / "manifest.json", manifest)
    summary = recalculate(
        manifest_path=artifact_root / "manifest.json",
        runs_path=artifact_root / "runs.jsonl",
    )
    write_json(artifact_root / "summary.json", summary)
    gates = _gates(
        runs=runs,
        attestations=attestations,
        summary=summary,
        expected_run_count=expected_run_count,
        host_ids=hosts,
    )
    write_json(
        artifact_root / "full-lifecycle-verdict.json",
        {
            "schema_version": "daemon.full-lifecycle-verdict.v2",
            "campaign_id": campaign_id,
            "conclusive": all(gates.values()),
            "gates": gates,
            "observations": {
                "success_first_attempt_count": sum(
                    run["outcome"]["success_first_attempt"] for run in runs
                ),
                "success_within_episode_count": sum(
                    run["outcome"]["success_within_episode"] for run in runs
                ),
            },
            "source": revision,
            "attestations": attestations,
        },
    )
    (artifact_root / "SHA256SUMS").write_text(
        _checksums(artifact_root), encoding="utf-8"
    )
    return artifact_root


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-id")
    parser.add_argument(
        "--scenario-id",
        action="append",
        choices=tuple(sorted(SCENARIOS)),
        dest="scenario_ids",
        help=(
            "implemented scenario to include; repeat for multiple scenarios "
            "(default: continuity.decision)"
        ),
    )
    parser.add_argument(
        "--host-id",
        action="append",
        choices=DEFAULT_HOSTS,
        dest="host_ids",
        help="host to include; repeat for both (default: Hermes and OpenClaw)",
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--runtime-parent", type=Path, default=DEFAULT_RUNTIME_PARENT)
    parser.add_argument("--hermes-binary", type=Path, default=DEFAULT_HERMES_BINARY)
    parser.add_argument("--hermes-home", type=Path, default=DEFAULT_HERMES_HOME)
    parser.add_argument("--openclaw-binary", type=Path, default=DEFAULT_OPENCLAW_BINARY)
    parser.add_argument("--daemon-python", type=Path, default=DEFAULT_DAEMON_PYTHON)
    parser.add_argument("--daemon-env", type=Path, default=DEFAULT_DAEMON_ENV)
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    parser.add_argument("--lifecycle-timeout-seconds", type=float, default=420.0)
    parser.add_argument("--keep-runtime", action="store_true")
    args = parser.parse_args()
    scenario_ids = tuple(args.scenario_ids or DEFAULT_SCENARIOS)
    host_ids = tuple(args.host_ids or DEFAULT_HOSTS)
    campaign_id = args.campaign_id or f"full-lifecycle-{stamp}"
    try:
        artifact_root = run_campaign(
            campaign_id=campaign_id,
            scenario_ids=scenario_ids,
            host_ids=host_ids,
            output_root=args.output_root.expanduser().resolve(),
            runtime_parent=args.runtime_parent.expanduser().resolve(),
            hermes_binary=(
                executable_path(args.hermes_binary)
                if "hermes" in host_ids
                else args.hermes_binary.expanduser().resolve()
            ),
            hermes_home=args.hermes_home.expanduser().resolve(),
            openclaw_binary=(
                executable_path(args.openclaw_binary)
                if "openclaw" in host_ids
                else args.openclaw_binary.expanduser().resolve()
            ),
            daemon_python=executable_path(args.daemon_python),
            daemon_env=args.daemon_env.expanduser().resolve(),
            timeout_ms=args.timeout_ms,
            lifecycle_timeout_seconds=args.lifecycle_timeout_seconds,
            keep_runtime=args.keep_runtime,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Full lifecycle replay failed: {exc}", file=sys.stderr)
        return 1
    verdict = json.loads(
        (artifact_root / "full-lifecycle-verdict.json").read_text(encoding="utf-8")
    )
    print(
        canonical_json(
            {
                "artifact_root": str(artifact_root),
                "conclusive": verdict["conclusive"],
                "gates": verdict["gates"],
            }
        ),
        end="",
    )
    return 0 if verdict["conclusive"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
