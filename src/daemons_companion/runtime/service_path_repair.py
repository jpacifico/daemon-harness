"""Repair only known private Windows service paths, preserving installed code."""

from __future__ import annotations

from daemons_companion.runtime.services import (
    _load_windows_task_capture,
    _windows_definition_signature,
    build_background_jobs,
    render_job_definition,
)
from daemons_companion.hermes.profile import _patch_env, _read_env_values
from daemons_companion.runtime.bootstrap import _source_io_path
from daemons_companion.runtime.environment import (
    resolve_release_environment,
    release_environment_interpreter,
)


def _private_write(path, content, mode=0o600):
    import os
    from uuid import uuid4
    from daemon_core.platform_support import create_private_file

    path = _source_io_path(path)
    temporary = path.with_name(".repair-" + uuid4().hex)
    fd = create_private_file(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _signature(capture):
    return (
        _windows_definition_signature(_load_windows_task_capture(capture)["definition"])
        if capture
        else None
    )


def service_path_repair(layout, runtime, scheduler, old_gateway, new_gateway):
    from daemons_companion.hermes.composition import HermesCompositionLayout
    from daemons_companion.hermes.ready_composition import _services_layout
    from daemons_companion.runtime.installation_scope import _read

    user = _read(layout.attempt / "connection.json")["user_id"]
    composition = HermesCompositionLayout(
        runtime,
        layout.profile,
        layout.uv_executable,
        layout.uv_sha256,
        "windows",
        scheduler.job_namespace,
    )
    services = _services_layout(composition, user)
    old_jobs = build_background_jobs(services, legacy=True)
    new_jobs = build_background_jobs(services)
    environment = resolve_release_environment(composition.runtime_environment)
    python = release_environment_interpreter(environment, "windows")
    from daemons_companion.runtime.release import verify_current_release
    state = runtime.state_root
    roots = {
        "DAEMON_CORE_SRC": verify_current_release(runtime) / "src",
        "DAEMON_CORE_MEMORY_ROOT": runtime.memory_root,
        "DAEMON_CORE_STATE_ROOT": state,
        "DAEMON_CORE_PROVIDER_CONFIG": state / "config/provider.json",
        "DAEMON_CORE_PYTHON": python,
        "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": state / "after-turn-spool",
        "DAEMON_CORE_LATENCY_LOG": state / "latency/hermes.jsonl",
    }
    pairs = [(old_gateway, new_gateway), *zip(old_jobs, new_jobs)]
    return ServicePathRepair(layout.profile / ".env", roots, pairs, scheduler)


class ServicePathRepair:
    def __init__(self, env_path, roots, pairs, scheduler):
        self.env_path, self.roots, self.pairs, self.scheduler = (
            env_path,
            roots,
            pairs,
            scheduler,
        )
        self.directory = None

    def _read_env(self, path):
        if (
            path.is_symlink()
            or path.resolve() != path
            or not path.is_file()
            or path.stat().st_size > 1048576
        ):
            raise ValueError(
                "Private service environment must remain a direct bounded file."
            )
        values = _read_env_values(path)
        updates = {}
        for key, root in self.roots.items():
            if values.get(key) not in {str(root), str(_source_io_path(root))}:
                raise ValueError(
                    "Private service path differs from the owned runtime; preserved."
                )
            updates[key] = str(_source_io_path(root))
        before = path.read_bytes()
        return before, _patch_env(before, updates)

    def _known(self, capture, old, new):
        return _signature(capture) in {
            _windows_definition_signature(render_job_definition(old, "windows")),
            _windows_definition_signature(render_job_definition(new, "windows")),
        }

    def needs_repair(self):
        before, after = self._read_env(self.env_path)
        needed = before != after
        for old, new in self.pairs:
            current = self.scheduler.capture(new.code)
            if not self._known(current, old, new):
                raise RuntimeError(
                    "A private service differs from its known installation; automatic repair refused."
                )
            needed |= new.persistent and not _load_windows_task_capture(current)["running"]
            needed |= _signature(current) != _windows_definition_signature(
                render_job_definition(new, "windows")
            )
        return needed

    def snapshot(self, step, directory):
        self.needs_repair()  # Validate every service before taking any action.
        before, _ = self._read_env(self.env_path)
        _private_write(directory / "environment.before", before, 0o600)
        for old, new in self.pairs:
            saved = self.scheduler.capture(new.code)
            if not self._known(saved, old, new):
                raise RuntimeError(
                    "Private service changed before its snapshot; preserved."
                )
            _private_write(directory / (new.code + ".before"), saved, 0o600)
        self.directory = directory

    def _snapshots(self, directory):
        directory = _source_io_path(directory)
        before, after = self._read_env(directory / "environment.before")
        saved = {}
        for old, new in self.pairs:
            path = directory / (new.code + ".before")
            if (
                path.is_symlink()
                or path.resolve() != path
                or path.stat().st_size > 131072
            ):
                raise ValueError("Private service snapshot is invalid.")
            value = path.read_bytes()
            if not self._known(value, old, new):
                raise ValueError("Private service snapshot is not canonical.")
            saved[new.code] = value
        return before, after, saved

    def apply(self, step):
        before, after, saved = self._snapshots(self.directory)
        if self._read_env(self.env_path)[0] != before or any(
            self.scheduler.capture(code) != value for code, value in saved.items()
        ):
            raise RuntimeError("Private service changed concurrently; preserved.")
        # All writers stop before switching path spelling, keeping lock identities
        # consistent. Restart only after the same roots are installed everywhere.
        for _, new in self.pairs:
            self.scheduler.restore(new.code, None)
        _private_write(self.env_path, after, 0o600)
        for _, new in reversed(self.pairs):
            self.scheduler.install(new, render_job_definition(new, "windows"))

    def verify(self, step):
        _, after, _ = self._snapshots(self.directory)
        if self._read_env(self.env_path)[0] != after or any(
            not self.scheduler.matches(new, render_job_definition(new, "windows"))
            for _, new in self.pairs
        ):
            raise RuntimeError(
                "Private services did not restart with the repaired paths."
            )

    def rollback(self, step, directory):
        before, after, saved = self._snapshots(directory)
        if self._read_env(self.env_path)[0] not in (before, after):
            raise RuntimeError(
                "Private environment changed concurrently; recovery refused."
            )
        for old, new in self.pairs:
            current = self.scheduler.capture(new.code)
            if current is not None and not self._known(current, old, new):
                raise RuntimeError(
                    "Private service changed concurrently; recovery refused."
                )
        for _, new in self.pairs:
            self.scheduler.restore(new.code, None)
        _private_write(self.env_path, before, 0o600)
        for _, new in reversed(self.pairs):
            self.scheduler.restore(new.code, saved[new.code])
            actual = self.scheduler.capture(new.code)
            if _signature(actual) != _signature(saved[new.code]) or (
                _load_windows_task_capture(saved[new.code])["running"]
                and not _load_windows_task_capture(actual)["running"]
            ):
                raise RuntimeError("Previous private service could not be restored.")
