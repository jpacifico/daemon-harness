"""Private, pinned OpenClaw acquisition. No daemon, account or service mutation.

Candidate implementation: not enabled by the distributed installer. An explicit
layout and consent digest are required. Native Node is private to this attempt;
no global npm installation or inherited npm/Python/host credentials are used.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile

from daemons_companion.runtime.bootstrap import (
    HostCommandError,
    _source_io_path,
    clean_environment,
    private_home_environment,
    run,
)
from daemons_companion.runtime.acquisition import open_with_retry
from daemons_companion.runtime.environment import check_windows_installation_room
from daemon_core.memory.integrity import atomic_write_text, memory_file_try_lock

@dataclass(frozen=True)
class OpenClawRelease:
    version: str
    node_version: str
    node_archives: dict


# New installations use CURRENT. An installed host is verified against the
# release named by its own staged manifest, so hosts installed by an earlier
# qualified release stay recognised for services, removal and attachment.
RELEASES = {
    release.version: release
    for release in (
        # The first release lists the GPT-6.2 models to a ChatGPT subscription.
        OpenClawRelease("2026.11.6", "24.22.1", {
            "macos": ("darwin-arm64.tar.gz",
                      "89c682593d76cd44233fa53b1563264e7de9cbb189cad169135c5b4fadfbc0a7"),
            "windows": ("win-x64.zip",
                        "d2046d28c2c3dcd211ac5145c0bf30e2047ef9dfb306693c8c6e2eccd8b95fa5"),
        }),
        OpenClawRelease("2026.10.7", "24.22.1", {
            "macos": ("darwin-arm64.tar.gz",
                      "89c682593d76cd44233fa53b1563264e7de9cbb189cad169135c5b4fadfbc0a7"),
            "windows": ("win-x64.zip",
                        "d2046d28c2c3dcd211ac5145c0bf30e2047ef9dfb306693c8c6e2eccd8b95fa5"),
        }),
        OpenClawRelease("2026.9.1-beta.1", "24.15.0", {
            "macos": ("darwin-arm64.tar.gz",
                      "372331b969779ab5d15b949884fc6eaf88d5afe87bde8ba881d6400b9100ffc4"),
            "windows": ("win-x64.zip",
                        "cc5149eabd53779ce1e7bdc5401643622d0c7e6800ade18928a767e940bb0e62"),
        }),
    )
}
CURRENT = RELEASES["2026.11.6"]
NODE_VERSION = CURRENT.node_version
OPENCLAW_VERSION = CURRENT.version
NODE_ARCHIVES = CURRENT.node_archives
SCHEMA = "daemon.companion-openclaw-host.v1"
MAX_DOWNLOAD = 128 * 1024 * 1024
MAX_EXTRACTED = 512 * 1024 * 1024


def direct(path: Path) -> Path:
    # Preserve logical paths for ownership/fingerprints; extend only filesystem I/O.
    # Check traversal before extension, whose normalization would otherwise hide it.
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Host installation paths must be direct and absolute.")
    io_path = _source_io_path(path)
    if io_path.is_symlink() or io_path.resolve() != io_path:
        raise ValueError("Host installation paths must be direct and absolute.")
    return path


def checked_file(path: Path, limit: int) -> bytes:
    direct(path)
    io_path = _source_io_path(path)
    info = io_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError("Host metadata must be a bounded regular file.")
    value = io_path.read_bytes()
    if len(value) > limit:
        raise ValueError("Host metadata exceeds its size limit.")
    return value


@dataclass(frozen=True)
class OpenClawHostLayout:
    companion_home: Path
    installation_id: str
    target_os: str
    lock_directory: Path

    @property
    def root(self):
        return self.companion_home / "hosts/openclaw"

    @property
    def attempt(self):
        return self.root / "installations" / self.installation_id

    @property
    def profile(self):
        return self.attempt / "profile"

    @classmethod
    def existing(cls, companion_home: Path, installation_id: str, target_os: str,
                 bundled_lock: Path) -> "OpenClawHostLayout":
        """Reopen a host with the lock it was installed from.

        A host installed by an earlier qualified release keeps its own staged
        manifest; the bundled lock only describes hosts this app would install.
        """
        candidate = cls(companion_home, installation_id, target_os, bundled_lock)
        staged = candidate.host
        if _source_io_path(staged / "package.json").is_file():
            return cls(companion_home, installation_id, target_os, staged)
        return candidate

    @property
    def release(self) -> OpenClawRelease:
        """The qualified release named by this layout's dependency manifest."""
        manifest = json.loads(checked_file(self.lock_directory / "package.json", 8192))
        dependencies = manifest.get("dependencies")
        if not isinstance(dependencies, dict) or set(dependencies) != {"openclaw"}:
            raise ValueError("The bundled OpenClaw dependency lock is invalid.")
        release = RELEASES.get(dependencies["openclaw"])
        if release is None:
            raise ValueError("The OpenClaw version is not a qualified release.")
        return release

    @property
    def node_root(self):
        return (
            self.attempt
            / "node"
            / (
                "node-v"
                + self.release.node_version
                + "-"
                + ("win-x64" if self.target_os == "windows" else "darwin-arm64")
            )
        )

    @property
    def node(self):
        return self.node_root / (
            "node.exe" if self.target_os == "windows" else "bin/node"
        )

    @property
    def npm(self):
        return self.node_root / (
            "node_modules/npm/bin/npm-cli.js"
            if self.target_os == "windows"
            else "lib/node_modules/npm/bin/npm-cli.js"
        )

    @property
    def host(self):
        return self.attempt / "host"

    @property
    def entry(self):
        return self.host / "node_modules/openclaw/openclaw.mjs"

    @property
    def receipt(self):
        return self.attempt / "owner.json"

    def validate(self):
        if (
            self.target_os not in CURRENT.node_archives
            or re.fullmatch(r"[a-f0-9]{32}", self.installation_id) is None
        ):
            raise ValueError("Unsupported OpenClaw installation target.")
        for p in (
            self.companion_home,
            self.lock_directory,
            self.attempt,
            self.profile,
            self.node,
            self.npm,
            self.host,
            self.entry,
        ):
            direct(p)
        manifest = json.loads(checked_file(self.lock_directory / "package.json", 8192))
        lock = json.loads(
            checked_file(self.lock_directory / "package-lock.json", 4 * 1024 * 1024)
        )
        release = self.release
        if (
            manifest.get("dependencies") != {"openclaw": release.version}
            or lock.get("lockfileVersion") != 3
        ):
            raise ValueError("The bundled OpenClaw dependency lock is invalid.")
        packages = lock.get("packages", {})
        if packages.get("node_modules/openclaw", {}).get("version") != release.version:
            raise ValueError(
                "The lock does not contain the supported OpenClaw version."
            )
        for name, item in packages.items():
            if not name:
                continue
            if item.get("link") or not (
                _registry_artifact(item) or _bundled_in_registry_artifact(packages, name)
            ):
                raise ValueError(
                    "Dependencies must use immutable npm registry artifacts."
                )


