import os
from zoneinfo import ZoneInfo

import pytest

from daemon_core.platform_support import create_private_file
from scripts.qualification.windows_portability_probe import process_probe


def test_timezone_database_is_available():
    for name in ("Europe/Paris", "America/New_York", "Asia/Tokyo", "UTC"):
        assert ZoneInfo(name).key == name


@pytest.mark.parametrize(
    "module", ["daemon_core.after_turn_spool", "daemon_core.registre"]
)
def test_liveness_consumers_do_not_terminate_a_live_child(module):
    result = process_probe(module)
    assert result == {
        "reported_alive": True,
        "child_survived_probe": True,
        "child_exit": 0,
        "reported_alive_after_exit": False,
    }


def test_file_is_private_before_content_and_creation_is_exclusive(
    tmp_path, assert_private_file
):
    path = tmp_path / "private.json"
    fd = create_private_file(path)
    try:
        assert os.fstat(fd).st_size == 0
        assert_private_file(path, fd=fd)
        assert not os.get_inheritable(fd)
        os.write(fd, b"synthetic private data")
    finally:
        os.close(fd)
    with pytest.raises(FileExistsError):
        create_private_file(path)
    assert path.read_bytes() == b"synthetic private data"
    assert_private_file(path)


def test_private_creation_fails_closed_if_permissions_cannot_be_verified(
    monkeypatch, tmp_path
):
    def denied(*args):
        raise PermissionError("synthetic permission failure")

    if os.name == "nt":
        from daemon_core.platform_support import _api

        monkeypatch.setattr(_api(), "GetSecurityInfo", denied)
    else:
        monkeypatch.setattr(os, "fchmod", denied)
    path = tmp_path / "private.json"
    with pytest.raises(PermissionError, match="synthetic permission failure"):
        create_private_file(path)
    assert not path.exists()


@pytest.mark.parametrize("error_code, expected", [(5, True), (87, False), (8, None)])
def test_windows_liveness_does_not_treat_unexpected_errors_as_dead(
    monkeypatch, error_code, expected
):
    if os.name != "nt":
        pytest.skip("Win32 error contract")
    import ctypes
    from daemon_core.platform_support import _api, process_is_alive

    def failed(*args):
        ctypes.set_last_error(error_code)
        return None

    monkeypatch.setattr(_api(), "OpenProcess", failed)
    if expected is None:
        with pytest.raises(OSError):
            process_is_alive(1234)
    else:
        assert process_is_alive(1234) is expected


@pytest.mark.skipif(os.name != "nt", reason="native Windows account lookup")
def test_windows_current_account_name_resolves_to_process_sid():
    import uuid
    import win32security
    from daemon_core.platform_support import _account_sid, _user_sid
    sid = _user_sid()
    name, domain, _ = win32security.LookupAccountSid(None, win32security.ConvertStringSidToSid(sid))
    assert _account_sid(domain + "\\" + name) == sid
    with pytest.raises(OSError):
        _account_sid("NoSuchDaemonUser" + uuid.uuid4().hex)
