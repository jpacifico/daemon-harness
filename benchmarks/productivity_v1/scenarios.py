"""Scenario registry and disposable fixture lifecycles for Productivity V1."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Callable
from urllib.parse import urlsplit

from benchmarks.productivity_v1.checkers import continuity_decision as decision_checker
from benchmarks.productivity_v1.checkers import native_files_aggregate as files_checker
from benchmarks.productivity_v1.checkers import native_tools_extract as tools_checker
from benchmarks.productivity_v1.fixtures import continuity_decision as decision_fixture
from benchmarks.productivity_v1.fixtures import native_files_aggregate as files_fixture
from benchmarks.productivity_v1.fixtures import native_tools_extract as tools_fixture


@dataclass(frozen=True)
class ScenarioDefinition:
    id: str
    output_path: str
    official_seeds: tuple[int, ...]
    episode_turns: int = 1
    history_messages: int = 0


class PreparedScenario:
    def __init__(
        self,
        *,
        definition: ScenarioDefinition,
        fixture: dict[str, Any],
        assess: Callable[[], dict[str, Any]],
        close: Callable[[], None] = lambda: None,
    ) -> None:
        self.definition = definition
        self.fixture = fixture
        self._assess = assess
        self._close = close

    def assess(self) -> dict[str, Any]:
        return self._assess()

    def close(self) -> None:
        self._close()


SCENARIOS = {
    decision_fixture.SCENARIO_ID: ScenarioDefinition(
        id=decision_fixture.SCENARIO_ID,
        output_path=decision_fixture.OUTPUT_PATH,
        official_seeds=decision_fixture.OFFICIAL_SEEDS,
        episode_turns=3,
        history_messages=4,
    ),
    files_fixture.SCENARIO_ID: ScenarioDefinition(
        id=files_fixture.SCENARIO_ID,
        output_path=files_fixture.OUTPUT_PATH,
        official_seeds=files_fixture.OFFICIAL_SEEDS,
    ),
    tools_fixture.SCENARIO_ID: ScenarioDefinition(
        id=tools_fixture.SCENARIO_ID,
        output_path=tools_fixture.OUTPUT_PATH,
        official_seeds=tools_fixture.OFFICIAL_SEEDS,
    ),
}


def get_scenario(scenario_id: str) -> ScenarioDefinition:
    try:
        return SCENARIOS[scenario_id]
    except KeyError as exc:
        raise ValueError(f"unknown productivity scenario: {scenario_id}") from exc


class _TrackingHandler(SimpleHTTPRequestHandler):
    server: "_TrackingServer"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.server.accessed_paths.append(urlsplit(self.path).path)
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        return


class _TrackingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.accessed_paths: list[str] = []
        super().__init__(*args, **kwargs)


def prepare_scenario(
    *,
    scenario_id: str,
    seed: int,
    variant: str,
    workspace: Path,
    control_path: Path,
    run_root: Path,
    additional_baseline_files: tuple[str, ...] = (),
) -> PreparedScenario:
    definition = get_scenario(scenario_id)
    if scenario_id == files_fixture.SCENARIO_ID:
        fixture = files_fixture.materialize_fixture(
            seed=seed,
            variant=variant,
            workspace=workspace,
            control_path=control_path,
        )
        control = files_checker.load_control(control_path)
        return PreparedScenario(
            definition=definition,
            fixture=fixture,
            assess=lambda: files_checker.check_workspace(
                workspace=workspace,
                control=control,
                additional_baseline_files=additional_baseline_files,
            ),
        )

    if scenario_id == decision_fixture.SCENARIO_ID:
        fixture = decision_fixture.materialize_fixture(
            seed=seed,
            variant=variant,
            workspace=workspace,
            control_path=control_path,
        )
        control = decision_checker.load_control(control_path)
        return PreparedScenario(
            definition=definition,
            fixture=fixture,
            assess=lambda: decision_checker.check_workspace(
                workspace=workspace,
                control=control,
                additional_baseline_files=additional_baseline_files,
            ),
        )

    source_root = run_root / "controlled-source"
    source_root.mkdir(parents=True)
    handler = partial(_TrackingHandler, directory=str(source_root))
    server = _TrackingServer(("127.0.0.1", 0), handler)
    port = int(server.server_address[1])
    fixture = tools_fixture.materialize_fixture(
        seed=seed,
        variant=variant,
        workspace=workspace,
        control_path=control_path,
        source_root=source_root,
        base_url=f"http://127.0.0.1:{port}",
    )
    control = tools_checker.load_control(control_path)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def assess() -> dict[str, Any]:
        return tools_checker.check_workspace(
            workspace=workspace,
            source_root=source_root,
            accessed_paths=tuple(server.accessed_paths),
            control=control,
            additional_baseline_files=additional_baseline_files,
        )

    def close() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return PreparedScenario(
        definition=definition,
        fixture=fixture,
        assess=assess,
        close=close,
    )
