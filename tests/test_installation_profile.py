import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from daemons_companion.desktop.memory import initialize_empty_vault
from daemons_companion.runtime.private_environment import private_process_environment
from daemon_core.installation_profile import (
    MARKER,
    PROFILE_ENV,
    initialize_profile,
    installed_profile_environment,
    profile_status,
    require_profile,
)
from tests.test_companion_installation_scope import scope_fixture
from tests.test_companion_private_setup import connection


@pytest.fixture
def installation(tmp_path):
    scope = scope_fixture(tmp_path)
    connection(scope, tmp_path)
    root = initialize_empty_vault(scope.home / "data/memory")
    return scope, root


def test_fresh_profile_activates_real_gates_for_only_its_owner(installation):
    from daemon_core.scene_model import (
        scene_model_voice_enabled,
        current_topics_canary_enabled,
    )
    from daemon_core.temporal_memory import temporal_curator_enabled
    from daemon_core.episodic_rem import episodic_scene_voice_enabled
    from daemon_core.personal_context import personal_context_canary_enabled
    from daemon_core.memory.note_relocation import relocation_enabled

    scope, root = installation
    # App replacement alone must not enroll an existing root-folder installation.
    assert PROFILE_ENV not in private_process_environment(scope)
    initialize_profile(scope, "12345")
    environments = [
        private_process_environment(scope, hermes=value) for value in (False, True)
    ]
    assert profile_status(environments[0]) == profile_status(environments[1])
    for environment in environments:
        with patch.dict(os.environ, environment, clear=True):
            assert scene_model_voice_enabled("12345")
            assert current_topics_canary_enabled("12345")
            assert temporal_curator_enabled("12345")
            assert relocation_enabled(root)
            for platform in ("telegram", "openclaw"):
                assert episodic_scene_voice_enabled("12345", platform)
                assert personal_context_canary_enabled("12345", platform)
                assert not episodic_scene_voice_enabled("67890", platform)
                assert not personal_context_canary_enabled("67890", platform)
            assert not scene_model_voice_enabled("67890")
            assert not temporal_curator_enabled("67890")
            assert not personal_context_canary_enabled("12345", "web")


@pytest.mark.parametrize(
    "mutation", ["missing_flag", "wildcard", "owner", "root", "marker"]
)
def test_profile_refuses_partial_or_misattributed_configuration(
    installation, tmp_path, mutation
):
    scope, root = installation
    initialize_profile(scope, "12345")
    environment = private_process_environment(scope)
    owner = "12345"
    memory = root
    if mutation == "marker":
        value = json.loads((scope.attempt / MARKER).read_text())
        value["user_ref"] = "different-owner"
        (scope.attempt / MARKER).write_text(json.dumps(value))
        with pytest.raises(ValueError, match="does not match"):
            installed_profile_environment(scope, owner)
        return
    if mutation == "missing_flag":
        environment.pop("DAEMON_CORE_TEMPORAL_CURATOR")
    elif mutation == "wildcard":
        environment["DAEMON_CORE_SCENE_MODEL_USER_IDS"] = "*"
    elif mutation == "owner":
        owner = "67890"
    elif mutation == "root":
        memory = tmp_path / "different-memory"
    with pytest.raises(ValueError, match="profile is incomplete"):
        require_profile(environment, user_ids=(owner,), memory_root=memory)


def test_heartbeat_attests_loaded_profile_and_missing_flag_fails_readiness(
    installation, monkeypatch
):
    from daemon_core import after_turn_service, after_turn_spool

    scope, root = installation
    initialize_profile(scope, "12345")
    environment = private_process_environment(scope)
    spool = after_turn_spool.AfterTurnSpool(scope.home / "data/state/after-turn-spool")
    monkeypatch.setattr(after_turn_service, "MEMORY_ROOT", root)
    monkeypatch.setattr(after_turn_spool, "MEMORY_ROOT", root)
    spool._ensure_runtime_namespace()
    service = after_turn_service.AfterTurnService(
        spool=spool, user_ids=("12345",), harness_factory=object
    )
    service._workers = {
        host: SimpleNamespace(alive=True) for host in ("hermes", "openclaw")
    }
    expected = profile_status(environment, user_ids=("12345",), memory_root=root)

    def ready():
        return after_turn_service.read_after_turn_service_status(
            spool_root=spool.root, memory_root=root, expected_profile=expected
        )

    with patch.dict(os.environ, environment, clear=True):
        service.write_heartbeat()
        assert ready()["ok"]
        del os.environ["DAEMON_CORE_SCENE_MODEL"]
        service.write_heartbeat()
        assert not ready()["ok"]
        assert "after_turn_service_profile_mismatch" in ready()["failures"]
    content = (spool.root / "service.json").read_text()
    assert str(root) not in content and "12345" not in content


