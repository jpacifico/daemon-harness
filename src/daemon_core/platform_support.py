"""OS primitives for private persistence; no host or conversation policy.

Windows uses the Win32 API through ctypes so the dependency-free installer and
Hermes' interpreter share the same contracts without a global pywin32 install.
"""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from functools import lru_cache
import ntpath
import os
from pathlib import Path
import stat


class _SecurityAttributes(c.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]


class _Overlapped(c.Structure):
    _fields_ = [
        ("internal", c.c_size_t),
        ("internal_high", c.c_size_t),
        ("offset", w.DWORD),
        ("offset_high", w.DWORD),
        ("event", w.HANDLE),
    ]


class _Acl(c.Structure):
    _fields_ = [
        ("revision", w.BYTE),
        ("reserved", w.BYTE),
        ("size", w.WORD),
        ("count", w.WORD),
        ("reserved2", w.WORD),
    ]


class _Ace(c.Structure):
    _fields_ = [
        ("kind", w.BYTE),
        ("flags", w.BYTE),
        ("size", w.WORD),
        ("mask", w.DWORD),
    ]


@lru_cache(maxsize=1)
def _api():
    kernel = c.WinDLL("kernel32", use_last_error=True)
    security = c.WinDLL("advapi32", use_last_error=True)
    pointer = c.c_void_p
    signatures = {
        "CloseHandle": (kernel, w.BOOL, [w.HANDLE]),
        "LocalFree": (kernel, pointer, [pointer]),
        "GetCurrentProcess": (kernel, w.HANDLE, []),
        "OpenProcess": (kernel, w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
        "WaitForSingleObject": (kernel, w.DWORD, [w.HANDLE, w.DWORD]),
        "CreateFileW": (
            kernel,
            w.HANDLE,
            [w.LPCWSTR, w.DWORD, w.DWORD, pointer, w.DWORD, w.DWORD, w.HANDLE],
        ),
        "LockFileEx": (
            kernel,
            w.BOOL,
            [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, pointer],
        ),
        "UnlockFileEx": (
            kernel,
            w.BOOL,
            [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, pointer],
        ),
        "OpenProcessToken": (security, w.BOOL, [w.HANDLE, w.DWORD, pointer]),
        "GetTokenInformation": (
            security,
            w.BOOL,
            [w.HANDLE, w.DWORD, pointer, w.DWORD, pointer],
        ),
        "ConvertSidToStringSidW": (security, w.BOOL, [pointer, pointer]),
        "LookupAccountNameW": (
            security, w.BOOL,
            [w.LPCWSTR, w.LPCWSTR, pointer, pointer, w.LPWSTR, pointer, pointer],
        ),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
            security,
            w.BOOL,
            [w.LPCWSTR, w.DWORD, pointer, pointer],
        ),
        "GetSecurityInfo": (
            security,
            w.DWORD,
            [w.HANDLE, w.DWORD, w.DWORD, pointer, pointer, pointer, pointer, pointer],
        ),
        "GetNamedSecurityInfoW": (
            security,
            w.DWORD,
            [w.LPCWSTR, w.DWORD, w.DWORD, pointer, pointer, pointer, pointer, pointer],
        ),
        "SetNamedSecurityInfoW": (
            security,
            w.DWORD,
            [w.LPCWSTR, w.DWORD, w.DWORD, pointer, pointer, pointer, pointer],
        ),
        "GetSecurityDescriptorControl": (security, w.BOOL, [pointer, pointer, pointer]),
        "GetSecurityDescriptorDacl": (
            security,
            w.BOOL,
            [pointer, pointer, pointer, pointer],
        ),
        "GetAce": (security, w.BOOL, [pointer, w.DWORD, pointer]),
    }
    from types import SimpleNamespace

    result = SimpleNamespace()
    for name, (library, restype, argtypes) in signatures.items():
        function = getattr(library, name)
        function.restype, function.argtypes = restype, argtypes
        setattr(result, name, function)
    return result


def _check(result):
    if not result:
        raise c.WinError(c.get_last_error())
    return result


def _status(code):
    if code:
        raise c.WinError(code)


def _windows_path(path):
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return value
    return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value


def _sid_text(sid):
    api = _api()
    value = c.c_void_p()
    _check(api.ConvertSidToStringSidW(sid, c.byref(value)))
    try:
        return c.wstring_at(value)
    finally:
        api.LocalFree(value)


def _user_sid():
    api = _api()
    token = w.HANDLE()
    _check(api.OpenProcessToken(api.GetCurrentProcess(), 8, c.byref(token)))
    try:
        size = w.DWORD()
        api.GetTokenInformation(token, 1, None, 0, c.byref(size))
        if not size.value:
            raise c.WinError(c.get_last_error())
        buffer = c.create_string_buffer(size.value)
        _check(api.GetTokenInformation(token, 1, buffer, size, c.byref(size)))
        return _sid_text(c.cast(buffer, c.POINTER(c.c_void_p))[0])
    finally:
        api.CloseHandle(token)



def _account_sid(name: str) -> str:
    """Resolve a Windows-exported account name for exact identity comparison."""
    if not name or len(name) > 1024 or "\0" in name:
        raise ValueError("Windows account name is invalid.")
    api = _api()
    sid_size, domain_size, kind = w.DWORD(), w.DWORD(), w.DWORD()
    api.LookupAccountNameW(None, name, None, c.byref(sid_size), None,
        c.byref(domain_size), c.byref(kind))
    if not sid_size.value:
        raise c.WinError(c.get_last_error())
    sid = c.create_string_buffer(sid_size.value)
    domain = c.create_unicode_buffer(max(1, domain_size.value))
    _check(api.LookupAccountNameW(None, name, sid, c.byref(sid_size), domain,
        c.byref(domain_size), c.byref(kind)))
    return _sid_text(sid)


