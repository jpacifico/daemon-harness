"""Continuity Benchmark: a fixed, balanced corpus and a daemon arm shaped like a fresh Companion daemon."""

import json
from collections import Counter

import pytest

from benchmarks.continuity import corpus
from benchmarks.continuity import run


def test_six_families_of_four_scenarios_with_one_counter_case_each():
    families = Counter(scenario.family for scenario in corpus.SCENARIOS)
    assert families == {name: 4 for name in ("people", "work", "time", "correction", "unknown", "relationship")}
    assert Counter(scenario.family for scenario in corpus.SCENARIOS if scenario.counter) == {name: 1 for name in families}
    assert len({scenario.id for scenario in corpus.SCENARIOS}) == 24


# sha256 of the same fields read from corpus.py at the tag `continuity-1.0.0`, where the
# outcome was called next_step; kept here so the check needs no Git history.
SCENARIOS_1_0 = "2a6699641c0753999f2e6a1d5702f3e4515593331209398038a6ca401e50f2fd"


def test_the_scenarios_are_exactly_those_of_1_0():
    import hashlib
    same = ("id", "family", "counter", "telling", "change", "asking", "must_use", "must_not", "no_repeat",
            "unknown", "elsewhere")
    rows = [[getattr(scenario, f) for f in same] + [scenario.outcome] for scenario in corpus.SCENARIOS]
    text = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    assert hashlib.sha256(text.encode()).hexdigest() == SCENARIOS_1_0


def test_every_scenario_has_a_person_sheet_facts_and_an_outcome():
    for scenario in (*corpus.SCENARIOS, corpus.ENGINEERING):
        assert scenario.telling and scenario.asking and scenario.person and scenario.must_use and scenario.outcome, scenario.id
        assert 0 <= scenario.elsewhere < len(corpus.ELSEWHERE), scenario.id
    assert all(scenario.unknown for scenario in corpus.SCENARIOS if scenario.family == "unknown" and not scenario.counter)


def test_a_scenario_is_four_sessions_or_three_without_a_change():
    with_change = next(scenario for scenario in corpus.SCENARIOS if scenario.change)
    without = next(scenario for scenario in corpus.SCENARIOS if not scenario.change)
    assert run.sessions(with_change) == [with_change.telling, corpus.ELSEWHERE[with_change.elsewhere],
                                         with_change.change, (with_change.asking,)]
    assert len(run.sessions(without)) == 3 and run.sessions(without)[-1] == (without.asking,)


def test_the_order_is_public_and_covers_every_episode_once():
    items = run.episodes()
    first, second = run.order(items), run.order(items)
    assert first == second
    assert sorted(entry["id"] for entry in first) == sorted(item["id"] for item in items)
    assert len(items) == 24 and all(sorted(entry["arms"]) == ["A", "C"] for entry in first)


def test_the_daemon_arm_gets_the_fresh_companion_profile_over_the_cli(tmp_path):
    memory_root = run.initialize_daemon_memory(tmp_path / "vault")
    values = run.daemon_values(tmp_path, memory_root, tmp_path / "t.jsonl", tmp_path / "l.jsonl")
    assert values["DAEMON_CORE_INSTALLATION_PROFILE"] == "parga-v1"
    assert values["DAEMON_CORE_TEMPORAL_CURATOR"] == "on" and values["DAEMON_CORE_FOLLOWUPS"] == "off"
    assert all("cli" in value.split(",") for key, value in values.items() if key.endswith("_PLATFORMS"))
    assert values["DAEMON_CORE_SOVEREIGN_PLATFORMS"] == "cli,telegram"
    assert values["DAEMON_CORE_MEMORY_ROOT"] == str(memory_root)
    identity = json.loads((memory_root / "core/daemon_identity.json").read_text())
    assert identity == {"name": "Aster", "gender": "feminine"}
    assert (memory_root / "users" / run.USER / "persona.md").is_file()