def test_gateway_readiness_requires_receipt_from_live_process(installation):
    from daemons_companion.hermes.gateway import _profile_ready, _gateway_ready

    scope, _ = installation
    initialize_profile(scope, "12345")
    expected = profile_status(private_process_environment(scope, hermes=True))
    assert not _profile_ready(scope, 123)
    path = scope.attempt / "gateway-continuity.json"
    path.write_text(json.dumps({"pid": 123, "profile": expected}))
    assert _profile_ready(scope, 123)
    assert not _profile_ready(scope, 456)
    probe = {
        "pid": 123,
        "status": {
            "gateway_state": "running",
            "platforms": {"telegram": {"state": "connected"}},
        },
    }
    assert _gateway_ready({**probe, "continuity_profile_ok": True})
    assert not _gateway_ready({**probe, "continuity_profile_ok": False})


@pytest.mark.parametrize("provider", ["mistral_api", "openai_api", "anthropic_api"])
def test_api_connection_loads_same_profile_without_querying_credentials(
    installation, provider
):
    scope, root = installation
    (scope.attempt / "connection.json").write_text(
        json.dumps(
            {
                "schema": "daemon.private-connection.v2",
                "provider_kind": provider,
                "user_id": "12345",
            }
        )
    )
    initialize_profile(scope, "12345")
    for hermes in (False, True):
        environment = private_process_environment(
            scope, hermes=hermes, credentials=False
        )
        require_profile(environment, user_ids=("12345",), memory_root=root)
        assert profile_status(environment)["ok"]
        assert environment["DAEMON_CORE_MEMORY_RELOCATION"] == "v1"
        assert "CODEX_HOME" not in environment


@pytest.mark.parametrize("invalid", [None, "owner", "flag"])
def test_attached_openclaw_keeps_shared_profile_and_refuses_incomplete_one(
    installation, monkeypatch, invalid
):
    from daemons_companion.runtime import service_entry as background_entry
    from daemons_companion.runtime import release
    from daemons_companion.hosts.attachment_entry import api_environment

    scope, root = installation
    initialize_profile(scope, "12345")
    source = scope.home / "data/runtime/releases" / ("a" * 64)
    source.mkdir(parents=True)
    environment = private_process_environment(scope)
    if invalid == "flag":
        del environment["DAEMON_CORE_TEMPORAL_CURATOR"]

    def configure(args):
        os.environ.clear()
        os.environ.update(environment)

    monkeypatch.setattr(background_entry, "_configure", configure)
    monkeypatch.setattr(
        release, "verify_current_release", lambda layout: source
    )
    state = scope.home / "data/state"
    record = {
        "profile": str(scope.attempt / "openclaw/profile"),
        "binding": {
            "state": str(state),
            "source": str(source),
            "memory": str(root),
            "python": str(source / "bin/python"),
            "user_id": "67890" if invalid == "owner" else "12345",
            "spool": str(state / "after-turn-spool"),
            "receipts": str(state / "receipts"),
        },
    }
    with patch.dict(os.environ, {}, clear=True):
        if invalid:
            with pytest.raises(ValueError, match="profile is incomplete"):
                api_environment(record)
        else:
            actual = api_environment(record)
            require_profile(actual, user_ids=("12345",), memory_root=root)
            assert profile_status(actual) == profile_status(environment)
            assert (
                actual["DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS"]
                == "telegram,openclaw"
            )


