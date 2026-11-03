"""Replay all six continuity.decision pairs in isolated OpenClaw profiles."""

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
from benchmarks.productivity_v1.fixtures.continuity_decision import OFFICIAL_SEEDS
from benchmarks.productivity_v1.report.recalculate import recalculate
from benchmarks.productivity_v1.runner.common import (
    canonical_json,
    executable_path,
    file_sha256,
    source_revision,
    write_json,
)
from benchmarks.productivity_v1.runner.openclaw import (
    DEFAULT_DAEMON_ENV,
    DEFAULT_DAEMON_PYTHON,
    DEFAULT_OPENCLAW_BINARY,
    run_pair,
)
from benchmarks.productivity_v1.runner.run_dry_run import _checksums, _version
from benchmarks.productivity_v1.runner.run_openclaw_window import (
    _gate,
    _stable_arm_attestations,
)


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = ROOT / ".cache" / "benchmarks" / "productivity-v1"
DEFAULT_RUNTIME_PARENT = (
    Path.home() / ".cache" / "daemon-public-productivity-v1" / "runtime"
)
PLUGIN_PATH = ROOT / "integrations" / "openclaw_plugin" / "index.js"
ORDER = tuple(
    (seed, ("A", "C") if index % 2 == 0 else ("C", "A"))
    for index, seed in enumerate(OFFICIAL_SEEDS)
)


def run_replay(
    *,
    campaign_id: str,
    output_root: Path,
    runtime_parent: Path,
    openclaw_binary: Path,
    daemon_python: Path,
    daemon_env: Path,
    timeout_ms: int,
    keep_runtime: bool = False,
) -> Path:
    revision = source_revision(ROOT)
    if revision["dirty"]:
        raise RuntimeError(
            "the live benchmark requires a clean checkout so its daemon commit is unambiguous"
        )
    artifact_root = (output_root / campaign_id).resolve()
    if artifact_root.exists():
        raise FileExistsError(f"campaign directory already exists: {artifact_root}")
    artifact_root.mkdir(parents=True)
    plan = {
        "schema_version": "daemon.openclaw-continuity-replay-plan.v1",
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": campaign_id,
        "campaign_kind": "corrective-dry-run",
        "report_eligible": False,
        "language": LANGUAGE,
        "source": revision,
        "scenario_id": "continuity.decision",
        "timeout_ms": timeout_ms,
        "order": [{"seed": seed, "arms": list(arm_order)} for seed, arm_order in ORDER],
        "isolation": {
            "synthetic_data_only": True,
            "production_channel": False,
            "production_gateway": False,
            "disposable_profiles": True,
            "disposable_home": True,
            "docker_sandbox": True,
            "host_home_mounted": False,
        },
        "native_memory": {
            "arm_a_provider": "memory-core",
            "transcript_index": "official_force_index_between_history_and_resume",
            "direct_memory_seed": False,
            "arm_c_provider": "daemon-harness",
        },
    }
    write_json(artifact_root / "plan.json", plan)

    runs: list[dict[str, Any]] = []
    pair_attestations: list[dict[str, dict[str, Any]]] = []
    for index, (seed, arm_order) in enumerate(ORDER, start=1):
        print(
            f"[{index}/{len(ORDER)}] continuity.decision seed={seed} "
            f"order={'/'.join(arm_order)}",
            flush=True,
        )
        pair_runs, attestations = run_pair(
            campaign_id=campaign_id,
            scenario_id="continuity.decision",
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
            keep_runtime=keep_runtime,
        )
        runs.extend(pair_runs)
        pair_attestations.append(attestations)

    arm_attestations, attestations_stable = _stable_arm_attestations(pair_attestations)
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
            {
                "id": "openclaw",
                "version": _version(openclaw_binary),
                "binary_sha256": file_sha256(openclaw_binary),
                "model": {
                    "provider": "openai",
                    "name": "gpt-5.6-terra",
                    "reasoning_effort": "medium",
                },
                "arm_attestations": arm_attestations,
            }
        ],
        "arms": [
            {"id": "A", "label": "native_host"},
            {"id": "C", "label": "host_with_daemon"},
        ],
        "scenarios": [
            {
                "id": "continuity.decision",
                "seeds": list(OFFICIAL_SEEDS),
                "max_turns": 3,
                "timeout_ms": timeout_ms,
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
        pair_attestations=pair_attestations,
        summary=summary,
        stable_attestations=attestations_stable,
    )
    write_json(
        artifact_root / "replay-verdict.json",
        {
            "schema_version": "daemon.openclaw-continuity-replay-verdict.v1",
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
            "plugin_sha256": file_sha256(PLUGIN_PATH),
            "pair_attestations": pair_attestations,
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
    parser.add_argument("--openclaw-binary", type=Path, default=DEFAULT_OPENCLAW_BINARY)
    parser.add_argument("--daemon-python", type=Path, default=DEFAULT_DAEMON_PYTHON)
    parser.add_argument("--daemon-env", type=Path, default=DEFAULT_DAEMON_ENV)
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    parser.add_argument("--keep-runtime", action="store_true")
    args = parser.parse_args()
    campaign_id = args.campaign_id or f"openclaw-continuity-replay-{stamp}"
    try:
        artifact_root = run_replay(
            campaign_id=campaign_id,
            output_root=args.output_root.expanduser().resolve(),
            runtime_parent=args.runtime_parent.expanduser().resolve(),
            openclaw_binary=executable_path(args.openclaw_binary),
            daemon_python=executable_path(args.daemon_python),
            daemon_env=args.daemon_env.expanduser().resolve(),
            timeout_ms=args.timeout_ms,
            keep_runtime=args.keep_runtime,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"OpenClaw continuity replay failed: {exc}", file=sys.stderr)
        return 1
    verdict = json.loads(
        (artifact_root / "replay-verdict.json").read_text(encoding="utf-8")
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
