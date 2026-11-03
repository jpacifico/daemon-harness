from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import urllib.request

import pytest

from benchmarks.productivity_v1.runner import openclaw as openclaw_runner

from benchmarks.productivity_v1.checkers.continuity_decision import (
    check_workspace as check_decision_workspace,
    load_control as load_decision_control,
)
from benchmarks.productivity_v1.fixtures.continuity_decision import (
    OFFICIAL_SEEDS as DECISION_SEEDS,
    build_fixture as build_decision_fixture,
    materialize_fixture as materialize_decision_fixture,
)
from benchmarks.productivity_v1 import (
    MANIFEST_SCHEMA_VERSION,
    RUN_SCHEMA_VERSION,
)
from benchmarks.productivity_v1.checkers.native_files_aggregate import (
    check_workspace,
    load_control,
)
from benchmarks.productivity_v1.fixtures.native_files_aggregate import (
    OFFICIAL_SEEDS,
    build_fixture,
    materialize_fixture,
)
from benchmarks.productivity_v1.fixtures.native_tools_extract import (
    SOURCE_PATHS as TOOL_SOURCE_PATHS,
    build_fixture as build_tool_fixture,
)
from benchmarks.productivity_v1.report.recalculate import (
    ContractError,
    canonical_json,
    recalculate,
)
from benchmarks.productivity_v1.runner.common import (
    completed_host_turn_count,
    durable_after_turn_env,
    durable_after_turn_snapshot,
    isolated_daemon_env,
    prepare_codex_cli_home,
    redact_local_paths,
)
from benchmarks.productivity_v1.runner.hermes import (
    _host_attestation as hermes_host_attestation,
    _host_token_scopes as hermes_host_token_scopes,
    _prepare_profile,
    _quiet_chat_command,
    _session_id_from_stderr,
)
from benchmarks.productivity_v1.runner.openclaw import (
    _config as openclaw_config,
    _isolate_host_environment,
    _rollout_telemetry,
    _split_process_envs,
    _sync_native_memory,
    _task_belongs_to_sessions,
)
from benchmarks.productivity_v1.runner.run_dry_run import _gate
from benchmarks.productivity_v1.runner.run_full_lifecycle_continuity import (
    ARM_ORDER as FULL_LIFECYCLE_ARM_ORDER,
    DEFAULT_SCENARIOS as FULL_LIFECYCLE_DEFAULT_SCENARIOS,
    _daemon_background_usage_complete,
    _hermes_memory_installations_attested,
    _hermes_native_background_usage_attributed,
    _ordered_cases as full_lifecycle_ordered_cases,
    _selected_hosts as full_lifecycle_selected_hosts,
    _selected_scenarios as full_lifecycle_selected_scenarios,
)
from benchmarks.productivity_v1.runner.run_native_afterturn_attribution import (
    _summary as native_afterturn_summary,
)
from benchmarks.productivity_v1.runner.run_openclaw_continuity_replay import (
    ORDER as OPENCLAW_CONTINUITY_REPLAY_ORDER,
)
from benchmarks.productivity_v1.runner.run_openclaw_window import (
    WINDOWS as OPENCLAW_WINDOWS,
    _stable_arm_attestations,
)
from benchmarks.productivity_v1.scenarios import prepare_scenario
ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "productivity_v1"
EXAMPLE = BENCHMARK / "examples" / "native_files_aggregate"


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write_expected_output(workspace: Path, control: dict) -> None:
    output_path = workspace / control["allowed_output"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(control["expected"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_native_files_fixture_is_english_deterministic_and_paired(tmp_path: Path):
    first_workspace = tmp_path / "first-workspace"
    second_workspace = tmp_path / "second-workspace"
    first_control = tmp_path / "first-control.json"
    second_control = tmp_path / "second-control.json"

    first = materialize_fixture(
        seed=OFFICIAL_SEEDS[0],
        variant="left",
        workspace=first_workspace,
        control_path=first_control,
    )
    second = materialize_fixture(
        seed=OFFICIAL_SEEDS[0],
        variant="left",
        workspace=second_workspace,
        control_path=second_control,
    )
    right = build_fixture(OFFICIAL_SEEDS[0], "right")

    assert _tree_bytes(first_workspace) == _tree_bytes(second_workspace)
    assert first_control.read_bytes() == second_control.read_bytes()
    assert first["prompt"] == second["prompt"]
    assert "Read both CSV files" in first["prompt"]
    assert first["fixture_ref"] != right["control"]["fixture_ref"]
    assert first["prompt"] == right["prompt"]
    assert (
        build_fixture(OFFICIAL_SEEDS[0], "left")["control"]["expected"]
        != right["control"]["expected"]
    )


def test_fixture_control_cannot_be_written_inside_agent_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"

    with pytest.raises(ValueError, match="outside the agent workspace"):
        materialize_fixture(
            seed=OFFICIAL_SEEDS[0],
            variant="left",
            workspace=workspace,
            control_path=workspace / "control.json",
        )


def test_native_files_checker_accepts_exact_output_and_rejects_mutation(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    control_path = tmp_path / "control.json"
    materialize_fixture(
        seed=OFFICIAL_SEEDS[0],
        variant="left",
        workspace=workspace,
        control_path=control_path,
    )
    control = load_control(control_path)
    _write_expected_output(workspace, control)

    exact = check_workspace(workspace=workspace, control=control)

    assert exact["passed"] is True
    assert all(exact["checks"].values())

    input_path = workspace / control["baseline_files"][0]
    input_path.write_text(
        input_path.read_text(encoding="utf-8") + "unexpected,row,1,1\n",
        encoding="utf-8",
    )
    (workspace / "notes.txt").write_text("out of scope\n", encoding="utf-8")

    mutated = check_workspace(workspace=workspace, control=control)

    assert mutated["passed"] is False
    assert mutated["checks"]["input_sources_unchanged"] is False
    assert mutated["checks"]["only_expected_output_changed"] is False


def test_native_tools_fixture_is_english_deterministic_and_paired():
    first = build_tool_fixture(1201, "left")
    second = build_tool_fixture(1201, "left")
    twin = build_tool_fixture(1201, "right")

    assert first == second
    assert "Inspect the controlled local source" in first["prompt_template"]
    assert first["control"]["language"] == "en"
    assert first["control"]["fixture_ref"] != twin["control"]["fixture_ref"]
    assert first["control"]["expected"] != twin["control"]["expected"]
    assert first["control"]["required_source_paths"] == list(TOOL_SOURCE_PATHS)


def test_continuity_decision_fixture_is_longitudinal_deterministic_and_paired(
    tmp_path: Path,
):
    first = build_decision_fixture(DECISION_SEEDS[0], "left")
    second = build_decision_fixture(DECISION_SEEDS[0], "left")
    twin = build_decision_fixture(DECISION_SEEDS[0], "right")

    assert first == second
    assert len(first["history_user_prompts"]) == 2
    assert first["control"]["history_message_count"] == 4
    assert first["control"]["episode_turn_count"] == 3
    assert first["control"]["fixture_ref"] != twin["control"]["fixture_ref"]
    assert all(
        first["control"]["expected"][key] != twin["control"]["expected"][key]
        for key in first["control"]["expected"]
    )
    assert first["prompt"] == twin["prompt"]
    assert all(
        value not in first["prompt"] for value in first["control"]["expected"].values()
    )

    workspace = tmp_path / "workspace"
    control_path = tmp_path / "control.json"
    materialize_decision_fixture(
        seed=DECISION_SEEDS[0],
        variant="left",
        workspace=workspace,
        control_path=control_path,
    )
    control = load_decision_control(control_path)
    _write_expected_output(workspace, control)
    assert check_decision_workspace(workspace=workspace, control=control)["passed"]

    (workspace / "notes.txt").write_text("out of scope\n", encoding="utf-8")
    assessment = check_decision_workspace(workspace=workspace, control=control)
    assert assessment["passed"] is False
    assert assessment["checks"]["only_expected_output_changed"] is False


def test_native_tools_checker_requires_exact_output_and_actual_source_reads(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    control_path = tmp_path / "control.json"
    prepared = prepare_scenario(
        scenario_id="native.tools.extract",
        seed=1201,
        variant="left",
        workspace=workspace,
        control_path=control_path,
        run_root=tmp_path,
    )
    try:
        control = json.loads(control_path.read_text(encoding="utf-8"))
        output_path = workspace / control["allowed_output"]
        output_path.parent.mkdir(parents=True)
        output_path.write_text(
            json.dumps(control["expected"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        unread = prepared.assess()
        assert unread["passed"] is False
        assert unread["checks"]["required_sources_accessed"] is False

        base_url = prepared.fixture["source_base_url"]
        for path in TOOL_SOURCE_PATHS:
            with urllib.request.urlopen(f"{base_url}{path}", timeout=2) as response:
                assert response.status == 200
                response.read()

        exact = prepared.assess()
        assert exact["passed"] is True
        assert all(exact["checks"].values())

        (tmp_path / "controlled-source" / "owners.json").write_text(
            "{}\n", encoding="utf-8"
        )
        mutated = prepared.assess()
        assert mutated["passed"] is False
        assert mutated["checks"]["source_payloads_unchanged"] is False
    finally:
        prepared.close()


def test_runtime_workspace_baseline_is_allowed_but_other_files_are_rejected(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    control_path = tmp_path / "control.json"
    prepared = prepare_scenario(
        scenario_id="native.tools.extract",
        seed=1201,
        variant="left",
        workspace=workspace,
        control_path=control_path,
        run_root=tmp_path,
        additional_baseline_files=("AGENTS.md",),
    )
    try:
        control = json.loads(control_path.read_text(encoding="utf-8"))
        (workspace / "AGENTS.md").write_text("Runtime contract.\n", encoding="utf-8")
        _write_expected_output(workspace, control)
        base_url = prepared.fixture["source_base_url"]
        for path in TOOL_SOURCE_PATHS:
            with urllib.request.urlopen(f"{base_url}{path}", timeout=2) as response:
                response.read()

        exact = prepared.assess()
        assert exact["checks"]["only_expected_output_changed"] is True

        (workspace / "notes.txt").write_text("out of scope\n", encoding="utf-8")
        mutated = prepared.assess()
        assert mutated["checks"]["only_expected_output_changed"] is False
    finally:
        prepared.close()


def test_offline_recalculation_matches_the_versioned_digest():
    summary = recalculate(
        manifest_path=EXAMPLE / "manifest.json",
        runs_path=EXAMPLE / "runs.jsonl",
    )
    digest = sha256(canonical_json(summary).encode("utf-8")).hexdigest()
    expected_digest = (
        (EXAMPLE / "expected-summary.sha256").read_text(encoding="utf-8").split()[0]
    )

    assert digest == expected_digest
    assert summary["report_eligible"] is False
    assert summary["run_count"] == 2
    assert summary["completed_count"] == 2
    assert summary["recalculation"] == {
        "implementation": "stdlib-python",
        "percentile_method": "nearest-rank",
        "llm_call_count": 0,
        "network_call_count": 0,
    }
    assert summary["paired_comparisons"][0]["wall_ms_c_minus_a"]["median"] == -100
    assert (
        summary["paired_comparisons"][0]["host_visible_tokens_c_minus_a"]["median"]
        == 70
    )


def test_offline_recalculation_rejects_cross_arm_fixture_reuse(tmp_path: Path):
    runs = [
        json.loads(line)
        for line in (EXAMPLE / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    runs[1]["fixture_ref"] = runs[0]["fixture_ref"]
    bad_runs = tmp_path / "runs.jsonl"
    bad_runs.write_text(
        "".join(json.dumps(run, sort_keys=True) + "\n" for run in runs),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="distinct fixture refs"):
        recalculate(
            manifest_path=EXAMPLE / "manifest.json",
            runs_path=bad_runs,
        )


def test_public_schemas_and_examples_use_the_frozen_english_contract():
    manifest_schema = json.loads(
        (BENCHMARK / "manifest.schema.json").read_text(encoding="utf-8")
    )
    run_schema = json.loads((BENCHMARK / "run.schema.json").read_text(encoding="utf-8"))
    manifest = json.loads((EXAMPLE / "manifest.json").read_text(encoding="utf-8"))
    runs = [
        json.loads(line)
        for line in (EXAMPLE / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert (
        manifest_schema["properties"]["schema_version"]["const"]
        == MANIFEST_SCHEMA_VERSION
    )
    assert run_schema["properties"]["schema_version"]["const"] == RUN_SCHEMA_VERSION
    assert manifest["language"] == "en"
    assert manifest["report_eligible"] is False
    assert {run["language"] for run in runs} == {"en"}
    assert "/Users/" not in (EXAMPLE / "runs.jsonl").read_text(encoding="utf-8")


def test_full_lifecycle_replay_alternates_arm_order_for_all_public_seeds():
    assert tuple(FULL_LIFECYCLE_ARM_ORDER) == DECISION_SEEDS
    assert tuple(FULL_LIFECYCLE_ARM_ORDER.values()) == (
        ("A", "C"),
        ("C", "A"),
        ("A", "C"),
        ("C", "A"),
        ("A", "C"),
        ("C", "A"),
    )


def test_full_lifecycle_replay_accepts_the_two_implemented_native_scenarios():
    assert FULL_LIFECYCLE_DEFAULT_SCENARIOS == ("continuity.decision",)
    scenarios = full_lifecycle_selected_scenarios(
        ("native.files.aggregate", "native.tools.extract")
    )
    cases = full_lifecycle_ordered_cases(scenarios)

    assert len(cases) == 24
    assert cases[0] == {
        "scenario_id": "native.files.aggregate",
        "seed": 1101,
        "host": "openclaw",
        "arms": ("A", "C"),
    }
    assert cases[1]["host"] == "hermes"
    assert cases[2]["seed"] == 1102
    assert cases[2]["host"] == "hermes"
    assert cases[2]["arms"] == ("C", "A")
    assert cases[12]["scenario_id"] == "native.tools.extract"
    assert cases[12]["seed"] == 1201
    assert cases[-1]["seed"] == 1206

    with pytest.raises(ValueError, match="must be unique"):
        full_lifecycle_selected_scenarios(
            ("native.files.aggregate", "native.files.aggregate")
        )
    with pytest.raises(ValueError, match="unknown productivity scenario"):
        full_lifecycle_selected_scenarios(("not.implemented",))


def test_full_lifecycle_replay_can_select_only_openclaw_without_changing_order():
    scenarios = full_lifecycle_selected_scenarios(
        (
            "native.files.aggregate",
            "native.tools.extract",
            "continuity.decision",
        )
    )
    hosts = full_lifecycle_selected_hosts(("openclaw",))
    cases = full_lifecycle_ordered_cases(scenarios, host_ids=hosts)

    assert len(cases) == 18
    assert {case["host"] for case in cases} == {"openclaw"}
    assert [case["arms"] for case in cases[:6]] == [
        ("A", "C"),
        ("C", "A"),
        ("A", "C"),
        ("C", "A"),
        ("A", "C"),
        ("C", "A"),
    ]

    with pytest.raises(ValueError, match="must be unique"):
        full_lifecycle_selected_hosts(("openclaw", "openclaw"))
    with pytest.raises(ValueError, match="unknown productivity host"):
        full_lifecycle_selected_hosts(("unknown",))


def test_hermes_external_tool_parity_excludes_both_memory_installations():
    common = ["terminal", "write_file"]
    native = hermes_host_attestation(
        [
            {
                "event": "api_request_start",
                "payload": {"tool_names": [*common, "memory"]},
            }
        ]
    )
    daemon = hermes_host_attestation(
        [
            {
                "event": "api_request_start",
                "payload": {
                    "tool_names": [
                        *common,
                        "daemon_read_souvenirs",
                        "daemon_recall_l0",
                    ]
                },
            }
        ]
    )

    assert native["external_tool_catalog_sha256"] == daemon[
        "external_tool_catalog_sha256"
    ]
    assert native["installation_memory_tools"] == ["memory"]
    assert daemon["installation_memory_tools"] == [
        "daemon_read_souvenirs",
        "daemon_recall_l0",
    ]

    assert _hermes_memory_installations_attested(
        [{"host": "hermes", "arms": {"A": native, "C": daemon}}]
    )
    assert not _hermes_memory_installations_attested([])


def test_native_afterturn_summary_keeps_visible_and_background_separate():
    runs = []
    for host in ("hermes", "openclaw"):
        runs.append(
            {
                "host_id": host,
                "outcome": {"success_within_episode": True},
                "tokens": {
                    "host.visible": {
                        "call_count": 2,
                        "total": {"value": 100, "quality": "host_reported"},
                    },
                    "host.background": {
                        "call_count": 1,
                        "total": {"value": 25, "quality": "host_reported"},
                    },
                },
            }
        )

    summary = native_afterturn_summary(runs)

    assert summary["run_count"] == 2
    assert summary["hosts"]["hermes"]["visible_total_tokens"] == 100
    assert summary["hosts"]["hermes"]["background_total_tokens"] == 25
    assert summary["hosts"]["hermes"]["complete_total_tokens"] == 125
    assert summary["hosts"]["openclaw"]["background_coverage_complete"] is True


def test_live_runner_redirects_all_mutable_daemon_state(tmp_path: Path):
    run_root = tmp_path / "run"
    memory_root = run_root / "memory"
    values = isolated_daemon_env(
        {
            "DAEMON_CORE_SCENE_MODEL_USER_IDS": "personal-id",
            "DAEMON_CORE_SOVEREIGN_PLATFORMS": "telegram",
        },
        run_root=run_root,
        memory_root=memory_root,
        user_id="productivity-benchmark",
        platform="openclaw",
        telemetry_path=run_root / "telemetry.jsonl",
        latency_path=run_root / "latency.jsonl",
    )

    assert values["DAEMON_CORE_MEMORY_ROOT"] == str(memory_root)
    assert values["DAEMON_CORE_SCENE_MODEL_USER_IDS"] == "productivity-benchmark"
    assert values["DAEMON_CORE_SOVEREIGN_PLATFORMS"] == "openclaw"
    assert values["DAEMON_CORE_SINGLE_PASS"] == "on"
    assert values["DAEMON_CORE_SOVEREIGN_LIBRARY"] == "on"
    assert values["DAEMON_CORE_THIN_SOVEREIGN_VOICE"] == "on"
    assert values["DAEMON_CORE_FOLLOWUPS"] == "off"
    assert values["DAEMON_CORE_AFTER_TURN_SPOOL"] == "off"
    assert values["DAEMON_CORE_VOICE_CAPTURE_FILE"] == ""
    assert Path(values["DAEMON_CORE_STATE_ROOT"]).is_relative_to(run_root)


def test_full_lifecycle_runner_enables_isolated_durable_after_turn(tmp_path: Path):
    values = durable_after_turn_env(
        isolated_daemon_env(
            {},
            run_root=tmp_path,
            memory_root=tmp_path / "memory",
            user_id="productivity-benchmark",
            platform="openclaw",
            telemetry_path=tmp_path / "telemetry.jsonl",
            latency_path=tmp_path / "latency.jsonl",
        ),
        user_id="productivity-benchmark",
    )

    assert values["DAEMON_CORE_AFTER_TURN_SPOOL"] == "on"
    assert values["DAEMON_CORE_AFTER_TURN_OWNER"] == "external"
    assert values["DAEMON_CORE_AFTER_TURN_USER_IDS"] == "productivity-benchmark"
    assert values["DAEMON_CORE_CONTINUITY_RECEIPTS"] == "shadow"
    assert values["DAEMON_CORE_SCENE_COALESCING"] == "canary"


def test_full_lifecycle_copies_codex_login_into_disposable_home(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "auth.json").write_text('{"test":"auth"}\n', encoding="utf-8")
    (source / "models_cache.json").write_text("{}\n", encoding="utf-8")

    prepare_codex_cli_home(target, source=source)

    assert (target / "auth.json").read_text(encoding="utf-8") == '{"test":"auth"}\n'
    assert (target / "models_cache.json").is_file()
    assert (target / "auth.json").stat().st_mode & 0o077 == 0


def test_full_lifecycle_snapshot_counts_terminal_receipts_and_spool(tmp_path: Path):
    spool = tmp_path / "spool"
    pending = spool / "pending" / "openclaw" / "productivity-benchmark"
    pending.mkdir(parents=True)
    (pending / "turn-2.json").write_text("{}", encoding="utf-8")
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(
        "\n".join(
            (
                '{"turn_id":"turn-1","stage":"cycle","status":"completed"}',
                '{"turn_id":"turn-2","stage":"cycle","status":"failed"}',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text("1234", encoding="utf-8")

    snapshot = durable_after_turn_snapshot(
        spool_root=spool,
        receipt_path=receipts,
        telemetry_path=telemetry,
        host="openclaw",
        user_id="productivity-benchmark",
    )

    assert snapshot["terminal_turn_count"] == 2
    assert snapshot["cycle_status_counts"] == {"completed": 1, "failed": 1}
    assert snapshot["spool_counts"]["pending"] == 1
    assert snapshot["telemetry_bytes"] == 4


def test_full_lifecycle_waits_only_for_completed_host_turns():
    assert completed_host_turn_count(
        [
            subprocess.CompletedProcess(["host"], 0),
            subprocess.CompletedProcess(["host"], 124),
            subprocess.CompletedProcess(["host"], 1),
        ]
    ) == 1


def test_full_lifecycle_accepts_zero_daemon_usage_when_no_host_turn_completed():
    runs = [
        {
            "host_id": "hermes",
            "arm_id": "C",
            "scenario_id": "native.files.aggregate",
            "seed": 1101,
            "tokens": {
                "daemon.background": {
                    "call_count": 0,
                    "total": {"value": 0},
                }
            },
        }
    ]
    attestations = [
        {
            "host": "hermes",
            "scenario_id": "native.files.aggregate",
            "seed": 1101,
            "arms": {"C": {"completed_host_turn_count": 0}},
        }
    ]

    assert _daemon_background_usage_complete(runs, attestations)
    runs[0]["tokens"]["daemon.background"]["call_count"] = 1
    assert not _daemon_background_usage_complete(runs, attestations)

    runs[0]["tokens"]["daemon.background"]["call_count"] = 1
    runs[0]["tokens"]["daemon.background"]["total"]["value"] = 10
    attestations[0]["arms"]["C"]["completed_host_turn_count"] = 1
    assert _daemon_background_usage_complete(runs, attestations)


def test_full_lifecycle_attributes_worker_calls_to_daemon_background():
    critical, background = openclaw_runner._daemon_token_scopes(
        [
            {
                "event": "llm_call",
                "phase": "unclassified",
                "thread_name": "daemon-after-turn-openclaw-productivity-benchmark",
                "provider_usage": {
                    "input_uncached_tokens": 40,
                    "cached_input_tokens": 60,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 2,
                    "total_tokens": 105,
                },
            }
        ],
        arm="C",
    )

    assert critical["call_count"] == 0
    assert background["call_count"] == 1
    assert background["input_uncached"]["value"] == 40
    assert background["cache_read"]["value"] == 60
    assert background["reasoning"]["value"] == 2
    assert background["total"]["value"] == 105


def test_hermes_native_background_requires_complete_turn_barriers():
    events = [
        {"event": "native_background_probe_installed", "payload": {}},
        {
            "event": "api_request_complete",
            "payload": {
                "phase": "visible",
                "usage": {"input_tokens": 100, "total_tokens": 120},
            },
        },
        {
            "event": "api_request_complete",
            "payload": {
                "phase": "native_background",
                "usage": {"input_tokens": 30, "total_tokens": 40},
            },
        },
        {
            "event": "native_background_barrier",
            "payload": {
                "complete": True,
                "scheduled": 1,
                "completed": 1,
                "failed": 0,
                "wait_ms": 12,
            },
        },
    ]

    visible, background, attestation = hermes_host_token_scopes(
        events,
        expected_barriers=1,
    )

    assert visible["call_count"] == 1
    assert visible["total"]["value"] == 120
    assert background["call_count"] == 1
    assert background["total"]["value"] == 40
    assert attestation["complete"] is True
    assert attestation["scheduled"] == 1
    assert attestation["completed"] == 1


def test_hermes_native_background_zero_is_unavailable_without_all_barriers():
    events = [
        {"event": "native_background_probe_installed", "payload": {}},
        {
            "event": "native_background_barrier",
            "payload": {
                "complete": True,
                "scheduled": 0,
                "completed": 0,
                "failed": 0,
                "wait_ms": 0,
            },
        },
    ]

    _, background, attestation = hermes_host_token_scopes(
        events,
        expected_barriers=2,
    )

    assert attestation["complete"] is False
    assert background["call_count"] == 0
    assert background["total"] == {"value": None, "quality": "unavailable"}


def test_openclaw_native_task_attribution_is_session_exact():
    session = "agent:main:explicit:episode-1"

    assert _task_belongs_to_sessions(
        {"ownerKey": session, "status": "running"},
        {session},
    )
    assert not _task_belongs_to_sessions(
        {"ownerKey": "agent:main:explicit:episode-10", "status": "running"},
        {session},
    )


def test_openclaw_native_memory_sync_retries_the_same_public_command(monkeypatch):
    outcomes = [
        subprocess.CompletedProcess(["openclaw"], 1, stdout="", stderr="changed"),
        subprocess.CompletedProcess(["openclaw"], 0, stdout="indexed", stderr=""),
    ]
    commands = []

    def fake_run_command(command, **_kwargs):
        commands.append(command)
        return outcomes.pop(0)

    monkeypatch.setattr(openclaw_runner, "run_command", fake_run_command)
    monkeypatch.setattr(openclaw_runner.time, "sleep", lambda _seconds: None)

    _, digest, attempts = _sync_native_memory(
        binary=Path("/bin/openclaw"),
        workspace=Path("/tmp/workspace"),
        env={},
        sandbox_profile=Path("/tmp/profile.sb"),
    )

    assert attempts == 2
    assert len(digest) == 64
    assert len(commands) == 2
    assert commands[0] == commands[1]


def test_openclaw_runner_keeps_daemon_credentials_out_of_host_environment():
    host, daemon = _split_process_envs(
        {"OPENCLAW_STATE_DIR": "/tmp/state", "PATH": "/usr/bin"},
        {
            "OPENAI_API_KEY": "daemon-only",
            "DAEMON_CORE_MEMORY_ROOT": "/tmp/memory",
        },
    )

    assert host == {"OPENCLAW_STATE_DIR": "/tmp/state", "PATH": "/usr/bin"}
    assert daemon["OPENAI_API_KEY"] == "daemon-only"
    assert daemon["DAEMON_CORE_MEMORY_ROOT"] == "/tmp/memory"
    assert daemon["OPENCLAW_STATE_DIR"] == "/tmp/state"


def test_openclaw_runner_redirects_home_and_drops_ambient_codex_identity(
    tmp_path: Path,
):
    isolated = _isolate_host_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/personal",
            "CODEX_SESSION_ID": "personal-session",
            "CODEX_THREAD_ID": "personal-thread",
            "CODEX_APP_TOOLS_PIPE_PATH": "/personal/tools.pipe",
        },
        home=tmp_path / "home",
    )

    assert isolated["PATH"] == "/usr/bin"
    assert isolated["HOME"] == str(tmp_path / "home")
    assert isolated["XDG_STATE_HOME"] == str(tmp_path / "home/.local/state")
    assert not any(key.startswith("CODEX_") for key in isolated)


def test_openclaw_runner_imports_auth_into_disposable_store_and_removes_transit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    auth_path.write_text("{}\n", encoding="utf-8")
    observed: list[list[str]] = []

    def fake_run(command, **_kwargs):
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(openclaw_runner, "run_command", fake_run)

    result = openclaw_runner._import_codex_auth(
        binary=Path("/bin/openclaw"),
        codex_home=codex_home,
        workspace=tmp_path,
        env={"HOME": str(tmp_path / "home")},
        sandbox_profile=tmp_path / "sandbox.sb",
    )

    assert observed == [
        [
            "/usr/bin/sandbox-exec",
            "-f",
            str(tmp_path / "sandbox.sb"),
            "/bin/openclaw",
            "migrate",
            "apply",
            "codex",
            "--from",
            str(codex_home),
            "--agent",
            "main",
            "--include-secrets",
            "--item",
            "auth:openai",
            "--yes",
        ]
    ]
    assert result["attempts"] == 1
    assert result["timed_out_attempts"] == 0
    assert result["duration_ms"] >= 0
    assert not auth_path.exists()


def test_openclaw_runner_retries_only_timed_out_auth_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    auth_path.write_text("{}\n", encoding="utf-8")
    attempts = 0

    def fake_run(command, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    ticks = iter((1.0, 62.0))
    monkeypatch.setattr(openclaw_runner, "run_command", fake_run)
    monkeypatch.setattr(openclaw_runner.time, "perf_counter", lambda: next(ticks))

    result = openclaw_runner._import_codex_auth(
        binary=Path("/bin/openclaw"),
        codex_home=codex_home,
        workspace=tmp_path,
        env={"HOME": str(tmp_path / "home")},
        sandbox_profile=tmp_path / "sandbox.sb",
    )

    assert result == {"attempts": 3, "timed_out_attempts": 2, "duration_ms": 61000}
    assert not auth_path.exists()


def test_openclaw_runner_requires_writable_runtime_and_blocked_real_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    profile = tmp_path / "sandbox.sb"
    profile.write_text("(version 1)\n", encoding="utf-8")

    def fake_run(command, *, cwd, **_kwargs):
        assert cwd == tmp_path
        if command[-2] == "/usr/bin/touch":
            Path(command[-1]).touch()
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="denied")

    monkeypatch.setattr(openclaw_runner, "run_command", fake_run)

    attestation = openclaw_runner._outer_sandbox_attestation(
        sandbox_profile=profile,
        runtime_root=tmp_path,
    )

    assert attestation["backend"] == "macos-sandbox-exec"
    assert attestation["runtime_is_writable"] is True
    assert attestation["real_home_is_blocked"] is True


def test_live_runner_builds_clean_hermes_profiles(tmp_path: Path):
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text("{}\n", encoding="utf-8")
    daemon_values = {"OPENAI_API_KEY": "test-only"}

    a_profile = tmp_path / "profiles" / "a"
    c_profile = tmp_path / "profiles" / "c"
    _prepare_profile(
        source_home=source_home,
        profile=a_profile,
        arm="A",
        model="gpt-5.6-terra",
        provider="openai-codex",
        daemon_values=daemon_values,
    )
    _prepare_profile(
        source_home=source_home,
        profile=c_profile,
        arm="C",
        model="gpt-5.6-terra",
        provider="openai-codex",
        daemon_values=daemon_values,
    )

    a_config = __import__("yaml").safe_load(
        (a_profile / "config.yaml").read_text(encoding="utf-8")
    )
    c_config = __import__("yaml").safe_load(
        (c_profile / "config.yaml").read_text(encoding="utf-8")
    )
    assert a_config["memory"] == {
        "memory_enabled": True,
        "user_profile_enabled": True,
        "provider": "",
        "write_approval": False,
    }
    assert c_config["memory"]["provider"] == "daemon_core"
    assert c_config["memory"]["memory_enabled"] is False
    assert a_config["skills"]["creation_nudge_interval"] == 10
    assert c_config["skills"]["creation_nudge_interval"] == 0
    assert a_config["auxiliary"]["background_review"]["enabled"] is True
    assert c_config["auxiliary"]["background_review"]["enabled"] is False
    assert c_config["display"]["memory_notifications"] == "off"
    assert a_config["plugins"]["hook_callback_timeout"] == 600
    assert c_config["plugins"]["hook_callback_timeout"] == 600
    assert "daemon_core" not in a_config["plugins"]["enabled"]
    assert "daemon_core" in c_config["plugins"]["enabled"]
    assert (a_profile / "skills").is_dir()
    assert not (a_profile / "SOUL.md").exists()


def test_live_runner_keeps_openclaw_external_tools_at_parity(tmp_path: Path):
    a_config = openclaw_config(
        arm="A",
        workspace=tmp_path / "a",
        daemon_url="http://127.0.0.1:1",
        model="openai/gpt-5.6-terra",
    )
    c_config = openclaw_config(
        arm="C",
        workspace=tmp_path / "c",
        daemon_url="http://127.0.0.1:2",
        model="openai/gpt-5.6-terra",
    )

    assert a_config["agents"]["defaults"]["skipBootstrap"] is True
    assert c_config["agents"]["defaults"]["skipBootstrap"] is True
    assert a_config["tools"]["profile"] == c_config["tools"]["profile"]
    assert "deny" not in a_config["tools"]
    assert c_config["tools"]["deny"] == ["memory_search", "memory_get"]
    assert a_config["plugins"]["slots"] == {"memory": "memory-core"}
    assert a_config["plugins"]["entries"]["memory-core"]["enabled"] is True
    assert a_config["plugins"]["entries"]["active-memory"]["enabled"] is True
    assert a_config["memory"]["search"] == {
        "rememberAcrossConversations": True,
        "sources": ["memory", "sessions"],
    }
    assert c_config["memory"]["search"] == {
        "rememberAcrossConversations": False,
        "sources": ["memory"],
    }
    assert a_config["hooks"]["internal"]["entries"]["session-memory"] == {
        "enabled": True
    }
    assert c_config["hooks"]["internal"]["enabled"] is False
    assert "sandbox" not in a_config["agents"]["defaults"]
    assert "sandbox" not in c_config["agents"]["defaults"]
    assert (
        a_config["plugins"]["entries"]["codex"]["config"]["appServer"]["sandbox"]
        == "danger-full-access"
    )
    assert "daemon-harness" not in a_config["plugins"]["entries"]
    assert c_config["plugins"]["slots"] == {"contextEngine": "daemon-harness"}
    assert a_config["plugins"]["allow"] == [
        "openai",
        "codex",
        "memory-core",
        "active-memory",
    ]
    assert "daemon-harness" in c_config["plugins"]["allow"]
    assert c_config["plugins"]["entries"]["daemon-harness"]["hooks"] == {
        "allowConversationAccess": True
    }


def test_full_lifecycle_openclaw_config_uses_disposable_local_gateway(
    tmp_path: Path,
):
    config = openclaw_config(
        arm="A",
        workspace=tmp_path / "workspace",
        daemon_url="http://127.0.0.1:1",
        model="openai/gpt-5.6-terra",
        gateway_port=29876,
    )

    assert config["gateway"] == {
        "mode": "local",
        "port": 29876,
        "bind": "loopback",
        "auth": {"mode": "none"},
    }


def test_openclaw_rollout_telemetry_is_content_safe(tmp_path: Path):
    sessions = tmp_path / "sessions" / "2026" / "08" / "29"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-test.jsonl"
    records = [
        {
            "timestamp": "2026-08-29T10:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "base_instructions": {"secret-context": "do not publish"},
                "dynamic_tools": [
                    {"name": "terminal", "type": "function"},
                    {
                        "name": "openclaw_direct",
                        "type": "namespace",
                        "tools": [{"name": "sessions_yield", "type": "function"}],
                    },
                ],
            },
        },
        {
            "timestamp": "2026-08-29T10:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-1",
                "name": "exec",
                "status": "completed",
                "input": "sensitive input",
            },
        },
        {
            "timestamp": "2026-08-29T10:00:01.125Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": "sensitive output",
            },
        },
        {
            "timestamp": "2026-08-29T10:00:02.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 800,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 90,
                        "reasoning_output_tokens": 20,
                        "total_tokens": 1290,
                    }
                },
            },
        },
        {
            "timestamp": "2026-08-29T10:00:02.250Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 2700,
                        "cached_input_tokens": 1800,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 150,
                        "reasoning_output_tokens": 30,
                        "total_tokens": 2850,
                    }
                },
            },
        },
        {
            "timestamp": "2026-08-29T10:00:03.000Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "duration_ms": 2000,
                "time_to_first_token_ms": 750,
                "last_agent_message": "sensitive answer",
            },
        },
    ]
    rollout.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    telemetry = _rollout_telemetry(tmp_path)

    assert (
        telemetry["context_sha256"]
        == sha256(b'{"secret-context":"do not publish"}').hexdigest()
    )
    assert telemetry["tool_calls"] == [
        {"name": "exec", "status": "ok", "duration_ms": 125}
    ]
    assert telemetry["rollout_count"] == 1
    assert telemetry["task_metrics"] == [
        {"duration_ms": 2000, "time_to_first_token_ms": 750}
    ]
    assert telemetry["turn_count"] == 1
    assert telemetry["token_scope"] == {
        "call_count": 2,
        "input_uncached": {"value": 900, "quality": "host_reported"},
        "cache_read": {"value": 1800, "quality": "host_reported"},
        "cache_write": {"value": 0, "quality": "host_reported"},
        "output": {"value": 150, "quality": "host_reported"},
        "reasoning": {"value": 30, "quality": "host_reported"},
        "total": {"value": 2850, "quality": "host_reported"},
    }
    assert "sensitive" not in json.dumps(telemetry)

    second_rollout = sessions / "rollout-test-2.jsonl"
    extra_turn = {
        "timestamp": "2026-08-29T10:00:04.000Z",
        "type": "event_msg",
        "payload": {"type": "task_complete", "duration_ms": 1000},
    }
    second_rollout.write_text(
        rollout.read_text(encoding="utf-8") + json.dumps(extra_turn) + "\n",
        encoding="utf-8",
    )
    combined = _rollout_telemetry(tmp_path)
    assert combined["rollout_count"] == 2
    assert combined["turn_count"] == 3
    assert combined["token_scope"]["total"]["value"] == 5700
    assert len(combined["tool_calls"]) == 2
    subset = _rollout_telemetry(tmp_path, paths=[second_rollout])
    assert subset["rollout_count"] == 1
    assert subset["token_scope"]["total"]["value"] == 2850


def test_hermes_quiet_session_id_parser_uses_the_last_reported_id():
    assert (
        _session_id_from_stderr("notice\nsession_id: first\nsession_id: resumed\n")
        == "resumed"
    )
    with pytest.raises(RuntimeError, match="native session id"):
        _session_id_from_stderr("no session here")


def test_hermes_quiet_command_uses_the_observable_chat_path():
    command = _quiet_chat_command(
        binary=Path("/tmp/hermes"),
        prompt="Do the task",
        model="gpt-5.6-terra",
        provider="openai-codex",
    )

    assert command[:3] == ["/tmp/hermes", "chat", "--query"]
    assert "--quiet" in command
    assert "--oneshot" not in command


def test_full_lifecycle_gate_requires_hermes_review_usage_attribution():
    attestations = [
        {
            "host": "hermes",
            "arms": {
                "A": {
                    "native_background_scheduled": 1,
                    "native_background_call_count": 0,
                },
                "C": {
                    "native_background_scheduled": 0,
                    "native_background_call_count": 0,
                },
            },
        }
    ]

    assert not _hermes_native_background_usage_attributed(attestations)
    attestations[0]["arms"]["A"]["native_background_call_count"] = 2
    assert _hermes_native_background_usage_attributed(attestations)


def test_live_runner_redacts_personal_paths():
    text = redact_local_paths(
        f"failure in {Path.home()}/private/session.json",
    )

    assert str(Path.home()) not in text
    assert text == "failure in <HOME>/private/session.json"


def test_dry_run_gate_accepts_an_authoritative_product_failure():
    runs = []
    for host in ("hermes", "openclaw"):
        for arm in ("A", "C"):
            runs.append(
                {
                    "host_id": host,
                    "arm_id": arm,
                    "status": "completed",
                    "timing": {
                        "first_visible_ms": 10 if host == "hermes" else None,
                        "daemon_compose_ms": 2 if arm == "C" else None,
                    },
                    "tokens": {"host.visible": {"total": {"value": 100}}},
                    "tool_calls": [{"name": "tool", "status": "ok"}],
                    "outcome": {
                        "authoritative": True,
                        "success_first_attempt": not (host == "hermes" and arm == "A"),
                        "success_within_episode": not (host == "hermes" and arm == "A"),
                    },
                }
            )
    attestations = {
        host: {
            arm: {
                "canonical_path_observed": True,
                "context_sha256": "1" * 64,
                "external_tool_catalog_sha256": "2" * 64,
                "episode_turns_observed": 1,
            }
            for arm in ("A", "C")
        }
        for host in ("hermes", "openclaw")
    }
    for arm in ("A", "C"):
        attestations["openclaw"][arm].update(
            {
                "native_memory_sync_observed": True,
                "installation_memory_tools": (
                    ["memory_get", "memory_search"]
                    if arm == "A"
                    else ["daemon_read_souvenirs", "daemon_recall_l0"]
                ),
                "sandbox": {
                    "host_process_is_sandboxed": True,
                    "backend": "macos-sandbox-exec",
                    "runtime_is_writable": True,
                    "real_home_is_blocked": True,
                    "attestation_sha256": "3" * 64,
                },
            }
        )

    gates = _gate(
        runs=runs,
        attestations=attestations,
        summary={"run_count": 4},
        expected_turns=1,
    )

    assert all(gates.values())
    assert "four_authoritative_successes" not in gates
    assert gates["four_authoritative_outcomes"] is True


def test_openclaw_native_windows_cover_the_preregistered_seeds_once():
    observed = {
        (scenario_id, seed)
        for pairs in OPENCLAW_WINDOWS.values()
        for scenario_id, seed, arm_order in pairs
        if set(arm_order) == {"A", "C"}
    }
    expected = {("native.files.aggregate", seed) for seed in range(1101, 1107)} | {
        ("native.tools.extract", seed) for seed in range(1201, 1207)
    }

    assert len(OPENCLAW_WINDOWS) == 2
    assert all(len(pairs) == 6 for pairs in OPENCLAW_WINDOWS.values())
    assert len(observed) == 12
    assert observed == expected


def test_openclaw_continuity_replay_covers_six_seeds_and_alternates_order():
    assert tuple(seed for seed, _ in OPENCLAW_CONTINUITY_REPLAY_ORDER) == DECISION_SEEDS
    assert tuple(order for _, order in OPENCLAW_CONTINUITY_REPLAY_ORDER) == (
        ("A", "C"),
        ("C", "A"),
        ("A", "C"),
        ("C", "A"),
        ("A", "C"),
        ("C", "A"),
    )


def test_openclaw_window_requires_stable_arm_attestations():
    pair_attestations = [
        {
            arm: {
                "context_sha256": f"{arm}1" * 32,
                "tool_catalog_sha256": f"{arm}2" * 32,
            }
            for arm in ("A", "C")
        }
        for _ in range(2)
    ]

    arm_attestations, stable = _stable_arm_attestations(pair_attestations)

    assert stable is True
    assert [item["arm_id"] for item in arm_attestations] == ["A", "C"]

    pair_attestations[1]["C"]["context_sha256"] = "changed"
    _, stable = _stable_arm_attestations(pair_attestations)

    assert stable is False
