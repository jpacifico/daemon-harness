"""Explicit, reversible source-runtime upgrade for the host-portability candidate.

Identity, provider and memory writers are deliberately absent. The old release
is retained. Unknown dependencies or changes outside this capability are refused.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import time
import re
import uuid

from daemons_companion.setup.plan import HostKind, HostProbe, PlanStep, SetupPlan
from daemons_companion.runtime.release import (
    CompanionRuntimeAdapter,
    CompanionRuntimeLayout,
    _bundle_manifest,
    verify_current_release,
)
from daemons_companion.runtime.environment import (
    CompanionRuntimeEnvironmentAdapter,
    resolve_release_environment,
    release_environment_interpreter,
    _subprocess_runner,
)
from daemons_companion.hermes.composition import HermesCompositionLayout
from daemons_companion.hermes.profile import (
    HermesProfileAdapter,
    HermesProfileLayout,
    _patch_hermes_config,
)
from daemons_companion.hermes.ready_composition import _services_layout
from daemons_companion.runtime.services import (
    build_background_jobs,
    known_job_definitions,
    render_job_definition,
)
from daemons_companion.hermes.gateway import private_gateway_job
from daemons_companion.hosts.attachment_services import capture_matches
from daemons_companion.openclaw.host_install import checked_file, direct
from daemons_companion.setup.transaction import TransactionEngine, public_plan_digest
from daemon_core.memory.integrity import atomic_write_text, memory_file_try_lock

SCHEMA = "daemon.source-upgrade.v1"
CODES = (
    "upgrade-stop",
    "runtime-layout",
    "runtime-environment",
    "hermes-profile",
    "upgrade-start",
)
OPENCLAW_CODES = (
    "upgrade-stop",
    "runtime-layout",
    "runtime-environment",
    "openclaw-profile",
    "upgrade-start",
)
# Companion code (installation, services, host connection) may change for an
# existing daemon. Its behaviour does not: the daemon core, prompts, memory
# templates, host plugins and dependencies must stay byte-identical.
COMPANION = "src/daemons_companion/"
# Host-connection capabilities outside the Companion package.
ALLOWED = {
    "src/daemon_core/host_bindings.py",
    "integrations/openclaw_plugin/followup_cron.py",
    "integrations/openclaw_plugin/origin.py",
}
PORTABILITY = (
    "src/daemons_companion/hosts/attachment_entry.py",
    "src/daemons_companion/hosts/creation.py",
    "src/daemon_core/host_bindings.py",
)


class UpgradeRefused(ValueError):
    """A review refusal whose code names the reason in diagnostics."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def write(path, value):
    atomic_write_text(path, json.dumps(value, sort_keys=True) + "\n")


def compatible(old, target):
    """Only the approved capability changes; prompts and dependencies stay exact."""

    def entries(value):
        return {e["path"]: e for e in value["entries"] if e["kind"] == "file"}

    before, after = entries(old), entries(target)
    for name in set(before) | set(after):
        protected = (
            name.startswith(("src/", "server/", "memory/"))
            or (name.startswith("integrations/") and not name.endswith("/INSTALL.md"))
            or name.startswith(("requirements", "pyproject.toml"))
        )
        if (
            protected
            and not name.startswith(COMPANION)
            and name not in ALLOWED
            and before.get(name) != after.get(name)
        ):
            raise UpgradeRefused("protected-runtime", "This runtime requires a separately qualified upgrade.")
    if any(name not in after for name in PORTABILITY):
        raise UpgradeRefused("portability", "The target runtime lacks host portability support.")


