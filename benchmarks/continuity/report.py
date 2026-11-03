"""Continuity Benchmark report: the published results table, recomputed offline from the records.

No model call. Inputs: runs.jsonl and judging/merged.json of one campaign.
Protocol: docs/50_continuity_benchmark.md, "Results table" and "What may be said publicly".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
from statistics import median

from benchmarks.continuity.corpus import LIVES
from benchmarks.productivity_v1.runner.common import read_jsonl, write_json

ARMS = ("A", "C")
FAMILIES = ("people", "work", "time", "correction", "unknown", "relationship")


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def total(scope: dict) -> int | None:
    value = scope.get("total", {}).get("value") if isinstance(scope, dict) else None
    return value if isinstance(value, int) else None


def life_measures(merged: dict, arm: str) -> dict[str, dict[str, bool | int]]:
    out = {}
    for life_id, result in merged.items():
        items = result["arms"][arm]
        def kind(name):
            return [v["result"] for k, v in items.items() if k.startswith(name)]
        out[life_id] = {
            "passed": all(v["result"] == "pass" for v in items.values()),
            "remembers": all(r == "pass" for r in kind("must_use")),
            "asks_again": kind("no_repeat").count("fail"),
            "outdated": "fail" in kind("must_not"),
            "invents": "fail" in kind("unknown"),
            "next_step": kind("next_step") == ["pass"],
            "disagreements": sum(v["result"] == "disagreement" for v in items.values()),
        }
    return out


def bootstrap_difference(a: list[bool], c: list[bool], *, draws: int = 10_000, seed: int = 20261031) -> tuple[float, float]:
    """95% paired bootstrap interval of passed(C) - passed(A), in lives."""
    rng = random.Random(seed)
    pairs = list(zip(a, c))
    stats = sorted(sum(cc - aa for aa, cc in (rng.choice(pairs) for _ in pairs)) for _ in range(draws))
    return stats[int(0.025 * draws)], stats[int(0.975 * draws) - 1]


def _median(values: list[int | None]) -> float | None:
    """A life with a missing count is left out and reported, never read as zero."""
    known = [v for v in values if v is not None]
    return median(known) if known else None


def build(campaign: Path, stage: str = "judging") -> dict:
    runs = [r for r in read_jsonl(campaign / "runs.jsonl") if not r["dry_run"]]
    merged = json.loads((campaign / stage / "merged.json").read_text())
    measures = {arm: life_measures(merged, arm) for arm in ARMS}
    lives = sorted(merged)
    table: dict = {"lives": len(lives), "arms": {}}
    for arm in ARMS:
        m = measures[arm]
        episodes = {r["episode"]: r for r in runs if r["arm"] == arm}
        native = [r for r in episodes.values() if r["kind"] == "native"]
        scored_ms = [t["response_ms"] for r in episodes.values() if r["kind"] == "life"
                     for t in r["turns"] if t["scored"] and t["returncode"] == 0]
        native_ms = [t["response_ms"] for r in native for t in r["turns"] if t["returncode"] == 0]
        # After-turn work on both sides: Hermes's native background review, the daemon's after-turn.
        life_runs = [r for r in episodes.values() if r["kind"] == "life"]
        life_turns = [t for r in life_runs for t in r["turns"] if t["returncode"] == 0]
        after_ms = [t["native_background_wait_ms"] + t["daemon_background_wait_ms"] for t in life_turns]
        full_ms = [t["response_ms"] + t["native_background_wait_ms"] + t["daemon_background_wait_ms"]
                   for t in life_turns]
        visible = [total(r["tokens"]["host.visible"]) for r in life_runs]
        critical = [total(r["tokens"]["daemon.critical"]) for r in life_runs] if arm == "C" else [0] * len(life_runs)
        after = [None if (h := total(r["tokens"]["host.background"])) is None
                 or (d := (total(r["tokens"]["daemon.background"]) if arm == "C" else 0)) is None else h + d
                 for r in life_runs]
        whole = [None if None in (v, c, a) else v + c + a for v, c, a in zip(visible, critical, after)]
        table["arms"][arm] = {
            "lives_passed": sum(v["passed"] for v in m.values()),
            "remembers": sum(v["remembers"] for v in m.values()),
            "asks_again": sum(v["asks_again"] for v in m.values()),
            "outdated": sum(v["outdated"] for v in m.values()),
            "invents": sum(v["invents"] for v in m.values()),
            "next_step": sum(v["next_step"] for v in m.values()),
            "disagreements": sum(v["disagreements"] for v in m.values()),
            "preferred": sum(result["preference"]["result"] == arm for result in merged.values()),
            "native_passed": sum(bool(r["native_outcome"] and r["native_outcome"].get("passed")) for r in native),
            "native_tasks": len(native),
            "tool_errors": sum(sum(1 for t in r["tools"] if t.get("status") == "error") for r in episodes.values()),
            "reply_ms_median": median(scored_ms) if scored_ms else None,
            "reply_ms_p90": percentile(scored_ms, 0.9),
            "native_reply_ms_median": median(native_ms) if native_ms else None,
            "after_turn_ms_median": median(after_ms) if after_ms else None,
            "full_turn_ms_median": median(full_ms) if full_ms else None,
            "full_turn_ms_p90": percentile(full_ms, 0.9),
            "tokens_reply_median": _median(visible),
            "tokens_before_reply_median": _median(critical),
            "tokens_after_turn_median": _median(after),
            "tokens_total_median": _median(whole),
            "tokens_total_sum": sum(whole) if None not in whole else None,
            "tokens_unavailable_lives": whole.count(None),
        }
    table["ties"] = sum(result["preference"]["result"] == "tie" for result in merged.values())
    a = [measures["A"][life]["passed"] for life in lives]
    c = [measures["C"][life]["passed"] for life in lives]
    low, high = bootstrap_difference(a, c)
    A, C = table["arms"]["A"], table["arms"]["C"]
    native_ratio = (C["native_reply_ms_median"] / A["native_reply_ms_median"]
                    if A["native_reply_ms_median"] and C["native_reply_ms_median"] else None)
    by_task = {}
    for r in runs:
        if r["kind"] == "native":
            by_task.setdefault((r["family"], r["arm"]), []).append(bool(r["native_outcome"] and r["native_outcome"].get("passed")))
    total_regression = any(not any(by_task.get((task, "C"), [True])) and any(by_task.get((task, "A"), []))
                           for task in {key[0] for key in by_task})
    token_ratio = (C["tokens_total_sum"] / A["tokens_total_sum"]
                   if A["tokens_total_sum"] and C["tokens_total_sum"] is not None else None)
    table["tokens_total_ratio_c_over_a"] = token_ratio
    table["efficiency_words_allowed"] = token_ratio is not None and token_ratio <= 1.0
    table["gates"] = {
        "improves": {
            "lives_passed_difference": C["lives_passed"] - A["lives_passed"],
            "difference_interval_95": [low, high],
            "difference_at_least_6": C["lives_passed"] - A["lives_passed"] >= 6,
            "interval_excludes_zero": low > 0,
            "invents_not_higher": C["invents"] <= A["invents"],
            "outdated_not_higher": C["outdated"] <= A["outdated"],
            "preferred_more": C["preferred"] > A["preferred"],
        },
        "keeps_capabilities": {
            "native_loss": A["native_passed"] - C["native_passed"],
            "native_loss_at_most_1": A["native_passed"] - C["native_passed"] <= 1,
            "no_task_lost_in_all_seeds": not total_regression,
            "native_reply_time_ratio": native_ratio,
            "native_reply_time_ratio_at_most_1_15": native_ratio is not None and native_ratio <= 1.15,
        },
    }
    table["gates"]["improves"]["claim_allowed"] = all(
        v for k, v in table["gates"]["improves"].items() if isinstance(v, bool))
    table["gates"]["keeps_capabilities"]["claim_allowed"] = all(
        v for k, v in table["gates"]["keeps_capabilities"].items() if isinstance(v, bool))
    table["families"] = {
        family: {"lives": sum(merged[life]["family"] == family for life in lives),
                 **{arm: sum(measures[arm][life]["passed"] for life in lives if merged[life]["family"] == family)
                    for arm in ARMS}} for family in FAMILIES}
    out = campaign / ("report" if stage == "judging" else f"{stage}-report")
    write_json(out / "summary.json", table)
    (out / "table.md").write_text(markdown(table))
    return table


def _s(ms) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f} s"


def _n(value) -> str:
    return "unavailable" if value is None else f"{round(value):,}"


def _ratio(value) -> str:
    return "unavailable" if value is None else f"{value:.2f}"


def markdown(t: dict) -> str:
    A, C, n = t["arms"]["A"], t["arms"]["C"], t["lives"]
    remembers_n = sum(1 for life in LIVES if life.must_use)
    rows = [
        ("**Personal assistant, %d lives**" % n, "", ""),
        ("Lives passed", f"{A['lives_passed']} / {n}", f"{C['lives_passed']} / {n}"),
        ("Remembers what was said earlier", f"{A['remembers']} / {remembers_n}", f"{C['remembers']} / {remembers_n}"),
        ("Questions about something already said", A["asks_again"], C["asks_again"]),
        ("Uses outdated or unrelated context", A["outdated"], C["outdated"]),
        ("Invents a fact", A["invents"], C["invents"]),
        ("Gives a useful next step", f"{A['next_step']} / {n}", f"{C['next_step']} / {n}"),
        ("Preferred in blind comparison", A["preferred"], f"{C['preferred']} (ties: {t['ties']})"),
        ("Judge disagreements (not counted as passed)", A["disagreements"], C["disagreements"]),
        ("**Agent capabilities, %d tasks**" % A["native_tasks"], "", ""),
        ("Native tasks passed", f"{A['native_passed']} / {A['native_tasks']}", f"{C['native_passed']} / {C['native_tasks']}"),
        ("Tool errors", A["tool_errors"], C["tool_errors"]),
        ("**Time and cost**", "", ""),
        ("Reply time per message, median (90th percentile)", f"{_s(A['reply_ms_median'])} ({_s(A['reply_ms_p90'])})",
         f"{_s(C['reply_ms_median'])} ({_s(C['reply_ms_p90'])})"),
        ("After-turn work per message, median", _s(A["after_turn_ms_median"]), _s(C["after_turn_ms_median"])),
        ("Full turn per message, median (90th percentile)", f"{_s(A['full_turn_ms_median'])} ({_s(A['full_turn_ms_p90'])})",
         f"{_s(C['full_turn_ms_median'])} ({_s(C['full_turn_ms_p90'])})"),
        ("Tokens per life, median: reply / after-turn", f"{_n(A['tokens_reply_median'])} / {_n(A['tokens_after_turn_median'])}",
         f"{_n(C['tokens_reply_median'])} / {_n(C['tokens_after_turn_median'])}"),
        ("Tokens per life, median: full turn", _n(A["tokens_total_median"]), _n(C["tokens_total_median"])),
        ("Tokens, whole campaign: ratio C / A", "1", _ratio(t["tokens_total_ratio_c_over_a"])),
    ]
    lines = ["| | Hermes alone | Hermes + daemon |", "| --- | ---: | ---: |"]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    lines += ["", "| Family | Hermes alone | Hermes + daemon |", "| --- | ---: | ---: |"]
    lines += [f"| {family} | {v['A']} / {v['lives']} | {v['C']} / {v['lives']} |" for family, v in t["families"].items()]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    stage = "preview-judging" if args.preview else "judging"
    print(json.dumps(build(args.campaign.resolve(), stage)["gates"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
