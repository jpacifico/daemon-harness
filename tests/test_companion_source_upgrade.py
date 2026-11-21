"""Upgrade transactions use isolated files, synthetic credentials and services."""

import base64
from dataclasses import replace
import json
import shutil

import pytest
from daemons_companion.runtime.upgrade import Upgrade, CODES
from daemons_companion.runtime.release import (
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    verify_current_release,
)
from daemons_companion.runtime.environment import CompanionRuntimeEnvironmentAdapter
from daemons_companion.hermes.composition import HermesCompositionLayout
from daemons_companion.runtime.services import known_job_definitions, render_job_definition
from daemons_companion.setup.plan import PlanStep
from tests.test_companion_private_setup import owned
from tests.test_companion_runtime_environment import _bundle, FakeRunner


class Scheduler:
    target_os = "macos"

    def __init__(self, namespace, target_os):
        self.target_os = target_os
        self.job_namespace = namespace
        self.values = {}

    def capture(self, code):
        return self.values.get(code)

    def install(self, job, definition):
        self.values[job.code] = json.dumps(
            {
                "schema": "daemon.launchd-capture.v1"
                if self.target_os == "macos"
                else "daemon.windows-task-capture.v1",
                ("loaded" if self.target_os == "macos" else "running"): job.persistent
                or self.target_os == "macos",
                "definition": base64.b64encode(definition).decode(),
            },
            sort_keys=True,
        ).encode()

    def restore(self, code, capture):
        if capture is None:
            self.values.pop(code, None)
        else:
            self.values[code] = capture

    def matches(self, job, definition):
        return (
            self.capture(job.code) is not None
            and base64.b64decode(json.loads(self.capture(job.code))["definition"])
            == definition
        )


def apply(adapter, code, directory):
    directory.mkdir()
    step = PlanStep(code, code, ())
    adapter.snapshot(step, directory)
    adapter.apply(step)
    adapter.verify(step)


@pytest.fixture(params=["macos", "windows"])
def setup(tmp_path, monkeypatch, request):
    target_os = request.param
    host, scope = owned(tmp_path, monkeypatch, target_os)
    host.uv_executable.chmod(0o700)
    bundle = _bundle(tmp_path / "old-bundle")
    (bundle / "src/daemon_core/loop.py").write_text("# canonical unchanged\n")
    runtime = CompanionRuntimeLayout(scope.home, bundle, scope)
    apply(
        CompanionRuntimeAdapter(runtime),
        "runtime-layout",
        tmp_path / "runtime-snapshot",
    )
    composition = HermesCompositionLayout(
        runtime,
        scope.profile,
        host.uv_executable,
        host.uv_sha256,
        target_os,
        scope.installation_id,
    )
    runner = FakeRunner(target_os)
    apply(
        CompanionRuntimeEnvironmentAdapter(
            composition.runtime_environment, runner=runner
        ),
        "runtime-environment",
        tmp_path / "env-snapshot",
    )
    scheduler = Scheduler(scope.installation_id, target_os)
    initial = Upgrade(
        composition, host, scheduler, "777001", runner=runner, probe=lambda: True
    )
    apply(initial.profile_adapter(), "hermes-profile", tmp_path / "profile-snapshot")
    for job in initial.jobs():
        scheduler.install(job, render_job_definition(job, scheduler.target_os))
    memory = runtime.memory_root / "note.md"
    memory.write_text("Synthetic canonical memory remains exact.\n")
    provider = runtime.state_root / "config/provider.json"
    provider.parent.mkdir()
    provider.write_text('{"kind":"synthetic-account"}')
    target = tmp_path / "new-bundle"
    shutil.copytree(bundle, target)
    for name in (
        "daemons_companion/hosts/attachment_entry",
        "daemons_companion/hosts/creation",
        "daemon_core/host_bindings",
    ):
        (target / f"src/{name}.py").parent.mkdir(parents=True, exist_ok=True)
        (target / f"src/{name}.py").write_text("# new portability capability\n")
    new_runtime = replace(runtime, bundle_root=target)
    new_composition = replace(composition, runtime=new_runtime)

    def factory(**kwargs):
        return Upgrade(
            new_composition,
            host,
            scheduler,
            "777001",
            runner=runner,
            probe=lambda: True,
            **kwargs,
        )

    return factory, initial, scheduler, memory, provider, target