class Upgrade:
    """The reviewed runtime update of a daemon created on Hermes Agent."""

    CODES = CODES
    profile_code = "hermes-profile"
    # Seconds the restarted services have to report ready before rollback.
    ready_seconds = 120

    def __init__(
        self, composition, host, scheduler, user, *, runner=None, probe=None, hook=None
    ):
        self.composition, self.host, self.scheduler, self.user = (
            composition,
            host,
            scheduler,
            user,
        )
        self.target_os = composition.target_os
        if host.target_os != self.target_os or scheduler.target_os != self.target_os:
            raise ValueError("Source platform and scheduler differ.")
        self.runtime = composition.runtime
        self.runner, self.probe = runner, probe or self.health
        self.hook = hook or (lambda phase: None)
        self.record_path = self.runtime.companion_home / "source-upgrade.json"
        self.command_marker = (
            self.runtime.companion_home / "source-upgrade-command.json"
        )
        self.engine = TransactionEngine(self.runtime.companion_home / "upgrades")
        self.delegates = {}
        self.record = None
        self.started_at = 0.0
        self.directories = {}

    def jobs(self):
        return (
            private_gateway_job(self.host, self.runtime, self.scheduler.job_namespace),
            *build_background_jobs(_services_layout(self.composition, self.user)),
        )

    def profile_adapter(self):
        release = verify_current_release(self.runtime)
        python = release_environment_interpreter(
            resolve_release_environment(self.composition.runtime_environment),
            self.target_os,
        )
        state = self.runtime.state_root
        return HermesProfileAdapter(
            HermesProfileLayout(
                self.host.profile, release, self.runtime.installation_scope
            ),
            selected_host=HostKind.HERMES,
            user_id=self.user,
            runtime_environment={
                "DAEMON_CORE_MEMORY_ROOT": str(self.runtime.memory_root),
                "DAEMON_CORE_STATE_ROOT": str(state),
                "DAEMON_CORE_PROVIDER_CONFIG": str(state / "config/provider.json"),
                "DAEMON_CORE_PYTHON": str(python),
                "DAEMON_CORE_LATENCY_LOG": str(state / "latency/hermes.jsonl"),
            },
        )

    def health(self):
        from daemons_companion.hermes.gateway import (
            private_gateway_status,
            _gateway_ready,
        )
        from daemons_companion.hermes.activation import _readiness_probe

        if not self._worker_restarted():
            return False
        from daemons_companion.runtime.bootstrap import _source_io_path

        spool = self.runtime.state_root / "after-turn-spool"
        path = _source_io_path if self.target_os == "windows" else lambda p: p
        return _gateway_ready(private_gateway_status(self.host)) and _readiness_probe(
            verify_current_release(self.runtime).name,
            path(self.runtime.memory_root),
            path(spool),
        )

    def _worker_restarted(self):
        """The after-turn worker beat since the services were (re)started."""
        from daemon_core.after_turn_service import after_turn_service_path

        spool = self.runtime.state_root / "after-turn-spool"
        try:
            beat = json.loads(checked_file(after_turn_service_path(spool), 65536))
            if float(beat.get("heartbeat_at", 0)) < self.started_at:
                return False
            pid = beat.get("pid")
            if type(pid) is not int or pid <= 1:
                return False
            from daemon_core.platform_support import process_is_alive

            if not process_is_alive(pid):
                return False
        except (OSError, ValueError, TypeError):
            return False
        return True

    def wait_ready(self):
        deadline = time.monotonic() + self.ready_seconds
        while not self.probe():
            if time.monotonic() >= deadline:
                raise RuntimeError("Gateway or memory worker did not become ready.")
            time.sleep(1)

    def state(self):
        if not self.record_path.exists():
            return None
        record = json.loads(checked_file(self.record_path, 262144))
        fixed = {
            k: v for k, v in record.items() if k not in {"revision", "transaction"}
        }
        if (
            not re.fullmatch(r"[a-f0-9]{32}", record.get("transaction", ""))
            or record.get("schema") != SCHEMA
            or record.get("revision") != digest(fixed)
            or record.get("profile") != str(self.host.profile)
        ):
            raise ValueError("Upgrade record changed.")
        self.record = record
        journal = self.engine.root / record["transaction"] / "journal.json"
        status = (
            self.engine.inspect(record["transaction"]).status
            if journal.exists()
            else "prepared"
        )
        return {
            "revision": record["revision"],
            "status": "command_recovery" if self.command_marker.exists() else status,
            "from": record["old"],
            "to": record["target"],
        }

    def review(self, *, fresh=False):
        pending = None if fresh else self.state()
        if pending and pending["status"] not in {"committed", "rolled_back"}:
            return pending
        self.runtime.installation_scope.validate_home(self.runtime.companion_home)
        if self.attached_hosts():
            raise UpgradeRefused(
                "attached-hosts", "Attached hosts must be handled by a coordinated upgrade."
            )
        old = verify_current_release(self.runtime)
        target = _bundle_manifest(self.runtime.bundle_root)
        old_manifest = json.loads(
            checked_file(old / ".daemon-harness-release.json", 4 * 1024 * 1024)
        )
        compatible(old_manifest, target)
        if old.name == target["digest"]:
            return {"revision": old.name, "status": "current"}
        self.check_profile()
        captures = {}
        for job in self.jobs():
            capture = self.scheduler.capture(job.code)
            if not any(
                capture_matches(capture, definition, self.target_os)
                and self.scheduler.matches(job, definition)
                for definition in known_job_definitions(job, self.target_os)
            ):
                raise UpgradeRefused("services-changed", "An owned source service is stopped or changed.")
            captures[job.code] = base64.b64encode(capture).decode()
        record = {
            "schema": SCHEMA,
            "profile": str(self.host.profile),
            "old": old.name,
            "target": target["digest"],
            "services": captures,
            "profile_state": self.profile_adapter()._descriptors(),
            "user": self.user,
            "uv": self.composition.uv_sha256,
        }
        return {
            "revision": digest(record),
            "status": "review",
            "from": old.name,
            "to": target["digest"],
            "_record": record,
        }

    def attached_hosts(self):
        """Any registered destination requires a coordinated multi-host upgrade."""
        state = self.runtime.state_root
        return any(
            any(state.glob(pattern))
            for pattern in ("host-attachment-*.json", "host-bindings/*.json")
        )

    def check_profile(self):
        """The host profile is exactly what this daemon's installation wrote."""
        try:
            self.profile_adapter().verify(
                PlanStep("hermes-profile", "Check source profile", ())
            )
        except ValueError as error:
            raise UpgradeRefused("hermes-profile", "The source Hermes profile changed.") from error
        config = checked_file(self.host.profile / "config.yaml", 1048576)
        if _patch_hermes_config(config) != config:
            raise UpgradeRefused("host-config", "Host configuration requires a separate migration.")

    def host_probe(self):
        return HostProbe(
            HostKind.HERMES,
            True,
            True,
            str(self.host.executable),
            version="owned",
            version_status="verified",
        )

    def plan(self):
        return SetupPlan(
            self.host_probe(),
            "Existing daemon",
            "unchanged",
            {"kind": "preserved"},
            tuple(PlanStep(c, c, (self.record["revision"],)) for c in self.CODES),
        )

    def run(self, revision, *, recover=False):
        with memory_file_try_lock(
            self.runtime.companion_home / "install-owner"
        ) as acquired:
            if not acquired:
                raise RuntimeError("The installation is busy.")
            if recover:
                state = self.state()
                if not state or state["revision"] != revision:
                    raise ValueError("Upgrade recovery changed.")
                if state["status"] == "command_recovery":
                    return state
                if state["status"] == "prepared":
                    # No transaction journal means no mutation was started.
                    self.record_path.unlink()
                    return {"revision": revision, "status": "rolled_back"}
            else:
                reviewed = self.review()
                if reviewed["status"] != "review" or reviewed["revision"] != revision:
                    raise ValueError("Review this runtime upgrade again.")
                self.record = reviewed["_record"] | {
                    "revision": revision,
                    "transaction": uuid.uuid4().hex,
                }
                write(self.record_path, self.record)
            plan = self.plan()
            try:
                operation = self.engine.recover if recover else self.engine.execute
                result = operation(
                    plan,
                    self,
                    transaction_id=self.record["transaction"],
                    confirmed_plan_digest=public_plan_digest(plan),
                )
            except Exception:
                # Public status exposes no paths, tokens or native exception text.
                result = self.engine.inspect(self.record["transaction"])
            return {
                "revision": revision,
                "status": "command_recovery"
                if self.command_marker.exists()
                else result.status,
            }

    def run_environment(self, command, environment):
        # A killed controller cannot prove that its native child exited. Preserve
        # all files until an operator has inspected the outstanding command.
        if self.command_marker.exists():
            raise RuntimeError("A native upgrade command needs recovery.")
        write(self.command_marker, {"transaction": self.record["transaction"]})
        result = (self.runner or _subprocess_runner)(command, environment)
        self.command_marker.unlink()
        return result

    def delegate(self, code):
        if code not in self.delegates:
            self.delegates[code] = {
                "runtime-layout": lambda: CompanionRuntimeAdapter(self.runtime),
                "runtime-environment": lambda: CompanionRuntimeEnvironmentAdapter(
                    self.composition.runtime_environment, runner=self.run_environment
                ),
                self.profile_code: self.profile_adapter,
            }[code]()
        return self.delegates[code]

    def snapshot(self, step, directory):
        self.directories[step.code] = directory
        if step.code == "upgrade-stop":
            # Revalidate all files and services immediately before stopping them.
            current = self.review(fresh=True)
            if current.get("revision") != self.record["revision"]:
                raise ValueError("Source changed before stopping.")
            write(directory / "captures.json", self.record["services"])
        elif step.code == "upgrade-start":
            definitions = {
                j.code: base64.b64encode(
                    render_job_definition(j, self.target_os)
                ).decode()
                for j in self.jobs()
            }
            if set(definitions) != set(self.record["services"]) or any(
                self.scheduler.capture(k) is not None for k in definitions
            ):
                raise ValueError("Source services changed during upgrade.")
            write(directory / "definitions.json", definitions)
        else:
            self.delegate(step.code).snapshot(step, directory)

    def apply(self, step):
        if step.code == "upgrade-stop":
            for code, saved in self.record["services"].items():
                if self.scheduler.capture(code) != base64.b64decode(saved):
                    raise ValueError("Source service changed before stopping.")
                self.scheduler.restore(code, None)
        elif step.code == "upgrade-start":
            self.started_at = time.time()
            for job in reversed(self.jobs()):
                if self.scheduler.capture(job.code) is not None:
                    raise ValueError("A source service was registered concurrently.")
                self.scheduler.install(job, render_job_definition(job, self.target_os))
        else:
            self.delegate(step.code).apply(step)
        self.hook(step.code)

    def verify(self, step):
        if step.code == "upgrade-stop":
            if any(
                self.scheduler.capture(k) is not None for k in self.record["services"]
            ):
                raise RuntimeError("Source services did not stop.")
        elif step.code == "upgrade-start":
            self.wait_ready()
            if not all(
                self.scheduler.matches(j, render_job_definition(j, self.target_os))
                for j in self.jobs()
            ):
                raise RuntimeError("Upgraded service definitions changed.")
        else:
            self.delegate(step.code).verify(step)

    def rollback(self, step, directory):
        if self.command_marker.exists():
            raise RuntimeError("An unresolved native command prevents cleanup.")
        if step.code == "upgrade-start":
            definitions = json.loads(
                checked_file(directory / "definitions.json", 262144)
            )
            for code, definition in definitions.items():
                current = self.scheduler.capture(code)
                if current is not None and not capture_matches(
                    current, base64.b64decode(definition), self.target_os
                ):
                    raise ValueError("Changed service prevents rollback.")
            for code in definitions:
                self.scheduler.restore(code, None)
        elif step.code == "upgrade-stop":
            if (
                verify_current_release(self.runtime).name != self.record["old"]
                or self.profile_adapter()._descriptors() != self.record["profile_state"]
            ):
                raise ValueError(
                    "Old runtime and profile must be restored before restarting."
                )
            for code, saved in self.record["services"].items():
                current = self.scheduler.capture(code)
                if current not in (None, base64.b64decode(saved)):
                    raise ValueError("Changed service prevents restart.")
            self.started_at = time.time()
            for code, saved in reversed(tuple(self.record["services"].items())):
                if self.scheduler.capture(code) is None:
                    self.scheduler.restore(code, base64.b64decode(saved))
            self.wait_ready()
            for job in self.jobs():
                if not any(
                    self.scheduler.matches(job, definition)
                    for definition in known_job_definitions(job, self.target_os)
                ):
                    raise RuntimeError("Old service did not restart.")
        else:
            self.delegate(step.code).rollback(step, directory)


