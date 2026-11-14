"""Runtime update of a daemon created on OpenClaw: isolated files, synthetic services."""

import json
import shutil

import pytest
from daemons_companion.openclaw.ready_composition import OpenClawReadyCompositionLayout
from daemons_companion.runtime.environment import CompanionRuntimeEnvironmentAdapter
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.runtime.release import (
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    verify_current_release,
)
from daemons_companion.runtime.services import (
    OPENCLAW_GATEWAY_JOB,
    known_job_definitions,
    render_job_definition,
    scoped_job_code,
)
from daemons_companion.runtime.upgrade import OPENCLAW_CODES, OpenClawUpgrade, check
from dataclasses import replace
from tests.test_companion_openclaw_first import owned_host
from tests.test_companion_runtime_environment import FakeRunner, _bundle
from tests.test_companion_source_upgrade import Scheduler, apply

USER = "777001"
MEMORY = "Synthetic canonical memory remains exact.\n"


def _openclaw_bundle(root):
    bundle = _bundle(root)
    for relative, content in {
        "src/daemon_core/loop.py": "# canonical unchanged\n",
        "integrations/openclaw_plugin/index.js": "export default {};\n",
        "integrations/openclaw_plugin/workspace/AGENTS.md": "# Canonical workspace\n",
    }.items():
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return bundle


@pytest.fixture(params=["macos", "windows"])
def setup(tmp_path, request):
    target_os = request.param
    host = owned_host(tmp_path / "owned", target_os)
    scope = InstallationScope(host.attempt)
    scope.initialize()
    (host.profile / "openclaw.json").write_text(
        json.dumps({"gateway": {"mode": "local", "port": 18790}, "owner": "kept"}, indent=2) + "\n"
    )
    uv = tmp_path / ("uv.exe" if target_os == "windows" else "uv")
    uv.write_bytes(b"synthetic uv")
    uv.chmod(0o700)
    import hashlib

    sha = hashlib.sha256(uv.read_bytes()).hexdigest()
    bundle = _openclaw_bundle(tmp_path / "old-bundle")
    runtime = CompanionRuntimeLayout(scope.home, bundle, scope)
    apply(CompanionRuntimeAdapter(runtime), "runtime-layout", tmp_path / "runtime-snapshot")
    composition = OpenClawReadyCompositionLayout(
        runtime, host.profile, uv, sha, target_os, 18000, 18790, scope.installation_id
    )
    runner = FakeRunner(target_os)
    apply(
        CompanionRuntimeEnvironmentAdapter(composition.runtime_environment, runner=runner),
        "runtime-environment",
        tmp_path / "env-snapshot",
    )
    scheduler = Scheduler(scope.installation_id, target_os)
    initial = OpenClawUpgrade(composition, host, scheduler, USER, runner=runner, probe=lambda: True)
    apply(initial.profile_adapter(), "openclaw-profile", tmp_path / "profile-snapshot")
    for job in initial.jobs():
        scheduler.install(job, render_job_definition(job, target_os))
    # The daemon's own host registration, as its first install writes it.
    bindings = runtime.state_root / "host-bindings"
    bindings.mkdir(parents=True)
    (bindings / ("a" * 32 + ".json")).write_text(json.dumps({"profile": str(host.profile)}))
    memory = runtime.memory_root / "note.md"
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text(MEMORY)
    provider = runtime.state_root / "config/provider.json"
    provider.parent.mkdir(parents=True)
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
    new_composition = replace(composition, runtime=replace(runtime, bundle_root=target))

    def factory(**kwargs):
        return OpenClawUpgrade(
            new_composition, host, scheduler, USER, runner=runner, probe=lambda: True, **kwargs
        )

    return factory, initial, scheduler, memory, provider, target


def _plugin_paths(upgrade):
    config = json.loads((upgrade.host.profile / "openclaw.json").read_text())
    return config["plugins"]["load"]["paths"]