def test_a_refreshed_sign_in_returns_to_the_store_and_a_broken_one_does_not(tmp_path):
    store, copy = tmp_path / "store.json", tmp_path / "copy.json"
    store.write_text('{"tokens": "old"}')
    copy.write_text('{"tokens": "new"}')
    run.return_credentials(copy, store)
    assert json.loads(store.read_text()) == {"tokens": "new"}
    copy.write_text('{"tokens": ')
    with pytest.raises(json.JSONDecodeError):
        run.return_credentials(copy, store)
    assert json.loads(store.read_text()) == {"tokens": "new"}


def test_a_quotation_must_occur_in_the_reply():
    from benchmarks.continuity.judge import _status, quoted
    reply = "Since Noor can’t do stairs,   let’s stay in with a board game."
    assert quoted("Noor can't do stairs, let's stay in", reply)
    assert not quoted("Noor loves hiking", reply) and not quoted("", reply)
    assert _status("must_use", {"verdict": "met", "quote": "board game"}, reply) == "pass"
    assert _status("must_use", {"verdict": "met", "quote": "a museum"}, reply) == "unverified"
    assert _status("no_repeat", {"verdict": "clear", "quote": ""}, reply) == "pass"
    assert _status("no_repeat", {"verdict": "asked", "quote": "Who is coming?"}, reply) == "unverified"


def _campaign(tmp_path, verdict_for):
    from benchmarks.continuity import judge
    from benchmarks.productivity_v1.runner.common import append_jsonl
    campaign = tmp_path / "campaign"
    for scenario in corpus.SCENARIOS:
        for arm in ("A", "C"):
            append_jsonl(campaign / "runs.jsonl", {
                "run_id": f"{scenario.id}-{arm}", "episode": scenario.id, "kind": "scenario", "arm": arm,
                "dry_run": False, "family": scenario.family, "status": "completed", "tools": [],
                "tokens": {"host.visible": {"total": {"value": 1000}}, "host.background": {"total": {"value": 0}},
                           "daemon.critical": {"total": {"value": 0}}, "daemon.background": {"total": {"value": 5}}},
                "turns": [{"scored": True, "returncode": 0, "response_ms": 9000, "native_background_wait_ms": 0,
                           "daemon_background_wait_ms": 100 if arm == "C" else 0, "prompt": f"message {n}",
                           "response": f"reply {'one' if arm == 'A' else 'two'} {n} for {scenario.id}"}
                          for n in (1, 2)]})
    judge.prepare(campaign)
    key = json.loads((campaign / "private/judging-blind-key.json").read_text())
    for judge_id in judge.JUDGES:
        for order in judge.ORDERS:
            for path in sorted((campaign / "judging/packets" / order).glob("batch-*.json")):
                verdicts = {}
                for packet in json.loads(path.read_text()):
                    scenario = next(item for item in corpus.SCENARIOS if item.id == key[packet["packet"]]["scenario"])
                    verdicts[packet["packet"]] = {
                        label: verdict_for(scenario, key[packet["packet"]][label],
                                           judge.assistant_text(packet["final_conversations"][label]), judge_id)
                        for label in ("X", "Y")} | {"preference": {"choice": next(
                            label for label in ("X", "Y") if key[packet["packet"]][label] == "C"), "reason": "r"}}
                out = campaign / "judging/verdicts" / f"{judge_id}-{order}-{path.name}"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps({"judge": judge_id, "order": order, "verdicts": verdicts}))
    return campaign