class OpenClawUpgrade(Upgrade):
    """The same reviewed runtime update for a daemon created on OpenClaw.

    Its services are the gateway, the daemon API and the background jobs; its
    profile is the OpenClaw configuration and the managed workspace. The
    gateway boots the whole OpenClaw CLI and loads the plugin, which takes
    minutes on a busy Windows PC, so readiness waits longer.
    """

    CODES = OPENCLAW_CODES
    profile_code = "openclaw-profile"
    ready_seconds = 600

    def __init__(self, composition, host, scheduler, user, **kwargs):
        super().__init__(composition, host, scheduler, user, **kwargs)
        self._control = None

    def jobs(self):
        from daemons_companion.openclaw.api_service import build_openclaw_api_job
        from daemons_companion.openclaw.gateway import openclaw_gateway_job
        from daemons_companion.openclaw.ready_composition import (
            _api_layout,
            _services_layout as _openclaw_services_layout,
        )

        layout = self.composition
        return (
            openclaw_gateway_job(self.host, self.runtime, layout.runtime_environment),
            build_openclaw_api_job(_api_layout(layout, self.user)),
            *build_background_jobs(_openclaw_services_layout(layout, self.user)),
        )

    def profile_adapter(self):
        from daemons_companion.openclaw.profile import (
            OpenClawProfileAdapter,
            OpenClawProfileLayout,
        )

        return OpenClawProfileAdapter(
            OpenClawProfileLayout(self.runtime, self.composition.openclaw_state),
            selected_host=HostKind.OPENCLAW,
            user_id=self.user,
            daemon_url=f"http://127.0.0.1:{self.composition.api_port}",
        )

    def attached_hosts(self):
        # A daemon created on OpenClaw registers its own host; only another
        # profile is an added host.
        state = self.runtime.state_root
        if any(state.glob("host-attachment-*.json")):
            return True
        for path in state.glob("host-bindings/*.json"):
            record = json.loads(checked_file(path, 16384))
            if record.get("profile") != str(self.host.profile):
                return True
        return False

    def check_profile(self):
        try:
            current = self.profile_adapter().current()
        except ValueError as error:
            raise UpgradeRefused("openclaw-profile", "The OpenClaw profile changed.") from error
        if not current:
            raise UpgradeRefused("openclaw-profile", "The OpenClaw profile changed.")

    def host_probe(self):
        return HostProbe(
            HostKind.OPENCLAW,
            True,
            True,
            str(self.host.entry),
            version="owned",
            version_status="verified",
        )

    def control(self):
        # One probe object per start: it remembers a loaded plugin, which
        # must not carry over from the new gateway to a restored old one.
        from daemons_companion.openclaw.gateway import PrivateOpenClawControl

        if self._control is None or self._control[0] != self.started_at:
            self._control = (
                self.started_at,
                PrivateOpenClawControl(
                    self.host,
                    self.runtime,
                    self.composition.runtime_environment,
                    self.scheduler,
                    self.composition.gateway_port,
                    existing=True,
                ),
            )
        return self._control[1]

    def health(self):
        from daemons_companion.openclaw.activation import _readiness_probe
        from daemons_companion.openclaw.gateway import api_ready
        from daemons_companion.runtime.bootstrap import _source_io_path

        if not self._worker_restarted():
            return False
        path = _source_io_path if self.target_os == "windows" else lambda p: p
        if not _readiness_probe(
            verify_current_release(self.runtime).name,
            path(self.runtime.memory_root),
            path(self.runtime.state_root / "after-turn-spool"),
        ):
            return False
        if not api_ready(self.composition.api_port, runtime=self.runtime):
            return False
        control = self.control()
        return control.gateway_ready() and control.plugin_ready()


