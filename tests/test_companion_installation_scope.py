from dataclasses import replace
import json
import pytest

from daemons_companion.runtime.installation_scope import InstallationScope, MARKER
from daemons_companion.runtime.release import CompanionRuntimeLayout, validate_companion_home, validate_disposable_companion_home
from daemons_companion.hermes.profile import validate_hermes_profile_scope
from daemons_companion.hermes.composition import build_dormant_hermes_composition_plan
from daemons_companion.setup.plan import HostKind, HostProbe, SetupRequest, build_setup_plan
from daemons_companion.setup.transaction import TransactionApplyError
from tests.test_companion_host_install import fixture, execute
from tests.test_companion_hermes_composition import _fixture, _adapter, _execute


def scope_fixture(tmp_path):
    host, runner, adapter = fixture(tmp_path)
    execute(host, adapter)
    scope = InstallationScope(host.attempt)
    scope.initialize()
    return scope


def test_existing_guards_never_infer_authority_from_owned_marker(tmp_path):
    scope = scope_fixture(tmp_path)
    assert validate_companion_home(scope.home, scope) == scope.home
    for validator in (validate_disposable_companion_home, validate_companion_home):
        with pytest.raises(ValueError, match="disposable"):
            validator(scope.home)
    with pytest.raises(ValueError, match="disposable"):
        validate_hermes_profile_scope(scope.profile)
    validate_hermes_profile_scope(scope.profile, scope)


def test_personal_paths_and_copied_marker_do_not_grant_scope(tmp_path):
    scope = scope_fixture(tmp_path)
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / MARKER).write_bytes((scope.home / MARKER).read_bytes())
    (personal / "keep.txt").write_text("original")
    with pytest.raises(ValueError):
        validate_companion_home(personal, scope)
    with pytest.raises(ValueError):
        validate_hermes_profile_scope(personal, scope)
    assert (personal / "keep.txt").read_text() == "original"


def test_changed_active_receipt_revokes_scope_before_writes(tmp_path):
    scope = scope_fixture(tmp_path)
    pointer = scope.attempt.parent.parent / "current.json"
    payload = json.loads(pointer.read_text())
    payload["installation_id"] = "b" * 32
    pointer.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="active host receipt"):
        scope.validate_home(scope.home)


def test_symlink_and_personal_job_namespace_refused(tmp_path):
    scope = scope_fixture(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(scope.home, target_is_directory=True)
    with pytest.raises(ValueError, match="direct"):
        scope.validate_home(link)
    with pytest.raises(ValueError, match="namespace"):
        scope.validate_namespace(None)
    scope.validate_namespace(scope.installation_id)


@pytest.mark.parametrize("target_os", ["macos", "windows"])
@pytest.mark.parametrize("fail", [False, True])
def test_five_existing_owners_execute_and_rollback_only_explicit_private_scope(tmp_path, target_os, fail):
    hostroot = tmp_path / "host"
    hostroot.mkdir()
    scope = scope_fixture(hostroot)
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    layout, _, selection, store, runner = _fixture(fixture_root, target_os=target_os)
    runtime = CompanionRuntimeLayout(scope.home, layout.runtime.bundle_root, scope)
    layout = replace(layout, runtime=runtime, hermes_home=scope.profile, job_namespace=scope.installation_id)
    reviewed = build_setup_plan(SetupRequest(HostKind.HERMES, "Aster", "feminine", selection), (
        HostProbe(HostKind.HERMES, True, True, "/synthetic/hermes", str(scope.profile), "0.21.5", "verified"),))
    plan = build_dormant_hermes_composition_plan(reviewed, layout)
    def hook(code):
        if fail and code == "hermes-profile":
            raise RuntimeError("synthetic failure after final owner")
    adapter = _adapter(layout, plan, selection, store, runner, mutation_hook=hook)
    if fail:
        with pytest.raises(TransactionApplyError):
            _execute(tmp_path, plan, adapter)
        assert not (scope.profile / "config.yaml").exists()
        assert not runtime.current_pointer.exists()
    else:
        assert _execute(tmp_path, plan, adapter).status == "committed"
        assert runtime.current_pointer.is_file()
        assert (scope.profile / "config.yaml").is_file()
    assert (scope.home / MARKER).is_file()
    assert not (scope.home / ".daemon-harness-disposable-companion").exists()


@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_review_targets_owned_runtime_and_jobs_without_initializing_it(tmp_path, target_os):
    from daemons_companion.desktop.app import handle_request
    from tests.test_companion_app import _preparation, _plan, _probes
    hostroot = tmp_path / "host"
    hostroot.mkdir()
    host, _, adapter = fixture(hostroot, target_os=target_os)
    execute(host, adapter)
    scope = InstallationScope(host.attempt)
    context = replace(_preparation(tmp_path, target_os=target_os),
                      companion_home=host.companion_home, installation_scope=scope)
    probes = (replace(_probes()[0], config_path=str(scope.profile)), _probes()[1])
    response = handle_request(_plan(action="prepare"), probes=probes,
                              preparation_context=context, finder=lambda _: "/fixture/codex")
    contents = json.dumps(response["plan"])
    assert str(scope.home) in contents
    assert scope.installation_id in contents
    assert not scope.home.exists()
    with pytest.raises(ValueError, match="scope"):
        context.install_layout(HostKind.HERMES, tmp_path / "personal")
    with pytest.raises(ValueError, match="scope"):
        context.install_layout(HostKind.OPENCLAW, scope.profile)
    with pytest.raises(ValueError, match="destination"):
        replace(context, companion_home=tmp_path / "another").validate()


def test_activation_rollback_rechecks_revoked_scope_before_control_commands(tmp_path):
    from types import SimpleNamespace
    from daemons_companion.hermes.activation import HermesActivationAdapter, HermesActivationLayout
    scope = scope_fixture(tmp_path)
    pointer = scope.attempt.parent.parent / "current.json"
    pointer.unlink()
    calls = []
    control = SimpleNamespace(hermes_home=scope.profile, state=lambda: calls.append("state"))
    adapter = HermesActivationAdapter(
        HermesActivationLayout(CompanionRuntimeLayout(scope.home, tmp_path / "bundle", scope), scope.profile),
        control=control)
    # Exercise the authority gate used by rollback and its finalizer independently
    # of a release that can already have been removed by an earlier rollback owner.
    with pytest.raises(ValueError, match="receipt"):
        adapter._validate_scope()
    assert calls == []
