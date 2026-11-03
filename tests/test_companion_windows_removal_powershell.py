"""Execute the actual dependency scan with synthetic scheduler objects.

Runs on Windows PowerShell or portable PowerShell. COM and Windows file deletion
are not exercised by this fixture; all other scan statements are unchanged.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from daemons_companion.runtime.services import SchedulerCommandResult
from daemons_companion.removal.windows import WindowsRemovalScheduler
from daemons_companion.removal.errors import Refused

PWSH = os.environ.get("DAEMON_TEST_POWERSHELL") or shutil.which("powershell.exe") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not PWSH, reason="PowerShell executable unavailable")

FIXTURE = r'''
$ErrorActionPreference='Stop';
$fixtureJson=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__FIXTURE__'));
$fixture=ConvertFrom-Json $fixtureJson;
$folder=[pscustomobject]@{Path=$fixture.folder;Tasks=@($fixture.tasks)};
$folder|Add-Member ScriptMethod GetTasks {param($flags) return ,$this.Tasks};
$folder|Add-Member ScriptMethod GetFolders {param($flags) return ,@()};
$scheduler=[pscustomobject]@{Folder=$folder};
$scheduler|Add-Member ScriptMethod GetFolder {param($path) return $this.Folder};
'''


def scan(tasks, folder="\\", legacy=False):
    fixture = base64.b64encode(json.dumps({"folder": folder, "tasks": tasks}).encode()).decode()
    results = []

    def runner(command, environment):
        script = command[-2].replace(WindowsRemovalScheduler._CONNECT, FIXTURE.replace("__FIXTURE__", fixture))
        assert "Schedule.Service" not in script
        assert all(operation not in script for operation in ("DeleteTask", ".Stop(", "RegisterTask"))
        if legacy:
            script = script.replace("$document=[xml]$t.Xml;$text=$document.DocumentElement.InnerText;",
                                    "$text=([xml]$t.Xml).InnerText;")
        result = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script, command[-1]],
                                capture_output=True, text=True, timeout=30)
        results.append(result)
        return SchedulerCommandResult(result.returncode, result.stdout, result.stderr)

    scheduler = WindowsRemovalScheduler(runner=runner, job_namespace="a" * 32)
    scheduler.require_exclusive(Path("C:/private & é/host"), Path("C:/private & é/memory"), ["owned"])
    return results


def task(text, name="unrelated"):
    if os.name == "nt":
        text = text.replace("/", "\\")
    return {"Name": name, "Xml": '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec><Command>' + text + '</Command></Exec></Actions></Task>'}


def test_legacy_scan_reproduces_reported_null_error():
    with pytest.raises(Refused) as caught:
        scan([task("C:/Windows/normal.exe")], legacy=True)
    assert caught.value.code == "services"
    assert caught.value.diagnostic == {"stage": "task_xml", "exit_code": 1, "hresult": -2146233087}


@pytest.mark.parametrize("tasks", [[], [task("C:/Windows/normal.exe")], [task("")]])
def test_valid_unrelated_tasks_do_not_block(tasks):
    assert scan(tasks)[0].returncode == 0


@pytest.mark.parametrize("root", ["host", "memory"])
@pytest.mark.parametrize("folder,name", [("\\", "other"), ("\\DaemonHarness", "other"), ("\\Elsewhere", "owned")])
def test_external_dependencies_still_block(root, folder, name):
    with pytest.raises(Refused, match="shared"):
        scan([task("C:/PRIVATE &amp; é/" + root + "/worker.exe", name)], folder)


def test_exact_owned_task_is_allowed():
    assert scan([task("C:/private &amp; é/host/worker.exe", "owned")], "\\DaemonHarness")[0].returncode == 0


def test_malformed_xml_is_not_silently_ignored():
    with pytest.raises(Refused, match="services") as caught:
        scan([{"Name": "broken", "Xml": "<Task>"}])
    assert caught.value.diagnostic["stage"] == "task_xml"