def require_completed_installation(scope):
    receipt = json.loads(checked_file(scope.attempt / "daemon-setup.json", 8192))
    transaction = receipt.get("transaction_id", "")
    if receipt.get("schema") != "daemon.private-setup.v1" or not re.fullmatch(
        r"[a-f0-9]{32}", transaction
    ):
        raise ValueError("A completed owned installation is required.")
    journal = scope.home / "transactions" / transaction / "journal.json"
    if json.loads(checked_file(journal, 1048576)).get("status") != "committed":
        raise ValueError("The original installation is not complete.")
    return receipt


def from_request(request):
    from daemons_companion.hosts.attachment_command import native_target, native_scheduler
    from daemons_companion.runtime.installation_scope import InstallationScope
    from daemons_companion.runtime.host_install import HostInstallLayout

    target_os = native_target()
    if set(request) - {
        "operation",
        "source_profile",
        "base",
        "revision",
    }:
        raise ValueError("Unsupported upgrade request.")
    base, profile = (
        direct(Path(request["base"])),
        direct(Path(request["source_profile"])),
    )
    if not profile.is_relative_to(base):
        raise ValueError("The source is not an owned private installation.")
    scope = InstallationScope(profile.parent)
    scope.validate_profile(profile)
    require_completed_installation(scope)
    bundle = direct(Path(os.environ["DAEMON_HARNESS_COMPANION_BUNDLE_ROOT"]))
    uv = direct(Path(os.environ["DAEMON_HARNESS_COMPANION_UV_EXECUTABLE"]))
    sha = os.environ["DAEMON_HARNESS_COMPANION_UV_SHA256"]
    if scope.host_kind == "openclaw":
        return _openclaw_upgrade(scope, profile, bundle, uv, sha, target_os)
    host = HostInstallLayout(
        profile.parents[4], uv, sha, scope.installation_id, target_os
    )
    host.validate()
    if host.profile != profile:
        raise ValueError("The private host layout changed.")
    runtime = CompanionRuntimeLayout(
        scope.home, bundle, scope, (scope.home / "data/memory/.daemon").is_dir()
    )
    user = _source_user(scope)
    composition = HermesCompositionLayout(
        runtime, profile, uv, sha, target_os, scope.installation_id
    )
    scheduler = native_scheduler(target_os, scope.installation_id)
    return Upgrade(composition, host, scheduler, user)


