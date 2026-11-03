"""Explicit, checked authority for one new private Hermes installation.

No validator discovers this authority from a marker. Callers must pass the
scope explicitly; existing callers retain their disposable-only protection.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

MARKER = ".daemon-harness-installation.json"


def _direct(path: Path):
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError("Installation scope requires direct absolute paths.")
    return path


def _read(path):
    _direct(path)
    if not path.is_file() or path.stat().st_size > 8192:
        raise ValueError("Installation ownership receipt is unavailable.")
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class InstallationScope:
    attempt: Path

    @classmethod
    def from_verified_host(cls, layout):
        from daemons_companion.runtime.host_install import installed_host
        host = installed_host(layout)
        if host is None or host.config_path != str(layout.profile):
            raise ValueError("A verified active private host is required.")
        scope = cls(layout.attempt)
        scope.receipt()
        return scope

    @property
    def installation_id(self):
        return self.attempt.name

    @property
    def home(self):
        return self.attempt / "daemon"

    @property
    def profile(self):
        return self.attempt / "profile"

    @property
    def host_kind(self):
        return self.attempt.parent.parent.name

    def receipt(self):
        if self.host_kind == "openclaw":
            return self._openclaw_receipt()
        from daemons_companion.runtime.acquisition import _RELEASES
        _direct(self.attempt)
        if (not re.fullmatch(r"[0-9a-f]{32}", self.installation_id)
                or self.attempt.parent.name != "installations"
                or self.attempt.parent.parent.name != "hermes"
                or self.attempt.parent.parent.parent.name != "hosts"):
            raise ValueError("Installation scope is outside the owned host layout.")
        release = _RELEASES["hermes"]
        receipt = {"schema": "daemon.companion-host-install.v1", "installation_id": self.installation_id,
                   "version": release.version, "revision": release.revision}
        if (_read(self.attempt / "owner.json") != receipt
                or _read(self.attempt.parent.parent / "current.json") != receipt):
            raise ValueError("Installation scope does not match the active host receipt.")
        _direct(self.profile)
        if not self.profile.is_dir():
            raise ValueError("The owned host profile is unavailable.")
        return {"schema": "daemon.companion-installation-scope.v1", "host": receipt}

    def _openclaw_receipt(self):
        from daemons_companion.openclaw.host_install import OpenClawHostLayout, verify_installed
        _direct(self.attempt)
        if (not re.fullmatch(r"[0-9a-f]{32}", self.installation_id)
                or self.attempt.parent.name != "installations"
                or self.attempt.parents[2].name != "hosts"):
            raise ValueError("OpenClaw scope is outside the owned host layout.")
        pointer = _read(self.attempt.parents[1] / "current.json")
        if (pointer.get("schema") != "daemon.openclaw-first.v1"
                or pointer.get("installation_id") != self.installation_id
                or pointer.get("target_os") not in {"macos", "windows"}):
            raise ValueError("OpenClaw scope does not match its private pointer.")
        host = OpenClawHostLayout(self.attempt.parents[3], self.installation_id,
                                 pointer["target_os"], self.attempt / "host")
        receipt = verify_installed(host)
        if pointer.get("acquisition") != receipt["revision"]:
            raise ValueError("OpenClaw acquisition changed.")
        _direct(self.profile)
        if not self.profile.is_dir():
            raise ValueError("OpenClaw profile is unavailable.")
        return {"schema": "daemon.companion-installation-scope.v1", "host": pointer}

    def initialize(self):
        """After plan confirmation, create only the new empty runtime container."""
        receipt = self.receipt()
        _direct(self.home)
        if self.home.exists():
            self.validate_home(self.home)
            return
        self.home.mkdir(mode=0o700)
        try:
            fd = os.open(self.home / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # Never recursively remove this container: concurrent user files
            # are preserved and a partial receipt fails closed on resumption.
            raise

    def validate_home(self, home):
        if _direct(home) != self.home or _read(home / MARKER) != self.receipt():
            raise ValueError("Runtime does not belong to the explicitly authorized installation.")
        return home

    def validate_profile(self, profile):
        self.validate_home(self.home)
        if _direct(profile) != self.profile:
            raise ValueError("Profile does not belong to the explicitly authorized installation.")
        return profile

    def validate_namespace(self, namespace):
        self.receipt()
        if namespace != self.installation_id:
            raise ValueError("Owned installation must use its own service namespace.")