def test_blind_packets_hide_the_arms_and_merge_back_to_them(tmp_path):
    from benchmarks.continuity import judge, report

    def verdict_for(scenario, arm, reply, judge_id):
        good = arm == "C"
        return {"must_use": [{"verdict": "met" if good else "not_met", "quote": reply[:5] if good else ""}
                             for _ in scenario.must_use],
                "must_not": [{"verdict": "clear", "quote": ""} for _ in scenario.must_not],
                "no_repeat": [{"verdict": "clear", "quote": ""} for _ in scenario.no_repeat],
                "unknown": [{"verdict": "clear", "quote": ""} for _ in scenario.unknown],
                "outcome": {"verdict": "met", "quote": reply[:5]}, "first_help": 2 if good else 1}

    campaign = _campaign(tmp_path, verdict_for)
    packets = json.loads((campaign / "judging/packets/XY/batch-1.json").read_text())
    assert all(set(p) == {"packet", "earlier_conversations", "criteria", "final_conversations"}
               and set(p["final_conversations"]) == {"X", "Y"} and "outcome" in p["criteria"] for p in packets)
    judge.merge(campaign)
    table = report.build(campaign)
    assert table["arms"]["C"]["scenarios_passed"] == 24 and table["arms"]["A"]["scenarios_passed"] == 0
    assert table["arms"]["C"]["preferred"] == 24 and table["ties"] == 0
    assert table["arms"]["C"]["first_help_median"] == 2 and table["arms"]["C"]["final_replies_median"] == 2
    assert table["gates"]["improves"]["claim_allowed"] is True
    assert table["gates"]["keeps_capabilities"] == {"measured": False, "see": "continuity-1.0.0"}
    table_md = (campaign / "report/table.md").read_text()
    assert "| Scenarios passed | 0 / 24 | 24 / 24 |" in table_md
    assert "| Full turn per message, median (90th percentile) | 9.0 s (9.0 s) | 9.1 s (9.1 s) |" in table_md
    assert table["tokens_total_ratio_c_over_a"] == 1005 / 1000


def test_one_judge_disagreeing_is_never_a_pass(tmp_path):
    from benchmarks.continuity import judge, report

    def verdict_for(scenario, arm, reply, judge_id):
        met = judge_id == "j1"  # the two judges never agree on facts
        return {"must_use": [{"verdict": "met" if met else "not_met", "quote": reply[:5]} for _ in scenario.must_use],
                "must_not": [{"verdict": "clear", "quote": ""} for _ in scenario.must_not],
                "no_repeat": [{"verdict": "clear", "quote": ""} for _ in scenario.no_repeat],
                "unknown": [{"verdict": "clear", "quote": ""} for _ in scenario.unknown],
                "outcome": {"verdict": "met", "quote": reply[:5]}, "first_help": 1}

    campaign = _campaign(tmp_path, verdict_for)
    judge.merge(campaign)
    table = report.build(campaign)
    assert table["arms"]["A"]["scenarios_passed"] == table["arms"]["C"]["scenarios_passed"] == 0
    assert table["arms"]["C"]["disagreements"] > 0


def test_the_published_dataset_is_exactly_the_preregistered_corpus():
    from benchmarks.continuity import export_dataset
    files = export_dataset.lines()
    files["manifest.json"] = export_dataset.manifest(files)
    for name, text in files.items():
        assert (export_dataset.DATASET / name).read_text() == text, name
    scenarios = [json.loads(line) for line in files["scenarios.jsonl"].splitlines()]
    assert [scenario["id"] for scenario in scenarios] == [scenario.id for scenario in corpus.SCENARIOS]
    assert all(scenario["sessions"][-1] == {"stage": "final", "messages": [source.asking]}
               and scenario["person"] == source.person for scenario, source in zip(scenarios, corpus.SCENARIOS))


