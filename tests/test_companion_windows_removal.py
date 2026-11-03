"""Windows cleanup boundary checks, simulated on non-Windows platforms."""

import json
import pytest
from daemons_companion.removal.removal import Refused


def test_windows_task_definition_requires_private_command_and_current_identity(
    tmp_path,
):
    from daemons_companion.removal.windows import validate_task
    from xml.etree import ElementTree as ET

    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    command = tmp_path / "worker.exe"
    command.write_bytes(b"fixture")
    xml = f"""<Task xmlns="{ns[1:-1]}"><Principals><Principal><UserId>S-1-5-21-123</UserId>
<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Actions><Exec><Command>{command}</Command><WorkingDirectory>{tmp_path}</WorkingDirectory></Exec></Actions></Task>"""
    validate_task(xml.encode(), tmp_path, "S-1-5-21-123")
    for key, value in (
        ("UserId", "S-1-5-21-999"),
        ("RunLevel", "HighestAvailable"),
        ("LogonType", "Password"),
        ("Command", "/outside/worker"),
    ):
        root = ET.fromstring(xml)
        root.find(".//" + ns + key).text = value
        with pytest.raises(Refused, match="services"):
            validate_task(ET.tostring(root), tmp_path, "S-1-5-21-123")


def test_windows_scheduler_arguments_do_not_become_powershell_code(tmp_path):
    import base64
    from daemons_companion.runtime.services import SchedulerCommandResult
    from daemons_companion.removal.windows import WindowsRemovalScheduler

    calls = []

    def runner(command, environment):
        calls.append(command)
        return SchedulerCommandResult(0, "", "")

    scheduler = WindowsRemovalScheduler(runner=runner, job_namespace="a" * 32)
    attempt = tmp_path / "space & accented é"
    scheduler.require_exclusive(attempt, attempt / "memory", ["safe.one", "safe.two"])
    payload = json.loads(base64.b64decode(calls[-1][-1]))
    assert payload["attempt"] == str(attempt)
    assert payload["codes"] == ["safe.one", "safe.two"]
    assert str(attempt) not in calls[-1][-2]
    scheduler.require_stopped(attempt)
    assert base64.b64decode(calls[-1][-1]).decode() == str(attempt)


def test_stop_failure_does_not_delete_task():
    from daemons_companion.runtime.services import SchedulerCommandResult
    from daemons_companion.removal.windows import WindowsRemovalScheduler

    calls = []

    def runner(command, environment):
        calls.append(command)
        return SchedulerCommandResult(1, "", "still running")

    scheduler = WindowsRemovalScheduler(runner=runner, job_namespace="a" * 32)
    with pytest.raises(RuntimeError, match="stopping"):
        scheduler.restore(
            "com.daemonharness.test." + "a" * 32 + ".hermes-gateway", None
        )
    assert len(calls) == 1
    assert "DeleteTask" not in str(calls)


def test_native_handle_backend_is_not_simulated_as_a_windows_pass():
    import os
    from daemons_companion.removal.windows import _WinFiles

    if os.name != "nt":
        with pytest.raises(OSError, match="Windows handles"):
            _WinFiles()


def test_handle_walk_unlinks_nested_reparse_and_binds_root(tmp_path, monkeypatch):
    """Model handle identities/traversal; this does not call Windows APIs."""
    from contextlib import contextmanager
    from types import SimpleNamespace
    from daemons_companion.removal import windows as module

    root = tmp_path / "owned"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("exact")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "program").write_text("program")
    removed = []

    class Handles:
        @contextmanager
        def opened(self, path, deleting=False):
            info = path.lstat()
            attrs = 0x400 if path.is_symlink() else (0x10 if path.is_dir() else 0)
            yield (
                path,
                SimpleNamespace(
                    attributes=attrs,
                    volume=info.st_dev,
                    index_high=info.st_ino >> 32,
                    index_low=info.st_ino & 0xFFFFFFFF,
                ),
            )

        def identity(self, handle, info):
            return [info.volume, (info.index_high << 32) | info.index_low]

        def remove(self, path, info):
            removed.append(path)
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()

    monkeypatch.setattr(module, "_WinFiles", Handles)
    monkeypatch.setattr(module, "_windows_extended_path", lambda p: p)
    info = root.stat()
    with pytest.raises(ValueError, match="changed"):
        module.remove_tree(root, [info.st_dev, info.st_ino + 1])
    assert not removed
    module.remove_tree(root, [info.st_dev, info.st_ino])
    assert not root.exists()
    assert (outside / "keep").read_text() == "exact"
    assert outside not in removed


