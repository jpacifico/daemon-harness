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

from benchmarks.continuity.corpus import SCENARIOS
from benchmarks.productivity_v1.runner.common import read_jsonl, write_json

ARMS = ("A", "C")
FAMILIES = ("people", "work", "time", "correction", "unknown", "relationship")
MIN_DIFFERENCE = 6  # of the 24 scenarios, as in 1.0


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def total(scope: dict) -> int | None:
    value = scope.get("total", {}).get("value") if isinstance(scope, dict) else None
    return value if isinstance(value, int) else None


def scenario_measures(merged: dict, arm: str) -> dict[str, dict[str, bool | int]]:
    out = {}
    for scenario_id, result in merged.items():
        items = result["arms"][arm]
        def kind(name):
            return [v["result"] for k, v in items.items() if k.startswith(name)]
        out[scenario_id] = {
            "passed": all(v["result"] == "pass" for v in items.values()),
            "remembers": all(r == "pass" for r in kind("must_use")),
            "asks_again": kind("no_repeat").count("fail"),
            "outdated": "fail" in kind("must_not"),
            "invents": "fail" in kind("unknown"),
            "outcome": kind("outcome") == ["pass"],
            "disagreements": sum(v["result"] == "disagreement" for v in items.values()),
        }
    return out


def bootstrap_difference(a: list[bool], c: list[bool], *, draws: int = 10_000, seed: int = 20261031) -> tuple[float, float]:
    """95% paired bootstrap interval of passed(C) - passed(A), in scenarios."""
    rng = random.Random(seed)
    pairs = list(zip(a, c))
    stats = sorted(sum(cc - aa for aa, cc in (rng.choice(pairs) for _ in pairs)) for _ in range(draws))
    return stats[int(0.025 * draws)], stats[int(0.975 * draws) - 1]


def _median(values: list[int | None]) -> float | None:
    """A scenario with a missing count is left out and reported, never read as zero."""
    known = [v for v in values if v is not None]
    return median(known) if known else None


def build(campaign: Path, stage: str = "judging") -> dict:
    runs = [r for r in read_jsonl(campaign / "runs.jsonl") if not r["dry_run"]]
    merged = json.loads((campaign / stage / "merged.json").read_text())
    measures = {arm: scenario_measures(merged, arm) for arm in ARMS}
    scenarios = sorted(merged)
    table: dict = {"scenarios": len(scenarios), "scenarios_with_earlier_facts": sum(
        1 for scenario in SCENARIOS if scenario.id in merged and scenario.must_use), "arms": {}}
    for arm in ARMS:
        m = measures[arm]
        episodes = {r["episode"]: r for r in runs if r["arm"] == arm and r["episode"] in merged}
        scored_ms = [t["response_ms"] for r in episodes.values() if r["kind"] == "scenario"
                     for t in r["turns"] if t["scored"] and t["returncode"] == 0]
        replies = [sum(1 for t in r["turns"] if t["scored"]) for r in episodes.values()]
        first_help = [merged[scenario]["first_help"][arm]["result"] for scenario in scenarios]
        # After-turn work on both sides: Hermes's native background review, the daemon's after-turn.
        scenario_runs = [r for r in episodes.values() if r["kind"] == "scenario"]
        scenario_turns = [t for r in scenario_runs for t in r["turns"] if t["returncode"] == 0]
        after_ms = [t["native_background_wait_ms"] + t["daemon_background_wait_ms"] for t in scenario_turns]
        full_ms = [t["response_ms"] + t["native_background_wait_ms"] + t["daemon_background_wait_ms"]
                   for t in scenario_turns]
        visible = [total(r["tokens"]["host.visible"]) for r in scenario_runs]
        critical = [total(r["tokens"]["daemon.critical"]) for r in scenario_runs] if arm == "C" else [0] * len(scenario_runs)
        after = [None if (h := total(r["tokens"]["host.background"])) is None
                 or (d := (total(r["tokens"]["daemon.background"]) if arm == "C" else 0)) is None else h + d
                 for r in scenario_runs]
        whole = [None if None in (v, c, a) else v + c + a for v, c, a in zip(visible, critical, after)]
        table["arms"][arm] = {
            "scenarios_passed": sum(v["passed"] for v in m.values()),
            "remembers": sum(v["remembers"] for v in m.values()),
            "asks_again": sum(v["asks_again"] for v in m.values()),
            "outdated": sum(v["outdated"] for v in m.values()),
            "invents": sum(v["invents"] for v in m.values()),
            "outcome": sum(v["outcome"] for v in m.values()),
            "final_replies_median": median(replies) if replies else None,
            "first_help_median": _median(first_help),
            "first_help_scenarios": sum(f is not None for f in first_help),
            "disagreements": sum(v["disagreements"] for v in m.values()),
            "preferred": sum(result["preference"]["result"] == arm for result in merged.values()),
            "tool_errors": sum(sum(1 for t in r["tools"] if t.get("status") == "error") for r in episodes.values()),
            "reply_ms_median": median(scored_ms) if scored_ms else None,
            "reply_ms_p90": percentile(scored_ms, 0.9),
            "after_turn_ms_median": median(after_ms) if after_ms else None,
            "full_turn_ms_median": median(full_ms) if full_ms else None,
            "full_turn_ms_p90": percentile(full_ms, 0.9),
            "tokens_reply_median": _median(visible),
            "tokens_before_reply_median": _median(critical),
            "tokens_after_turn_median": _median(after),
            "tokens_total_median": _median(whole),
            "tokens_total_sum": sum(whole) if None not in whole else None,
            "tokens_unavailable_scenarios": whole.count(None),
        }
    table["ties"] = sum(result["preference"]["result"] == "tie" for result in merged.values())
    a = [measures["A"][scenario]["passed"] for scenario in scenarios]
    c = [measures["C"][scenario]["passed"] for scenario in scenarios]
    low, high = bootstrap_difference(a, c)
    A, C = table["arms"]["A"], table["arms"]["C"]
    token_ratio = (C["tokens_total_sum"] / A["tokens_total_sum"]
                   if A["tokens_total_sum"] and C["tokens_total_sum"] is not None else None)
    table["tokens_total_ratio_c_over_a"] = token_ratio
    table["efficiency_words_allowed"] = token_ratio is not None and token_ratio <= 1.0
    table["gates"] = {
        "improves": {
            "scenarios_passed_difference": C["scenarios_passed"] - A["scenarios_passed"],
            "difference_interval_95": [low, high],
            f"difference_at_least_{MIN_DIFFERENCE}": C["scenarios_passed"] - A["scenarios_passed"] >= MIN_DIFFERENCE,
            "interval_excludes_zero": low > 0,
            "invents_not_higher": C["invents"] <= A["invents"],
            "outdated_not_higher": C["outdated"] <= A["outdated"],
            "preferred_more": C["preferred"] > A["preferred"],
        },
        # Agent capabilities are not rerun in 1.1: version 1.0 measured them with the same hosts and model.
        "keeps_capabilities": {"measured": False, "see": "continuity-1.0.0"},
    }
    table["gates"]["improves"]["claim_allowed"] = all(
        v for k, v in table["gates"]["improves"].items() if isinstance(v, bool))
    table["families"] = {
        family: {"scenarios": sum(merged[scenario]["family"] == family for scenario in scenarios),
                 **{arm: sum(measures[arm][scenario]["passed"] for scenario in scenarios if merged[scenario]["family"] == family)
                    for arm in ARMS}} for family in FAMILIES}
    out = campaign / ("report" if stage == "judging" else f"{stage}-report")
    write_json(out / "summary.json", table)
    (out / "table.md").write_text(markdown(table))
    return table


