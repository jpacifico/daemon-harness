"""Run one preregistered OpenClaw-only native validation window."""

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
from benchmarks.productivity_v1.runner.openclaw import (
    DEFAULT_DAEMON_ENV,
    DEFAULT_DAEMON_PYTHON,
    DEFAULT_OPENCLAW_BINARY,
    run_pair,
)
from benchmarks.productivity_v1.runner.run_dry_run import _checksums, _version


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = ROOT / ".cache" / "benchmarks" / "productivity-v1"
DEFAULT_RUNTIME_PARENT = (
    Path.home() / ".cache" / "daemon-public-productivity-v1" / "runtime"
)
PLUGIN_PATH = ROOT / "integrations" / "openclaw_plugin" / "index.js"

WINDOWS: dict[int, tuple[tuple[str, int, tuple[str, str]], ...]] = {
    1: (
        ("native.files.aggregate", 1101, ("A", "C")),
        ("native.tools.extract", 1201, ("C", "A")),
        ("native.files.aggregate", 1102, ("C", "A")),
        ("native.tools.extract", 1202, ("A", "C")),
        ("native.files.aggregate", 1103, ("A", "C")),
        ("native.tools.extract", 1203, ("C", "A")),
    ),
    2: (
        ("native.files.aggregate", 1104, ("C", "A")),
        ("native.tools.extract", 1204, ("A", "C")),
        ("native.files.aggregate", 1105, ("A", "C")),
        ("native.tools.extract", 1205, ("C", "A")),
        ("native.files.aggregate", 1106, ("C", "A")),
        ("native.tools.extract", 1206, ("A", "C")),
    ),
}


def _stable_arm_attestations(
    pair_attestations: list[dict[str, dict[str, Any]]],
) -> tuple[list[dict[str, str]], bool]:
    stable = True
    result: list[dict[str, str]] = []
    for arm in ("A", "C"):
        values = [item[arm] for item in pair_attestations]
        context_hashes = {item["context_sha256"] for item in values}
        catalog_hashes = {item["tool_catalog_sha256"] for item in values}
        stable = stable and len(context_hashes) == 1 and len(catalog_hashes) == 1
        result.append(
            {
                "arm_id": arm,
                "context_sha256": values[0]["context_sha256"],
                "tool_catalog_sha256": values[0]["tool_catalog_sha256"],
            }
        )
    return result, stable


def _gate(
    *,
    runs: list[dict[str, Any]],
    pair_attestations: list[dict[str, dict[str, Any]]],
    summary: dict[str, Any],
    stable_attestations: bool,
) -> dict[str, bool]:
    return {
        "twelve_completed_records": len(runs) == 12
        and all(run["status"] == "completed" for run in runs),
        "twelve_authoritative_outcomes": len(runs) == 12
        and all(
            run["outcome"]["authoritative"] is True
            and isinstance(run["outcome"]["success_first_attempt"], bool)
            and isinstance(run["outcome"]["success_within_episode"], bool)
            for run in runs
        ),
        "host_tokens_observed": all(
            run["tokens"]["host.visible"]["total"]["value"] is not None for run in runs
        ),
        "tool_calls_observed": all(bool(run["tool_calls"]) for run in runs),
        "first_fragment_explicitly_unavailable": all(
            run["timing"]["first_visible_ms"] is None for run in runs
        ),
        "hard_sandbox_attested": all(
            item[arm].get("sandbox", {}).get("host_process_is_sandboxed") is True
            and item[arm]["sandbox"].get("backend") == "macos-sandbox-exec"
            and item[arm]["sandbox"].get("runtime_is_writable") is True
            and item[arm]["sandbox"].get("real_home_is_blocked") is True
            and len(item[arm]["sandbox"].get("attestation_sha256", "")) == 64
            for item in pair_attestations
            for arm in ("A", "C")
        ),
        "native_memory_sync_observed": all(
            item[arm].get("native_memory_sync_observed") is True
            for item in pair_attestations
            for arm in ("A", "C")
        ),
        "memory_tools_match_installation": all(
            set(item["A"].get("installation_memory_tools", ()))
            >= {"memory_get", "memory_search"}
            and set(item["C"].get("installation_memory_tools", ()))
            >= {"daemon_read_souvenirs", "daemon_recall_l0"}
            for item in pair_attestations
        ),
        "daemon_compose_observed": all(
            run["timing"]["daemon_compose_ms"] is not None
            for run in runs
            if run["arm_id"] == "C"
        ),
        "canonical_daemon_path_observed": all(
            item["C"]["canonical_path_observed"] is True for item in pair_attestations
        ),
        "external_tools_at_parity": all(
            item["A"]["external_tool_catalog_sha256"]
            == item["C"]["external_tool_catalog_sha256"]
            for item in pair_attestations
        ),
        "context_and_catalog_attestations_stable": stable_attestations,
        "offline_recalculation_complete": summary["run_count"] == 12,
    }