def test_dependency_scan_reports_only_stage_and_native_number(tmp_path):
    from daemons_companion.runtime.services import SchedulerCommandResult
    from daemons_companion.removal.windows import WindowsRemovalScheduler
    calls = []
    def runner(command, environment):
        calls.append(command)
        return SchedulerCommandResult(1, "private task XML", "secret path and key\nDAEMON_REMOVAL|task_xml|-2147024891\n")
    scheduler = WindowsRemovalScheduler(runner=runner, job_namespace="a" * 32)
    with pytest.raises(Refused) as caught:
        scheduler.require_exclusive(tmp_path, tmp_path / "memory", [])
    assert caught.value.code == "services"
    assert caught.value.diagnostic == {"stage": "task_xml", "exit_code": 1, "hresult": -2147024891}
    assert "secret" not in json.dumps(caught.value.diagnostic)
    assert "try{$diagnosticStage='scheduler_connect';" in calls[0][-2]
    assert "DeleteTask" not in calls[0][-2] and ".Stop(" not in calls[0][-2]


def test_shared_scan_remains_blocked_with_diagnostic(tmp_path):
    from daemons_companion.runtime.services import SchedulerCommandResult
    from daemons_companion.removal.windows import WindowsRemovalScheduler
    scheduler = WindowsRemovalScheduler(runner=lambda *a: SchedulerCommandResult(4, "", ""), job_namespace="a" * 32)
    with pytest.raises(Refused) as caught:
        scheduler.require_exclusive(tmp_path, tmp_path / "memory", [])
    assert caught.value.code == "shared"
    assert caught.value.diagnostic["stage"] == "dependency_scan"


def test_reviewed_modern_identity_survives_legacy_high_bits(tmp_path, monkeypatch):
    """Modern stat identity differs from legacy info for the same opened root."""
    from contextlib import contextmanager
    from types import SimpleNamespace
    from daemons_companion.removal import windows as module

    root = tmp_path / "owned"
    root.mkdir()
    expected = [0x12345678ABCDEF01, (1 << 96) | 123]
    removed = []

    class Handles:
        @contextmanager
        def opened(self, path, deleting=False):
            yield path, SimpleNamespace(attributes=0x10, volume=expected[0] & 0xFFFFFFFF,
                                        index_high=0, index_low=123)

        def identity(self, handle, info):
            assert handle == root
            return expected

        def remove(self, handle, info):
            removed.append(handle)

    monkeypatch.setattr(module, "_WinFiles", Handles)
    monkeypatch.setattr(module, "_windows_extended_path", lambda p: p)
    module.remove_tree(root, expected)
    assert removed == [root]
    removed.clear()
    with pytest.raises(ValueError):
        module.remove_tree(root, [expected[0] + 1, expected[1]])
    assert not removed


def test_file_id_info_keeps_all_bits_and_uses_exact_buffer():
    import ctypes
    from types import SimpleNamespace
    from daemons_companion.removal.windows import _WinFiles, _FileIdInfo
    expected = [0x12345678ABCDEF01, (1 << 110) | 123]

    def query(handle, info_class, buffer, size):
        assert (handle, info_class, size) == (42, 18, 24)
        value = ctypes.cast(buffer, ctypes.POINTER(_FileIdInfo)).contents
        value.volume = expected[0]
        value.file_id[:] = expected[1].to_bytes(16, 'little')
        return True

    api = _WinFiles.__new__(_WinFiles)
    api.api = SimpleNamespace(GetFileInformationByHandleEx=query)
    legacy = SimpleNamespace(volume=expected[0] & 0xFFFFFFFF, index_high=0, index_low=123)
    assert api.identity(42, legacy) == expected


@pytest.mark.parametrize('error,allowed', [(1, True), (50, True), (87, True), (5, False), (32, False)])
def test_file_id_fallback_requires_unsupported_api(monkeypatch, error, allowed):
    import ctypes
    from types import SimpleNamespace
    from daemons_companion.removal.windows import _WinFiles
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: error, raising=False)
    api = _WinFiles.__new__(_WinFiles)
    api.api = SimpleNamespace(GetFileInformationByHandleEx=lambda *args: False)
    legacy = SimpleNamespace(volume=10, index_high=20, index_low=30)
    if allowed:
        assert api.identity(42, legacy) == [10, (20 << 32) | 30]
    else:
        with pytest.raises(Refused) as caught:
            api.identity(42, legacy)
        assert caught.value.diagnostic == {'stage': 'file_identity', 'winerror': error}