def _registry_artifact(item: dict) -> bool:
    return (str(item.get("resolved", "")).startswith("https://registry.npmjs.org/")
            and str(item.get("integrity", "")).startswith("sha512-"))


def _bundled_in_registry_artifact(packages: dict, name: str) -> bool:
    """A bundled dependency ships inside its parent's verified tarball.

    It has no artifact of its own; accept it only when the nearest enclosing
    package that is not itself bundled is an immutable registry artifact.
    """
    item = packages[name]
    if item.get("inBundle") is not True or "resolved" in item or "integrity" in item:
        return False
    parent = name
    while "/node_modules/" in parent:
        parent = parent.rsplit("/node_modules/", 1)[0]
        enclosing = packages.get(parent)
        if enclosing is None or enclosing.get("link"):
            return False
        if enclosing.get("inBundle") is not True:
            return _registry_artifact(enclosing)
    return False


def plan(layout: OpenClawHostLayout) -> dict:
    layout.validate()
    release = layout.release
    suffix, sha = release.node_archives[layout.target_os]
    value = {
        "schema": SCHEMA,
        "installation_id": layout.installation_id,
        "profile": str(layout.profile),
        "attempt": str(layout.attempt),
        "target_os": layout.target_os,
        "node_version": release.node_version,
        "openclaw_version": release.version,
        "node_url": f"https://nodejs.org/dist/v{release.node_version}/node-v{release.node_version}-{suffix}",
        "node_sha256": sha,
        "lock_sha256": hashlib.sha256(
            checked_file(layout.lock_directory / "package-lock.json", 4 * 1024 * 1024)
        ).hexdigest(),
        "package_sha256": hashlib.sha256(
            checked_file(layout.lock_directory / "package.json", 8192)
        ).hexdigest(),
        "starts_services": False,
        "configures_model": False,
        "configures_channel": False,
    }
    value["revision"] = hashlib.sha256(
        json.dumps(value, sort_keys=True).encode()
    ).hexdigest()
    return value