def run_window(
    *,
    campaign_id: str,
    window: int,
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
    ordered_pairs = WINDOWS[window]
    artifact_root = (output_root / campaign_id).resolve()
    if artifact_root.exists():
        raise FileExistsError(f"campaign directory already exists: {artifact_root}")
    artifact_root.mkdir(parents=True)
    plan = {
        "schema_version": "daemon.openclaw-native-window-plan.v1",
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": campaign_id,
        "campaign_kind": "dry-run",
        "report_eligible": False,
        "language": LANGUAGE,
        "window": window,
        "source": revision,
        "plugin_sha256": file_sha256(PLUGIN_PATH),
        "timeout_ms": timeout_ms,
        "order": [
            {"scenario_id": scenario_id, "seed": seed, "arms": list(arm_order)}
            for scenario_id, seed, arm_order in ordered_pairs
        ],
        "isolation": {
            "personal_memory": False,
            "production_channel": False,
            "production_gateway": False,
            "disposable_profiles": True,
        },
    }
    write_json(artifact_root / "plan.json", plan)

    runs: list[dict[str, Any]] = []
    pair_attestations: list[dict[str, dict[str, Any]]] = []
    for index, (scenario_id, seed, arm_order) in enumerate(ordered_pairs, start=1):
        print(
            f"[{index}/{len(ordered_pairs)}] {scenario_id} seed={seed} "
            f"order={'/'.join(arm_order)}",
            flush=True,
        )
        pair_runs, attestations = run_pair(
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
            keep_runtime=keep_runtime,
        )
        runs.extend(pair_runs)
        pair_attestations.append(attestations)

    arm_attestations, attestations_stable = _stable_arm_attestations(pair_attestations)
    scenarios = [
        {
            "id": scenario_id,
            "seeds": [
                seed
                for current_id, seed, _ in ordered_pairs
                if current_id == scenario_id
            ],
            "max_turns": 1,
            "timeout_ms": timeout_ms,
        }
        for scenario_id in ("native.files.aggregate", "native.tools.extract")
    ]
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": campaign_id,
        "campaign_kind": "dry-run",
        "language": LANGUAGE,
        "report_eligible": False,
        "ordering_seed": 20260830 + window,
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
        "scenarios": scenarios,
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
        artifact_root / "window-verdict.json",
        {
            "schema_version": "daemon.openclaw-native-window-verdict.v1",
            "campaign_id": campaign_id,
            "window": window,
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
    parser.add_argument("--window", type=int, choices=tuple(WINDOWS), required=True)
    parser.add_argument("--campaign-id")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--runtime-parent", type=Path, default=DEFAULT_RUNTIME_PARENT)
    parser.add_argument("--openclaw-binary", type=Path, default=DEFAULT_OPENCLAW_BINARY)
    parser.add_argument("--daemon-python", type=Path, default=DEFAULT_DAEMON_PYTHON)
    parser.add_argument("--daemon-env", type=Path, default=DEFAULT_DAEMON_ENV)
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    parser.add_argument("--keep-runtime", action="store_true")
    args = parser.parse_args()
    campaign_id = args.campaign_id or f"openclaw-native-window-{args.window}-{stamp}"
    try:
        artifact_root = run_window(
            campaign_id=campaign_id,
            window=args.window,
            output_root=args.output_root.expanduser().resolve(),
            runtime_parent=args.runtime_parent.expanduser().resolve(),
            openclaw_binary=executable_path(args.openclaw_binary),
            daemon_python=executable_path(args.daemon_python),
            daemon_env=args.daemon_env.expanduser().resolve(),
            timeout_ms=args.timeout_ms,
            keep_runtime=args.keep_runtime,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"OpenClaw native window failed: {exc}", file=sys.stderr)
        return 1
    verdict = json.loads(
        (artifact_root / "window-verdict.json").read_text(encoding="utf-8")
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
