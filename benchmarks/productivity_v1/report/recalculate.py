"""Recalculate a public benchmark summary without a model or network call."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from benchmarks.productivity_v1 import (
    LANGUAGE,
    MANIFEST_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    RUN_SCHEMA_VERSION,
    SUMMARY_SCHEMA_VERSION,
)


TOKEN_SCOPES = (
    "host.visible",
    "daemon.critical",
    "host.background",
    "daemon.background",
)
TOKEN_FIELDS = (
    "input_uncached",
    "cache_read",
    "cache_write",
    "output",
    "reasoning",
    "total",
)
TOKEN_QUALITIES = {
    "provider_reported",
    "host_reported",
    "tokenizer_estimated",
    "unavailable",
}
TIMING_FIELDS = (
    "wall_ms",
    "startup_ms",
    "first_visible_ms",
    "correct_result_ms",
    "host_ms",
    "daemon_compose_ms",
    "tool_ms",
)


class ContractError(ValueError):
    """Raised when a public artifact violates the frozen protocol contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), "manifest must be a JSON object")
    _require(
        value.get("schema_version") == MANIFEST_SCHEMA_VERSION,
        "manifest schema_version is not supported",
    )
    _require(
        value.get("protocol_version") == PROTOCOL_VERSION,
        "manifest protocol_version is not supported",
    )
    _require(value.get("language") == LANGUAGE, "manifest language must be en")
    _require(isinstance(value.get("campaign_id"), str), "campaign_id is required")
    _require(isinstance(value.get("report_eligible"), bool), "report_eligible is required")
    _require(
        value.get("analysis", {}).get("percentile_method") == "nearest-rank",
        "only nearest-rank percentiles are supported",
    )
    _require(isinstance(value.get("hosts"), list) and value["hosts"], "hosts are required")
    _require(isinstance(value.get("arms"), list) and value["arms"], "arms are required")
    _require(
        isinstance(value.get("scenarios"), list) and value["scenarios"],
        "scenarios are required",
    )
    return value