def test_upgrade_switches_every_pointer_and_preserves_memory_and_account(setup):
    factory, initial, scheduler, memory, provider, target = setup
    upgrade = factory()
    before = verify_current_release(initial.runtime)
    review = upgrade.review()
    assert review["status"] == "review"
    assert upgrade.run(review["revision"])["status"] == "committed"
    assert verify_current_release(upgrade.runtime).name == review["to"]
    assert before.is_dir()
    assert all(
        scheduler.matches(j, render_job_definition(j, scheduler.target_os))
        for j in upgrade.jobs()
    )
    assert memory.read_text() == "Synthetic canonical memory remains exact.\n"
    assert provider.read_text() == '{"kind":"synthetic-account"}'
    assert factory().state()["status"] == "committed"


@pytest.mark.parametrize("phase", CODES)
def test_failure_after_each_step_restores_original_services_and_profile(setup, phase):
    factory, initial, scheduler, memory, provider, _ = setup
    source = verify_current_release(initial.runtime)
    saved = dict(scheduler.values)
    profile = initial.profile_adapter()._descriptors()

    def fail(step):
        if step == phase:
            raise RuntimeError("Synthetic failure")

    upgrade = factory(hook=fail)
    result = upgrade.run(upgrade.review()["revision"])
    assert result["status"] == "rolled_back"
    assert verify_current_release(initial.runtime) == source
    assert scheduler.values == saved
    assert initial.profile_adapter()._descriptors() == profile
    assert memory.read_text() == "Synthetic canonical memory remains exact.\n"


@pytest.mark.parametrize("phase", CODES)
def test_fresh_controller_recovers_interrupted_upgrade(setup, phase):
    factory, initial, scheduler, _, _, _ = setup
    source = verify_current_release(initial.runtime)
    saved = dict(scheduler.values)

    def interrupt(step):
        if step == phase:
            raise KeyboardInterrupt()

    upgrade = factory(hook=interrupt)
    review = upgrade.review()
    with pytest.raises(KeyboardInterrupt):
        upgrade.run(review["revision"])
    assert factory().run(review["revision"], recover=True)["status"] == "rolled_back"
    assert verify_current_release(initial.runtime) == source
    assert scheduler.values == saved


def test_changed_prompt_and_dependency_lock_refused_before_services(setup):
    factory, initial, scheduler, _, _, target = setup
    saved = dict(scheduler.values)
    for name in ("src/daemon_core/loop.py", "requirements-runtime.lock"):
        p = target / name
        old = p.read_bytes()
        p.write_text("unreviewed change")
        with pytest.raises(ValueError, match="separately qualified"):
            factory().review()
        p.write_bytes(old)
    assert scheduler.values == saved


def test_concurrent_profile_edit_blocks_apply_and_is_preserved(setup):
    factory, initial, scheduler, _, _, _ = setup
    upgrade = factory()
    review = upgrade.review()
    saved = dict(scheduler.values)
    env = initial.host.profile / ".env"
    env.write_text(env.read_text() + "\nUSER_EDIT=retained\n")
    with pytest.raises(ValueError, match="again"):
        upgrade.run(review["revision"])
    assert "USER_EDIT=retained" in env.read_text()
    assert scheduler.values == saved


def _install_pre_priority_definitions(initial, scheduler):
    """Register services exactly as releases up to 0.11.15 rendered them."""
    for job in initial.jobs():
        scheduler.install(job, known_job_definitions(job, scheduler.target_os)[-1])
    if scheduler.target_os == "macos":
        assert any(job.conversational for job in initial.jobs())
        assert not all(
            scheduler.matches(j, render_job_definition(j, "macos")) for j in initial.jobs()
        )
    return dict(scheduler.values)


def test_upgrade_moves_pre_priority_services_to_their_current_class(setup):
    factory, initial, scheduler, memory, _, _ = setup
    _install_pre_priority_definitions(initial, scheduler)
    upgrade = factory()
    assert upgrade.run(upgrade.review()["revision"])["status"] == "committed"
    assert all(
        scheduler.matches(j, render_job_definition(j, scheduler.target_os))
        for j in upgrade.jobs()
    )
    assert memory.read_text() == "Synthetic canonical memory remains exact.\n"


@pytest.mark.parametrize("phase", CODES)
def test_failed_upgrade_restores_pre_priority_services_exactly(setup, phase):
    factory, initial, scheduler, _, _, _ = setup
    saved = _install_pre_priority_definitions(initial, scheduler)

    def fail(step):
        if step == phase:
            raise RuntimeError("Synthetic failure")

    upgrade = factory(hook=fail)
    assert upgrade.run(upgrade.review()["revision"])["status"] == "rolled_back"
    assert scheduler.values == saved


