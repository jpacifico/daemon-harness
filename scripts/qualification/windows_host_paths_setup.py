"""Disposable VM only: run realistic private setup with Windows long paths off."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import winreg

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--uv", required=True)
parser.add_argument("--baseline", required=True)
args = parser.parse_args()
report = {"registry_change": "disposable CI VM only", "restored": False}
key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem", 0,
                     winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE)
previous, kind = winreg.QueryValueEx(key, "LongPathsEnabled")
try:
    winreg.SetValueEx(key, "LongPathsEnabled", 0, winreg.REG_DWORD, 0)
    report["tested"] = winreg.QueryValueEx(key, "LongPathsEnabled")[0]
    # Fresh interpreter: no cached long-path policy inherited from the parent.
    result = subprocess.run([sys.executable, "-S", str(Path(__file__).with_name("companion_windows_setup_probe.py")),
                             "--uv", args.uv, "--baseline", args.baseline,
                             "--host-paths", "--output", "qualification/windows-host-setup.json"], timeout=1200)
    report["exit"] = result.returncode
finally:
    winreg.SetValueEx(key, "LongPathsEnabled", 0, kind, previous)
    report["restored"] = winreg.QueryValueEx(key, "LongPathsEnabled")[0] == previous
    winreg.CloseKey(key)
    Path("qualification").mkdir(exist_ok=True)
    Path("qualification/windows-host-policy.json").write_text(json.dumps(report, indent=2))
raise SystemExit(0 if report.get("exit") == 0 and report["restored"] else 1)
