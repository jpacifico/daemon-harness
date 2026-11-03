"""Native credential-store boundaries for companion provider secrets."""

from __future__ import annotations

import base64
import ctypes
import os
import sys
import subprocess
from typing import Protocol


KEYCHAIN_SERVICE = "com.daemon-harness.provider"
WINDOWS_TARGET_PREFIX = "DaemonHarness/provider/"


class CredentialStore(Protocol):
    def get(self, reference: str) -> str | None: ...

    def put(self, reference: str, protected_value: str) -> None: ...

    def delete(self, reference: str) -> None: ...


class MacOSKeychainStore:
    """One signed OS accessor across installer, gateway and worker interpreters.

    ``security -w`` alone does NOT read a piped password. Its interactive command
    mode does. Encode the protected envelope to one delimiter-free token so its
    contents can never add another command. Never invoke a shell or pass the
    value in argv. Trust only /usr/bin/security for this newly owned entry.
    """

    _PREFIX = "daemon-keychain-b64-v1:"

    def __init__(self, *, runner=subprocess.run) -> None:
        self._runner = runner

    def _security(self, arguments, **kwargs):
        # security resolves the user's keychain search list from HOME. Private
        # runtimes deliberately use another HOME; scope the OS home to this
        # signed accessor only, using the OS account database rather than input.
        environment = dict(os.environ)
        if sys.platform == "darwin":
            import pwd
            environment["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
        return self._runner(arguments, env=environment, **kwargs)

    def get(self, reference: str) -> str | None:
        _validate_reference(reference)
        result = self._security(['/usr/bin/security', 'find-generic-password', '-a', reference,
                               '-s', KEYCHAIN_SERVICE, '-w'], check=False, capture_output=True,
                              text=True, timeout=30)
        if result.returncode == 44:
            return None
        if result.returncode:
            raise RuntimeError("macOS Keychain could not read the provider credential.")
        value = result.stdout.rstrip('\n')
        if value.startswith(self._PREFIX):
            try:
                return base64.b64decode(value[len(self._PREFIX):], validate=True).decode('utf-8')
            except (ValueError, UnicodeError):
                raise RuntimeError("macOS Keychain returned an invalid provider credential.") from None
        return value  # retain reads of prior, unencoded owned envelopes

    def put(self, reference: str, protected_value: str) -> None:
        _validate_reference(reference)
        _validate_protected_value(protected_value)
        encoded = self._PREFIX + base64.b64encode(protected_value.encode('utf-8')).decode('ascii')
        command = ('add-generic-password -U -a ' + reference + ' -s ' + KEYCHAIN_SERVICE
                   + ' -T /usr/bin/security -w ' + encoded + '\n')
        result = self._security(['/usr/bin/security', '-i'], input=command, check=False,
                              capture_output=True, text=True, timeout=30)
        if result.returncode or self.get(reference) != protected_value:
            raise RuntimeError("macOS Keychain could not store the provider credential.")

    def delete(self, reference: str) -> None:
        _validate_reference(reference)
        result = self._security(['/usr/bin/security', 'delete-generic-password', '-a', reference,
                               '-s', KEYCHAIN_SERVICE], check=False, capture_output=True,
                              text=True, timeout=30)
        if result.returncode not in (0, 44):
            raise RuntimeError("macOS Keychain could not delete the provider credential.")


class WindowsCredentialStore:
    """Use Windows Credential Manager through the native WinCred API."""

    def __init__(self, *, api: "_WindowsCredentialApi | None" = None) -> None:
        self._api = api or _WindowsCredentialApi()

    def get(self, reference: str) -> str | None:
        _validate_reference(reference)
        raw = self._api.read(WINDOWS_TARGET_PREFIX + reference)
        return None if raw is None else raw.decode("utf-8")

    def put(self, reference: str, protected_value: str) -> None:
        _validate_reference(reference)
        _validate_protected_value(protected_value)
        self._api.write(
            WINDOWS_TARGET_PREFIX + reference,
            protected_value.encode("utf-8"),
        )

    def delete(self, reference: str) -> None:
        _validate_reference(reference)
        self._api.delete(WINDOWS_TARGET_PREFIX + reference)


def system_credential_store() -> CredentialStore:
    if sys.platform == "darwin":
        return MacOSKeychainStore()
    if os.name == "nt":
        return WindowsCredentialStore()
    raise RuntimeError("The companion credential store supports only macOS and Windows.")


class _WindowsCredentialApi:
    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows Credential Manager is unavailable on this platform.")
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self._credential_type = CREDENTIALW
        self._pointer_type = ctypes.POINTER(CREDENTIALW)
        self._advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi.CredWriteW.argtypes = [self._pointer_type, wintypes.DWORD]
        self._advapi.CredWriteW.restype = wintypes.BOOL
        self._advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(self._pointer_type),
        ]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None

    def write(self, target: str, value: bytes) -> None:
        blob = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        credential = self._credential_type()
        credential.Type = self._CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(value)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self._CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "Daemon Harness"
        if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def read(self, target: str) -> bytes | None:
        pointer = self._pointer_type()
        if not self._advapi.CredReadW(
            target,
            self._CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            error = ctypes.get_last_error()
            if error == self._ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            credential = pointer.contents
            return ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
        finally:
            self._advapi.CredFree(pointer)

    def delete(self, target: str) -> None:
        if self._advapi.CredDeleteW(target, self._CRED_TYPE_GENERIC, 0):
            return
        error = ctypes.get_last_error()
        if error != self._ERROR_NOT_FOUND:
            raise ctypes.WinError(error)


def _validate_reference(reference: str) -> None:
    if (
        not isinstance(reference, str)
        or not reference.startswith("provider-")
        or len(reference) != 45
        or any(character not in "0123456789abcdef-" for character in reference[9:])
    ):
        raise ValueError("Credential reference is invalid.")


def _validate_protected_value(value: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2_048:
        raise ValueError("Protected credential payload is invalid.")


__all__ = [
    "CredentialStore",
    "KEYCHAIN_SERVICE",
    "MacOSKeychainStore",
    "WINDOWS_TARGET_PREFIX",
    "WindowsCredentialStore",
    "system_credential_store",
]