def _source_user(scope):
    user = json.loads(checked_file(scope.attempt / "connection.json", 8192)).get(
        "user_id"
    )
    if not isinstance(user, str) or not user.isascii() or not user.isdecimal():
        raise ValueError("The source user is ambiguous.")
    if require_completed_installation(scope).get("user_id") != user:
        raise ValueError("The completed installation identity changed.")
    return user


def _openclaw_upgrade(scope, profile, bundle, uv, sha, target_os):
    from daemons_companion.hosts.attachment_command import native_scheduler
    from daemons_companion.openclaw.gateway import host_for
    from daemons_companion.openclaw.ready_composition import (
        OpenClawReadyCompositionLayout,
    )

    pointer = scope.receipt()["host"]
    if pointer.get("target_os") != target_os:
        raise ValueError("Source platform changed.")
    ports = pointer.get("api_port"), pointer.get("gateway_port")
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports) or ports[0] == ports[1]:
        raise ValueError("The OpenClaw ports are ambiguous.")
    host = host_for(scope)
    if host.profile != profile:
        raise ValueError("The private host layout changed.")
    runtime = CompanionRuntimeLayout(
        scope.home, bundle, scope, (scope.home / "data/memory/.daemon").is_dir()
    )
    composition = OpenClawReadyCompositionLayout(
        runtime, profile, uv, sha, target_os, ports[0], ports[1], scope.installation_id
    )
    scheduler = native_scheduler(target_os, scope.installation_id)
    return OpenClawUpgrade(composition, host, scheduler, _source_user(scope))


