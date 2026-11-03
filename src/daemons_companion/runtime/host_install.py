"""Execute a confirmed fresh Hermes host acquisition through the transaction engine.

This operation installs only the host. It neither configures a model/channel nor
starts a gateway. All paths originate in the verified package context.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable

from daemons_companion.setup.plan import HostKind, HostProbe, PlanStep, SetupPlan
from daemons_companion.runtime.acquisition import (
    DISPOSABLE_MARKER, DISPOSABLE_MARKER_CONTENT, _RELEASES,
    recognized_version, stage_disposable_host_artifact,
)
from daemons_companion.runtime.bootstrap import clean_environment, extract_source, private_home_environment, run, source_unchanged, _source_io_path
from daemons_companion.runtime.environment import check_windows_installation_room
from daemons_companion.setup.transaction import TransactionEngine, public_plan_digest
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock

STEP = "acquire-hermes-host"
HERMES_EXTRAS = ("messaging", "bedrock", "edge-tts", "anthropic")
SCHEMA = "daemon.companion-host-install.v1"


@dataclass(frozen=True)
class HostInstallLayout:
    companion_home: Path
    uv_executable: Path
    uv_sha256: str
    installation_id: str
    target_os: str = "macos"

    @property
    def root(self) -> Path:
        return self.companion_home / "hosts" / "hermes"

    @property
    def attempt(self) -> Path:
        return self.root / "installations" / self.installation_id

    @property
    def pointer(self) -> Path:
        return self.root / "current.json"

    @property
    def host(self) -> Path:
        return self.attempt / "source" / ("hermes-agent-" + _RELEASES["hermes"].revision)

    @property
    def executable(self) -> Path:
        return self.host / ".venv" / ("Scripts/hermes.exe" if self.target_os == "windows" else "bin/hermes")

    @property
    def python(self) -> Path:
        return self.host / ".venv" / ("Scripts/python.exe" if self.target_os == "windows" else "bin/python")

    @property
    def launch_python(self) -> str:
        # CPython derives sys.prefix/sys.path from its executable. Prefixing the
        # owned interpreter also protects imports and child pip under MAX_PATH.
        # Logical paths and upstream files remain unchanged.
        return str(_source_io_path(self.python)) if self.target_os == "windows" else str(self.python)

    @property
    def profile(self) -> Path:
        return self.attempt / "profile"

    @property
    def transactions(self) -> Path:
        return self.root / "transactions"

    def validate(self) -> None:
        if self.target_os not in {"macos", "windows"}:
            raise ValueError("Executable host setup supports macOS and Windows only.")
        if not isinstance(self.installation_id, str) or re.fullmatch(r"[0-9a-f]{32}", self.installation_id) is None:
            raise ValueError("Host installation id is invalid.")
        for path in (self.companion_home, self.uv_executable, self.attempt, self.pointer,
                     self.host, self.profile, self.attempt / "probe-profile",
                     self.attempt / "os-home", self.attempt / "source-manifest.json"):
            if not path.is_absolute() or path.resolve() != path or path.is_symlink():
                raise ValueError("Host setup requires direct absolute paths.")
        if not self.uv_executable.is_file() or hashlib.sha256(self.uv_executable.read_bytes()).hexdigest() != self.uv_sha256:
            raise ValueError("Host setup uv integrity check failed.")


def host_install_plan(layout: HostInstallLayout) -> SetupPlan:
    layout.validate()
    release = _RELEASES["hermes"]
    return SetupPlan(
        HostProbe(HostKind.HERMES, False, False, str(layout.executable), str(layout.profile)),
        "", "", {"kind": "not-configured"},
        (PlanStep(STEP, f"Install Hermes {release.version} in a separate private location.", (
            f"Destination: {layout.attempt}", f"Source: {release.url}",
            f"Source SHA-256: {release.sha256}", f"Source size: {release.size}",
            f"Private bootstrap: {layout.uv_executable}", f"uv SHA-256: {layout.uv_sha256}",
            f"Target OS: {layout.target_os}",
            "Python 3.12 and upstream frozen messaging dependencies",
            "No existing host, service, login or channel will be changed",
            "An interrupted download/build may leave inactive private files for inspection",
        )),), installs_host=True,
    )


class HostInstallAdapter:
    def __init__(self, layout: HostInstallLayout, *, progress: Callable[[str], None] = lambda _: None,
                 runner=run, downloader=stage_disposable_host_artifact):
        self.layout = layout
        self.progress = progress
        self.runner = runner
        self.downloader = downloader
        self.snapshot_dir: Path | None = None

    def _validate(self, step: PlanStep) -> None:
        if step != host_install_plan(self.layout).steps[0]:
            raise ValueError("Host setup plan changed after confirmation.")

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate(step)
        if self.layout.pointer.exists() or self.layout.pointer.is_symlink():
            raise ValueError("A managed host already exists. It will not be replaced.")
        if self.layout.attempt.exists() or self.layout.attempt.is_symlink():
            raise ValueError("Host installation destination is already occupied.")
        if self.layout.target_os == "windows" and os.name == "nt":
            check_windows_installation_room(self.layout.attempt)
        atomic_write_text(snapshot_dir / "manifest.json", json.dumps(self._pointer()) + "\n")
        self.snapshot_dir = snapshot_dir

    def _pointer(self) -> dict:
        return {"schema": SCHEMA, "installation_id": self.layout.installation_id,
                "version": _RELEASES["hermes"].version, "revision": _RELEASES["hermes"].revision}

    def apply(self, step: PlanStep) -> None:
        self._validate(step)
        if self.snapshot_dir is None:
            raise RuntimeError("Host setup needs its transaction snapshot.")
        self.layout.attempt.mkdir(parents=True, mode=0o700)
        atomic_write_text(self.layout.attempt / "owner.json", json.dumps(self._pointer()) + "\n")
        for name in ("temp", "probe-profile", "profile", "os-home"):
            (self.layout.attempt / name).mkdir(mode=0o700)
        environment = clean_environment(self.layout.attempt)
        environment.update(private_home_environment(self.layout.attempt / "os-home"))
        uv = str(self.layout.uv_executable)
        if self.runner([uv, "--version"], environment).split()[:2] != ["uv", "0.12.16"]:
            raise ValueError("Host setup requires the verified uv 0.12.16.")
        release = _RELEASES["hermes"]
        self.progress("Downloading and verifying Hermes " + release.version + "...")
        with tempfile.TemporaryDirectory(prefix="download-", dir=self.layout.attempt) as raw:
            download = Path(raw).resolve()
            (download / DISPOSABLE_MARKER).write_bytes(DISPOSABLE_MARKER_CONTENT)
            archive = self.downloader(release.artifact_id, download)
            self.progress("Extracting and verifying Hermes files...")
            hashes = extract_source(archive, self.layout.attempt / "source", "hermes-agent-" + release.revision)
        atomic_write_text(self.layout.attempt / "source-manifest.json", json.dumps(hashes, sort_keys=True) + "\n")
        self.progress("Installing private Python 3.12...")
        self.runner([uv, "python", "install", "3.12", "--managed-python", "--no-bin", "--no-registry", "--no-config", "--no-progress"], environment)
        self.progress("Installing Hermes dependencies. This may take a few minutes...")
        # Hermes installs bedrock and edge-tts on first use when they are missing: the
        # gateway start and the first Telegram reply then wait for an unlocked download
        # (two minutes observed). The locked extras install the same pins now.
        extras = [arg for extra in HERMES_EXTRAS for arg in ("--extra", extra)]
        self.runner([uv, "sync", "--project", str(self.layout.host), "--frozen", "--no-dev", *extras, "--python", "3.12", "--managed-python", "--no-python-downloads", "--no-config", "--no-progress"], environment, timeout=1800, progress=self.progress)
        if self.layout.target_os == "windows":
            from daemons_companion.runtime.uv import install_private_uv
            install_private_uv(self.layout)
        self.progress("Checking the installed host...")
        self._verify_host(environment)
        # No pointer is published before the entire host check passes.
        if self.layout.pointer.exists() or self.layout.pointer.is_symlink():
            raise RuntimeError("Managed host pointer changed concurrently.")
        with tempfile.NamedTemporaryFile(dir=self.layout.root, prefix=".pointer-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(self._pointer()) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, self.layout.pointer)
        finally:
            temporary.unlink(missing_ok=True)

    def _verify_host(self, environment: dict[str, str]) -> None:
        layout = self.layout
        hashes = json.loads((layout.attempt / "source-manifest.json").read_text())
        if not hashes or not source_unchanged(layout.attempt / "source", hashes):
            raise RuntimeError("Installed host sources differ from the verified archive.")
        python = layout.python
        if not python.is_file() or not layout.executable.is_file():
            raise RuntimeError("Hermes installation did not produce its private executables.")
        self.runner([str(layout.uv_executable), "pip", "check", "--python", str(python), "--no-config"], environment)
        self.runner([layout.launch_python, "-B", "-c",
                     "from openai import OpenAI; import telegram, pydantic_core, boto3, edge_tts, anthropic; print('dependencies ready')"],
                    environment, timeout=45)
        version = self.runner([str(layout.executable), "--version"], environment, timeout=30)
        version_line = version.splitlines()[0] if version else ""
        if recognized_version("hermes", version_line, "verified") != _RELEASES["hermes"].version:
            raise RuntimeError("Installed Hermes version differs from the confirmed release.")

    def verify(self, step: PlanStep) -> None:
        self._validate(step)
        if self.layout.pointer.is_symlink() or json.loads(self.layout.pointer.read_text()) != self._pointer():
            raise RuntimeError("Managed host pointer was not applied exactly.")
        environment = clean_environment(self.layout.attempt)
        environment.update(private_home_environment(self.layout.attempt / "os-home"))
        self._verify_host(environment)

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate(step)
        expected = json.loads((snapshot_dir / "manifest.json").read_text())
        if expected != self._pointer():
            raise RuntimeError("Host setup snapshot no longer matches its plan.")
        if self.layout.pointer.is_symlink():
            raise RuntimeError("Concurrent managed host change; rollback refused.")
        if self.layout.pointer.exists():
            if json.loads(self.layout.pointer.read_text()) != expected:
                raise RuntimeError("Concurrent managed host change; rollback refused.")
            self.layout.pointer.unlink()
        # Keep inactive downloaded files rather than deleting possible concurrent
        # user additions. No process or service is kept running here.


def execute_host_install(layout: HostInstallLayout, *, confirmed_digest: str,
                         progress=lambda _: None, adapter: HostInstallAdapter | None = None):
    plan = host_install_plan(layout)
    engine = TransactionEngine(layout.transactions)
    owner = adapter or HostInstallAdapter(layout, progress=progress)
    if confirmed_digest != public_plan_digest(plan):
        raise ValueError("Confirmed host installation differs from the reviewed plan.")
    with memory_file_lock(layout.root / "install-owner"):
        try:
            return engine.execute(plan, owner, transaction_id=layout.installation_id,
                                  confirmed_plan_digest=confirmed_digest)
        except KeyboardInterrupt:
            # Ctrl-C before the first journal write needs no recovery.
            journal = layout.transactions / layout.installation_id / "journal.json"
            if journal.is_file():
                engine.recover(plan, owner, transaction_id=layout.installation_id,
                               confirmed_plan_digest=confirmed_digest)
            raise


def installed_host(layout: HostInstallLayout, *, runner=run) -> HostProbe | None:
    """Recheck a previously published private host; never start its gateway."""
    layout.validate()
    if not layout.pointer.exists():
        return None
    if layout.pointer.is_symlink():
        raise ValueError("Managed host pointer must be a regular file.")
    payload = json.loads(layout.pointer.read_text())
    if (not isinstance(payload, dict) or set(payload) != {"schema", "installation_id", "version", "revision"}
            or payload.get("schema") != SCHEMA or payload.get("version") != _RELEASES["hermes"].version
            or payload.get("revision") != _RELEASES["hermes"].revision):
        raise ValueError("Managed host pointer is invalid.")
    from dataclasses import replace
    active = replace(layout, installation_id=payload["installation_id"])
    active.validate()
    if not active.executable.is_file() or not active.profile.is_dir():
        raise ValueError("Managed host installation is incomplete.")
    environment = clean_environment(active.attempt)
    environment.update(private_home_environment(active.attempt / "os-home"))
    HostInstallAdapter(active, runner=runner)._verify_host(environment)
    return HostProbe(HostKind.HERMES, True, True, str(active.executable), str(active.profile),
                     payload["version"], "verified")
