"""Prepare blinded review and deterministically recalculate recorded judgements."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
import secrets
import shutil
import statistics

from benchmarks.useful_continuity.corpus import CASES
from benchmarks.productivity_v1.runner.common import file_sha256, read_jsonl, write_json


def distribution(values: list[int | float | None]) -> dict:
    measured = sorted(v for v in values if v is not None)
    return {"measured": len(measured), "unavailable": len(values) - len(measured),
            "min": min(measured) if measured else None,
            "median": statistics.median(measured) if measured else None,
            "p90_nearest_rank": measured[math.ceil(len(measured) * .9) - 1] if measured else None,
            "max": max(measured) if measured else None}


def prepare(campaign: Path) -> None:
    if (campaign / "blind.json").exists():
        raise ValueError("Do not reshuffle an existing review")
    cases = {c.id: c for c in CASES}
    runs = [r for r in read_jsonl(campaign / "runs.jsonl") if not r["dry_run"]]
    shuffled = list(runs)
    secrets.SystemRandom().shuffle(shuffled)
    blind = []
    mapping = {}
    for index, run in enumerate(shuffled):
        case = cases[run["case_id"]]
        scored = next((t for t in run["turns"] if t["phase"] == "scored"), {})
        identifier = f"response-{index + 1:02d}"
        response = scored.get("response", "")
        mapping[identifier] = run["run_id"]
        blind.append({"id": identifier, "case": case.id, "history_user_messages": case.history,
                      "prompt": case.prompt, "response": response,
                      "response_sha256": sha256(response.encode()).hexdigest(),
                      "criteria": case.criteria})
    write_json(campaign / "private" / "blind-mapping.json", mapping)
    write_json(campaign / "blind.json", blind)
    write_json(campaign / "review-template.json", [{"id": b["id"], "checks": [
        {"criterion": i, "passed": None, "quote": "", "reason": ""} for i in range(3)],
        "naturalness": "", "redundant_questions": None} for b in blind])


def validate_review(blind: list[dict], review: list[dict]) -> dict[str, bool]:
    sources = {item["id"]: item for item in blind}
    if len({item["id"] for item in review}) != len(review) or set(sources) != {item["id"] for item in review}:
        raise ValueError("Review must contain each response exactly once")
    results = {}
    for item in review:
        source = sources[item["id"]]
        checks = item["checks"]
        if len(checks) != 3 or {c["criterion"] for c in checks} != {0, 1, 2}:
            raise ValueError("All three preregistered criteria are required")
        for check in checks:
            if type(check["passed"]) is not bool or not check["reason"].strip():
                raise ValueError("A boolean judgement and explicit reason are required")
            quote = check["quote"]
            if source["response"] and (not quote or quote not in source["response"]):
                raise ValueError("Evidence must be an exact quotation of this response")
            if not source["response"] and check["passed"]:
                raise ValueError("An absent response cannot pass")
        if type(item["redundant_questions"]) is not int or item["redundant_questions"] < 0:
            raise ValueError("Record the count of questions for already-established facts")
        results[item["id"]] = all(c["passed"] for c in checks)
    return results


def recalculate(campaign: Path) -> dict:
    blind = json.loads((campaign / "blind.json").read_text())
    review = json.loads((campaign / "review.json").read_text())
    outcomes = validate_review(blind, review)
    # Seal judgement before consulting arm identities. Reject silent relabelling.
    digest = file_sha256(campaign / "review.json")
    seal = campaign / "review-seal.json"
    if seal.exists() and json.loads(seal.read_text())["sha256"] != digest:
        raise ValueError("Review changed after unblinding; preserve it as a separate review")
    if not seal.exists():
        write_json(seal, {"sha256": digest, "reviewer": "model-assisted, arm labels withheld"})
    mapping_path = campaign / "unblinding.json"
    if not mapping_path.exists():
        mapping_path = campaign / "private/blind-mapping.json"
    mapping = json.loads(mapping_path.read_text())
    success = {mapping[key]: passed for key, passed in outcomes.items()}
    review_by_run = {mapping[item["id"]]: item for item in review}
    runs = read_jsonl(campaign / "runs.jsonl")
    arms = {}
    for arm in ("A", "C"):
        selected = [r for r in runs if not r["dry_run"] and r["arm"] == arm]
        all_turns = [t for r in selected for t in r["turns"]]
        scored_turns = [t for r in selected for t in r["turns"] if t["phase"] == "scored"]
        scopes = {}
        for scope in ("host.visible", "host.background", "daemon.critical", "daemon.background"):
            scope_values = [r["tokens"][scope] for r in selected]
            scopes[scope] = {"calls": sum(s["call_count"] for s in scope_values)}
            for field in ("input_uncached", "cache_read", "cache_write", "output", "reasoning", "total"):
                values = [s[field]["value"] for s in scope_values]
                scopes[scope][field] = sum(values) if all(v is not None for v in values) else None
        arms[arm] = {"episodes": len(selected),
                     "positive_success": sum(success[r["run_id"]] for r in selected if not r["control"]),
                     "control_success": sum(success[r["run_id"]] for r in selected if r["control"]),
                     "criterion_pass_counts": [sum(
                         next(c["passed"] for c in review_by_run[r["run_id"]]["checks"] if c["criterion"] == i)
                         for r in selected) for i in range(3)],
                     "redundant_questions": sum(review_by_run[r["run_id"]]["redundant_questions"] for r in selected),
                     "response_median_ms": statistics.median(t["response_ms"] for t in scored_turns) if scored_turns else None,
                     "latency_ms": {
                         "scored_response": distribution([t["response_ms"] for t in scored_turns]),
                         "scored_first_native_text": distribution([t["first_native_text_ms"] for t in scored_turns]),
                         "all_responses": distribution([t["response_ms"] for t in all_turns]),
                         "daemon_after_turn_wait": distribution([t["daemon_background_wait_ms"] for t in all_turns]),
                         "episode_including_maintenance": distribution([r["episode_ms"] for r in selected]),
                     },
                     "episode_total_ms": sum(r["episode_ms"] for r in selected), "tokens": scopes}
    pairs = [{"case": c.id, "control": c.control,
              "A": success.get(f"{c.id}-A"), "C": success.get(f"{c.id}-C")} for c in CASES]
    judge_path = campaign / "judge-usage.json"
    judge = json.loads(judge_path.read_text()) if judge_path.exists() else {}
    judge_total = (judge.get("usage") or {}).get("total_tokens")
    episode_total = sum(s["total"]["value"] or 0 for r in runs for s in r["tokens"].values())
    result = {"arms": arms, "pairs": pairs, "review_sha256": digest,
              "daemon_wins": sum(p["C"] is True and p["A"] is False for p in pairs),
              "native_wins": sum(p["A"] is True and p["C"] is False for p in pairs),
              "observed_episode_tokens_including_engineering": episode_total,
              "blind_judge_tokens": judge_total,
              "observed_campaign_tokens_including_engineering_and_judge": episode_total + (judge_total or 0),
              "missing_token_scopes": sum(s["total"]["value"] is None for r in runs for s in r["tokens"].values())}
    write_json(campaign / "summary.json", result)
    return result


def verify_accounting(campaign: Path) -> dict:
    """Rebuild recorded token scopes from content-safe per-call evidence."""
    from benchmarks.productivity_v1.runner.hermes import _host_token_scopes
    from benchmarks.useful_continuity.run import internal_scope

    checked = []
    for run in read_jsonl(campaign / "runs.jsonl"):
        if run["dry_run"]:
            continue
        evidence = campaign / "evidence" / run["run_id"]
        events = json.loads((evidence / "host-trace.jsonl.json").read_text())
        internal = json.loads((evidence / "daemon-llm.jsonl.json").read_text())
        visible, background, _ = _host_token_scopes(events, expected_barriers=len(run["turns"]))
        critical, deferred = internal_scope(internal, run["arm"])
        rebuilt = {"host.visible": visible, "host.background": background,
                   "daemon.critical": critical, "daemon.background": deferred}
        if rebuilt != run["tokens"]:
            raise ValueError(f"Token accounting differs from per-call evidence: {run['run_id']}")
        checked.append(run["run_id"])
    return {"checked_episodes": checked, "all_token_scopes_match": True}


def export_evidence(campaign: Path, destination: Path) -> None:
    """Export only synthetic answers and numeric telemetry, never runtime files."""
    recalculate(campaign)
    write_json(campaign / "accounting-audit.json", verify_accounting(campaign))
    if destination.exists():
        raise ValueError("Do not overwrite a published evidence bundle")
    destination.mkdir(parents=True)
    for name in ("plan.json", "runner-revision.json", "runs.jsonl", "blind.json",
                 "review.json", "review-seal.json", "summary.json", "accounting-audit.json",
                 "judge-usage.json", "judge-plan.json", "model-review-original.json"):
        source = campaign / name
        if source.exists():
            shutil.copy2(source, destination / name)
    shutil.copy2(campaign / "private/blind-mapping.json", destination / "unblinding.json")
    # No raw prompts or personal state occur in these telemetry records. Do not
    # export latency logs, stderr, auth, config, memory or native session DBs.
    for source in sorted((campaign / "evidence").glob("*/*.json")):
        if source.name not in ("host-trace.jsonl.json", "daemon-llm.jsonl.json"):
            continue
        target = destination / "evidence" / source.parent.name / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    checksums = {str(p.relative_to(destination)): file_sha256(p)
                 for p in sorted(destination.rglob("*")) if p.is_file()}
    write_json(destination / "checksums.json", checksums)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "recalculate", "export", "verify-accounting"))
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.campaign)
    elif args.action == "recalculate":
        print(json.dumps(recalculate(args.campaign), indent=2))
    elif args.action == "verify-accounting":
        print(json.dumps(verify_accounting(args.campaign), indent=2))
    elif args.destination is None:
        parser.error("export requires --destination")
    else:
        export_evidence(args.campaign, args.destination)


if __name__ == "__main__":
    main()