def check(upgrade):
    """What the daemon's page offers: nothing, an update, a recovery, or why not."""
    status = current_status(upgrade)
    if status["status"] != "required":
        return status
    try:
        reviewed = upgrade.review()
    except UpgradeRefused as error:
        return {"revision": status["revision"], "status": "refused", "code": error.code}
    if reviewed["status"] == "review":
        return {"revision": status["revision"], "status": "available"}
    return {k: v for k, v in reviewed.items() if k != "_record"}


def dispatch(request):
    from daemons_companion.hosts.attachment_command import Refused

    try:
        upgrade = from_request(request)
        operation = request["operation"]
        if operation == "upgrade-status":
            return current_status(upgrade)
        if operation == "upgrade-check":
            return check(upgrade)
        if operation == "upgrade-review":
            return {k: v for k, v in upgrade.review().items() if k != "_record"}
        if operation in ("upgrade-apply", "upgrade-recover"):
            return upgrade.run(
                request["revision"], recover=operation == "upgrade-recover"
            )
        raise ValueError("Unsupported operation.")
    except Exception:
        raise Refused("source-upgrade") from None


def current_status(upgrade):
    """Pending recovery, or whether this daemon already runs the app's runtime."""
    pending = upgrade.state()
    if pending and pending["status"] not in {"committed", "rolled_back"}:
        return pending
    source = verify_current_release(upgrade.runtime)
    # A file's presence does not prove that its Windows implementation is current.
    target = _bundle_manifest(upgrade.runtime.bundle_root)
    installed = json.loads(
        checked_file(source / ".daemon-harness-release.json", 4 * 1024 * 1024)
    )
    before = {e["path"]: e for e in installed["entries"] if e["kind"] == "file"}
    after = {e["path"]: e for e in target["entries"] if e["kind"] == "file"}
    upgradable = {
        name for name in set(before) | set(after)
        if name.startswith(COMPANION) or name in ALLOWED
    }
    ready = all(before.get(name) == after.get(name) for name in upgradable)
    return {
        "revision": source.name,
        "status": "current" if ready else "required",
    }


def require_settled_upgrade(home):
    if (home / "source-upgrade-command.json").exists():
        raise ValueError("A native upgrade command needs recovery.")
    marker = home / "source-upgrade.json"
    if not marker.exists():
        return
    record = json.loads(checked_file(marker, 262144))
    if not re.fullmatch(r"[a-f0-9]{32}", record.get("transaction", "")):
        raise ValueError("Runtime upgrade needs recovery.")
    journal = home / "upgrades" / record["transaction"] / "journal.json"
    if not journal.exists() or json.loads(checked_file(journal, 65536)).get(
        "status"
    ) not in {"committed", "rolled_back"}:
        raise ValueError("Runtime upgrade needs recovery before another operation.")
