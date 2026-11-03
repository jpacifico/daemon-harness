"""Execute four disposable live runs for one English scenario."""

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
    run_command,
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
from benchmarks.productivity_v1.scenarios import SCENARIOS, get_scenario


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = ROOT / ".cache" / "benchmarks" / "productivity-v1"
DEFAULT_RUNTIME_PARENT = (
    Path.home() / ".cache" / "daemon-public-productivity-v1" / "runtime"
)


def _version(binary: Path, *, first_line: bool = False) -> str:
    completed = run_command([str(binary), "--version"], cwd=ROOT, timeout=30)
    if completed.returncode:
        raise RuntimeError(f"cannot resolve version for {binary.name}")
    value = completed.stdout.strip()
    return value.splitlines()[0] if first_line else value


def _manifest_attestations(
    values: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    return [
        {
            "arm_id": arm,
            "context_sha256": values[arm]["context_sha256"],
            "tool_catalog_sha256": values[arm]["tool_catalog_sha256"],
        }
        for arm in ("A", "C")
    ]


def _checksums(root: Path) -> str:
    paths = sorted(
        path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS"
    )
    return "".join(
        f"{file_sha256(path)}  {path.relative_to(root).as_posix()}\n" for path in paths
    )


def _gate(
    *,
    runs: list[dict[str, Any]],
    attestations: dict[str, dict[str, dict[str, Any]]],
    summary: dict[str, Any],
    expected_turns: int,
) -> dict[str, bool]:
    by_host_arm = {(run["host_id"], run["arm_id"]): run for run in runs}
    return {
        "four_completed_records": (
            len(runs) == 4 and all(run["status"] == "completed" for run in runs)
        ),
        "four_authoritative_outcomes": (
            len(runs) == 4
            and all(
                run["outcome"]["authoritative"] is True
                and isinstance(run["outcome"]["success_first_attempt"], bool)
                and isinstance(run["outcome"]["success_within_episode"], bool)
                for run in runs
            )
        ),
        "host_tokens_observed": all(
            run["tokens"]["host.visible"]["total"]["value"] is not None for run in runs
        ),
        "episode_turns_observed": all(
            attestations[host][arm]["episode_turns_observed"] == expected_turns
            for host in ("hermes", "openclaw")
            for arm in ("A", "C")
        ),
        "tool_calls_observed": all(bool(run["tool_calls"]) for run in runs),
        "hermes_first_fragment_observed": all(
            by_host_arm[("hermes", arm)]["timing"]["first_visible_ms"] is not None
            for arm in ("A", "C")
        ),
        "openclaw_first_fragment_explicitly_unavailable": all(
            by_host_arm[("openclaw", arm)]["timing"]["first_visible_ms"] is None
            for arm in ("A", "C")
        ),
        "openclaw_hard_sandbox_attested": all(
            attestations["openclaw"][arm]
            .get("sandbox", {})
            .get("host_process_is_sandboxed")
            is True
            and attestations["openclaw"][arm]["sandbox"].get("backend")
            == "macos-sandbox-exec"
            and attestations["openclaw"][arm]["sandbox"].get("runtime_is_writable")
            is True
            and attestations["openclaw"][arm]["sandbox"].get("real_home_is_blocked")
            is True
            and len(
                attestations["openclaw"][arm]["sandbox"].get("attestation_sha256", "")
            )
            == 64
            for arm in ("A", "C")
        ),
        "openclaw_native_memory_sync_observed": all(
            attestations["openclaw"][arm].get("native_memory_sync_observed") is True
            for arm in ("A", "C")
        ),
        "openclaw_memory_tools_match_installation": set(
            attestations["openclaw"]["A"].get("installation_memory_tools", ())
        )
        >= {"memory_get", "memory_search"}
        and set(attestations["openclaw"]["C"].get("installation_memory_tools", ()))
        >= {"daemon_read_souvenirs", "daemon_recall_l0"},
        "daemon_compose_observed": all(
            by_host_arm[(host, "C")]["timing"]["daemon_compose_ms"] is not None
            for host in ("hermes", "openclaw")
        ),
        "canonical_daemon_path_observed": all(
            attestations[host]["C"]["canonical_path_observed"] is True
            for host in ("hermes", "openclaw")
        ),
        "external_tools_at_parity": all(
            attestations[host]["A"]["external_tool_catalog_sha256"]
            == attestations[host]["C"]["external_tool_catalog_sha256"]
            for host in ("hermes", "openclaw")
        ),
        "context_attestations_observed": all(
            attestations[host][arm]["context_sha256"] != "0" * 64
            for host in ("hermes", "openclaw")
            for arm in ("A", "C")
        ),
        "offline_recalculation_complete": summary["run_count"] == 4,
    }


def run_campaign(
    *,
    campaign_id: str,
    scenario_id: str,
    seed: int,
    output_root: Path,
    runtime_parent: Path,
    hermes_binary: Path,
    hermes_home: Path,
    openclaw_binary: Path,
    daemon_python: Path,
    daemon_env: Path,
    timeout_ms: int,
    keep_runtime: bool = False,
) -> Path:
    scenario = get_scenario(scenario_id)
    if seed not in scenario.official_seeds:
        raise ValueError(f"seed {seed} is not registered for {scenario_id}")
    revision = source_revision(ROOT)
    if revision["dirty"]:
        raise RuntimeError(
            "the live benchmark requires a clean checkout so its daemon commit "
            "is unambiguous"
        )
    artifact_root = (output_root / campaign_id).resolve()
    if artifact_root.exists():
        raise FileExistsError(f"campaign directory already exists: {artifact_root}")
    artifact_root.mkdir(parents=True)
    plan = {
        "schema_version": "daemon.public-productivity.dry-run-plan.v1",
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": campaign_id,
        "campaign_kind": "dry-run",
        "report_eligible": False,
        "language": LANGUAGE,
        "source": revision,
        "scenario_id": scenario_id,
        "seed": seed,
        "timeout_ms": timeout_ms,
        "episode_turns": scenario.episode_turns,
        "history_messages": scenario.history_messages,
        "order": ["hermes:A", "hermes:C", "openclaw:C", "openclaw:A"],
        "isolation": {
            "personal_memory": False,
            "production_channel": False,
            "production_gateway": False,
            "disposable_profiles": True,
        },
    }
    write_json(artifact_root / "plan.json", plan)

    hermes_runs, hermes_attestations = run_hermes_pair(
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
        arm_order=("A", "C"),
        keep_runtime=keep_runtime,
    )
    openclaw_runs, openclaw_attestations = run_openclaw_pair(
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
        arm_order=("C", "A"),
        keep_runtime=keep_runtime,
    )
    runs = [*hermes_runs, *openclaw_runs]
    attestations = {
        "hermes": hermes_attestations,
        "openclaw": openclaw_attestations,
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": campaign_id,
        "campaign_kind": "dry-run",
        "language": LANGUAGE,
        "report_eligible": False,
        "ordering_seed": 20260829,
        "analysis": {"percentile_method": "nearest-rank"},
        "environment": {
            "operating_system": platform.platform(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
        },
        "hosts": [
            {
                "id": "hermes",
                "version": _version(hermes_binary, first_line=True),
                "binary_sha256": file_sha256(hermes_binary),
                "model": {
                    "provider": "openai-codex",
                    "name": "gpt-5.6-terra",
                    "reasoning_effort": "host-default",
                },
                "arm_attestations": _manifest_attestations(hermes_attestations),
            },
            {
                "id": "openclaw",
                "version": _version(openclaw_binary),
                "binary_sha256": file_sha256(openclaw_binary),
                "model": {
                    "provider": "openai",
                    "name": "gpt-5.6-terra",
                    "reasoning_effort": "medium",
                },
                "arm_attestations": _manifest_attestations(openclaw_attestations),
            },
        ],
        "arms": [
            {"id": "A", "label": "native_host"},
            {"id": "C", "label": "host_with_daemon"},
        ],
        "scenarios": [
            {
                "id": scenario_id,
                "seeds": [seed],
                "max_turns": scenario.episode_turns,
                "timeout_ms": timeout_ms * scenario.episode_turns,
            }
        ],
    }
    write_json(artifact_root / "manifest.json", manifest)
    summary = recalculate(
        manifest_path=artifact_root / "manifest.json",
        runs_path=artifact_root / "runs.jsonl",
    )
    write_json(artifact_root / "summary.json", summary)
    gates = _gate(
        runs=runs,
        attestations=attestations,
        summary=summary,
        expected_turns=scenario.episode_turns,
    )
    write_json(
        artifact_root / "dry-run-verdict.json",
        {
            "schema_version": "daemon.public-productivity.dry-run-verdict.v2",
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
        choices=tuple(sorted(SCENARIOS)),
        default="native.files.aggregate",
    )
    parser.add_argument("--seed", type=int)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--runtime-parent", type=Path, default=DEFAULT_RUNTIME_PARENT)
    parser.add_argument("--hermes-binary", type=Path, default=DEFAULT_HERMES_BINARY)
    parser.add_argument("--hermes-home", type=Path, default=DEFAULT_HERMES_HOME)
    parser.add_argument("--openclaw-binary", type=Path, default=DEFAULT_OPENCLAW_BINARY)
    parser.add_argument("--daemon-python", type=Path, default=DEFAULT_DAEMON_PYTHON)
    parser.add_argument("--daemon-env", type=Path, default=DEFAULT_DAEMON_ENV)
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    parser.add_argument("--keep-runtime", action="store_true")
    args = parser.parse_args()
    scenario = get_scenario(args.scenario_id)
    seed = args.seed if args.seed is not None else scenario.official_seeds[0]
    campaign_id = args.campaign_id or (
        f"dry-run-{args.scenario_id.replace('.', '-')}-{stamp}"
    )
    try:
        artifact_root = run_campaign(
            campaign_id=campaign_id,
            scenario_id=args.scenario_id,
            seed=seed,
            output_root=args.output_root.expanduser().resolve(),
            runtime_parent=args.runtime_parent.expanduser().resolve(),
            hermes_binary=executable_path(args.hermes_binary),
            hermes_home=args.hermes_home.expanduser().resolve(),
            openclaw_binary=executable_path(args.openclaw_binary),
            daemon_python=executable_path(args.daemon_python),
            daemon_env=args.daemon_env.expanduser().resolve(),
            timeout_ms=args.timeout_ms,
            keep_runtime=args.keep_runtime,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Dry-run campaign failed: {exc}", file=sys.stderr)
        return 1
    verdict = json.loads(
        (artifact_root / "dry-run-verdict.json").read_text(encoding="utf-8")
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
