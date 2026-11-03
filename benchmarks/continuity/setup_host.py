"""Prepare a Continuity Benchmark home: Companion's pinned Hermes, the official Codex CLI, two sign-ins.

Everything lands under CONTINUITY_HOME (default ~/continuity-bench);
no existing host, profile, login or service is read or changed. The two ChatGPT
sign-ins are made by the person running the benchmark, in their browser.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import uuid

from benchmarks.continuity.run import BENCH_HOME, STORE
from daemons_companion.runtime.host_install import HostInstallLayout, execute_host_install, host_install_plan
from daemons_companion.setup import codex_install
from daemons_companion.setup.transaction import public_plan_digest
from scripts.fetch_companion_uv import fetch


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_hermes(uv: Path) -> None:
    home = (BENCH_HOME / "companion").resolve()
    if (home / "hosts/hermes/current.json").exists():
        return
    home.mkdir(parents=True, exist_ok=True)
    layout = HostInstallLayout(home, uv.resolve(), sha256(uv), uuid.uuid4().hex)
    execute_host_install(layout, confirmed_digest=public_plan_digest(host_install_plan(layout)),
                         progress=lambda message: print(message, flush=True))


def install_codex() -> Path:
    """The official package Companion pins, verified against Companion's digest."""
    destination = BENCH_HOME / "codex" / codex_install.VERSION
    binary = destination / "bin/codex"
    if binary.is_file():
        return binary
    triple, size, digest = codex_install.PACKAGES["macos"]
    url = (f"https://github.com/openai/codex/releases/download/rust-v{codex_install.VERSION}/"
           f"codex-package-{triple}.tar.gz")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as raw:
        archive = Path(raw) / "codex.tar.gz"
        codex_install.download_package(url, archive, size, digest)
        package = Path(raw) / "package"
        package.mkdir()
        files = codex_install.extract_package(archive, package)
        (package / "daemons-package.json").write_text(json.dumps(
            {"version": codex_install.VERSION, "target_os": "macos", "files": files}, sort_keys=True))
        package.rename(destination)
    return binary


def main() -> int:
    uv = BENCH_HOME / "uv/uv"
    if not uv.is_file():
        fetch("macos", uv)
    install_hermes(uv)
    codex = install_codex()
    for name in ("hermes", "codex", "home"):
        (STORE / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    hermes = sorted((BENCH_HOME / "companion/hosts/hermes/installations").glob(
        "*/source/hermes-agent-*/.venv/bin/hermes"))[-1]
    print(json.dumps({"hermes": str(hermes), "codex": str(codex), "codex_sha256": sha256(codex)}, indent=2))
    print("\nTwo ChatGPT sign-ins, both made for this benchmark only:")
    print(f"  CODEX_HOME={STORE / 'codex'} {codex} -c 'cli_auth_credentials_store=\"file\"' "
          "-c 'forced_login_method=\"chatgpt\"' login")
    print(f"  HOME={STORE / 'home'} HERMES_HOME={STORE / 'hermes'} {hermes} auth add openai-codex --no-browser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
