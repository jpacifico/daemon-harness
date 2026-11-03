"""Diagnostic only: exercise V0 OS boundaries against disposable state/children."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def process_probe(module_name):
    print(json.dumps({"phase": "import", "module": module_name}), flush=True)
    check = importlib.import_module(module_name)._pid_alive
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.readline(); print('alive', flush=True)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        print(json.dumps({"phase": "before_probe", "module": module_name, "parent_pid": os.getpid(), "owned_child_pid": child.pid}), flush=True)
        reported_alive = check(child.pid)
        print(json.dumps({"phase": "after_probe", "module": module_name, "reported_alive": reported_alive}), flush=True)
        output, _ = child.communicate(input="finish\n", timeout=5)
        return {
            "reported_alive": reported_alive,
            "child_survived_probe": "alive" in output.splitlines(),
            "child_exit": child.returncode,
            "reported_alive_after_exit": check(child.pid),
        }
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Run only in the dedicated disposable GitHub qualification job.")
    with tempfile.TemporaryDirectory(prefix="daemon-windows-probe-") as temporary:
        os.environ["DAEMON_CORE_MEMORY_ROOT"] = str(Path(temporary) / "memory")
        results = {}
        for name in ("daemon_core.after_turn_spool", "daemon_core.registre"):
            try:
                results[name] = process_probe(name)
            except Exception as exc:
                results[name] = {"error": type(exc).__name__}
            print(json.dumps({"module": name, "result": results[name]}), flush=True)
        print(json.dumps(results, indent=2))
        return 0 if all(
            row.get("child_survived_probe") and row.get("reported_alive")
            and row.get("reported_alive_after_exit") is False and row.get("child_exit") == 0
            for row in results.values()
        ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
