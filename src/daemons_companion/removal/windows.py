"""Windows-only removal primitives: user tasks and handle-bound tree deletion.

No task is selected by a display name and no external process is killed.
A locked file leaves resumable cleanup instead of widening permissions.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
import base64
import json
from ctypes import wintypes
import os
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from daemons_companion.runtime.services import (
    WindowsTaskSchedulerBackend,
    _powershell_command,
    _windows_task_name,
    _windows_task_identity,
)
from daemons_companion.runtime.bootstrap import _windows_extended_path
from daemons_companion.removal.errors import Refused


def logical(path):
    value = str(path)
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value)


class WindowsRemovalScheduler(WindowsTaskSchedulerBackend):
    """Wait for Stop before deleting a registration; keep install behavior intact."""

    def _stop(self, job_code):
        result = self._run(
            _powershell_command(
                self._LOOKUP
                + "if($null -ne $task){if($task.State -in @(2,4)){$task.Stop(0)};"
                "$end=[DateTime]::UtcNow.AddSeconds(10);"
                "while($task.GetInstances(0).Count -gt 0){"
                "if([DateTime]::UtcNow -gt $end){throw 'Task still running'};"
                "Start-Sleep -Milliseconds 100}}",
                self._TASK_PATH,
                _windows_task_name(job_code, self.job_namespace),
            )
        )
        if result.returncode:
            raise RuntimeError("Task did not finish stopping")

    def require_exclusive(self, attempt, memory, codes):
        # Match XML text after decoding it: spaces, accents and XML escaping do
        # not affect dependency detection. The UI also checks discovered hosts.
        result = self._run(
            _powershell_command(
                "try{$diagnosticStage='scheduler_connect';" + self._CONNECT
                + "$diagnosticStage='decode_arguments';$inputData=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($args[0])) | ConvertFrom-Json);"
                "$roots=@($inputData.attempt,$inputData.memory);$allowed=@($inputData.codes);"
                "$pending=[Collections.Generic.Queue[object]]::new();"
                "$pending.Enqueue($scheduler.GetFolder('\\'));$count=0;"
                "while($pending.Count){$f=$pending.Dequeue();$diagnosticStage='folder_scan';"
                "try{$tasks=$f.GetTasks(1);$folders=$f.GetFolders(0)}catch{"
                "if($_.Exception.GetBaseException().HResult -eq -2147024891 -and "
                "$f.Path -ne '\\DaemonHarness' -and $f.Path -ne '\\'){continue};throw};"
                "foreach($t in $tasks){$count++;if($count -gt 10000){throw 'Task limit'};"
                "$diagnosticStage='task_xml';$document=[xml]$t.Xml;$text=$document.DocumentElement.InnerText;"
                "foreach($root in $roots){if($text.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0 "
                "-and ($f.Path -ne '\\DaemonHarness' -or $t.Name -notin $allowed)){"
                "[Console]::Error.WriteLine('DAEMON_REMOVAL_SHARED|'+$f.Path+'|'+$t.Name);exit 4}}};"
                "foreach($child in $folders){$pending.Enqueue($child)}}"
                "}catch{[Console]::Error.WriteLine('DAEMON_REMOVAL|'+$diagnosticStage+'|'"
                "+$_.Exception.GetBaseException().HResult);exit 1}",
                base64.b64encode(
                    json.dumps(
                        {"attempt": str(attempt), "memory": str(memory), "codes": codes}
                    ).encode()
                ).decode(),
            )
        )
        if result.returncode:
            from daemons_companion.removal.errors import Refused
            diagnostic = {"stage": "dependency_scan", "exit_code": result.returncode}
            match = re.search(
                r"DAEMON_REMOVAL\|(scheduler_connect|decode_arguments|folder_scan|task_xml)\|(-?[0-9]{1,11})(?:\r?\n|$)",
                result.stderr,
            )
            if match:
                diagnostic.update(stage=match[1], hresult=int(match[2]))
            blocking = re.search(r"DAEMON_REMOVAL_SHARED\|([^\r\n|]{1,200})\|([^\r\n|]{1,200})", result.stderr)
            if blocking:
                diagnostic["blocking_task"] = task_reference(blocking[1].rstrip("\\") + "\\" + blocking[2])
            raise Refused("shared" if result.returncode == 4 else "services", diagnostic)

    def require_stopped(self, attempt):
        # Wait for normal child shutdown, without terminating unmanaged programs.
        result = self._run(
            _powershell_command(
                "$ErrorActionPreference='Stop';$root=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($args[0]));"
                "$end=[DateTime]::UtcNow.AddSeconds(10);"
                "do{$busy=$false;foreach($p in Get-CimInstance Win32_Process){"
                "if($p.ProcessId -eq $PID){continue};"
                "foreach($s in @($p.ExecutablePath,$p.CommandLine)){"
                "if($s -and $s.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0){$busy=$true}}};"
                "if(!$busy){exit 0};Start-Sleep -Milliseconds 200"
                "}while([DateTime]::UtcNow -lt $end);exit 4",
                base64.b64encode(str(attempt).encode()).decode(),
            )
        )
        if result.returncode:
            raise ValueError("services")


def task_reference(value):
    """A scheduled task's folder and name, kept only when made of plain characters."""
    value = str(value)
    return value if re.fullmatch(r"[A-Za-z0-9 ._\\()-]{1,200}", value) else "unrecognized"


