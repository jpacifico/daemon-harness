"""Transactional execution binding for a newly owned OpenClaw profile."""

from __future__ import annotations
import hashlib
import json

from daemons_companion.openclaw.profile import OpenClawProfileAdapter
from daemons_companion.openclaw.host_install import checked_file
from daemons_companion.runtime.bootstrap import _source_io_path as _io_path
from daemons_companion.runtime.release import verify_current_release
from daemon_core.installation_profile import DURABLE_AFTER_TURN
from daemon_core.memory.integrity import atomic_write_text


class FirstProfileAdapter(OpenClawProfileAdapter):
    def paths(self):
        ref = hashlib.sha256(str(self.layout.openclaw_state).encode()).hexdigest()[:32]
        return {
            "env": self.layout.openclaw_state / ".env",
            "binding": self.layout.runtime.state_root
            / "host-bindings"
            / (ref + ".json"),
        }, ref

    def expected(self):
        from daemons_companion.openclaw.gateway import host_for

        runtime = self.layout.runtime
        scope = runtime.installation_scope
        host = host_for(scope)
        release = verify_current_release(runtime)
        _, ref = self.paths()
        environment = {
            "DAEMON_CORE_SRC": str(release / "src"),
            "DAEMON_CORE_MEMORY_ROOT": str(runtime.memory_root),
            "DAEMON_CORE_STATE_ROOT": str(runtime.state_root),
            "DAEMON_CORE_USER_ID": self.user_id,
            "DAEMON_CORE_HOST_REF": ref,
            **DURABLE_AFTER_TURN,
            "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(
                runtime.state_root / "after-turn-spool"
            ),
            "DAEMON_CORE_CONTINUITY_RECEIPT_DIR": str(
                runtime.state_root / "continuity/shared"
            ),
        }
        execution = dict(
            schema="daemon.host-execution.v1",
            host_ref=ref,
            status="active",
            host="openclaw",
            user_id=self.user_id,
            memory=str(runtime.memory_root),
            profile=str(host.profile),
            executables=[
                dict(
                    path=str(p),
                    sha256=hashlib.sha256(
                        checked_file(p, 256 * 1024 * 1024)
                    ).hexdigest(),
                )
                for p in (host.node, host.entry)
            ],
        )
        return {
            "env": "".join(
                k + "=" + json.dumps(v) + "\n" for k, v in environment.items()
            ),
            "binding": json.dumps(execution, sort_keys=True) + "\n",
        }

    def snapshot(self, step, directory):
        super().snapshot(step, directory)
        paths, _ = self.paths()
        if any(p.exists() or p.is_symlink() for p in paths.values()):
            raise ValueError(
                "A new OpenClaw profile already contains a daemon binding."
            )
        # Snapshot folders sit deep in the installation: write beyond MAX_PATH.
        atomic_write_text(_io_path(directory / "first-binding.json"), json.dumps(self.expected()))

    def apply(self, step):
        directory = self._snapshot_dirs[step.code]
        expected = json.loads(checked_file(directory / "first-binding.json", 65536))
        if expected != self.expected():
            raise ValueError("First host binding changed after review.")
        paths, _ = self.paths()
        if any(p.exists() or p.is_symlink() for p in paths.values()):
            raise ValueError("A host binding appeared after review.")
        super().apply(step)
        for name, path in paths.items():
            if path.resolve() != path:
                raise ValueError("Host binding path was redirected.")
            _io_path(path.parent).mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_text(_io_path(path), expected[name])

    def verify(self, step):
        super().verify(step)
        for name, path in self.paths()[0].items():
            if checked_file(path, 65536).decode() != self.expected()[name]:
                raise RuntimeError("First OpenClaw execution binding is incomplete.")

    def rollback(self, step, directory):
        manifest = directory / "first-binding.json"
        if _io_path(manifest).exists():
            expected = json.loads(checked_file(manifest, 65536))
            paths, _ = self.paths()
            for name, path in paths.items():
                if path.is_symlink() or (
                    path.exists()
                    and checked_file(path, 65536).decode() != expected[name]
                ):
                    raise RuntimeError("Host binding changed; rollback refused.")
            for path in paths.values():
                _io_path(path).unlink(missing_ok=True)
        super().rollback(step, directory)