def test_each_judge_call_is_isolated_and_its_raw_answer_kept(tmp_path):
    import stat
    import sys
    from benchmarks.continuity import judge

    def verdict_for(scenario, arm, reply, judge_id):
        return {}

    campaign = _campaign(tmp_path, verdict_for)
    for path in (campaign / "judging/verdicts").glob("*.json"):
        path.unlink()
    fake = tmp_path / "claude"
    fake.write_text(f"""#!{sys.executable}
import json, os, re, sys
args, prompt = sys.argv[1:], sys.stdin.read()
assert args[args.index("--tools") + 1] == "" and "--strict-mcp-config" in args and os.listdir(".") == []
packets = re.findall(r'"packet": "(P[0-9]+)"', prompt)
print(json.dumps({{"is_error": False, "modelUsage": {{"claude-opus-5-5": {{}}}},
                  "result": json.dumps({{"verdicts": {{p: {{}} for p in packets}}}})}}))
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    judge.run_judges(campaign, fake)
    assert len(list((campaign / "judging/verdicts").glob("*.json"))) == 2 * 2 * 3
    assert len(list((campaign / "judging/raw").glob("*.json"))) == 12


def test_the_simulated_person_never_sees_the_criteria_and_can_end(tmp_path):
    import stat
    import sys
    from benchmarks.continuity import simulate
    scenario = next(item for item in corpus.SCENARIOS if item.id == "people.01")
    before = run.sessions(scenario)[:-1]
    text = simulate.prompt(before, scenario, [(scenario.asking, "Who is coming?")])
    assert scenario.person in text and "Assistant: Who is coming?" in text and scenario.telling[0] in text
    assert scenario.outcome not in text and not any(item in text for item in (*scenario.must_use, *scenario.must_not))
    answers = tmp_path / "answers"
    answers.write_text("Just the two of us.\nEND\n")
    fake = tmp_path / "claude"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
assert args[args.index("--tools") + 1] == "" and "--strict-mcp-config" in args and os.listdir(".") == []
assert args[args.index("--model") + 1] == "claude-opus-5-5"
lines = open({str(answers)!r}).read().splitlines()
open({str(answers)!r}, "w").write("\\n".join(lines[1:]))
print(json.dumps({{"is_error": False, "result": lines[0]}}))
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "sim" / "after-turn-1"
    assert simulate.next_message(fake, before, scenario, [(scenario.asking, "Who is coming?")], record) == "Just the two of us."
    assert simulate.next_message(fake, before, scenario, [(scenario.asking, "x"), ("Just the two of us.", "y")], record) is None
    assert (tmp_path / "sim" / "after-turn-1-attempt-1.json").is_file()


def test_published_records_leave_out_runtimes_and_personal_paths(tmp_path):
    from benchmarks.continuity import export_records
    campaign = tmp_path / "continuity-test"
    home = "/Users/someone"
    files = {
        "plan.json": json.dumps({"root": f"{home}/bench"}),
        "runs.jsonl": "{}\n",
        "episodes/people.01-A.json": json.dumps({"response": f"File: {home}/bench/out.csv"}),
        "evidence/people.01-A/host-trace.jsonl.json": "[]",
        "judging/packets/XY/batch-1.json": "{}",
        "judging/merged.json": "{}",
        "report/summary.json": "{}",
        "private/judging-blind-key.json": "{}",
        "private/people.01-A/simulator/after-turn-4-attempt-1.json": "{}",
        "private/people.01-A/codex-home/auth.json": json.dumps({"refresh_token": "x"}),
        "private/people.01-A/vault/.daemon/users/1/persona.md": "persona",
        ".hermes/auth.json": "{}",
    }
    for name, text in files.items():
        (campaign / name).parent.mkdir(parents=True, exist_ok=True)
        (campaign / name).write_text(text)
    manifest = export_records.export(campaign, tmp_path / "out", home=home)
    kept = set(manifest["files"])
    assert "private/people.01-A/simulator/after-turn-4-attempt-1.json" in kept
    assert "private/judging-blind-key.json" in kept and "judging/merged.json" in kept
    assert not any("codex-home" in name or "vault" in name or ".hermes" in name for name in kept)
    published = tmp_path / "out" / "continuity-test"
    assert "~/bench/out.csv" in (published / "episodes/people.01-A.json").read_text()
    assert home not in "".join(p.read_text() for p in published.rglob("*") if p.is_file())


def test_published_records_refuse_a_forbidden_pattern(tmp_path):
    from benchmarks.continuity import export_records
    campaign = tmp_path / "continuity-test"
    (campaign / "episodes").mkdir(parents=True)
    (campaign / "episodes/x.json").write_text('{"note": "refresh_token here"}')
    with pytest.raises(SystemExit):
        export_records.export(campaign, tmp_path / "out", home="/Users/someone")
    assert not (tmp_path / "out" / "continuity-test").exists()