def test_upgrade_refuses_a_service_class_it_never_rendered(setup):
    factory, initial, scheduler, _, _, _ = setup
    if scheduler.target_os != "macos":
        pytest.skip("The scheduling class is a launchd key.")
    job = next(j for j in initial.jobs() if j.conversational)
    definition = render_job_definition(job, "macos").replace(
        b"<string>Standard</string>", b"<string>Interactive</string>"
    )
    assert definition != render_job_definition(job, "macos")
    scheduler.install(job, definition)
    with pytest.raises((ValueError, RuntimeError)):
        factory().review()


def test_unknown_service_and_existing_attachment_refused(setup):
    factory, initial, scheduler, _, _, _ = setup
    key = next(iter(scheduler.values))
    value = scheduler.values[key]
    scheduler.values[key] = b"{}"
    with pytest.raises((ValueError, RuntimeError)):
        factory().review()
    scheduler.values[key] = value
    (initial.runtime.state_root / "host-attachment-test.json").write_text("{}")
    with pytest.raises(ValueError, match="coordinated"):
        factory().review()


def test_concurrent_edit_during_rollback_keeps_services_off(setup):
    factory, initial, scheduler, _, _, _ = setup
    env = initial.host.profile / ".env"

    def conflict(step):
        if step == "hermes-profile":
            env.write_text(env.read_text() + "\nUSER_EDIT=retained\n")
            raise RuntimeError("Synthetic conflict")

    upgrade = factory(hook=conflict)
    assert upgrade.run(upgrade.review()["revision"])["status"] == "rollback_failed"
    assert not scheduler.values
    assert "USER_EDIT=retained" in env.read_text()


def test_a_restored_upgrade_can_be_retried_with_a_new_transaction(setup):
    factory, _, _, _, _, _ = setup

    def fail(phase):
        if phase == "runtime-layout":
            raise RuntimeError("Synthetic failure")

    first = factory(hook=fail)
    assert first.run(first.review()["revision"])["status"] == "rolled_back"
    second = factory()
    assert second.run(second.review()["revision"])["status"] == "committed"


def test_readiness_failure_rolls_back(setup):
    factory, initial, scheduler, _, _, _ = setup
    original = dict(scheduler.values)
    upgrade = factory()

    called = []

    def failed_probe():
        if not called:
            called.append(True)
            raise RuntimeError("Native readiness failed")
        return True

    upgrade.probe = failed_probe
    assert upgrade.run(upgrade.review()["revision"])["status"] == "rolled_back"
    assert scheduler.values == original


def test_interrupted_native_command_preserves_files_and_blocks_recovery(setup):
    from daemons_companion.runtime.upgrade import require_settled_upgrade

    factory, initial, scheduler, memory, provider, _ = setup
    upgrade = factory()
    review = upgrade.review()

    def interrupted(command, environment):
        raise KeyboardInterrupt()

    upgrade.runner = interrupted
    with pytest.raises(KeyboardInterrupt):
        upgrade.run(review["revision"])
    assert upgrade.command_marker.exists()
    current = verify_current_release(initial.runtime)
    assert current.name == review["to"]
    assert not scheduler.values
    restarted = factory()
    assert restarted.state()["status"] == "command_recovery"
    assert (
        restarted.run(review["revision"], recover=True)["status"] == "command_recovery"
    )
    assert verify_current_release(initial.runtime) == current
    assert memory.read_text() == "Synthetic canonical memory remains exact.\n"
    assert provider.read_text() == '{"kind":"synthetic-account"}'
    with pytest.raises(ValueError, match="command"):
        require_settled_upgrade(initial.runtime.companion_home)


def test_only_a_committed_installation_receipt_authorizes_upgrade(setup):
    from daemons_companion.runtime.upgrade import require_completed_installation

    _, initial, _, _, _, _ = setup
    scope = initial.runtime.installation_scope
    receipt = scope.attempt / "daemon-setup.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "daemon.private-setup.v1",
                "transaction_id": "b" * 32,
                "user_id": "777001",
            }
        )
    )
    journal = scope.home / "transactions" / ("b" * 32) / "journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"status":"failed"}')
    with pytest.raises(ValueError, match="not complete"):
        require_completed_installation(scope)
    journal.write_text('{"status":"committed"}')
    assert require_completed_installation(scope)["user_id"] == "777001"
    receipt.write_text(
        '{"schema":"daemon.private-setup.v1","transaction_id":"../other"}'
    )
    with pytest.raises(ValueError, match="completed owned"):
        require_completed_installation(scope)


