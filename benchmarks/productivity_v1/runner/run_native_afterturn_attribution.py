"""Measure attributable native after-turn work on Hermes and OpenClaw."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import platform
from pathlib import Path
import sys
from typing import Any

from benchmarks.productivity_v1 import LANGUAGE, PROTOCOL_VERSION
from benchmarks.productivity_v1.fixtures.continuity_decision import OFFICIAL_SEEDS
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
    run_arm as run_hermes_arm,
)
from benchmarks.productivity_v1.runner.openclaw import (
    DEFAULT_DAEMON_ENV,
    DEFAULT_DAEMON_PYTHON,
    DEFAULT_OPENCLAW_BINARY,
    _discover_codex_plugin,
    run_arm as run_openclaw_arm,
)
from benchmarks.productivity_v1.runner.run_dry_run import _checksums, _version


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = ROOT / ".cache" / "benchmarks" / "productivity-v1"
DEFAULT_RUNTIME_PARENT = Path("/private/tmp/daemon-native-afterturn-runtime")


def _sum_scope(runs: list[dict[str, Any]], scope: str, field: str) -> int | None:
    values = [run["tokens"][scope][field]["value"] for run in runs]
    return sum(values) if all(isinstance(value, int) for value in values) else None


def _summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    hosts: dict[str, Any] = {}
    for host in ("hermes", "openclaw"):
        selected = [run for run in runs if run["host_id"] == host]
        hosts[host] = {
            "run_count": len(selected),
            "success_count": sum(
                run["outcome"]["success_within_episode"] for run in selected
            ),
            "visible_call_count": sum(
                run["tokens"]["host.visible"]["call_count"] for run in selected
            ),
            "background_call_count": sum(
                run["tokens"]["host.background"]["call_count"] for run in selected
            ),
            "visible_total_tokens": _sum_scope(selected, "host.visible", "total"),
            "background_total_tokens": _sum_scope(selected, "host.background", "total"),
            "complete_total_tokens": (
                _sum_scope(selected, "host.visible", "total")
                + _sum_scope(selected, "host.background", "total")
                if _sum_scope(selected, "host.visible", "total") is not None
                and _sum_scope(selected, "host.background", "total") is not None
                else None
            ),
            "background_coverage_complete": all(
                run["tokens"]["host.background"]["total"]["value"] is not None
                for run in selected
            ),
        }
    return {
        "schema_version": "daemon.native-afterturn-summary.v1",
        "run_count": len(runs),
        "hosts": hosts,
    }


def _gates(
    runs: list[dict[str, Any]],
    attestations: list[dict[str, Any]],
) -> dict[str, bool]:
    return {
        "twelve_completed_records": len(runs) == 12
        and all(run["status"] == "completed" for run in runs),
        "authoritative_outcomes": all(
            run["outcome"]["authoritative"] is True for run in runs
        ),
        "three_turns_observed": all(
            item["attestation"]["episode_turns_observed"] == 3 for item in attestations
        ),
        "two_persistent_conversations": all(
            item["attestation"]["persistent_host_path"] is True
            and item["attestation"]["persistent_conversation_count"] == 2
            for item in attestations
        ),
        "native_background_closed": all(
            item["attestation"]["native_background_complete"] is True
            for item in attestations
        ),
        "native_background_usage_attributed": all(
            run["tokens"]["host.background"]["total"]["value"] is not None
            for run in runs
        ),
        "host_visible_usage_observed": all(
            run["tokens"]["host.visible"]["total"]["value"] is not None for run in runs
        ),
        "openclaw_native_memory_active": all(
            set(item["attestation"].get("installation_memory_tools", ()))
            >= {"memory_get", "memory_search"}
            and item["attestation"].get("native_memory_sync_observed") is True
            for item in attestations
            if item["host"] == "openclaw"
        ),
        "openclaw_real_home_blocked": all(
            item["attestation"].get("sandbox", {}).get("real_home_is_blocked") is True
            for item in attestations
            if item["host"] == "openclaw"
        ),
    }


def run_campaign(
    *,
    campaign_id: str,
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
    revision = source_revision(ROOT)
    if revision["dirty"]:
        raise RuntimeError("native after-turn attribution requires a clean checkout")
    artifact_root = (output_root / campaign_id).resolve()
    if artifact_root.exists():
        raise FileExistsError(f"campaign directory already exists: {artifact_root}")
    artifact_root.mkdir(parents=True)
    codex_plugin_root = _discover_codex_plugin(openclaw_binary)
    order = []
    for index, seed in enumerate(OFFICIAL_SEEDS):
        hosts = ("openclaw", "hermes") if index % 2 == 0 else ("hermes", "openclaw")
        order.extend({"seed": seed, "host": host, "arm": "A"} for host in hosts)
    write_json(
        artifact_root / "plan.json",
        {
            "schema_version": "daemon.native-afterturn-plan.v1",
            "protocol_version": PROTOCOL_VERSION,
            "campaign_id": campaign_id,
            "campaign_kind": "corrective-dry-run",
            "report_eligible": False,
            "language": LANGUAGE,
            "source": revision,
            "scenario_id": "continuity.decision",
            "seeds": list(OFFICIAL_SEEDS),
            "timeout_ms": timeout_ms,
            "lifecycle_timeout_seconds": lifecycle_timeout_seconds,
            "openclaw_minimum_final_observation_seconds": 35,
            "order": order,
            "synthetic_isolation": True,
        },
    )

    runs: list[dict[str, Any]] = []
    attestations: list[dict[str, Any]] = []
    for sequence, item in enumerate(order, start=1):
        seed = int(item["seed"])
        host = str(item["host"])
        print(f"[{sequence}/12] host={host} seed={seed} arm=A", flush=True)
        pair_id = f"native-afterturn-{host}-{seed}"
        if host == "hermes":
            run, attestation = run_hermes_arm(
                campaign_id=campaign_id,
                arm="A",
                scenario_id="continuity.decision",
                seed=seed,
                pair_id=pair_id,
                artifact_root=artifact_root,
                runtime_parent=runtime_parent,
                hermes_binary=hermes_binary,
                hermes_home=hermes_home,
                model="gpt-5.6-terra",
                provider="openai-codex",
                timeout_ms=timeout_ms,
                full_lifecycle=True,
                lifecycle_timeout_seconds=lifecycle_timeout_seconds,
                keep_runtime=keep_runtime,
            )
        else:
            run, attestation = run_openclaw_arm(
                campaign_id=campaign_id,
                arm="A",
                scenario_id="continuity.decision",
                seed=seed,
                pair_id=pair_id,
                artifact_root=artifact_root,
                runtime_parent=runtime_parent,
                openclaw_binary=openclaw_binary,
                daemon_python=daemon_python,
                daemon_env_path=daemon_env,
                codex_plugin_root=codex_plugin_root,
                model="openai/gpt-5.6-terra",
                thinking="medium",
                timeout_ms=timeout_ms,
                full_lifecycle=True,
                lifecycle_timeout_seconds=lifecycle_timeout_seconds,
                keep_runtime=keep_runtime,
            )
        runs.append(run)
        attestations.append({"seed": seed, "host": host, "attestation": attestation})

    manifest = {
        "schema_version": "daemon.native-afterturn-manifest.v1",
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": campaign_id,
        "campaign_kind": "dry-run",
        "language": LANGUAGE,
        "report_eligible": False,
        "environment": {
            "operating_system": platform.platform(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
        },
        "source": revision,
        "hosts": [
            {
                "id": "hermes",
                "version": _version(hermes_binary, first_line=True),
                "binary_sha256": file_sha256(hermes_binary),
            },
            {
                "id": "openclaw",
                "version": _version(openclaw_binary),
                "binary_sha256": file_sha256(openclaw_binary),
            },
        ],
        "arm": {"id": "A", "label": "native_host"},
        "scenario": {
            "id": "continuity.decision",
            "seeds": list(OFFICIAL_SEEDS),
            "episode_turns": 3,
        },
    }
    write_json(artifact_root / "manifest.json", manifest)
    write_json(artifact_root / "attestations.json", attestations)
    summary = _summary(runs)
    write_json(artifact_root / "summary-native-afterturn.json", summary)
    gates = _gates(runs, attestations)
    write_json(
        artifact_root / "native-afterturn-verdict.json",
        {
            "schema_version": "daemon.native-afterturn-verdict.v1",
            "campaign_id": campaign_id,
            "conclusive": all(gates.values()),
            "gates": gates,
            "source": revision,
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
    campaign_id = args.campaign_id or f"native-afterturn-{stamp}"
    try:
        artifact_root = run_campaign(
            campaign_id=campaign_id,
            output_root=args.output_root.expanduser().resolve(),
            runtime_parent=args.runtime_parent.expanduser().resolve(),
            hermes_binary=executable_path(args.hermes_binary),
            hermes_home=args.hermes_home.expanduser().resolve(),
            openclaw_binary=executable_path(args.openclaw_binary),
            daemon_python=executable_path(args.daemon_python),
            daemon_env=args.daemon_env.expanduser().resolve(),
            timeout_ms=args.timeout_ms,
            lifecycle_timeout_seconds=args.lifecycle_timeout_seconds,
            keep_runtime=args.keep_runtime,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Native after-turn attribution failed: {exc}", file=sys.stderr)
        return 1
    verdict = json.loads(
        (artifact_root / "native-afterturn-verdict.json").read_text(encoding="utf-8")
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