def test_fresh_v1_profile_activates_reference_faculties_for_its_owner(installation):
    from daemon_core.common_ground_library import common_ground_library_enabled
    from daemon_core.common_ground_shadow import common_ground_shadow_enabled
    from daemon_core.memory.relation_shadow import (
        memory_collection_bootstrap_shadow_enabled,
        memory_performative_relation_retractions_enabled,
        memory_relation_shadow_enabled,
        memory_topology_bundle_shadow_enabled,
    )
    from daemon_core.receipt_reaction import receipt_reaction_emoji
    from integrations.hermes_voice_plugin import _hermes_progress_preamble_canary_enabled

    scope, _ = installation
    initialize_profile(scope, "12345")
    assert json.loads((scope.attempt / MARKER).read_text())["profile"] == "parga-v1"
    for hermes in (False, True):
        environment = private_process_environment(scope, hermes=hermes)
        assert profile_status(environment) == {**profile_status(environment), "profile": "parga-v1", "ok": True}
        with patch.dict(os.environ, environment, clear=True):
            for owner, expected in (("12345", True), ("67890", False)):
                assert common_ground_shadow_enabled(owner) is expected
                assert memory_relation_shadow_enabled(owner) is expected
                assert memory_collection_bootstrap_shadow_enabled(owner) is expected
                assert memory_topology_bundle_shadow_enabled(owner) is expected
                assert memory_performative_relation_retractions_enabled(owner) is expected
                for platform in ("telegram", "openclaw"):
                    assert common_ground_library_enabled(owner, platform) is expected
            assert _hermes_progress_preamble_canary_enabled("telegram")
            assert not _hermes_progress_preamble_canary_enabled("openclaw")
            assert receipt_reaction_emoji() is None
            assert environment["DAEMON_CORE_AFTER_TURN_USER_IDS"] == "12345"


def test_existing_v0_marker_keeps_exactly_its_switches(installation):
    from daemon_core.installation_profile import _marker_value, profile_environment

    scope, root = installation
    marker = _marker_value(scope, "12345", "parga-v0")
    (scope.attempt / MARKER).write_text(json.dumps(marker, sort_keys=True) + "\n")
    environment = private_process_environment(scope)
    expected = profile_environment("12345", root, profile="parga-v0")
    assert {key: environment[key] for key in expected} == expected
    assert profile_status(environment)["profile"] == "parga-v0"
    assert profile_status(environment)["ok"]
    for absent in ("COMMON_GROUND_LIBRARY", "HERMES_PROGRESS_PREAMBLE", "RECEIPT_REACTION"):
        assert "DAEMON_CORE_" + absent not in environment
    # The marker is never rewritten to the newer profile.
    assert json.loads((scope.attempt / MARKER).read_text())["profile"] == "parga-v0"


def test_unknown_profile_marker_or_environment_is_refused(installation):
    scope, root = installation
    initialize_profile(scope, "12345")
    environment = private_process_environment(scope)
    environment[PROFILE_ENV] = "parga-v9"
    with pytest.raises(ValueError, match="profile is incomplete"):
        require_profile(environment, user_ids=("12345",), memory_root=root)
    value = json.loads((scope.attempt / MARKER).read_text())
    value["profile"] = "parga-v9"
    (scope.attempt / MARKER).write_text(json.dumps(value))
    with pytest.raises(ValueError, match="does not match"):
        installed_profile_environment(scope, "12345")


def test_behavior_switch_values_have_one_owner():
    """Companion code imports switch groups instead of repeating their literals."""
    import re
    from pathlib import Path
    from daemon_core import installation_profile

    groups = (
        installation_profile.DURABLE_AFTER_TURN, installation_profile.HOST_VOICE,
        installation_profile.CONTINUITY_RECEIPTS, installation_profile.SCENE_CONTINUITY,
        installation_profile.VAULT_FORMAT, installation_profile.EMBEDDING_SELECTION,
    )
    switches = {
        key for values, _, _ in installation_profile._PROFILES.values() for key in values
    }.union(*groups)
    switches = {key for key in switches if key.startswith("DAEMON_CORE_")}
    pattern = re.compile(r"""["'](%s)["']\s*:\s*["']""" % "|".join(sorted(switches)))
    root = Path(installation_profile.__file__).resolve().parents[2]
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for directory in (root / "src", root / "integrations")
        for path in directory.rglob("*.py")
        if path.name != "installation_profile.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []
