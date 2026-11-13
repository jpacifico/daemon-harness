"""Scoped native services for an explicitly associated private OpenClaw host.

Only the target bridge and gateway are owned here. The existing daemon worker,
nightly maintenance, provider store and embedding service are never registered.
"""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request

from daemons_companion.runtime.services import (
    BackgroundJob,
    OPENCLAW_API_JOB,
    OPENCLAW_GATEWAY_JOB,
    render_job_definition,
    scoped_job_code,
    _load_windows_task_capture,
    _windows_definition_signature,
)
from daemons_companion.hosts.attachment import digest
from daemons_companion.openclaw.host_install import checked_file, verify_installed


def port_closed(port):
    with socket.socket() as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def capture_matches(capture, definition, target_os):
    if capture is None:
        return False
    if target_os == "macos":
        value = json.loads(capture)
        return (
            value.get("schema") == "daemon.launchd-capture.v1"
            and base64.b64decode(value.get("definition") or "", validate=True)
            == definition
        )
    value = _load_windows_task_capture(capture)
    return _windows_definition_signature(
        value["definition"]
    ) == _windows_definition_signature(definition)


def build_jobs(layout, host, target_os):
    if layout.profile != host.profile or layout.executables != (host.node, host.entry):
        raise ValueError("The target service does not match its private installation.")
    if target_os != host.target_os:
        raise ValueError("Host and service platforms differ.")
    verify_installed(host)
    checked_file(
        layout.binding.source / "src/daemons_companion/hosts/attachment_entry.py",
        1024 * 1024,
    )
    config = json.loads(checked_file(layout.profile / "openclaw.json", 2 * 1024 * 1024))
    port = config.get("gateway", {}).get("port")
    if type(port) is not int or not 1024 <= port <= 65535 or port == layout.api_port:
        raise ValueError("The private gateway needs a distinct loopback port.")
    b = layout.binding
    from daemons_companion.runtime.bootstrap import _source_io_path

    path = _source_io_path if target_os == "windows" else lambda p: p
    bootstrap = (
        "import runpy,sys;sys.dont_write_bytecode=True;"
        f"sys.path.insert(0,{str(path(b.source))!r});sys.path.insert(0,{str(path(b.source / 'src'))!r});"
        "runpy.run_module('daemons_companion.hosts.attachment_entry',run_name='__main__')"
    )
    jobs = tuple(
        BackgroundJob(
            scoped_job_code(code, host.installation_id),
            str(path(b.python)),
            (
                "-I",
                "-B",
                "-c",
                (
                    "import os,sys;"
                    f"os.makedirs({str(path(host.attempt / 'logs'))!r},exist_ok=True);"
                    f"sys.stdout=open({str(path(host.attempt / 'logs' / (kind + '.log')))!r},'a',encoding='utf-8',buffering=1);"
                    "sys.stderr=sys.stdout;"
                    if target_os == "windows"
                    else ""
                )
                + bootstrap,
                kind,
                "--record",
                str(layout.record),
            ),
            str(host.attempt),
            str(host.attempt / "logs" / f"{kind}.log"),
            True,
            conversational=True,
        )
        for code, kind in ((OPENCLAW_API_JOB, "api"), (OPENCLAW_GATEWAY_JOB, "gateway"))
    )
    return jobs, port