def load_runs(path: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ContractError(f"runs line {line_number} is invalid JSON") from exc
        _require(isinstance(value, dict), f"runs line {line_number} must be an object")
        runs.append(value)
    _require(bool(runs), "runs.jsonl must contain at least one record")
    return runs


def _validate_token_value(value: Any, location: str) -> None:
    _require(isinstance(value, dict), f"{location} must be an object")
    quality = value.get("quality")
    token_value = value.get("value")
    _require(quality in TOKEN_QUALITIES, f"{location}.quality is invalid")
    if quality == "unavailable":
        _require(token_value is None, f"{location}.value must be null when unavailable")
    else:
        _require(
            isinstance(token_value, int) and token_value >= 0,
            f"{location}.value must be a non-negative integer",
        )


def _validate_run(
    run: dict[str, Any],
    *,
    manifest: dict[str, Any],
    host_ids: set[str],
    arm_ids: set[str],
    scenario_seeds: dict[str, set[int]],
) -> None:
    run_id = run.get("run_id")
    _require(isinstance(run_id, str) and run_id, "run_id is required")
    _require(run.get("schema_version") == RUN_SCHEMA_VERSION, f"{run_id}: bad schema")
    _require(run.get("protocol_version") == PROTOCOL_VERSION, f"{run_id}: bad protocol")
    _require(run.get("campaign_id") == manifest["campaign_id"], f"{run_id}: bad campaign")
    _require(run.get("language") == LANGUAGE, f"{run_id}: language must be en")
    _require(run.get("host_id") in host_ids, f"{run_id}: host is not declared")
    _require(run.get("arm_id") in arm_ids, f"{run_id}: arm is not declared")
    scenario_id = run.get("scenario_id")
    _require(scenario_id in scenario_seeds, f"{run_id}: scenario is not declared")
    _require(run.get("seed") in scenario_seeds[scenario_id], f"{run_id}: seed is not declared")
    _require(run.get("status") in {"completed", "excluded"}, f"{run_id}: bad status")
    _require(isinstance(run.get("tool_calls"), list), f"{run_id}: tool_calls are required")
    _require(isinstance(run.get("artifacts"), list), f"{run_id}: artifacts are required")

    if run["status"] == "excluded":
        _require(isinstance(run.get("exclusion"), dict), f"{run_id}: exclusion is required")
        _require(run.get("outcome") is None, f"{run_id}: excluded outcome must be null")
        return

    _require(run.get("exclusion") is None, f"{run_id}: completed exclusion must be null")
    timing = run.get("timing")
    _require(isinstance(timing, dict), f"{run_id}: timing is required")
    for field in TIMING_FIELDS:
        value = timing.get(field)
        _require(
            value is None or (isinstance(value, int) and value >= 0),
            f"{run_id}: timing.{field} is invalid",
        )
    _require(timing.get("wall_ms") is not None, f"{run_id}: wall_ms is required")
    _require(
        timing.get("correct_result_ms") is not None,
        f"{run_id}: correct_result_ms is required",
    )

    tokens = run.get("tokens")
    _require(isinstance(tokens, dict), f"{run_id}: tokens are required")
    for scope in TOKEN_SCOPES:
        token_scope = tokens.get(scope)
        _require(isinstance(token_scope, dict), f"{run_id}: {scope} is required")
        call_count = token_scope.get("call_count")
        _require(
            isinstance(call_count, int) and call_count >= 0,
            f"{run_id}: {scope}.call_count is invalid",
        )
        for field in TOKEN_FIELDS:
            _validate_token_value(
                token_scope.get(field),
                f"{run_id}: tokens.{scope}.{field}",
            )

    outcome = run.get("outcome")
    _require(isinstance(outcome, dict), f"{run_id}: outcome is required")
    _require(isinstance(outcome.get("checks"), dict), f"{run_id}: checks are required")
    _require(
        bool(outcome["checks"])
        and all(isinstance(value, bool) for value in outcome["checks"].values()),
        f"{run_id}: checks must contain booleans",
    )
    for field in ("authoritative", "success_first_attempt", "success_within_episode"):
        _require(isinstance(outcome.get(field), bool), f"{run_id}: outcome.{field} is invalid")


def validate_campaign(
    manifest: dict[str, Any],
    runs: list[dict[str, Any]],
) -> None:
    host_ids = {item["id"] for item in manifest["hosts"]}
    arm_ids = {item["id"] for item in manifest["arms"]}
    scenario_seeds = {
        item["id"]: set(item["seeds"])
        for item in manifest["scenarios"]
    }
    _require(arm_ids == {"A", "C"}, "manifest must declare arms A and C")

    run_ids: set[str] = set()
    session_refs: set[str] = set()
    fixture_refs_by_pair: dict[tuple[str, str], set[str]] = defaultdict(set)
    for run in runs:
        _validate_run(
            run,
            manifest=manifest,
            host_ids=host_ids,
            arm_ids=arm_ids,
            scenario_seeds=scenario_seeds,
        )
        run_id = run["run_id"]
        _require(run_id not in run_ids, f"duplicate run_id: {run_id}")
        run_ids.add(run_id)
        session_ref = run.get("session_ref")
        _require(
            isinstance(session_ref, str) and session_ref not in session_refs,
            f"{run_id}: session_ref must be unique",
        )
        session_refs.add(session_ref)
        fixture_ref = run.get("fixture_ref")
        _require(isinstance(fixture_ref, str) and fixture_ref, f"{run_id}: fixture_ref is required")
        fixture_refs_by_pair[(run["host_id"], run["pair_id"])].add(fixture_ref)

    for pair, fixture_refs in fixture_refs_by_pair.items():
        pair_runs = [
            run
            for run in runs
            if (run["host_id"], run["pair_id"]) == pair
        ]
        arms = {run["arm_id"] for run in pair_runs}
        if arms == {"A", "C"}:
            _require(
                len(fixture_refs) == 2,
                f"paired runs must use distinct fixture refs: {pair}",
            )


def _number(value: float | int) -> float | int:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return round(value, 6) if isinstance(value, float) else value


def _nearest_rank(values: Iterable[int], percentile: float) -> int | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _distribution(values: list[int]) -> dict[str, Any]:
    return {
        "observed_count": len(values),
        "median": _number(median(values)) if values else None,
        "p90": _nearest_rank(values, 0.90),
    }


def _rate(count: int, denominator: int) -> float | None:
    return round(count / denominator, 6) if denominator else None


def _group_summary(
    *,
    key: tuple[str, str, str],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    host_id, arm_id, scenario_id = key
    completed = [run for run in runs if run["status"] == "completed"]
    excluded = [run for run in runs if run["status"] == "excluded"]
    first_successes = sum(
        bool(run["outcome"]["success_first_attempt"])
        for run in completed
    )
    episode_successes = sum(
        bool(run["outcome"]["success_within_episode"])
        for run in completed
    )
    checks: dict[str, Counter[bool]] = defaultdict(Counter)
    for run in completed:
        for name, passed in run["outcome"]["checks"].items():
            checks[name][bool(passed)] += 1

    timing = {
        field: _distribution(
            [
                int(run["timing"][field])
                for run in completed
                if run["timing"].get(field) is not None
            ]
        )
        for field in TIMING_FIELDS
    }
    token_summary: dict[str, Any] = {}
    for scope in TOKEN_SCOPES:
        totals = [
            int(run["tokens"][scope]["total"]["value"])
            for run in completed
            if run["tokens"][scope]["total"]["value"] is not None
        ]
        successful_totals = [
            int(run["tokens"][scope]["total"]["value"])
            for run in completed
            if run["outcome"]["success_within_episode"]
            and run["tokens"][scope]["total"]["value"] is not None
        ]
        quality_counts = Counter(
            run["tokens"][scope]["total"]["quality"]
            for run in completed
        )
        token_summary[scope] = {
            "observed_count": len(totals),
            "coverage": _rate(len(totals), len(completed)),
            "quality_counts": dict(sorted(quality_counts.items())),
            "total": sum(totals) if totals else None,
            "per_completed": _distribution(totals),
            "per_success": _distribution(successful_totals),
        }

    return {
        "host_id": host_id,
        "arm_id": arm_id,
        "scenario_id": scenario_id,
        "run_count": len(runs),
        "completed_count": len(completed),
        "excluded_count": len(excluded),
        "success_first_attempt": {
            "count": first_successes,
            "rate": _rate(first_successes, len(completed)),
        },
        "success_within_episode": {
            "count": episode_successes,
            "rate": _rate(episode_successes, len(completed)),
        },
        "checks": {
            name: {
                "passed": counts[True],
                "failed": counts[False],
            }
            for name, counts in sorted(checks.items())
        },
        "timing_ms": timing,
        "tokens": token_summary,
    }


def _paired_comparisons(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for run in runs:
        if run["status"] == "completed":
            buckets[(run["host_id"], run["pair_id"])][run["arm_id"]] = run

    by_scenario: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for (host_id, _pair_id), arms in buckets.items():
        if set(arms) == {"A", "C"}:
            a_run = arms["A"]
            c_run = arms["C"]
            _require(
                a_run["scenario_id"] == c_run["scenario_id"],
                "paired runs must share a scenario_id",
            )
            by_scenario[(host_id, a_run["scenario_id"])].append((a_run, c_run))

    comparisons: list[dict[str, Any]] = []
    for (host_id, scenario_id), pairs in sorted(by_scenario.items()):
        wall_deltas = [
            c_run["timing"]["wall_ms"] - a_run["timing"]["wall_ms"]
            for a_run, c_run in pairs
        ]
        token_deltas = [
            c_run["tokens"]["host.visible"]["total"]["value"]
            - a_run["tokens"]["host.visible"]["total"]["value"]
            for a_run, c_run in pairs
            if c_run["tokens"]["host.visible"]["total"]["value"] is not None
            and a_run["tokens"]["host.visible"]["total"]["value"] is not None
        ]
        first_delta = sum(
            int(c_run["outcome"]["success_first_attempt"])
            - int(a_run["outcome"]["success_first_attempt"])
            for a_run, c_run in pairs
        )
        episode_delta = sum(
            int(c_run["outcome"]["success_within_episode"])
            - int(a_run["outcome"]["success_within_episode"])
            for a_run, c_run in pairs
        )
        comparisons.append(
            {
                "host_id": host_id,
                "scenario_id": scenario_id,
                "matched_pair_count": len(pairs),
                "success_first_attempt_delta_count": first_delta,
                "success_within_episode_delta_count": episode_delta,
                "wall_ms_c_minus_a": _distribution(wall_deltas),
                "host_visible_tokens_c_minus_a": _distribution(token_deltas),
            }
        )
    return comparisons


def recalculate(
    *,
    manifest_path: Path,
    runs_path: Path,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    runs = load_runs(runs_path)
    validate_campaign(manifest, runs)

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        groups[(run["host_id"], run["arm_id"], run["scenario_id"])].append(run)

    exclusions = Counter(
        run["exclusion"]["code"]
        for run in runs
        if run["status"] == "excluded"
    )
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": manifest["campaign_id"],
        "campaign_kind": manifest["campaign_kind"],
        "language": LANGUAGE,
        "report_eligible": manifest["report_eligible"],
        "source": {
            "manifest_sha256": _file_sha256(manifest_path),
            "runs_sha256": _file_sha256(runs_path),
        },
        "run_count": len(runs),
        "completed_count": sum(run["status"] == "completed" for run in runs),
        "excluded_count": sum(run["status"] == "excluded" for run in runs),
        "exclusions": dict(sorted(exclusions.items())),
        "groups": [
            _group_summary(key=key, runs=group_runs)
            for key, group_runs in sorted(groups.items())
        ],
        "paired_comparisons": _paired_comparisons(runs),
        "recalculation": {
            "implementation": "stdlib-python",
            "percentile_method": "nearest-rank",
            "llm_call_count": 0,
            "network_call_count": 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    summary = recalculate(
        manifest_path=args.manifest,
        runs_path=args.runs,
    )
    rendered = canonical_json(summary)
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