def test_update_points_every_service_and_the_profile_at_the_new_release(setup):
    factory, initial, scheduler, memory, provider, _ = setup
    before = verify_current_release(initial.runtime)
    assert _plugin_paths(initial) == [str(before / "integrations/openclaw_plugin")]
    upgrade = factory()
    review = upgrade.review()
    assert review["status"] == "review"
    assert upgrade.run(review["revision"])["status"] == "committed"
    after = verify_current_release(upgrade.runtime)
    assert after.name == review["to"] and before.is_dir()
    assert all(
        scheduler.matches(j, render_job_definition(j, scheduler.target_os)) for j in upgrade.jobs()
    )
    gateway = scoped_job_code(OPENCLAW_GATEWAY_JOB, upgrade.host.installation_id)
    assert gateway in {j.code for j in upgrade.jobs()}
    # The old plugin copy stays for rollback but is no longer loaded.
    assert _plugin_paths(upgrade) == [str(after / "integrations/openclaw_plugin")]
    config = json.loads((upgrade.host.profile / "openclaw.json").read_text())
    assert config["owner"] == "kept"
    assert memory.read_text() == MEMORY
    assert provider.read_text() == '{"kind":"synthetic-account"}'
    assert factory().state()["status"] == "committed"
    assert check(factory())["status"] == "current"


@pytest.mark.parametrize("phase", OPENCLAW_CODES)
def test_failure_after_each_step_restores_services_profile_and_release(setup, phase):
    factory, initial, scheduler, memory, _, _ = setup
    source = verify_current_release(initial.runtime)
    saved = dict(scheduler.values)
    profile = initial.profile_adapter()._descriptors()

    def fail(step):
        if step == phase:
            raise RuntimeError("Synthetic failure")

    upgrade = factory(hook=fail)
    assert upgrade.run(upgrade.review()["revision"])["status"] == "rolled_back"
    assert verify_current_release(initial.runtime) == source
    assert scheduler.values == saved
    assert initial.profile_adapter()._descriptors() == profile
    assert memory.read_text() == MEMORY


@pytest.mark.parametrize("phase", OPENCLAW_CODES)
def test_fresh_controller_recovers_an_interrupted_update(setup, phase):
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
    assert check(factory())["status"] not in {"current", "available"}
    assert factory().run(review["revision"], recover=True)["status"] == "rolled_back"
    assert verify_current_release(initial.runtime) == source
    assert scheduler.values == saved


def test_readiness_failure_rolls_back(setup):
    factory, _, scheduler, _, _, _ = setup
    original = dict(scheduler.values)
    upgrade = factory()
    called = []

    def failed_probe():
        if not called:
            called.append(True)
            raise RuntimeError("Gateway did not load its plugin")
        return True

    upgrade.probe = failed_probe
    assert upgrade.run(upgrade.review()["revision"])["status"] == "rolled_back"
    assert scheduler.values == original


def test_pre_priority_services_move_to_their_current_class(setup):
    factory, initial, scheduler, _, _, _ = setup
    for job in initial.jobs():
        scheduler.install(job, known_job_definitions(job, scheduler.target_os)[-1])
    upgrade = factory()
    assert upgrade.run(upgrade.review()["revision"])["status"] == "committed"
    assert all(
        scheduler.matches(j, render_job_definition(j, scheduler.target_os)) for j in upgrade.jobs()
    )


def test_edited_profile_is_refused_before_any_service_stops(setup):
    factory, initial, scheduler, _, _, _ = setup
    saved = dict(scheduler.values)
    agents = initial.host.profile / "workspace-daemon-harness/AGENTS.md"
    agents.write_text(agents.read_text() + "\nOwner edit.\n")
    assert check(factory()) == {
        "revision": verify_current_release(initial.runtime).name,
        "status": "refused",
        "code": "openclaw-profile",
    }
    assert scheduler.values == saved
    assert "Owner edit." in agents.read_text()


def test_engine_change_is_refused_with_its_code(setup):
    factory, initial, _, _, _, target = setup
    (target / "src/daemon_core/loop.py").write_text("# unreviewed engine change\n")
    result = check(factory())
    assert result["status"] == "refused" and result["code"] == "protected-runtime"


def test_only_another_profile_counts_as_an_added_host(setup):
    factory, initial, _, _, _, _ = setup
    assert check(factory())["status"] == "available"
    other = initial.runtime.state_root / "host-bindings" / ("b" * 32 + ".json")
    other.write_text(json.dumps({"profile": str(initial.host.profile.parent / "other")}))
    result = check(factory())
    assert result["status"] == "refused" and result["code"] == "attached-hosts"