def validate_task(raw, attempt, sid):
    from daemons_companion.removal.errors import Refused

    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    root = ET.fromstring(raw)
    principals = root.findall("t:Principals/t:Principal", ns)
    actions = root.findall("t:Actions/*", ns)
    if (
        len(principals) != 1
        or len(actions) != 1
        or actions[0].tag != "{" + ns["t"] + "}Exec"
    ):
        raise Refused("services", {"stage": "task_shape"})
    principal, action = principals[0], actions[0]

    def value(node, key):
        return node.findtext("t:" + key, "", ns)

    if _windows_task_identity(value(principal, "UserId")) != sid or value(principal, "GroupId"):
        raise Refused("services", {"stage": "task_owner"})
    if (value(principal, "LogonType") != "InteractiveToken"
            or value(principal, "RunLevel") not in ("", "LeastPrivilege", "0")):
        raise Refused("services", {"stage": "task_privileges"})
    for key in ("Command", "WorkingDirectory"):
        path = logical(value(action, key))
        if (
            not path.is_absolute()
            or not path.is_relative_to(attempt)
            or not path.resolve().is_relative_to(attempt)
        ):
            raise Refused("services", {"stage": "task_command" if key == "Command" else "task_directory"})


class _Info(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", wintypes.FILETIME),
        ("access", wintypes.FILETIME),
        ("write", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class _Basic(ctypes.Structure):
    _fields_ = [
        ("creation", ctypes.c_longlong),
        ("access", ctypes.c_longlong),
        ("write", ctypes.c_longlong),
        ("change", ctypes.c_longlong),
        ("attributes", wintypes.DWORD),
    ]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_uint64), ("file_id", ctypes.c_ubyte * 16)]


def _native_failure(stage):
    # Never include OS messages or paths in an exported diagnostic.
    return Refused("recovery", {"stage": stage, "winerror": ctypes.get_last_error()})


class _WinFiles:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows handles require Windows")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        self.api.CreateFileW.restype = wintypes.HANDLE
        self.api.GetFileInformationByHandle.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_Info),
        )
        self.api.GetFileInformationByHandle.restype = wintypes.BOOL
        self.api.GetFileInformationByHandleEx.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        )
        self.api.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.api.SetFileInformationByHandle.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        self.api.SetFileInformationByHandle.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.api.CloseHandle.restype = wintypes.BOOL

    @contextmanager
    def opened(self, path, deleting=False):
        # Deny concurrent writes/renames to each directory being traversed.
        # OPEN_REPARSE_POINT opens a junction/link itself, never its destination.
        handle = self.api.CreateFileW(
            _windows_extended_path(str(path)),
            0x80 | (0x10000 | 0x100 if deleting else 0),
            1,
            None,
            3,
            0x02200000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise _native_failure("file_open")
        try:
            info = _Info()
            if not self.api.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise _native_failure("file_identity")
            yield handle, info
        finally:
            self.api.CloseHandle(handle)

    def identity(self, handle, info):
        # Match Python 3.12+ stat without truncating volume or file identifiers.
        modern = _FileIdInfo()
        if self.api.GetFileInformationByHandleEx(
            handle, 18, ctypes.byref(modern), ctypes.sizeof(modern)
        ):
            return [modern.volume, int.from_bytes(bytes(modern.file_id), "little")]
        # FileIdInfo is unavailable on some filesystems. No fallback for access
        # errors: an uncertain identity must never authorize deletion.
        if ctypes.get_last_error() not in (1, 50, 87):
            raise _native_failure("file_identity")
        return [info.volume, (info.index_high << 32) | info.index_low]

    def remove(self, handle, info):
        if info.attributes & 1:  # read-only vendor files, only on this open handle
            basic = _Basic(attributes=(info.attributes & ~1) or 0x80)
            if not self.api.SetFileInformationByHandle(
                handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)
            ):
                raise _native_failure("file_attributes")
        delete = ctypes.c_ubyte(1)
        if not self.api.SetFileInformationByHandle(
            handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)
        ):
            raise _native_failure("file_delete")


def remove_tree(path, expected):
    """Delete only opened objects, with ancestors locked against substitution.

    Reparse points inside the owned tree are unlinked without visiting them.
    The reviewed root itself must still be the same ordinary directory.
    """
    path = logical(path)
    api = _WinFiles()
    with ExitStack() as stack:
        for parent in reversed(path.parents):
            _, info = stack.enter_context(api.opened(parent))
            if info.attributes & 0x400:
                raise Refused("unsafe", {"stage": "file_ancestor"})
        visited = 0

        def walk(current, root=False):
            nonlocal visited
            visited += 1
            if visited > 300000:
                raise Refused("unsafe", {"stage": "file_limit"})
            with api.opened(current, deleting=True) as (handle, info):
                if root and (
                    info.attributes & 0x400
                    or not info.attributes & 0x10
                    or api.identity(handle, info) != expected
                ):
                    raise Refused("changed", {"stage": "program_identity"})
                if info.attributes & 0x10 and not info.attributes & 0x400:
                    with os.scandir(_windows_extended_path(str(current))) as entries:
                        names = [entry.name for entry in entries]
                    for name in names:
                        walk(current / name)
                api.remove(handle, info)

        walk(path, root=True)
