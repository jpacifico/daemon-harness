"""Reversible host association with one existing daemon, outside its memory.

The trusted desktop/backend selects paths. A confirmation digest binds both
profiles and the complete runtime identity. No model call or service operation
is performed by inspection. Execution requires an explicit native controller.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import ast
import hashlib
import json
from pathlib import Path
import re
import traceback
from typing import Protocol

from daemons_companion.runtime.bootstrap import _source_io_path
from daemons_companion.openclaw.host_install import checked_file, direct
from daemons_companion.openclaw.profile import _render_config
from daemons_companion.hermes.profile import _patch_env
from daemon_core.installation_profile import DURABLE_AFTER_TURN
from daemon_core.memory.integrity import atomic_write_text, memory_file_try_lock

SCHEMA = "daemon.host-attachment.v1"
MAX_CONFIG = 2 * 1024 * 1024


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def optional(path: Path) -> bytes | None:
    direct(path)
    return checked_file(path, MAX_CONFIG) if path.exists() else None


def encode(value):
    return None if value is None else base64.b64encode(value).decode("ascii")


def decode(value):
    return None if value is None else base64.b64decode(value, validate=True)


@dataclass(frozen=True)
class RuntimeBinding:
    source: Path
    memory: Path
    state: Path
    python: Path
    user_id: str
    maintenance_owner: str
    spool: Path
    receipts: Path

    def descriptor(self) -> dict:
        for p in (self.source, self.memory, self.state, self.spool, self.receipts):
            direct(p)
            if not _source_io_path(p).is_dir():
                raise ValueError("The shared daemon runtime is unavailable.")
        # A venv interpreter may be an internal symlink; its canonical target is
        # captured explicitly, unlike mutable configuration and memory paths.
        if not self.python.is_absolute() or not _source_io_path(self.python).is_file():
            raise ValueError("The shared daemon interpreter is unavailable.")
        if (
            not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,128}", self.user_id)
            or not self.maintenance_owner
            or len(self.maintenance_owner) > 256
        ):
            raise ValueError("The daemon identity or maintenance owner is ambiguous.")
        routing = checked_file(
            self.source / "src/daemon_core/host_bindings.py", MAX_CONFIG
        )
        declared = [
            node.value.value
            for node in ast.parse(routing).body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in node.targets)
        ]
        if declared != ["daemon.host-execution.v1"]:
            raise ValueError(
                "This daemon runtime must be upgraded before adding a host."
            )
        identity = checked_file(self.memory / "core/daemon_identity.json", 8192)
        parsed = json.loads(identity)
        if not isinstance(parsed.get("name"), str) or not parsed["name"].strip():
            raise ValueError("The existing daemon identity is unavailable.")
        plugin = self.source / "integrations/openclaw_plugin"
        checked_file(plugin / "openclaw.plugin.json", 65536)
        checked_file(plugin / "index.js", MAX_CONFIG)
        checked_file(self.source / "src/daemon_core/canonical_voice.py", MAX_CONFIG)
        return {
            "source": str(self.source),
            "memory": str(self.memory),
            "state": str(self.state),
            "python": str(self.python),
            "python_target": str(self.python.resolve()),
            "spool": str(self.spool),
            "receipts": str(self.receipts),
            "user_id": self.user_id,
            "maintenance_owner": self.maintenance_owner,
            "identity_sha256": digest(identity),
            "name": parsed["name"],
            "plugin_sha256": digest(checked_file(plugin / "index.js", MAX_CONFIG)),
            "routing_sha256": digest(routing),
        }


@dataclass(frozen=True)
class AttachmentLayout:
    binding: RuntimeBinding
    profile: Path
    record: Path
    api_port: int
    host_version: str
    executables: tuple[Path, ...]

    def validate(self):
        from daemons_companion.runtime.acquisition import recognized_version

        self.binding.descriptor()
        if not 1 <= len(self.executables) <= 2:
            raise ValueError("An explicit verified host command is required.")
        for executable in self.executables:
            checked_file(executable, 256 * 1024 * 1024)
        for p in (self.profile, self.record, self.record.parent):
            direct(p)
        if not self.profile.is_dir() or not self.record.parent.is_dir():
            raise ValueError(
                "The selected host or association registry is unavailable."
            )
        if (
            self.profile == self.binding.memory
            or self.profile.is_relative_to(self.binding.memory)
            or self.binding.memory.is_relative_to(self.profile)
        ):
            raise ValueError("Host profile and canonical memory must remain separate.")
        if (
            self.record.is_relative_to(self.profile)
            or self.record.is_relative_to(self.binding.source)
            or self.record.is_relative_to(self.binding.state / "host-bindings")
        ):
            raise ValueError(
                "Recovery records must remain separate from host and runtime files."
            )
        if self.record.is_relative_to(self.binding.memory):
            raise ValueError("Host associations must remain outside personal memory.")
        if (
            recognized_version("openclaw", self.host_version, "verified")
            != self.host_version
        ):
            raise ValueError("The target OpenClaw version is not compatible.")
        if type(self.api_port) is not int or not 1024 <= self.api_port <= 65535:
            raise ValueError("A non-privileged loopback API port is required.")


class AttachmentController(Protocol):
    """Trusted native adapter. Captures are private, bounded, and serializable.

    prepare starts only the loopback bridge, never a channel, worker or nightly.
    quiesce must attest the selected destination gateway stopped and prevent
    concurrent/reboot starts until activation; external changes fail closed.
    Same-bot transfer needs a separately reviewed source service handover.
    """

    def snapshot(self) -> dict: ...
    def quiesce(self, capture: dict) -> None: ...
    def prepare(
        self, binding: RuntimeBinding, profile: Path, api_port: int
    ) -> None: ...
    def activate(self) -> None: ...
    def verify(self, binding: RuntimeBinding, profile: Path, api_port: int) -> None: ...
    def stop(self) -> None: ...
    def restore(self, capture: dict) -> None: ...


def host_reference(profile: Path) -> str:
    return digest(str(profile).encode())[:32]


def files(layout: AttachmentLayout) -> dict[str, Path]:
    return {
        "config": layout.profile / "openclaw.json",
        "environment": layout.profile / ".env",
        "agents": layout.profile / "workspace-daemon-harness/AGENTS.md",
        "host-binding": layout.binding.state
        / "host-bindings"
        / (host_reference(layout.profile) + ".json"),
    }


def inspect(layout: AttachmentLayout) -> dict:
    layout.validate()
    if layout.record.exists():
        raise ValueError(
            "An association record already exists; resume or undo it first."
        )
    paths = files(layout)
    before = {name: optional(path) for name, path in paths.items()}
    if before["host-binding"] is not None:
        raise ValueError("The destination already has a host execution registration.")
    if before["config"] is None:
        raise ValueError(
            "Configure the destination model and channel before attaching the daemon."
        )
    # Public OpenClaw profiles may use JSON5; preserve semantics, not comments.
    from daemons_companion.openclaw.profile import _load_config

    config = _load_config(before["config"])
    plugins = config.get("plugins", {})
    if plugins.get("entries", {}).get("daemon-harness") or plugins.get("slots", {}).get(
        "contextEngine"
    ) not in (None, "legacy"):
        raise ValueError(
            "The destination already has a daemon or another context engine."
        )
    if not config.get("agents", {}).get("defaults", {}).get("model"):
        raise ValueError("The destination host has no configured conversation model.")
    workspace = layout.profile / "workspace-daemon-harness"
    direct(workspace)
    if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise ValueError("The managed destination workspace is already occupied.")
    existing_env = before["environment"] or b""
    if re.search(
        rb"^\s*(?:export\s+)?DAEMON_CORE_(?:SRC|MEMORY_ROOT|USER_ID)\s*=",
        existing_env,
        re.M,
    ):
        raise ValueError("The destination already contains daemon bindings.")
    b = layout.binding
    from daemon_core.host_bindings import SCHEMA as HOST_SCHEMA

    execution = {
        "schema": HOST_SCHEMA,
        "host_ref": host_reference(layout.profile),
        "status": "active",
        "host": "openclaw",
        "user_id": b.user_id,
        "memory": str(b.memory),
        "profile": str(layout.profile),
        "executables": [
            {"path": str(p), "sha256": digest(checked_file(p, 256 * 1024 * 1024))}
            for p in layout.executables
        ],
    }
    expected = {
        "config": _render_config(
            before["config"],
            plugin_source=b.source / "integrations/openclaw_plugin",
            workspace=workspace,
            user_id=b.user_id,
            daemon_url=f"http://127.0.0.1:{layout.api_port}",
            runtime_health_path=b.state
            / (
                "openclaw-" + digest(str(layout.profile).encode())[:16] + "-health.json"
            ),
        ),
        "environment": _patch_env(
            existing_env,
            {
                "DAEMON_CORE_SRC": str(b.source / "src"),
                "DAEMON_CORE_MEMORY_ROOT": str(b.memory),
                "DAEMON_CORE_STATE_ROOT": str(b.state),
                "DAEMON_CORE_USER_ID": b.user_id,
                **DURABLE_AFTER_TURN,
                "DAEMON_CORE_AFTER_TURN_SPOOL_DIR": str(b.spool),
                "DAEMON_CORE_CONTINUITY_RECEIPT_DIR": str(b.receipts),
                "DAEMON_CORE_HOST_REF": host_reference(layout.profile),
            },
        ),
        "agents": checked_file(
            b.source / "integrations/openclaw_plugin/workspace/AGENTS.md", 65536
        ),
        "host-binding": (json.dumps(execution, sort_keys=True) + "\n").encode(),
    }
    private = {
        "schema": SCHEMA,
        "binding": b.descriptor(),
        "profile": str(layout.profile),
        "host": "openclaw",
        "host_version": layout.host_version,
        "api_port": layout.api_port,
        "record": str(layout.record),
        "workspace_existed": workspace.exists(),
        "before": {k: encode(v) for k, v in before.items()},
        "expected": {k: encode(v) for k, v in expected.items()},
    }
    private["revision"] = digest(json.dumps(private, sort_keys=True).encode())
    return private


def public_review(plan: dict) -> dict:
    # Never include config snapshots: they may contain native host credentials.
    memory = Path(plan["binding"]["memory"])
    return {
        k: plan[k]
        for k in ("schema", "revision", "host", "host_version", "profile", "api_port")
    } | {
        "name": plan["binding"]["name"],
        "memory": str(memory.parent if memory.name == ".daemon" else memory),
        "maintenance": "existing",
        "copies_memory": False,
        "reuses_host_credentials": True,
    }


def save_record(layout, data):
    direct(layout.record)
    atomic_write_text(layout.record, json.dumps(data, sort_keys=True) + "\n")


def apply(
    layout: AttachmentLayout,
    revision: str,
    controller: AttachmentController,
    *,
    mutation_hook=lambda name: None,
) -> dict:
    with memory_file_try_lock(layout.profile / "companion-attachment") as acquired:
        if not acquired:
            raise RuntimeError("The selected host is busy.")
        planned = inspect(layout)
        if planned["revision"] != revision:
            raise ValueError(
                "The source or destination changed; review the association again."
            )
        capture = controller.snapshot()
        if len(json.dumps(capture).encode()) > MAX_CONFIG:
            raise ValueError("Service snapshot is too large.")
        record = {**planned, "status": "preparing", "capture": capture, "written": []}
        save_record(layout, record)
        phase = "quiesce"
        try:
            controller.quiesce(capture)
            if layout.binding.descriptor() != planned["binding"]:
                raise RuntimeError("The shared daemon changed before association.")
            for name, path in files(layout).items():
                phase = "write-" + name
                if optional(path) != decode(planned["before"][name]):
                    raise RuntimeError("The destination changed before association.")
                # Intent is durable before mutation, including process interruption.
                record["written"].append(name)
                save_record(layout, record)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_write_text(
                    path, decode(planned["expected"][name]).decode("utf8")
                )
                mutation_hook(name)
            record["status"] = "prepared"
            save_record(layout, record)
            phase = "prepare-api"
            controller.prepare(layout.binding, layout.profile, layout.api_port)
            phase = "activate-gateway"
            controller.activate()
            phase = "verify-services"
            controller.verify(layout.binding, layout.profile, layout.api_port)
            phase = "verify-binding"
            if layout.binding.descriptor() != planned["binding"]:
                raise RuntimeError(
                    "The shared daemon binding changed during activation."
                )
            phase = "verify-files"
            for name, path in files(layout).items():
                if optional(path) != decode(planned["expected"][name]):
                    raise RuntimeError(
                        "The host changed its managed files during activation."
                    )
            record["status"] = "active"
            save_record(layout, record)
            return public_review(planned) | {"status": "active"}
        except BaseException as error:
            # Keep a bounded diagnostic even after rollback. Never persist the
            # exception text/locals, which can contain credentials or user paths.
            record["failure"] = {
                "phase": phase,
                "error_type": type(error).__name__,
                "frames": [
                    {
                        "module": Path(frame.filename).name,
                        "function": frame.name,
                        "line": frame.lineno,
                    }
                    for frame in traceback.extract_tb(error.__traceback__)[-8:]
                ],
            }
            _restore(layout, record, controller)
            raise


def _restore(layout, record, controller):
    # Never revert configuration while the destination may still be using it.
    record["status"] = "rolling_back"
    save_record(layout, record)
    try:
        controller.stop()
        for name in reversed(record["written"]):
            path = files(layout)[name]
            current = optional(path)
            before = decode(record["before"][name])
            expected = decode(record["expected"][name])
            if current == before:
                continue
            if current != expected:
                raise RuntimeError("The target changed; recovery requires review.")
            if before is None:
                path.unlink()
            else:
                atomic_write_text(path, before.decode("utf8"))
        workspace = layout.profile / "workspace-daemon-harness"
        if (
            not record["workspace_existed"]
            and workspace.is_dir()
            and not any(workspace.iterdir())
        ):
            workspace.rmdir()
        controller.restore(record["capture"])
        record["status"] = "rolled_back"
        save_record(layout, record)
    except BaseException:
        record["status"] = "recovery_required"
        save_record(layout, record)
        raise


def rollback(layout: AttachmentLayout, revision: str, controller: AttachmentController):
    """Recover after a process restart, or undo a completed association."""
    layout.validate()
    with memory_file_try_lock(layout.profile / "companion-attachment") as acquired:
        if not acquired:
            raise RuntimeError("The selected host is busy.")
        record = json.loads(checked_file(layout.record, 12 * 1024 * 1024))
        if (
            record.get("schema") != SCHEMA
            or record.get("revision") != revision
            or record.get("profile") != str(layout.profile)
            or record.get("record") != str(layout.record)
        ):
            raise ValueError("The recovery record does not match the selected host.")
        if (
            record.get("binding") != layout.binding.descriptor()
            or record.get("api_port") != layout.api_port
        ):
            raise ValueError("The daemon binding changed before recovery.")
        plan_keys = {
            "schema",
            "binding",
            "profile",
            "host",
            "host_version",
            "api_port",
            "record",
            "workspace_existed",
            "before",
            "expected",
        }
        if (
            set(record) != plan_keys | {"revision", "status", "capture", "written"}
            or set(record["before"]) != set(files(layout))
            or set(record["expected"]) != set(files(layout))
        ):
            raise ValueError("The recovery record is incomplete.")
        planned = {k: record[k] for k in plan_keys}
        if (
            digest(json.dumps(planned, sort_keys=True).encode()) != revision
            or not isinstance(record["written"], list)
            or any(k not in files(layout) for k in record["written"])
        ):
            raise ValueError("The recovery record failed its integrity check.")
        if record["status"] == "rolled_back":
            return {"status": "rolled_back"}
        _restore(layout, record, controller)
        return {"status": "rolled_back"}
