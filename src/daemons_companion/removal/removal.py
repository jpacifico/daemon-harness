"""Receipt-bound removal. Never erase a vault or a host profile.

Hermes Agent installations can be uninstalled or keep Hermes without the
daemon. An OpenClaw daemon created by the app has its own private OpenClaw,
so it is only uninstalled whole: services, programs and its saved credentials.

Native transports: macOS launchd and Windows per-user scheduled tasks.
No host program or installed daemon module is imported by this controller.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time

import yaml

from daemons_companion.runtime.services import (
    AFTER_TURN_JOB,
    NIGHTLY_JOB,
    EMBEDDINGS_JOB,
    HERMES_GATEWAY_JOB,
    OPENCLAW_API_JOB,
    OPENCLAW_GATEWAY_JOB,
    MacLaunchdSchedulerBackend,
    scoped_job_code,
)
from daemons_companion.hermes.profile import (
    HERMES_CONFIG_VALUES,
    _ENV_KEYS,
    _patch_env,
    _read_env_values,
    _mapping_value,
    _MISSING,
    _write_private_bytes,
    _copy_descriptor,
)
from daemons_companion.runtime.installation_scope import InstallationScope
from daemon_core.memory.integrity import memory_file_lock
from daemons_companion.removal.errors import Refused, failure_details

MARKER = "companion-removal.json"
SCHEMA = "daemon.companion-removal.v1"
PROGRAMS = (
    "source",
    "python",
    "codex",
    "embeddings",
    "uv-cache",
    "cache",
    "temp",
    "probe-profile",
    "daemon/data/runtime",
)
# Programs that serve only the daemon, removed even when Hermes stays.
DAEMON_ONLY_PROGRAMS = ("embeddings",)
# A private OpenClaw installation: the host, its Node runtime and caches.
OPENCLAW_PROGRAMS = (
    "host",
    "node",
    "npm-cache",
    "tools",
    "cache",
    "temp",
    "embeddings",
    "daemon/data/runtime",
)
PLUGINS = ("daemon_core", "daemon_voice")
LIMIT = 2 * 1024 * 1024
STOP_CHECKS = 20
STOP_WAIT_SECONDS = 0.5


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def direct(path):
    from daemons_companion.removal.windows import logical

    path = logical(path)
    if (
        not path.is_absolute()
        or path.is_symlink()
        or getattr(path, "is_junction", lambda: False)()
        or path.resolve() != path
    ):
        raise Refused("unsafe")
    return path


def read(path, maximum=LIMIT):
    direct(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise Refused("unsafe")
    return path.read_bytes()


def document(path):
    return json.loads(read(path))


def removable_tree(path):
    count = 0
    for root, directories, files in os.walk(path, followlinks=False):
        count += len(directories) + len(files)
        if count > 300000 or os.path.ismount(root):
            raise Refused("unsafe")
        for name in list(directories):
            child = Path(root) / name
            if child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                directories.remove(name)
            elif os.path.ismount(child):
                raise Refused("unsafe")


def save(path, value):
    direct(path)
    _write_private_bytes(path, encoded(value) + b"\n", 0o600)


def definition(capture):
    return (
        base64.b64decode(json.loads(capture)["definition"], validate=True)
        if capture
        else None
    )


def yaml_restore(content, before):
    """Restore owned scalar values only while preserving other values/comments."""
    text = content.decode("utf-8")
    if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):
        raise Refused("configuration")
    parsed = yaml.safe_load(text)
    old = yaml.safe_load(before) or {}
    if not isinstance(parsed, dict) or not isinstance(old, dict):
        raise Refused("configuration")
    nodes = {}

    def walk(node, parts=()):
        if not isinstance(node, yaml.MappingNode):
            return
        seen = set()
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode) or key.value in seen:
                raise Refused("configuration")
            seen.add(key.value)
            name = (*parts, key.value)
            nodes[name] = (key, value)
            walk(value, name)

    walk(yaml.compose(text))
    changes = []

    def patch(parts, value):
        if parts not in nodes:
            raise Refused("configuration")
        key, node = nodes[parts]
        if value is _MISSING:
            # Only installation-owned scalar leaves are removed. Keep containers.
            if not isinstance(node, yaml.ScalarNode):
                raise Refused("configuration")
            start = text.rfind("\n", 0, key.start_mark.index) + 1
            end = text.find("\n", node.end_mark.index)
            changes.append((start, len(text) if end < 0 else end + 1, ""))
        else:
            start, end = node.start_mark.index, node.end_mark.index
            tail = (
                re.search(r"\s*$", text[start:end]).group()
                if "\n" in text[start:end]
                else ""
            )
            changes.append((start, end, json.dumps(value, ensure_ascii=False) + tail))

    for dotted, installed in HERMES_CONFIG_VALUES:
        value = _mapping_value(parsed, dotted)
        if value == installed and type(value) is type(installed):
            patch(tuple(dotted.split(".")), _mapping_value(old, dotted))
    plugins = parsed.get("plugins", {})
    if not isinstance(plugins, dict):
        raise Refused("configuration")
    for key in ("enabled", "disabled"):
        values = plugins.get(key)
        if values is not None:
            if not isinstance(values, list) or not all(
                isinstance(v, str) for v in values
            ):
                raise Refused("configuration")
            patch(("plugins", key), [v for v in values if v not in PLUGINS])
    for start, end, value in sorted(changes, reverse=True):
        text = text[:start] + value + text[end:]
    result = yaml.safe_load(text)
    # Deleting all owned leaves must not turn a native mapping into YAML null.
    # Preserve the container type so Hermes can merge its normal defaults.
    for parts, (_, original) in nodes.items():
        if (
            isinstance(original, yaml.MappingNode)
            and _mapping_value(result, ".".join(parts)) is None
        ):
            fresh = {}

            def collect(node, prefix=()):
                if isinstance(node, yaml.MappingNode):
                    for key, value in node.value:
                        fresh[(*prefix, key.value)] = value
                        collect(value, (*prefix, key.value))

            collect(yaml.compose(text))
            node = fresh[parts]
            text = text[: node.start_mark.index] + " {}" + text[node.end_mark.index :]
            result = yaml.safe_load(text)
    if (
        not isinstance(result, dict)
        or _mapping_value(result, "memory.provider") == "daemon_core"
    ):
        raise Refused("configuration")
    return text.encode()


def env_restore(content, before, expected, relocation=None):
    # Reconstruct the install-time output with just the current owned values.
    # This allows unrelated later changes, but refuses changed Daemons pointers.
    def values(raw):
        result = {}
        for line in raw.decode().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                key = key.strip()
                if key in result:
                    raise Refused("configuration")
                result[key] = value
        return result

    current, old = values(content), values(before)
    managed = {key: current[key] for key in _ENV_KEYS if key in current}
    comparison = dict(managed)
    if relocation:
        previous_release, current_release = relocation
        for key in ("DAEMON_CORE_SRC", "DAEMON_CORE_PYTHON"):
            if key in comparison:
                value = Path(comparison[key])
                if not value.is_relative_to(
                    current_release.parents[1]
                ) or not value.resolve().is_relative_to(current_release.parents[1]):
                    raise Refused("configuration")
                comparison[key] = str(value).replace(
                    current_release.name, previous_release.name
                )
    if digest(_patch_env(before, comparison)) != expected:
        raise Refused("configuration")
    lines = []
    for line in content.decode().splitlines(keepends=True):
        key = line.split("=", 1)[0].strip()
        if key not in managed or line.lstrip().startswith("#"):
            lines.append(line)
        elif key in old:
            lines.append(
                key + "=" + old[key] + ("\r\n" if line.endswith("\r\n") else "\n")
            )
    return "".join(lines).encode()


def _blocking(label):
    """The launchd label that still uses this daemon's files, for the diagnostic."""
    from daemons_companion.removal.windows import task_reference

    return {"stage": "dependency_scan", "blocking_task": task_reference(label)}