def test_status_detects_outdated_capability_even_when_files_exist(setup, monkeypatch):
    from daemons_companion.runtime import upgrade as module

    factory, _, _, _, _, _ = setup
    upgrade = factory()
    assert upgrade.run(upgrade.review()["revision"])["status"] == "committed"
    monkeypatch.setattr(module, "from_request", lambda request: factory())
    assert module.dispatch({"operation": "upgrade-status"})["status"] == "current"
    (
        upgrade.runtime.bundle_root / "src/daemons_companion/hosts/creation.py"
    ).write_text("# updated platform support\n")
    assert module.dispatch({"operation": "upgrade-status"})["status"] == "required"


def test_health_uses_non_destructive_process_query(setup, monkeypatch):
    from daemon_core import platform_support
    from daemon_core.after_turn_service import after_turn_service_path

    factory, _, _, _, _, _ = setup
    upgrade = factory()
    beat = after_turn_service_path(upgrade.runtime.state_root / "after-turn-spool")
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text(json.dumps({"heartbeat_at": 1, "pid": 777777}))
    queried = []
    monkeypatch.setattr(
        platform_support, "process_is_alive", lambda pid: queried.append(pid) or False
    )
    assert upgrade.health() is False
    assert queried == [777777]


def _without_load_timeout(initial):
    """Write the profile exactly as an installation before the plugin load deadline did."""
    from daemons_companion.hermes.profile import (
        HERMES_CONFIG_VALUES,
        HERMES_LATER_CONFIG_KEYS,
        _patch_hermes_config,
    )

    config = initial.host.profile / "config.yaml"
    earlier = tuple(v for v in HERMES_CONFIG_VALUES if v[0] not in HERMES_LATER_CONFIG_KEYS)
    text = config.read_text()
    assert "load_timeout_seconds: 120" in text
    # The current file minus the later setting, then re-applied as the earlier release did.
    lines = [line for line in text.splitlines(keepends=True) if "load_timeout_seconds" not in line]
    stripped = "".join(lines)
    if "plugins:\n" in stripped and not any(
        line.startswith("  ") for line in stripped.split("plugins:\n", 1)[1].splitlines()[:1]
    ):
        stripped = stripped.replace("plugins:\n", "", 1)
    earlier_config = _patch_hermes_config(stripped.encode(), earlier)
    config.write_bytes(earlier_config)
    return config, earlier_config


def test_update_adds_the_plugin_load_deadline_to_an_earlier_profile(setup):
    factory, initial, scheduler, memory, _, _ = setup
    config, earlier = _without_load_timeout(initial)
    upgrade = factory()
    review = upgrade.review()
    assert review["status"] == "review"
    assert upgrade.run(review["revision"])["status"] == "committed"
    import yaml

    assert yaml.safe_load(config.read_text())["plugins"]["load_timeout_seconds"] == 120
    assert memory.read_text() == "Synthetic canonical memory remains exact.\n"


@pytest.mark.parametrize("phase", CODES)
def test_failed_update_restores_the_earlier_profile_without_the_deadline(setup, phase):
    factory, initial, scheduler, _, _, _ = setup
    config, earlier = _without_load_timeout(initial)
    saved = dict(scheduler.values)

    def fail(step):
        if step == phase:
            raise RuntimeError("Synthetic failure")

    upgrade = factory(hook=fail)
    assert upgrade.run(upgrade.review()["revision"])["status"] == "rolled_back"
    assert config.read_bytes() == earlier
    assert scheduler.values == saved


def test_a_changed_plugin_load_deadline_is_refused_before_services(setup):
    factory, initial, scheduler, _, _, _ = setup
    config = initial.host.profile / "config.yaml"
    config.write_text(config.read_text().replace("load_timeout_seconds: 120", "load_timeout_seconds: 30"))
    saved = dict(scheduler.values)
    # The same refusal as any other changed installation setting.
    with pytest.raises((ValueError, RuntimeError), match="load_timeout_seconds|migration"):
        factory().review()
    assert scheduler.values == saved
