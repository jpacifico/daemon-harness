"""Read-only comparison of Windows scheduler process environments on disposable CI."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from daemons_companion.runtime.services import _scheduler_environment

system_environment = _scheduler_environment()
base = {k: v for k, v in system_environment.items() if k.upper() != "PSMODULEPATH"}
groups = {
    "filtered": (),
    "system_modules": (),
    "directories": (
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "PUBLIC",
        "ALLUSERSPROFILE",
        "SYSTEMDRIVE",
    ),
    "identity": (
        "COMPUTERNAME",
        "USERNAME",
        "USERDOMAIN",
        "USERDNSDOMAIN",
        "LOGONSERVER",
        "HOMEDRIVE",
        "HOMEPATH",
    ),
    "modules": ("PSMODULEPATH",),
    "ci_session": tuple(os.environ),
}
command = [
    "powershell.exe",
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    "$ErrorActionPreference='Stop';$s=New-Object -ComObject Schedule.Service;$s.Connect();$null=$s.GetFolder('\\');$env:PSModulePath",
]


def probe(item):
    name, keys = item
    env = {**base, **{k: v for k, v in os.environ.items() if k.upper() in keys}}
    if name == "system_modules":
        env = system_environment
    stamp = time.monotonic()
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=120
    )
    return {
        "environment": name,
        "seconds": round(time.monotonic() - stamp, 3),
        "exit": result.returncode,
        "stdout": result.stdout[-100:],
        "stderr": result.stderr[-3000:],
    }


with ThreadPoolExecutor(max_workers=5) as pool:
    results = list(pool.map(probe, groups.items()))
Path("qualification").mkdir(exist_ok=True)
Path("qualification/scheduler-environment.json").write_text(
    json.dumps(results, indent=2)
)
print(json.dumps(results, indent=2))
assert all(item["exit"] == 0 for item in results)