def download(url: str, destination: Path, expected: str):
    request = urllib.request.Request(url, headers={"User-Agent": "Daemons-Companion"})
    hasher = hashlib.sha256()
    total = 0
    with (
        open_with_retry(lambda: urllib.request.urlopen(request, timeout=45), url=url) as response,
        destination.open("xb") as output,
    ):
        if response.geturl() != url:
            raise ValueError("The Node download was redirected unexpectedly.")
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise ValueError("Node download is too large.")
            output.write(chunk)
            hasher.update(chunk)
    if hasher.hexdigest() != expected:
        raise ValueError("The private Node download failed its integrity check.")


def extract_node(archive: Path, destination: Path, prefix: str):
    """Bounded extraction; npm aliases are unnecessary because its JS is invoked."""
    archive = _source_io_path(archive)
    destination = _source_io_path(destination)
    destination.mkdir(mode=0o700)
    total = 0
    names = set()

    def target(name, size):
        nonlocal total
        parts = PurePosixPath(name).parts
        if (
            not parts
            or parts[0] != prefix
            or any(p in ("..", ".") or ":" in p or "\\" in p for p in parts)
            or name.startswith("/")
        ):
            raise ValueError("Unsafe Node archive member.")
        if name in names or len(names) >= 20000:
            raise ValueError("Duplicate or oversized Node archive.")
        names.add(name)
        total += size
        if size < 0 or total > MAX_EXTRACTED:
            raise ValueError("Node archive is too large.")
        return destination.joinpath(*parts)

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                out = target(member.filename, member.file_size)
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("Node archive links are unsupported.")
                if member.is_dir():
                    out.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with bundle.open(member) as source, out.open("xb") as output:
                    output.write(source.read(member.file_size + 1))
    else:
        with tarfile.open(archive) as bundle:
            for member in bundle:
                out = target(member.name, member.size)
                if member.issym() and member.name in {
                    f"{prefix}/bin/npm",
                    f"{prefix}/bin/npx",
                    f"{prefix}/bin/corepack",
                }:
                    continue
                if member.isdir():
                    out.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                if not member.isfile():
                    raise ValueError("Node archive contains unsupported entries.")
                out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with bundle.extractfile(member) as source, out.open("xb") as output:
                    output.write(source.read(member.size + 1))
                out.chmod(0o700 if member.mode & 0o111 else 0o600)


