import importlib.util
import json
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/manage_memory_structure_canary.py"
spec = importlib.util.spec_from_file_location("structure_canary", SCRIPT)
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    runtime = tmp_path / "canary"
    runtime.mkdir()
    source = tmp_path / "original-profile"
    source.mkdir()
    (source / "gateway_state.json").write_text('{"active_agents": 0}')
    candidate = runtime / "hermes-home"
    candidate.mkdir()
    gateway = tmp_path / "gateway.plist"
    worker = tmp_path / "worker.plist"
    for path, label in ((gateway, "original"), (runtime / "original-gateway.plist", "original"),
                        (runtime / "candidate-gateway.plist", "candidate"), (runtime / "candidate-worker.plist", "worker")):
        path.write_bytes(plistlib.dumps({"Label": label}))
    record = {"status": "prepared", "revision": {"commit": "abc"},
        "gateway_path": str(gateway), "worker_path": str(worker),
        "source_profile": str(source), "profile": str(candidate),
        "original_gateway_sha256": canary.digest(gateway),
        "candidate_gateway_sha256": canary.digest(runtime / "candidate-gateway.plist"),
        "candidate_worker_sha256": canary.digest(runtime / "candidate-worker.plist")}
    (tmp_path / "qualification.json").write_text(json.dumps({"candidate_commit": "abc",
        "isolated_lifecycle_passed": True, "context_page_writes_passed": True,
        "content_integrity_passed": True}))
    events = []
    monkeypatch.setattr(canary, "verify_source_snapshot", lambda root: None)
    monkeypatch.setattr(canary, "require_codex_login", lambda runtime: None)
    monkeypatch.setattr(canary, "stop_job", lambda label: events.append(("stop", label)))
    monkeypatch.setattr(canary, "launchctl", lambda *args, **kwargs: events.append(args) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(canary, "worker_ready", lambda runtime: True)
    monkeypatch.setattr(canary, "gateway_ready", lambda profile: True)
    return tmp_path, record, events


def test_persistent_canary_auth_requires_native_login_without_copying(tmp_path):
    home = tmp_path / "codex-home"
    canary.prepare_persistent_codex_home(home)
    assert not (home / "auth.json").exists()
    assert (home / "config.toml").read_text() == 'cli_auth_credentials_store = "file"\n'
    (tmp_path / "canary.env").write_text(f'CODEX_HOME="{home}"\n')
    with pytest.raises(RuntimeError, match="Sign in"):
        canary.require_codex_login(tmp_path)


def test_canary_stops_original_before_start_and_rollback_restores_exactly(deployment):
    root, record, events = deployment
    before = (root / "gateway.plist").read_bytes()
    canary.activate(root, record)
    assert record["status"] == "active"
    assert events[0] == ("stop", canary.GATEWAY_LABEL)
    assert events[1][-1] == record["worker_path"]
    assert events[2][-1] == record["gateway_path"]
    canary.restore(root, record)
    assert record["status"] == "rolled_back"
    assert (root / "gateway.plist").read_bytes() == before
    assert not (root / "worker.plist").exists()
    assert (root / "canary/hermes-home").is_dir()


def test_failed_start_restores_original_without_losing_private_copy(deployment, monkeypatch):
    root, record, events = deployment
    calls = []
    def fail_once(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("injected startup failure")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(canary, "launchctl", fail_once)
    with pytest.raises(RuntimeError, match="injected"):
        canary.activate(root, record)
    assert record["status"] == "rolled_back"
    assert (root / "gateway.plist").read_bytes() == (root / "canary/original-gateway.plist").read_bytes()
    assert (root / "canary/hermes-home").is_dir()


@pytest.mark.parametrize("fault", ["revision", "active_turn", "external_definition"])
def test_canary_refuses_stale_qualification_busy_turn_and_external_edit(deployment, fault):
    root, record, events = deployment
    if fault == "revision":
        record["revision"]["commit"] = "other"
    elif fault == "active_turn":
        (root / "original-profile/gateway_state.json").write_text('{"active_agents": 1}')
    else:
        (root / "gateway.plist").write_text("external change")
    with pytest.raises((ValueError, RuntimeError)):
        canary.activate(root, record)
    assert not events


def test_rollback_preserves_external_configuration_change(deployment):
    root, record, events = deployment
    canary.activate(root, record)
    (root / "worker.plist").write_text("external change")
    events.clear()
    with pytest.raises(ValueError):
        canary.restore(root, record)
    assert not events
    assert (root / "worker.plist").read_text() == "external change"


def test_no_second_consumer_if_launchd_job_remains_loaded(monkeypatch):
    monkeypatch.setattr(canary, "launchctl", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    moments = iter([0, 16])
    monkeypatch.setattr(canary.time, "monotonic", lambda: next(moments))
    with pytest.raises(RuntimeError, match="second consumer"):
        canary.stop_job(canary.GATEWAY_LABEL)


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="Unix gateway socket")
def test_profile_snapshot_skips_live_socket_preserves_durable_state():
    import socket
    import tempfile
    with tempfile.TemporaryDirectory(prefix="daemon-ipc-", dir="/tmp") as folder:
        root = Path(folder)
        source = root / "original"
        (source / "state").mkdir(parents=True)
        (source / "state/data.json").write_text('{"retained":true}')
        with socket.socket(socket.AF_UNIX) as endpoint:
            endpoint.bind(str(source / "state/live.sock"))
            canary.copy_profile(source, root / "copy")
        assert not (root / "copy/state/live.sock").exists()
        assert (root / "copy/state/data.json").read_bytes() == (source / "state/data.json").read_bytes()


def test_snapshot_gate_rejects_new_personal_dialogue(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    note = source / "journal.md"
    note.write_text("Original source")
    (tmp_path / "manifest.json").write_text(json.dumps({"source": str(source),
        "source_hashes": {"journal.md": canary.digest(note)}}))
    canary.verify_source_snapshot(tmp_path)
    note.write_text("Original source\nNew dialogue")
    with pytest.raises(ValueError, match="changed since snapshot"):
        canary.verify_source_snapshot(tmp_path)



def test_stop_waits_for_launchd_async_teardown(monkeypatch):
    statuses = iter([0, 0, 113])
    calls = []
    def control(*args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(returncode=next(statuses) if args[0] == 'print' else 0)
    monkeypatch.setattr(canary, 'launchctl', control)
    monkeypatch.setattr(canary.time, 'sleep', lambda seconds: None)
    canary.stop_job(canary.GATEWAY_LABEL)
    assert calls == ['bootout', 'print', 'print', 'print']



def test_profile_location_bounds_native_unix_socket(monkeypatch):
    monkeypatch.setattr(canary.Path, 'home', classmethod(lambda cls: Path('/Users/test')))
    root = Path('/Users/test/Documents/Daemons/prototypes/long-memory-structure-prototype')
    profile = canary.profile_location(root)
    assert str(profile).startswith('/Users/test/.daemon-canaries/')
    assert len(str(profile / 'state/gateway.loop-tick.9999999999.sock').encode()) < 104
    with pytest.raises(ValueError, match='shorter'):
        canary.profile_location(root, root / ('x' * 80))


def test_canary_capture_path_is_accepted_by_the_real_writer(tmp_path, monkeypatch):
    from daemon_core import voice_request_capture as capture

    monkeypatch.setattr(canary, "REPO", tmp_path / "checkout")
    monkeypatch.setattr(capture, "_REPOSITORY_ROOT", canary.REPO)
    root = tmp_path / "visible-vault"
    target = canary.voice_capture_file(root)
    monkeypatch.setenv(capture.VOICE_CAPTURE_FILE_ENV, str(target))
    body = {"instructions": "Synthetic complete prompt", "input": [], "tools": []}
    handle = capture.begin_voice_request_capture(body, metadata={"api_call_count": 1})
    assert handle is not None
    assert json.loads(target.read_text())["request"]["body"] == body
    assert target.stat().st_mode & 0o777 == 0o600
    assert canary.voice_capture_file(root) == target
    assert canary.voice_capture_file(tmp_path / "other-vault") != target
    assert capture.begin_voice_request_capture(body, metadata={"api_call_count": 1}) is None


def test_hermes_rollback_preserves_worker_used_by_openclaw(deployment):
    root, record, events = deployment
    canary.activate(root, record)
    marker = root / "canary/openclaw/deployment.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"status": "active"}))
    events.clear()
    with pytest.raises(ValueError, match="OpenClaw"):
        canary.restore(root, record)
    assert events == []
    assert Path(record["worker_path"]).exists()
    marker.write_text(json.dumps({"status": "rolled_back"}))
    canary.restore(root, record)
    assert record["status"] == "rolled_back"