class Removal:
    def __init__(
        self, profile, base, launch_agents, *, scheduler=None, hook=lambda _: None
    ):
        self.profile, self.base = direct(Path(profile)), direct(Path(base))
        self.scope = InstallationScope(self.profile.parent)
        self.attempt = self.scope.attempt
        relative = self.attempt.relative_to(self.base).parts
        prefix = relative[:-4]
        instance = (
            len(prefix) == 2
            and prefix[0] == "Companion"
            and re.fullmatch(r"instance-[A-Za-z0-9]{6}", prefix[1]) is not None
        )
        self.openclaw = relative[-4:-1] == ("hosts", "openclaw", "installations")
        self.instance = instance
        # An OpenClaw added to a Hermes daemon (Companion/private) is removed
        # with that daemon, never on its own.
        if self.openclaw:
            if not instance:
                raise Refused("ownership")
        elif relative[-4:-1] != ("hosts", "hermes", "installations") or (
            prefix not in ((), ("Companion", "private")) and not instance
        ):
            raise Refused("ownership")
        if self.profile != self.scope.profile:
            raise Refused("ownership")
        self.launch_agents = direct(Path(launch_agents))
        if scheduler is None and sys.platform == "win32":
            from daemons_companion.removal.windows import WindowsRemovalScheduler

            scheduler = WindowsRemovalScheduler(
                job_namespace=self.scope.installation_id
            )
        self.scheduler = scheduler or MacLaunchdSchedulerBackend(
            self.launch_agents,
            user_id=os.getuid(),
            job_namespace=self.scope.installation_id,
        )
        self.hook = hook
        self.marker = self.attempt / MARKER
        # The gateway comes first and the nightly job second (see service_snapshot).
        self.codes = [
            scoped_job_code(code, self.scope.installation_id)
            for code in (
                (OPENCLAW_GATEWAY_JOB, NIGHTLY_JOB, AFTER_TURN_JOB, EMBEDDINGS_JOB, OPENCLAW_API_JOB)
                if self.openclaw
                else (HERMES_GATEWAY_JOB, NIGHTLY_JOB, AFTER_TURN_JOB, EMBEDDINGS_JOB)
            )
        ]
        self.programs = OPENCLAW_PROGRAMS if self.openclaw else PROGRAMS

    def ownership(self):
        self.scope.validate_profile(self.profile)
        receipt = document(self.attempt / "daemon-setup.json")
        transaction = receipt.get("transaction_id", "")
        if receipt.get("schema") != "daemon.private-setup.v1" or not re.fullmatch(
            r"[0-9a-f]{32}", transaction
        ):
            raise Refused("ownership")
        root = self.scope.home / "transactions" / transaction
        journal = document(root / "journal.json")
        if journal.get("status") != "committed":
            raise Refused("ownership")
        return None if self.openclaw else root / "snapshots" / "hermes-profile"

    def status(self):
        if not self.marker.exists() and not self.marker.is_symlink():
            return None
        data = document(self.marker)
        if (
            data.get("schema") != SCHEMA
            or data.get("profile") != str(self.profile)
            or data.get("action") not in ("detach", "uninstall")
            or data.get("status")
            not in (
                "prepared",
                "applying",
                "cleaning",
                "done",
                "rolled_back",
                "recovery",
            )
        ):
            raise Refused("recovery")
        self.validate_record(data)
        return data

    def validate_record(self, data):
        immutable = dict(data)
        revision = immutable.pop("revision", None)
        immutable["status"] = "prepared"
        if revision != digest(encoded(immutable)):
            raise Refused("recovery")
        memory = direct(Path(data["memory"]))
        if (
            not memory.is_dir()
            or set(data["changes"]) - {"config.yaml", ".env"}
            or set(data["links"]) - set(PLUGINS)
            or set(data.get("copies", {})) - set(PLUGINS)
        ):
            raise Refused("unsafe")
        if set(data["services"]) != set(self.codes) or any(
            p not in self.programs for p in data["programs"]
        ):
            raise Refused("unsafe")
        if self.openclaw and (data["changes"] or data["links"] or data["action"] != "uninstall"):
            raise Refused("unsafe")
        for target in data["links"].values():
            if not Path(target).is_relative_to(self.scope.home / "data/runtime"):
                raise Refused("unsafe")
        for relative in self.programs:
            folder = direct(self.attempt / relative)
            if memory.is_relative_to(folder) or folder.is_relative_to(memory):
                raise Refused("memory")

    def service_snapshot(self, memory, added=()):
        """Capture this daemon's jobs; only it and the hosts added to it may use its files."""
        allowed = list(self.codes) + [
            scoped_job_code(code, host["installation_id"])
            for host in added
            for code in (OPENCLAW_API_JOB, OPENCLAW_GATEWAY_JOB)
        ]
        captures = {}
        for code in self.codes:
            try:
                captures[code] = self.scheduler.capture(code)
            except Exception:
                raise Refused("services", {"stage": "task_capture"}) from None
        if getattr(self.scheduler, "target_os", None) == "windows":
            from daemons_companion.removal.windows import validate_task
            from daemon_core.platform_support import _user_sid

            try:
                self.scheduler.require_exclusive(self.attempt, memory, allowed)
                sid = _user_sid()
                for capture in captures.values():
                    if capture is not None:
                        validate_task(definition(capture), self.attempt, sid)
            except Refused:
                raise
            except ValueError as error:
                raise Refused(str(error), {"stage": "dependency_scan"}) from None
            return captures
        # All definitions that reference this installation or vault must belong
        # to its namespace. A shared API/worker or a second host blocks removal.
        for file in self.launch_agents.glob("*.plist"):
            raw = read(file)
            try:
                data = plistlib.loads(raw)
            except Exception:
                if str(self.attempt).encode() in raw or str(memory).encode() in raw:
                    raise Refused("shared", _blocking(file.stem))
                continue
            rendered = repr(data)
            if (str(self.attempt) in rendered or str(memory) in rendered) and data.get(
                "Label"
            ) not in allowed:
                raise Refused("shared", _blocking(str(data.get("Label") or file.stem)))
        for code, capture in captures.items():
            if capture is None:
                continue
            raw = definition(capture)
            if raw is None:
                raise Refused("services")
            data = plistlib.loads(raw)
            args, cwd = data.get("ProgramArguments"), data.get("WorkingDirectory")
            # The canonical daily job wraps its private interpreter in the
            # macOS power assertion utility. Validate the wrapped executable.
            if (
                code == self.codes[1]
                and isinstance(args, list)
                and args[:2] == ["/usr/bin/caffeinate", "-s"]
            ):
                args = args[2:]
            if (
                data.get("Label") != code
                or not isinstance(args, list)
                or not args
                or not all(isinstance(v, str) for v in args)
                or not Path(args[0]).is_relative_to(self.attempt)
                or not isinstance(cwd, str)
                or not Path(cwd).is_relative_to(self.attempt)
                or not Path(args[0]).resolve().is_relative_to(self.attempt)
                or data.get("UserName")
                or data.get("GroupName")
            ):
                raise Refused("services")
        return captures

    def require_stopped(self, root=None):
        root = root or self.attempt
        if getattr(self.scheduler, "target_os", None) == "windows":
            try:
                self.scheduler.require_stopped(root)
            except ValueError:
                raise Refused("services") from None
            return
        # launchd registrations are gone; refuse a manually launched process or
        # a child that escaped supervision rather than deleting its executable.
        # Stopped jobs get a few seconds to exit (an OpenClaw gateway closes its
        # Telegram connection and its workers first).
        for attempt in range(STOP_CHECKS):
            if attempt:
                time.sleep(STOP_WAIT_SECONDS)
            result = subprocess.run(
                ["/bin/ps", "-axo", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            if not any(str(root) in line for line in result.stdout.splitlines()):
                return
        raise Refused("services")

    def inspect(self, action):
        from daemons_companion.runtime.upgrade import require_settled_upgrade

        require_settled_upgrade(self.scope.home)
        if action not in ("detach", "uninstall") or (self.openclaw and action != "uninstall"):
            raise Refused("request")
        snapshot = self.ownership()
        status = self.status()
        if status and status["status"] not in ("done", "rolled_back"):
            if action != status["action"]:
                raise Refused("recovery")
            # Recovery is bound to the saved exact plan, never a new destination.
            return self.public(status, recovery=True)
        detached = bool(
            status
            and (
                (status["status"] == "done" and status["action"] == "detach")
                or (
                    status["status"] == "rolled_back"
                    and status.get("detached_before") is True
                )
            )
        )
        if (
            status
            and status["status"] == "done"
            and (status["action"] == "uninstall" or action == "detach")
        ):
            raise Refused("removed")
        if self.openclaw:
            return self.inspect_openclaw()
        config = read(self.profile / "config.yaml")
        env = read(self.profile / ".env")
        memory = (
            Path(status["memory"])
            if detached
            else Path(
                _read_env_values(self.profile / ".env").get(
                    "DAEMON_CORE_MEMORY_ROOT", ""
                )
            )
        )
        memory = direct(memory)
        if memory.name == ".daemon":
            memory = memory.parent
        if not memory.is_dir():
            raise Refused("memory")
        manifest = document(snapshot / "manifest.json")
        changes, links, copies = {}, {}, {}
        relocation = None
        if not detached:
            original = Path(manifest["expected"]["memory_plugin"].get("target", ""))
            current_link = self.profile / "plugins/daemon_core"
            if current_link.is_symlink() and current_link.resolve() != original:
                from daemons_companion.runtime.release import (
                    CompanionRuntimeLayout,
                    verify_current_release,
                )

                try:
                    current_release = verify_current_release(
                        CompanionRuntimeLayout(self.scope.home, self.base, self.scope)
                    )
                except Exception:
                    raise Refused("configuration") from None
                old_release = original.parents[1]
                if (
                    old_release.parent != self.scope.home / "data/runtime/releases"
                    or current_link.resolve()
                    != current_release / "integrations/hermes_plugin"
                ):
                    raise Refused("configuration")
                relocation = (old_release, current_release)
        if not detached:
            if manifest.get("schema") != "daemon.hermes-profile-snapshot.v1":
                raise Refused("ownership")
            for key, filename in (("config", "config.yaml"), ("env", ".env")):
                old = (
                    read(snapshot / (key + ".before"))
                    if manifest["before"][key]["kind"] == "file"
                    else b""
                )
                if old and digest(old) != manifest["before"][key]["sha256"]:
                    raise Refused("ownership")
                raw = config if key == "config" else env
                new = (
                    yaml_restore(raw, old)
                    if key == "config"
                    else env_restore(
                        raw, old, manifest["expected"]["env"]["sha256"], relocation
                    )
                )
                changes[filename] = {
                    "before": base64.b64encode(raw).decode(),
                    "after": base64.b64encode(new).decode(),
                }
            for plugin, key in zip(PLUGINS, ("memory_plugin", "voice_plugin")):
                link = self.profile / "plugins" / plugin
                expected = dict(manifest["expected"][key])
                if relocation and expected.get("kind") == "symlink":
                    old_release, current_release = relocation
                    old_target = Path(expected["target"])
                    if not old_target.is_relative_to(old_release):
                        raise Refused("configuration")
                    expected["target"] = str(
                        current_release / old_target.relative_to(old_release)
                    )
                if expected["kind"] == "copy":
                    actual = _copy_descriptor(direct(link))
                    # Only derived Python bytecode may have appeared since install.
                    expected_files = expected["files"]
                    for name, value in actual["files"].items():
                        if name not in expected_files and not (
                            "__pycache__" in Path(name).parts
                            and (value == "directory" or name.endswith(".pyc"))
                        ):
                            raise Refused("configuration")
                    if any(
                        actual["files"].get(k) != v for k, v in expected_files.items()
                    ):
                        raise Refused("configuration")
                    backup = self.copy_backup(plugin)
                    if backup.exists() or backup.is_symlink():
                        raise Refused("recovery")
                    copies[plugin] = actual
                    continue
                if (
                    expected["kind"] != "symlink"
                    or not link.is_symlink()
                    or os.readlink(link) != expected["target"]
                    or not Path(expected["target"]).is_relative_to(
                        self.scope.home / "data/runtime"
                    )
                    or link.resolve() != Path(expected["target"])
                ):
                    raise Refused("configuration")
                direct(link.parent)
                links[plugin] = expected["target"]
        cron = self.profile / "cron/jobs.json"
        if action == "detach" and (cron.exists() or cron.is_symlink()):
            raw = read(cron).decode()
            if any(
                token in raw
                for token in ("daemon_core", "followup_", str(self.scope.home))
            ):
                raise Refused("followups")
        added = self.added_hosts()
        captures = self.service_snapshot(memory, added)
        identity = memory / ".daemon/core/daemon_identity.json"
        if not identity.exists():
            identity = memory / "core/daemon_identity.json"
        name = document(identity).get("name", "") if identity.exists() else ""
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 80
            or any(ord(c) < 32 for c in name)
        ):
            name = "Hermes " + self.scope.installation_id[:8]
        programs = []
        # Removing only the daemon keeps Hermes; the memory search model and its
        # server serve the daemon alone, so they go too (about 640 MB).
        for relative in PROGRAMS if action == "uninstall" else DAEMON_ONLY_PROGRAMS:
            folder = direct(self.attempt / relative)
            if memory.is_relative_to(folder) or folder.is_relative_to(memory):
                raise Refused("memory")
            if folder.exists():
                if not folder.is_dir():
                    raise Refused("unsafe")
                removable_tree(folder)
                programs.append(relative)
        data = {
            "schema": SCHEMA,
            "profile": str(self.profile),
            "action": action,
            "memory": str(memory),
            "name": name,
            "status": "prepared",
            "detached_before": detached,
            "changes": changes,
            "links": links,
            "programs": programs,
            "config_digest": digest(config),
            "env_digest": digest(env),
            "program_roots": {
                p: [
                    os.lstat(self.attempt / p).st_dev,
                    os.lstat(self.attempt / p).st_ino,
                ]
                for p in programs
            },
            "services": {
                k: base64.b64encode(v).decode() if v else None
                for k, v in captures.items()
            },
        }
        if copies:
            data["copies"] = copies
        if added:
            data["added_hosts"] = added
        data["revision"] = digest(encoded(data))
        self.validate_record(data)
        self.prepared = data
        return self.public(data)

    def inspect_openclaw(self):
        """Review the whole uninstall of an OpenClaw daemon; its vault stays."""
        config = read(self.profile / "openclaw.json")
        memory = direct(self.scope.home / "data/memory")
        if not memory.is_dir():
            raise Refused("memory")
        captures = self.service_snapshot(memory)
        identity = memory / ".daemon/core/daemon_identity.json"
        name = document(identity).get("name", "") if identity.exists() else ""
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 80
            or any(ord(c) < 32 for c in name)
        ):
            name = "OpenClaw " + self.scope.installation_id[:8]
        programs = []
        for relative in self.programs:
            folder = direct(self.attempt / relative)
            if memory.is_relative_to(folder) or folder.is_relative_to(memory):
                raise Refused("memory")
            if folder.exists():
                if not folder.is_dir():
                    raise Refused("unsafe")
                removable_tree(folder)
                programs.append(relative)
        # The API key and the bot and gateway tokens are useless without the
        # programs; they are deleted from the system credential store.
        credentials = []
        connection = self.attempt / "openclaw-connection.json"
        if connection.exists():
            credentials.append(document(connection).get("credential"))
        provider = self.scope.home / "data/state/config/provider.json"
        if provider.exists():
            credentials.append((document(provider).get("credential") or {}).get("reference"))
        credentials = [
            c for c in credentials
            if isinstance(c, str) and re.fullmatch(r"provider-[0-9a-f-]{36}", c)
        ]
        data = {
            "schema": SCHEMA,
            "profile": str(self.profile),
            "action": "uninstall",
            "memory": str(memory),
            "name": name,
            "status": "prepared",
            "detached_before": False,
            "changes": {},
            "links": {},
            "programs": programs,
            "credentials": credentials,
            "config_digest": digest(config),
            "env_digest": digest(b""),
            "program_roots": {
                p: [os.lstat(self.attempt / p).st_dev, os.lstat(self.attempt / p).st_ino]
                for p in programs
            },
            "services": {
                k: base64.b64encode(v).decode() if v else None
                for k, v in captures.items()
            },
        }
        data["revision"] = digest(encoded(data))
        self.validate_record(data)
        self.prepared = data
        return self.public(data)

    def remove_credentials(self, data):
        from daemons_companion.setup.credentials import system_credential_store

        for reference in data.get("credentials", []):
            try:
                system_credential_store().delete(reference)
            except Exception:
                pass  # A missing or locked entry never blocks the uninstall.
            self.hook("credential")

    def public(self, data, recovery=False):
        return {
            key: data[key]
            for key in ("profile", "action", "memory", "name", "revision")
        } | {
            "recovery": recovery,
            "retainedProfile": str(self.profile),
            "retainedSignIns": str(self.attempt / "os-home"),
            "programs": [str(self.attempt / p) for p in data["programs"]],
            "services": [
                code
                for code, capture in data["services"].items()
                if capture and (data["action"] == "uninstall" or code != self.codes[0])
            ],
            "done": data["status"] == "done",
            "addedHosts": len(data.get("added_hosts", [])),
        } | self.memory_offer(data)

    def owned_memory(self, memory):
        """Only the vault Companion created inside this daemon's own folder."""
        return (
            self.instance
            and memory == direct(self.scope.home / "data/memory")
            and not os.path.ismount(memory)
        )

    def memory_offer(self, data):
        memory = direct(Path(data["memory"]))
        if data["action"] != "uninstall" or not self.owned_memory(memory):
            return {"memoryDeletable": False}
        files = size = 0
        for parent, _directories, names in os.walk(memory, followlinks=False):
            for name in names:
                files += 1
                size += os.lstat(os.path.join(parent, name)).st_size
        return {"memoryDeletable": True, "memoryFiles": files, "memoryBytes": size}

    def memory_check(self, revision):
        """Confirm that the uninstalled daemon's vault may go to the trash."""
        data = self.status()
        if (
            not data
            or data["action"] != "uninstall"
            or data["status"] != "done"
            or data["revision"] != revision
        ):
            raise Refused("changed")
        memory = direct(Path(data["memory"]))
        if not self.owned_memory(memory):
            raise Refused("memory")
        # Nothing may still use it: none of this daemon's jobs, and no other
        # definition naming it (service_snapshot refuses those as shared).
        if any(self.service_snapshot(memory).values()):
            raise Refused("services")
        return {"memory": str(memory), "revision": revision}

    def apply(self, action, revision, name):
        with memory_file_lock(self.attempt.parent.parent / "setup-owner"):
            with memory_file_lock(self.scope.home / "install-owner"):
                review = self.inspect(action)
                if review["revision"] != revision or review["name"] != name:
                    raise Refused("changed")
                if review["recovery"]:
                    data = self.status()
                    if data["status"] != "cleaning":
                        self.rollback(data)
                        raise Refused("restored")
                else:
                    data = self.prepared
                    try:
                        self.detach_added(data)
                    except BaseException as error:
                        raise Refused("attached", failure_details(error, "detach_host")) from None
                    save(self.marker, data)
                    data["status"] = "applying"
                    save(self.marker, data)
                    stage = "stop_services"
                    try:
                        for code, capture in data["services"].items():
                            current = self.scheduler.capture(code)
                            if definition(current) != definition(
                                base64.b64decode(capture) if capture else None
                            ):
                                raise Refused("changed")
                            self.scheduler.restore(code, None)
                            self.hook("service:" + code)
                        stage = "stop_processes"
                        self.require_stopped()
                        stage = "restore_config"
                        # Stop writers before replacing settings or unlinking plugins.
                        for filename, change in data["changes"].items():
                            target = self.profile / filename
                            if read(target) != base64.b64decode(change["before"]):
                                raise Refused("changed")
                            _write_private_bytes(
                                target, base64.b64decode(change["after"]), 0o600
                            )
                            self.hook(filename)
                        stage = "remove_plugins"
                        for plugin, target in data["links"].items():
                            link = self.profile / "plugins" / plugin
                            if not link.is_symlink() or os.readlink(link) != target:
                                raise Refused("changed")
                            link.unlink()
                            self.hook(plugin)
                        for plugin, expected in data.get("copies", {}).items():
                            source = direct(self.profile / "plugins" / plugin)
                            if _copy_descriptor(source) != expected:
                                raise Refused("changed")
                            backup = self.copy_backup(plugin)
                            backup.parent.mkdir(exist_ok=True)
                            direct(backup.parent)
                            if backup.exists():
                                raise Refused("changed")
                            source.rename(backup)
                            self.hook(plugin)
                        stage = "restart_host" if action == "detach" else "save_state"
                        if action == "detach":
                            gateway = data["services"][self.codes[0]]
                            self.scheduler.restore(
                                self.codes[0],
                                base64.b64decode(gateway) if gateway else None,
                            )
                            self.hook("gateway-restored")
                        if action == "uninstall" or data["programs"]:
                            data["status"] = "cleaning"
                            save(self.marker, data)
                    except BaseException as error:
                        self.rollback(data)
                        raise Refused("rolled_back", failure_details(error, stage)) from None
                if action == "uninstall" or data["programs"]:
                    # Retain profile, credentials, conversations, memory, diagnostic
                    # state and install snapshots. Never recurse over the attempt.
                    # Removing only the daemon keeps Hermes and its Gateway running.
                    stage = "cleanup_services"
                    try:
                        if action == "uninstall":
                            self.service_snapshot(Path(data["memory"]))
                        kept = self.codes[:1] if action == "detach" else []
                        if any(self.scheduler.capture(code) for code in self.codes if code not in kept):
                            raise Refused("services")
                        stage = "cleanup_processes"
                        if action == "uninstall":
                            self.require_stopped()
                        else:
                            for relative in data["programs"]:
                                self.require_stopped(self.attempt / relative)
                        for relative in data["programs"]:
                            stage = "program_identity"
                            if relative not in self.programs:
                                raise Refused("unsafe")
                            folder = direct(self.attempt / relative)
                            if folder.exists():
                                info = folder.stat()
                                if [info.st_dev, info.st_ino] != data["program_roots"][
                                    relative
                                ] or os.path.ismount(folder):
                                    raise Refused("changed")
                                stage = "program_delete"
                                if (
                                    getattr(self.scheduler, "target_os", None)
                                    == "windows"
                                ):
                                    from daemons_companion.removal.windows import (
                                        remove_tree,
                                    )

                                    remove_tree(folder, data["program_roots"][relative])
                                else:
                                    if not shutil.rmtree.avoids_symlink_attacks:
                                        raise Refused("unsafe")
                                    shutil.rmtree(
                                        folder
                                    )  # fd-safe; never follows links
                            self.hook("program:" + relative)
                        if action == "uninstall":
                            stage = "remove_added_hosts"
                            self.remove_added(data)
                            stage = "remove_credentials"
                            self.remove_credentials(data)
                    except BaseException as error:
                        save(
                            self.marker, data
                        )  # explicit resumable cleanup; no false rollback
                        raise Refused("recovery", failure_details(error, stage)) from None
                data["status"] = "done"
                save(self.marker, data)
                return self.public(data)

    def added_hosts(self):
        """OpenClaw hosts that Add a host created for this daemon, and their association."""
        from daemons_companion.hosts.attachment import host_reference

        root = self.base / "Companion/private/host-setups"
        installations = self.base / "Companion/private/hosts/openclaw/installations"
        hosts = []
        for path in sorted(root.glob("*.json")) if root.is_dir() else ():
            if not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
                continue
            setup = document(path)
            if setup.get("source") != str(self.profile):
                continue
            profile = Path(setup.get("profile", ""))
            if profile != installations / path.stem / "profile":
                raise Refused("ownership")
            record = self.scope.home / "data/state" / (
                "host-attachment-" + host_reference(profile) + ".json"
            )
            association = json.loads(read(record, 12 * 1024 * 1024)) if record.exists() else {}
            attempt = profile.parent
            hosts.append({
                "installation_id": path.stem,
                "setup": str(path),
                "profile": str(profile),
                "credential": setup.get("credential"),
                "association": association.get("status"),
                "association_revision": association.get("revision"),
                "api_port": association.get("api_port"),
                "root": [os.lstat(attempt).st_dev, os.lstat(attempt).st_ino] if attempt.is_dir() else None,
            })
        return hosts

    def detach_added(self, data):
        """Stop every host added to this daemon before its own removal.

        A recorded association is undone first, which also restores the host's
        configuration. An association written by an earlier version may not match
        today's definitions; its services are then removed by ownership instead.
        """
        from daemons_companion.hosts.attachment import rollback
        from daemons_companion.hosts.attachment_command import layouts

        for host in data.get("added_hosts", []):
            if host["association"] not in (None, "rolled_back"):
                try:
                    layout, controller = layouts({
                        "source_profile": str(self.profile),
                        "target_profile": host["profile"],
                        "base": str(self.base),
                        "api_port": host["api_port"],
                    }, undo=True)
                    rollback(layout, host["association_revision"], controller)
                    self.hook("detached:" + host["installation_id"])
                except Exception:
                    self.hook("detach-by-ownership:" + host["installation_id"])
            self.stop_host_services(host)

    def host_scheduler(self, namespace):
        if getattr(self.scheduler, "target_os", None) == "windows":
            from daemons_companion.removal.windows import WindowsRemovalScheduler

            return WindowsRemovalScheduler(job_namespace=namespace)
        if isinstance(self.scheduler, MacLaunchdSchedulerBackend):
            return MacLaunchdSchedulerBackend(
                self.launch_agents,
                user_id=self.scheduler.user_id,
                job_namespace=namespace,
                runner=self.scheduler._runner,
            )
        return self.scheduler

    def stop_host_services(self, host):
        """Remove an added host's jobs when they are this user's and run Companion programs."""
        scheduler = self.host_scheduler(host["installation_id"])
        for job in (OPENCLAW_API_JOB, OPENCLAW_GATEWAY_JOB):
            code = scoped_job_code(job, host["installation_id"])
            capture = scheduler.capture(code)
            if capture is None:
                continue
            raw = definition(capture)
            if getattr(self.scheduler, "target_os", None) == "windows":
                from daemons_companion.removal.windows import validate_task
                from daemon_core.platform_support import _user_sid

                validate_task(raw, self.base, _user_sid())
            else:
                value = plistlib.loads(raw)
                program = (value.get("ProgramArguments") or [""])[0]
                if (
                    value.get("Label") != code
                    or not isinstance(program, str)
                    or not Path(program).is_absolute()
                    or not Path(program).resolve().is_relative_to(self.base.resolve())
                ):
                    raise Refused("unsafe")
            scheduler.restore(code, None)
            self.hook("host-service:" + code)
        if any(
            scheduler.capture(scoped_job_code(job, host["installation_id"]))
            for job in (OPENCLAW_API_JOB, OPENCLAW_GATEWAY_JOB)
        ):
            raise Refused("services")

    def remove_added(self, data):
        """Uninstall the hosts created only for this daemon; its memory stays in place."""
        from daemons_companion.setup.credentials import system_credential_store

        for host in data.get("added_hosts", []):
            attempt = direct(Path(host["profile"]).parent)
            scheduler = self.host_scheduler(host["installation_id"])
            codes = [scoped_job_code(c, host["installation_id"]) for c in (OPENCLAW_API_JOB, OPENCLAW_GATEWAY_JOB)]
            if any(scheduler.capture(code) for code in codes):
                raise Refused("services")
            if attempt.exists():
                info = attempt.stat()
                if [info.st_dev, info.st_ino] != host["root"] or os.path.ismount(attempt):
                    raise Refused("changed")
                if getattr(self.scheduler, "target_os", None) == "windows":
                    from daemons_companion.removal.windows import remove_tree

                    remove_tree(attempt, host["root"])
                else:
                    if not shutil.rmtree.avoids_symlink_attacks:
                        raise Refused("unsafe")
                    shutil.rmtree(attempt)
            if host["credential"]:
                try:
                    system_credential_store().delete(host["credential"])
                except Exception:
                    pass  # A missing or locked entry never blocks removal of the programs.
            Path(host["setup"]).unlink(missing_ok=True)
            self.hook("added-host:" + host["installation_id"])

    def copy_backup(self, plugin):
        if plugin not in PLUGINS:
            raise Refused("unsafe")
        return direct(self.scope.home / "data/runtime/removal-plugins" / plugin)

    def rollback(self, data):
        try:
            for code, capture in data["services"].items():
                current = self.scheduler.capture(code)
                original = base64.b64decode(capture) if capture else None
                if current is not None and definition(current) != definition(original):
                    raise Refused("changed")
                self.scheduler.restore(code, None)
            self.require_stopped()
            for filename, change in data["changes"].items():
                target = direct(self.profile / filename)
                before, after = (
                    base64.b64decode(change[k]) for k in ("before", "after")
                )
                if read(target) not in (before, after):
                    raise Refused("changed")
                _write_private_bytes(target, before, 0o600)
            for plugin, target in data["links"].items():
                link = self.profile / "plugins" / plugin
                direct(link.parent)
                if link.is_symlink():
                    if os.readlink(link) != target:
                        raise Refused("changed")
                elif link.exists():
                    raise Refused("changed")
                else:
                    link.symlink_to(target)
            for plugin, expected in data.get("copies", {}).items():
                source = direct(self.profile / "plugins" / plugin)
                backup = self.copy_backup(plugin)
                if source.exists():
                    if backup.exists() or _copy_descriptor(source) != expected:
                        raise Refused("changed")
                elif _copy_descriptor(backup) != expected:
                    raise Refused("changed")
                else:
                    backup.rename(source)
            for code, capture in reversed(list(data["services"].items())):
                self.scheduler.restore(
                    code, base64.b64decode(capture) if capture else None
                )
            data["status"] = "rolled_back"
            save(self.marker, data)
        except BaseException as error:
            data["status"] = "recovery"
            save(self.marker, data)
            raise Refused("recovery", failure_details(error, "rollback")) from None


def main():
    try:
        if sys.platform not in ("darwin", "win32"):
            raise Refused("platform")
        request = json.loads(sys.stdin.buffer.read(32769))
        if set(request) - {
            "profile",
            "base",
            "action",
            "operation",
            "revision",
            "name",
        }:
            raise Refused("request")
        removal = Removal(
            request["profile"], request["base"], Path.home() / "Library/LaunchAgents"
        )
        if request["operation"] == "inspect":
            result = removal.inspect(request["action"])
        elif request["operation"] == "apply":
            result = removal.apply(
                request["action"], request["revision"], request["name"]
            )
        elif request["operation"] == "memory-check":
            result = removal.memory_check(request["revision"])
        else:
            raise Refused("request")
        print(json.dumps({"ok": True, "result": result}))
    except Refused as error:
        print(json.dumps({"ok": False, "code": error.code, "diagnostic": error.diagnostic}))
    except Exception:
        print(json.dumps({"ok": False, "code": "unconfirmed"}))


if __name__ == "__main__":
    main()