def _s(ms) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f} s"


def _n(value) -> str:
    return "unavailable" if value is None else f"{round(value):,}"


def _k(value) -> str:
    return "—" if value is None else f"{value:g}"


def _ratio(value) -> str:
    return "unavailable" if value is None else f"{value:.2f}"


def markdown(t: dict) -> str:
    A, C, n = t["arms"]["A"], t["arms"]["C"], t["scenarios"]
    remembers_n = t["scenarios_with_earlier_facts"]
    rows = [
        ("**Personal assistant, %d scenarios**" % n, "", ""),
        ("Scenarios passed", f"{A['scenarios_passed']} / {n}", f"{C['scenarios_passed']} / {n}"),
        ("Remembers what was said earlier", f"{A['remembers']} / {remembers_n}", f"{C['remembers']} / {remembers_n}"),
        ("Questions about something already said", A["asks_again"], C["asks_again"]),
        ("Uses outdated or unrelated context", A["outdated"], C["outdated"]),
        ("Invents a fact", A["invents"], C["invents"]),
        ("Concrete help by the end of the conversation", f"{A['outcome']} / {n}", f"{C['outcome']} / {n}"),
        ("Reply that first gives that help, median", _k(A["first_help_median"]), _k(C["first_help_median"])),
        ("Assistant replies in the final conversation, median", _k(A["final_replies_median"]), _k(C["final_replies_median"])),
        ("Preferred in blind comparison", A["preferred"], f"{C['preferred']} (ties: {t['ties']})"),
        ("Judge disagreements (not counted as passed)", A["disagreements"], C["disagreements"]),
        ("Tool errors", A["tool_errors"], C["tool_errors"]),
        ("**Time and cost**", "", ""),
        ("Reply time per message, median (90th percentile)", f"{_s(A['reply_ms_median'])} ({_s(A['reply_ms_p90'])})",
         f"{_s(C['reply_ms_median'])} ({_s(C['reply_ms_p90'])})"),
        ("After-turn work per message, median", _s(A["after_turn_ms_median"]), _s(C["after_turn_ms_median"])),
        ("Full turn per message, median (90th percentile)", f"{_s(A['full_turn_ms_median'])} ({_s(A['full_turn_ms_p90'])})",
         f"{_s(C['full_turn_ms_median'])} ({_s(C['full_turn_ms_p90'])})"),
        ("Tokens per scenario, median: reply / after-turn", f"{_n(A['tokens_reply_median'])} / {_n(A['tokens_after_turn_median'])}",
         f"{_n(C['tokens_reply_median'])} / {_n(C['tokens_after_turn_median'])}"),
        ("Tokens per scenario, median: full turn", _n(A["tokens_total_median"]), _n(C["tokens_total_median"])),
        ("Tokens, whole campaign: ratio C / A", "1", _ratio(t["tokens_total_ratio_c_over_a"])),
    ]
    lines = ["| | Hermes alone | Hermes + daemon |", "| --- | ---: | ---: |"]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    lines += ["", "| Family | Hermes alone | Hermes + daemon |", "| --- | ---: | ---: |"]
    lines += [f"| {family} | {v['A']} / {v['scenarios']} | {v['C']} / {v['scenarios']} |" for family, v in t["families"].items()]
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
