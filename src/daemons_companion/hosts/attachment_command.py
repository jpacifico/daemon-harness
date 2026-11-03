"""Bounded desktop transport for a prepared private host association."""

from __future__ import annotations
import json
import os
import re
from pathlib import Path
import sys

from daemons_companion.runtime.bootstrap import _source_io_path
from daemons_companion.hosts.attachment import (
    RuntimeBinding,
    AttachmentLayout,
    inspect,
    public_review,
    apply,
    host_reference,
)
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.openclaw.host_install import OpenClawHostLayout, checked_file
from daemons_companion.runtime.release import CompanionRuntimeLayout, verify_current_release
from daemons_companion.runtime.environment import (
    CompanionRuntimeEnvironmentLayout,
    resolve_release_environment,
    release_environment_interpreter,
)
from daemons_companion.hosts.attachment_services import PrivateAttachmentServices
from daemons_companion.runtime.services import (
    MacLaunchdSchedulerBackend,
    WindowsTaskSchedulerBackend,
)


class Refused(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_channel(telegram, target_env, source_env, user):
    """Only a single literal-token channel is supported by this transaction."""
    if (
        not isinstance(telegram, dict)
        or telegram.get("accounts")
        or telegram.get("tokenFile")
    ):
        raise Refused("channel")
    token = telegram.get("botToken") or target_env.get("TELEGRAM_BOT_TOKEN")
    original = source_env.get("TELEGRAM_BOT_TOKEN")
    # Native environment/file indirections cannot establish distinct consumers.
    pattern = r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}"
    if not isinstance(token, str) or not re.fullmatch(pattern, token):
        raise Refused("channel")
    if not isinstance(original, str) or not re.fullmatch(pattern, original):
        raise Refused("channel")
    if token.split(":", 1)[0] == original.split(":", 1)[0]:
        raise Refused("handover")
    if telegram.get("enabled") is False or telegram.get("allowFrom") != [user]:
        raise Refused("channel")
    if telegram.get("dmPolicy") != "allowlist":
        raise Refused("channel")


def native_target():
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    raise Refused("platform")


def native_scheduler(target_os, namespace):
    if target_os == "windows":
        return WindowsTaskSchedulerBackend(job_namespace=namespace)
    if target_os == "macos":
        return MacLaunchdSchedulerBackend(
            Path.home() / "Library/LaunchAgents",
            user_id=os.getuid(),
            job_namespace=namespace,
        )
    raise Refused("platform")


def source_binding(source_profile, *, require_capability=True):
    target_os = native_target()
    scope = InstallationScope(source_profile.parent)
    scope.validate_profile(source_profile)
    from daemons_companion.runtime.upgrade import require_settled_upgrade

    require_settled_upgrade(scope.home)
    root_memory = scope.home / "data/memory/.daemon"
    runtime = CompanionRuntimeLayout(
        scope.home, scope.home, scope, root_memory.is_dir()
    )
    source = verify_current_release(runtime)
    # Creating an association needs a runtime that supports it; undoing one does not.
    if require_capability and not _source_io_path(source / "src/daemons_companion/hosts/attachment_entry.py").is_file():
        raise Refused("upgrade")
    environment = CompanionRuntimeEnvironmentLayout(
        runtime, scope.attempt / "unused-uv", "", target_os
    )
    python = release_environment_interpreter(
        resolve_release_environment(environment), target_os
    )
    connection = json.loads(checked_file(scope.attempt / "connection.json", 8192))
    user = connection.get("user_id")
    if not isinstance(user, str) or not user.isascii() or not user.isdecimal():
        raise Refused("identity")
    binding = RuntimeBinding(
        source,
        runtime.memory_root,
        runtime.state_root,
        python,
        user,
        "com.daemonharness.test." + scope.installation_id + ".after-turn-worker",
        runtime.state_root / "after-turn-spool",
        runtime.state_root / "receipts",
    )
    return scope, runtime, binding


def layouts(request, *, undo=False):
    target_os = native_target()
    if set(request) - {
        "operation",
        "source_profile",
        "target_profile",
        "base",
        "api_port",
        "revision",
    }:
        raise Refused("request")
    base = Path(request["base"])
    source_profile = Path(request["source_profile"])
    target = Path(request["target_profile"])
    for path in (base, source_profile, target):
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise Refused("ownership")
    if not source_profile.is_relative_to(base) or not target.is_relative_to(base):
        raise Refused("ownership")
    scope, runtime, binding = source_binding(source_profile, require_capability=not undo)
    user = binding.user_id
    host = OpenClawHostLayout.existing(
        target.parents[4],
        target.parent.name,
        target_os,
        Path(os.environ["DAEMON_HARNESS_COMPANION_BUNDLE_ROOT"])
        / "apps/companion/openclaw-runtime",
    )
    if host.profile != target:
        raise Refused("ownership")
    # Same-token handover needs its own source-service transaction. Until it is
    # qualified, a distinct prepared channel is required rather than two polls.
    from dotenv import dotenv_values

    source_env = dotenv_values(scope.profile / ".env")
    target_env = dotenv_values(target / ".env")
    from daemons_companion.hosts.creation import credential_environment

    managed_credentials = credential_environment(host)
    target_env.update(managed_credentials)
    config = json.loads(checked_file(target / "openclaw.json", 2 * 1024 * 1024))
    telegram = config.get("channels", {}).get("telegram", {})
    if managed_credentials and telegram.get("botToken") == "${TELEGRAM_BOT_TOKEN}":
        telegram = dict(telegram, botToken=managed_credentials["TELEGRAM_BOT_TOKEN"])
    validate_channel(telegram, target_env, source_env, user)
    if type(request.get("api_port")) is not int:
        raise Refused("request")
    layout = AttachmentLayout(
        binding,
        target,
        runtime.state_root / ("host-attachment-" + host_reference(target) + ".json"),
        request["api_port"],
        host.release.version,
        (host.node, host.entry),
    )
    backend = native_scheduler(target_os, host.installation_id)
    return layout, PrivateAttachmentServices(layout, host, backend)


def execute(request):
    if str(request.get("operation", "")).startswith("upgrade-"):
        from daemons_companion.runtime.upgrade import dispatch

        return dispatch(request)
    if str(request.get("operation", "")).startswith("create-"):
        from daemons_companion.hosts.creation import dispatch

        return dispatch(request)
    layout, controller = layouts(request)
    if request.get("operation") == "review":
        plan = inspect(layout)
        controller.snapshot()
        if not controller.probe("maintenance"):
            raise Refused("maintenance")
        return public_review(plan) | {
            "profile": str(layout.profile),
            "status": "review",
            "model": "existing",
            "channel": "separate",
        }
    if request.get("operation") == "apply" and isinstance(request.get("revision"), str):
        return apply(layout, request["revision"], controller)
    raise Refused("request")


def main():
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise Refused("request")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise Refused("request")
        result = execute(request)
        print(json.dumps({"ok": True, "result": result}))
    except Refused as error:
        print(json.dumps({"ok": False, "code": error.code}))
    except Exception:
        print(json.dumps({"ok": False, "code": "unconfirmed"}))


if __name__ == "__main__":
    main()