def verify_installed(layout: OpenClawHostLayout) -> dict:
    """Recheck owned metadata and the exact executable before handing it off."""
    reviewed = plan(layout)
    receipt = json.loads(checked_file(layout.receipt, 16384))
    if receipt.get("status") != "installed" or any(
        receipt.get(k) != v for k, v in reviewed.items()
    ):
        raise ValueError("The OpenClaw installation receipt changed.")
    if hashlib.sha256(
        checked_file(layout.node, MAX_EXTRACTED)
    ).hexdigest() != receipt.get("node_executable_sha256"):
        raise ValueError("The private Node executable changed.")
    if hashlib.sha256(
        checked_file(layout.entry, 1024 * 1024)
    ).hexdigest() != receipt.get("entry_sha256"):
        raise ValueError("The private OpenClaw entry point changed.")
    package = json.loads(
        checked_file(layout.host / "node_modules/openclaw/package.json", 1024 * 1024)
    )
    if package.get("version") != layout.release.version:
        raise ValueError("The private OpenClaw package version changed.")
    return receipt


def install(
    layout: OpenClawHostLayout,
    revision: str,
    *,
    runner=run,
    downloader=download,
    progress=lambda message: None,
    resume=False,
) -> dict:
    reviewed = plan(layout)
    if reviewed["revision"] != revision:
        raise ValueError("OpenClaw installation changed; review it again.")
    if layout.release is not CURRENT:
        raise ValueError("New OpenClaw installations use the current qualified release.")
    if layout.target_os == "windows" and os.name == "nt":
        check_windows_installation_room(layout.attempt)
    layout.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    direct(layout.root)
    with memory_file_try_lock(layout.root / ".install.lock") as acquired:
        if not acquired:
            raise RuntimeError("Another OpenClaw installation is running.")
        if layout.attempt.exists():
            receipt = json.loads(checked_file(layout.receipt, 16384))
            if not resume or any(receipt.get(k) != v for k, v in reviewed.items()):
                raise ValueError(
                    "The private installation already exists; inspect its diagnostic before retrying."
                )
            if receipt.get("status") == "installed":
                return verify_installed(layout)
            if receipt.get("command_active"):
                raise ValueError(
                    "An interrupted native command needs recovery before retrying."
                )
            if receipt.get("status") not in {"preparing", "failed"} or (
                layout.profile.exists() and any(layout.profile.iterdir())
            ):
                raise ValueError("Only an unconfigured owned download can be retried.")
            # These two directories contain only acquired program files. Retain
            # profile, private home, logs and unknown files, never follow links.
            for name in ("node", "host"):
                folder = direct(layout.attempt / name)
                if folder.exists():
                    shutil.rmtree(_source_io_path(folder))
        else:
            layout.attempt.mkdir(parents=True, mode=0o700)
        receipt = {**reviewed, "status": "preparing"}
        atomic_write_text(layout.receipt, json.dumps(receipt, sort_keys=True) + "\n")

        def execute(command, environment, *, timeout):
            # Persist intent before spawning. A hard-killed parent may leave npm
            # alive; never prune its directories merely because its lock vanished.
            receipt["command_active"] = True
            atomic_write_text(
                layout.receipt, json.dumps(receipt, sort_keys=True) + "\n"
            )
            try:
                result = runner(command, environment, timeout=timeout, **({"progress": progress} if "ci" in command else {}))
            except BaseException as error:
                if isinstance(error, HostCommandError) or getattr(error, "private_command_stopped", False):
                    receipt.pop("command_active", None)
                raise
            receipt.pop("command_active", None)
            atomic_write_text(
                layout.receipt, json.dumps(receipt, sort_keys=True) + "\n"
            )
            return result

        host_stage = "prepare"
        try:
            for name in ("host", "profile", "os-home", "temp", "logs"):
                (layout.attempt / name).mkdir(mode=0o700, exist_ok=True)
            environment = clean_environment(layout.attempt)
            environment = {
                k: v
                for k, v in environment.items()
                if not k.upper().startswith(
                    ("NPM_", "NODE_", "COREPACK_", "OPENCLAW_", "HERMES_")
                )
            }
            environment.update(private_home_environment(layout.attempt / "os-home"))
            environment.update(
                {
                    "OPENCLAW_STATE_DIR": str(layout.profile),
                    "NPM_CONFIG_USERCONFIG": str(layout.attempt / "empty.npmrc"),
                    "NPM_CONFIG_GLOBALCONFIG": str(
                        layout.attempt / "empty-global.npmrc"
                    ),
                    "NPM_CONFIG_CACHE": str(layout.attempt / "npm-cache"),
                    "PATH": str(layout.node.parent)
                    + os.pathsep
                    + environment.get("PATH", os.defpath),
                }
            )
            for name in ("empty.npmrc", "empty-global.npmrc"):
                (layout.attempt / name).write_text("", encoding="utf8")
            host_stage = "node-download"
            progress("Downloading and verifying the private Node runtime...")
            with tempfile.TemporaryDirectory(
                prefix="download-", dir=layout.attempt
            ) as folder:
                archive = Path(folder) / reviewed["node_url"].rsplit("/", 1)[1]
                downloader(reviewed["node_url"], archive, reviewed["node_sha256"])
                host_stage = "node-extract"
                extract_node(archive, layout.attempt / "node", layout.node_root.name)
            host_stage = "node-check"
            if (
                execute(
                    [str(layout.node), "--version"], environment, timeout=30
                ).strip()
                != "v" + NODE_VERSION
            ):
                raise RuntimeError("The private Node runtime version is incorrect.")
            for name in ("package.json", "package-lock.json"):
                (layout.host / name).write_bytes(
                    checked_file(layout.lock_directory / name, 4 * 1024 * 1024)
                )
            if plan(layout) != reviewed:
                raise RuntimeError("The dependency lock changed during installation.")
            if (
                hashlib.sha256(
                    (layout.host / "package-lock.json").read_bytes()
                ).hexdigest()
                != reviewed["lock_sha256"]
                or hashlib.sha256(
                    (layout.host / "package.json").read_bytes()
                ).hexdigest()
                != reviewed["package_sha256"]
            ):
                raise RuntimeError("The staged OpenClaw dependency lock changed.")
            host_stage = "dependencies"
            progress("Installing the pinned OpenClaw dependencies...")
            execute(
                [
                    str(layout.node),
                    str(layout.npm),
                    "ci",
                    "--prefix",
                    str(layout.host),
                    "--ignore-scripts",
                    "--omit=dev",
                    "--no-audit",
                    "--no-fund",
                    "--registry=https://registry.npmjs.org/",
                ],
                environment,
                timeout=1800,
            )
            host_stage = "host-check"
            actual = json.loads(
                checked_file(
                    layout.host / "node_modules/openclaw/package.json", 1024 * 1024
                )
            )
            if actual.get("version") != OPENCLAW_VERSION:
                raise RuntimeError("The installed OpenClaw package is incorrect.")
            progress("Checking the private OpenClaw executable...")
            version = execute(
                [str(layout.node), str(layout.entry), "--version"],
                environment,
                timeout=60,
            )
            from daemons_companion.runtime.acquisition import recognized_version

            if recognized_version("openclaw", version, "verified") != OPENCLAW_VERSION:
                raise RuntimeError(
                    "The private OpenClaw executable did not pass its version check."
                )
            receipt.update(
                status="installed",
                entry_sha256=hashlib.sha256(
                    checked_file(layout.entry, 1024 * 1024)
                ).hexdigest(),
                node_executable_sha256=hashlib.sha256(
                    layout.node.read_bytes()
                ).hexdigest(),
            )
            atomic_write_text(
                layout.receipt, json.dumps(receipt, sort_keys=True) + "\n"
            )
            return receipt
        except BaseException as error:
            error.host_stage = host_stage
            # Retain evidence and owned inactive files; never recursively delete a
            # destination that might now include files supplied by its owner.
            receipt.update(
                status="failed",
                failed_operation="host-acquisition",
                error_type=type(error).__name__,
                host_stage=host_stage,
            )
            atomic_write_text(
                layout.receipt, json.dumps(receipt, sort_keys=True) + "\n"
            )
            raise