class PrivateAttachmentServices:
    """New private target only. Existing native services require explicit adoption."""

    def __init__(
        self, layout, host, backend, *, probe=None, command=None, wait_seconds=420
    ):
        self.layout, self.host, self.backend = layout, host, backend
        if backend.job_namespace != host.installation_id:
            raise ValueError("Native service namespace differs from the target.")
        self.jobs, self.gateway_port = build_jobs(layout, host, backend.target_os)
        self.definitions = tuple(
            render_job_definition(j, backend.target_os) for j in self.jobs
        )
        self.probe = probe or self._probe
        self.command = command or subprocess.run
        self.wait_seconds = wait_seconds

    def snapshot(self):
        # Refuse unknown, stopped and pre-existing definitions alike. No adoption
        # is inferred from a name or a gateway which happens to be stopped.
        if any(self.backend.capture(j.code) is not None for j in self.jobs):
            raise RuntimeError("The destination already has registered services.")
        return {
            "schema": "daemon.attachment-services.v1",
            "jobs": [j.public_dict() for j in self.jobs],
            "definitions": [digest(d) for d in self.definitions],
        }

    def _capture(self, capture):
        if capture != {
            "schema": "daemon.attachment-services.v1",
            "jobs": [j.public_dict() for j in self.jobs],
            "definitions": [digest(d) for d in self.definitions],
        }:
            raise RuntimeError("Native service recovery inputs changed.")

    def quiesce(self, capture):
        self._capture(capture)
        if any(self.backend.capture(j.code) is not None for j in self.jobs):
            raise RuntimeError("A destination service appeared after review.")
        if not all(port_closed(p) for p in (self.layout.api_port, self.gateway_port)):
            raise RuntimeError("A destination port is already in use.")

    def _wait(self, predicate, message, until=None):
        # A cold first OpenClaw start, notably on Windows, can take minutes:
        # the same bound as a first OpenClaw installation.
        until = until or time.monotonic() + self.wait_seconds
        while True:
            if predicate():
                return
            if time.monotonic() >= until:
                raise RuntimeError(message)
            time.sleep(0.2)

    def _install(self, index):
        job = self.jobs[index]
        if self.backend.capture(job.code) is not None:
            raise RuntimeError("A destination service appeared during activation.")
        self.backend.install(job, self.definitions[index])

    def prepare(self, binding, profile, api_port):
        if (
            binding != self.layout.binding
            or profile != self.layout.profile
            or api_port != self.layout.api_port
        ):
            raise ValueError(
                "Service activation differs from the reviewed association."
            )
        self._install(0)
        self._wait(
            lambda: self.probe("api"),
            "The associated daemon bridge did not become ready.",
        )

    def activate(self):
        self._install(1)

    def verify(self, *_args):
        # One shared bound; separate checks name the part that never got ready.
        until = time.monotonic() + self.wait_seconds
        self._wait(
            lambda: all(
                self.backend.matches(j, d) for j, d in zip(self.jobs, self.definitions)
            ),
            "The associated host services changed during activation.",
            until,
        )
        self._wait(
            lambda: self.probe("gateway"),
            "The associated host did not become ready.",
            until,
        )
        self._wait(
            lambda: self.probe("maintenance"),
            "The shared memory maintenance did not become ready.",
            until,
        )

    def stop(self):
        # Check all definitions before removing any. Never replace an unrelated
        # or concurrently edited job, including one currently stopped/crashed.
        captures = [self.backend.capture(j.code) for j in self.jobs]
        if any(
            c is not None and not capture_matches(c, d, self.backend.target_os)
            for c, d in zip(captures, self.definitions)
        ):
            raise RuntimeError(
                "A destination service changed; recovery requires review."
            )
        for j, c in reversed(list(zip(self.jobs, captures))):
            if c is not None:
                self.backend.restore(j.code, None)
        self._wait(
            lambda: all(
                port_closed(p) for p in (self.layout.api_port, self.gateway_port)
            ),
            "The associated host did not stop completely.",
        )

    def restore(self, capture):
        self._capture(capture)
        if any(self.backend.capture(j.code) is not None for j in self.jobs):
            raise RuntimeError("Destination services remain registered after rollback.")

    def _probe(self, kind):
        from daemons_companion.runtime.bootstrap import _source_io_path

        if kind == "maintenance":
            from daemon_core.after_turn_service import read_after_turn_service_status

            b = self.layout.binding
            return bool(
                read_after_turn_service_status(
                    spool_root=_source_io_path(b.spool),
                    memory_root=_source_io_path(b.memory),
                    expected_revision=b.source.name,
                )["ok"]
            )
        if kind == "api":
            from daemon_core.continuity import technical_ref

            try:
                try:
                    response = urllib.request.urlopen(
                        f"http://127.0.0.1:{self.layout.api_port}/ready", timeout=1
                    )
                except urllib.error.HTTPError as error:
                    if error.code != 503:
                        return False
                    response = error
                with response:
                    data = json.loads(response.read(65537))
                return data.get(
                    "runtime_revision"
                ) == self.layout.binding.source.name and data.get("refs", {}).get(
                    "memory_root"
                ) == technical_ref(str(_source_io_path(self.layout.binding.memory)))
            except (OSError, ValueError):
                return False
        from daemons_companion.hosts.attachment_entry import host_environment
        from daemons_companion.openclaw.gateway import HEALTH_TIMEOUT_SECONDS

        # The CLI probe costs tens of seconds on Windows; a closed port cannot pass.
        if port_closed(self.gateway_port):
            return False
        try:
            result = self.command(
                [
                    str(self.host.node),
                    str(self.host.entry),
                    "gateway",
                    "health",
                    "--json",
                    "--port",
                    str(self.gateway_port),
                    "--timeout",
                    "1000",
                ],
                env=host_environment(self.host.profile),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                # Cold native CLI startup takes 10-35 s on Windows before it
                # even reaches the one-second Gateway transport probe.
                timeout=HEALTH_TIMEOUT_SECONDS,
            )
            health = json.loads(result.stdout)
            return (
                result.returncode == 0
                and isinstance(health, dict)
                and health.get("ok") is True
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
