"""A shared worker must never route a scoped follow-up to its default host."""

import hashlib
import json
from pathlib import Path
import pytest
from daemon_core.host_bindings import resolve_host_execution, SCHEMA


@pytest.fixture
def binding(tmp_path):
    root = tmp_path.resolve()
    state = root / "state"
    registry = state / "host-bindings"
    registry.mkdir(parents=True)
    memory = root / "memory"
    memory.mkdir()
    profile = root / "host"
    profile.mkdir()
    (profile / "openclaw.json").write_text("{}")
    executable = root / "node"
    executable.write_text("synthetic executable")
    data = {
        "schema": SCHEMA,
        "host_ref": "a" * 32,
        "status": "active",
        "host": "openclaw",
        "user_id": "u1",
        "memory": str(memory),
        "profile": str(profile),
        "executables": [
            {
                "path": str(executable),
                "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            }
        ],
    }
    file = registry / ("a" * 32 + ".json")
    file.write_text(json.dumps(data))
    env = {
        "DAEMON_CORE_STATE_ROOT": str(state),
        "OPENCLAW_STATE_DIR": "/wrong-default",
        "OPENCLAW_CONFIG_PATH": "/wrong-config",
    }
    return data, file, memory, env


def resolve(binding, origin=None):
    _, _, memory, env = binding
    return resolve_host_execution(
        {"host_ref": "a" * 32} if origin is None else origin,
        host="openclaw",
        user_id="u1",
        memory=memory,
        environment=env,
    )


def test_legacy_origin_is_unchanged(binding):
    assert resolve(binding, {}) is None


def test_explicit_host_overrides_wrong_worker_defaults(binding):
    data, _, _, env = binding
    r = resolve(binding)
    assert r.environment(env)["OPENCLAW_STATE_DIR"] == data["profile"]
    assert r.environment(env)["OPENCLAW_CONFIG_PATH"] == str(
        Path(data["profile"]) / "openclaw.json"
    )
    assert r.argv == (data["executables"][0]["path"],)
    assert r.relay_environment()["DAEMON_CORE_HOST_REF"] == "a" * 32


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("status", "prepared"),
        ("host", "hermes"),
        ("user_id", "other"),
        ("schema", "other"),
    ],
)
def test_unavailable_or_wrong_owner_never_falls_back(binding, key, value):
    data, file, _, _ = binding
    data[key] = value
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        resolve(binding)


@pytest.mark.parametrize("reference", ["../other", "", None, 123, "A" * 32])
def test_malformed_explicit_reference(binding, reference):
    with pytest.raises(ValueError):
        resolve(binding, {"host_ref": reference})


def test_missing_registry_refuses_default(binding):
    binding[1].unlink()
    with pytest.raises(FileNotFoundError):
        resolve(binding)


def test_modified_executable_refused(binding):
    Path(binding[0]["executables"][0]["path"]).write_text("changed")
    with pytest.raises(ValueError, match="executable changed"):
        resolve(binding)


def test_different_memory_refused(binding):
    data, file, memory, _ = binding
    other = memory.parent / "other"
    other.mkdir()
    data["memory"] = str(other)
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="another daemon"):
        resolve(binding)


def test_registry_symlink_refused(binding):
    data, file, _, _ = binding
    other = file.with_name("original.json")
    file.rename(other)
    file.symlink_to(other)
    with pytest.raises(ValueError):
        resolve(binding)
