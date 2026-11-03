#!/usr/bin/env python3
"""Opt-in fresh-host qualification; never an installer for a personal profile.

Only the verified uv executable is supplied. Host source, interpreter, packages
and profiles are new. The existing end-to-end harness owns gateway/worker tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from daemons_companion.runtime.bootstrap import clean_environment, extract_source, run, source_unchanged
from daemons_companion.runtime.acquisition import (
    DISPOSABLE_MARKER,
    DISPOSABLE_MARKER_CONTENT,
    _RELEASES,
    stage_disposable_host_artifact,
)




WEB_PROBE = '''
import asyncio, json
from tools import web_tools
web_tools._ensure_web_plugins_loaded()
search_backend = web_tools._get_search_backend()
extract_backend = web_tools._get_extract_backend()
search = json.loads(web_tools.web_search_tool("Python programming language official documentation", limit=2))
extract = json.loads(asyncio.run(web_tools.web_extract_tool(["https://example.com"], char_limit=2000)))
items = search.get("data", {}).get("web", [])
pages = extract.get("results", [])
search_ok = search.get("success") is True and any(item.get("url") for item in items)
extract_ok = any("Example Domain" in str(page.get("content", "")) for page in pages)
print(json.dumps({"search_backend": search_backend, "extract_backend": extract_backend,
    "search_ok": search_ok, "extract_ok": extract_ok,
    "search_diagnostic": None if search_ok else search,
    "extract_diagnostic": None if extract_ok else extract}))
raise SystemExit(0 if search_ok and extract_ok else 2)
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--uv-sha256", required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise RuntimeError("This foreground qualification is macOS-only; Windows is not yet qualified.")
    uv = args.uv.absolute()
    if uv.is_symlink() or not uv.is_file() or hashlib.sha256(uv.read_bytes()).hexdigest() != args.uv_sha256:
        raise ValueError("An independently verified direct uv executable is required.")
    repository = Path(__file__).resolve().parents[1]
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=repository, text=True):
        raise RuntimeError("Qualification requires a clean committed candidate.")
    started = time.monotonic()
    result = {"schema": "daemon.companion-fresh-hermes.v1", "platform": sys.platform}
    with tempfile.TemporaryDirectory(prefix="daemon-companion-fresh-hermes-") as raw:
        root = Path(raw).resolve()
        (root / "temp").mkdir()
        (root / "probe-profile").mkdir()
        environment = clean_environment(root)
        version = run([str(uv), "--version"], environment)
        if version.split()[:2] != ["uv", "0.12.16"]:
            raise ValueError("uv 0.12.16 is required.")
        release = _RELEASES["hermes"]
        (root / DISPOSABLE_MARKER).write_bytes(DISPOSABLE_MARKER_CONTENT)
        archive = stage_disposable_host_artifact(release.artifact_id, root)
        sources = root / "source"
        prefix = "hermes-agent-" + release.revision
        hashes = extract_source(archive, sources, prefix)
        host = sources / prefix
        print(json.dumps({"phase": "verified_source", "files": len(hashes)}), flush=True)
        run([str(uv), "python", "install", "3.12", "--managed-python", "--no-bin", "--no-registry", "--no-config", "--no-progress"], environment)
        run([str(uv), "sync", "--project", str(host), "--frozen", "--no-dev", "--extra", "messaging", "--python", "3.12", "--managed-python", "--no-python-downloads", "--no-config", "--no-progress"], environment)
        binary_dir = host / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        python = binary_dir / ("python.exe" if os.name == "nt" else "python")
        hermes = binary_dir / ("hermes.exe" if os.name == "nt" else "hermes")
        run([str(uv), "pip", "check", "--python", str(python), "--no-config"], environment)
        result["host_version"] = run([str(hermes), "--version"], environment)
        if "0.21.5" not in result["host_version"]:
            raise RuntimeError("Fresh host version mismatch.")
        result["bootstrap_seconds"] = round(time.monotonic() - started, 3)
        print(json.dumps({"phase": "fresh_host_ready", **result}), flush=True)
        result["web_probe"] = run([str(python), "-c", WEB_PROBE], environment, timeout=90)
        print(json.dumps({"phase": "native_web", "result": result["web_probe"]}), flush=True)
        # Reuse the existing transaction, real tool-loop and restart proof.
        qualification_env = dict(environment)
        qualification_env["PYTHONPATH"] = str(repository) + os.pathsep + str(repository / "src")
        result["daemon_e2e"] = run([
            sys.executable, str(repository / "scripts/qualify_companion_hermes_e2e.py"),
            "--hermes", str(hermes), "--uv", str(uv), "--uv-sha256", args.uv_sha256,
            "--exercise-souvenir-read",
        ], qualification_env, timeout=600)
        run([str(uv), "pip", "check", "--python", str(python), "--no-config"], environment)
        if not source_unchanged(sources, hashes):
            raise RuntimeError("Upstream source files changed.")
        result["upstream_files_unchanged"] = True
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