def _descriptor(sid, *, directory=False):
    descriptor = c.c_void_p()
    flags = "OICI" if directory else ""
    _check(
        _api().ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"D:P(A;{flags};FA;;;{sid})", 1, c.byref(descriptor), None
        )
    )
    return descriptor


def _dacl(descriptor):
    present, defaulted, acl = w.BOOL(), w.BOOL(), c.c_void_p()
    _check(
        _api().GetSecurityDescriptorDacl(
            descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)
        )
    )
    return acl if present.value else c.c_void_p()


def _private_descriptor(descriptor, sid, *, directory=False):
    api = _api()
    control, revision = w.WORD(), w.DWORD()
    _check(
        api.GetSecurityDescriptorControl(
            descriptor, c.byref(control), c.byref(revision)
        )
    )
    acl = _dacl(descriptor)
    if (
        not control.value & 0x1000
        or not acl
        or c.cast(acl, c.POINTER(_Acl)).contents.count != 1
    ):
        return False
    ace = c.c_void_p()
    _check(api.GetAce(acl, 0, c.byref(ace)))
    header = c.cast(ace, c.POINTER(_Ace)).contents
    return (
        header.kind == 0
        and header.flags == (3 if directory else 0)
        and header.mask == 0x1F01FF
        and _sid_text(ace.value + 8) == sid
    )


def process_is_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    if pid <= 0:
        return False
    api = _api()
    handle = api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE, never a signal
    if not handle:
        error = c.get_last_error()
        if error == 87:
            return False
        if error == 5:
            return True
        raise c.WinError(error)
    try:
        state = api.WaitForSingleObject(handle, 0)
        if state == 0xFFFFFFFF:
            raise c.WinError(c.get_last_error())
        return state == 258
    finally:
        api.CloseHandle(handle)


def create_private_file(path: Path) -> int:
    if os.name != "nt":
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            path.unlink(missing_ok=True)
            raise
        return fd
    import msvcrt

    api = _api()
    sid = _user_sid()
    descriptor = _descriptor(sid)
    attributes = _SecurityAttributes(c.sizeof(_SecurityAttributes), descriptor, False)
    try:
        handle = api.CreateFileW(
            _windows_path(path), 0x40020000, 0, c.byref(attributes), 1, 0x80, None
        )
        last_error = c.get_last_error()
    finally:
        api.LocalFree(descriptor)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(last_error)
    fd = None
    try:
        actual = c.c_void_p()
        _status(
            api.GetSecurityInfo(handle, 1, 4, None, None, None, None, c.byref(actual))
        )
        try:
            if not _private_descriptor(actual, sid):
                raise PermissionError(
                    "Filesystem did not enforce private file permissions"
                )
        finally:
            api.LocalFree(actual)
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        os.set_inheritable(fd, False)
        return fd
    except BaseException:
        if fd is not None:
            os.close(fd)
        else:
            api.CloseHandle(handle)
        path.unlink(missing_ok=True)
        raise


def is_private_file(path: Path) -> bool:
    if os.name != "nt":
        return not (stat.S_IMODE(path.stat().st_mode) & 0o077)
    api = _api()
    descriptor = c.c_void_p()
    _status(
        api.GetNamedSecurityInfoW(
            _windows_path(path), 1, 4, None, None, None, None, c.byref(descriptor)
        )
    )
    try:
        return _private_descriptor(descriptor, _user_sid())
    finally:
        api.LocalFree(descriptor)


def protect_private_path(path: Path, *, directory=False):
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    api = _api()
    descriptor = _descriptor(_user_sid(), directory=directory)
    try:
        _status(
            api.SetNamedSecurityInfoW(
                _windows_path(path), 1, 0x80000004, None, None, _dacl(descriptor), None
            )
        )
    finally:
        api.LocalFree(descriptor)
    actual = c.c_void_p()
    _status(
        api.GetNamedSecurityInfoW(
            _windows_path(path), 1, 4, None, None, None, None, c.byref(actual)
        )
    )
    try:
        if not _private_descriptor(actual, _user_sid(), directory=directory):
            raise PermissionError("Filesystem did not enforce private path permissions")
    finally:
        api.LocalFree(actual)


def windows_file_lock(handle, *, blocking: bool) -> bool:
    import msvcrt

    result = _api().LockFileEx(
        msvcrt.get_osfhandle(handle.fileno()),
        2 if blocking else 3,
        0,
        1,
        0,
        c.byref(_Overlapped()),
    )
    if not result:
        if not blocking and c.get_last_error() == 33:
            return False
        raise c.WinError(c.get_last_error())
    return True


def windows_file_unlock(handle):
    import msvcrt

    _check(
        _api().UnlockFileEx(
            msvcrt.get_osfhandle(handle.fileno()), 0, 1, 0, c.byref(_Overlapped())
        )
    )


def windows_extended_path(value: str) -> str:
    """Use Win32 extended paths without changing the machine's registry policy."""
    value = ntpath.normpath(value)
    if value.startswith('\\\\?\\'):
        return value
    if not ntpath.isabs(value) or not ntpath.splitdrive(value)[0]:
        raise ValueError("An absolute Windows path is required.")
    if value.startswith('\\\\'):
        return '\\\\?\\UNC\\' + value[2:]
    return '\\\\?\\' + value


def io_path(path: Path) -> Path:
    """The spelling used for file operations: extended on Windows."""
    return Path(windows_extended_path(str(path.absolute()))) if os.name == "nt" else path
