"""Strict JSON boundary used by the native companion application shells."""

from __future__ import annotations

from daemon_core.onboarding_user import preferences_from_request

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

from daemons_companion.setup.plan import (
    HostKind,
    HostProbe,
    ProviderKind,
    ProviderSelection,
    SetupPlan,
    SetupRequest,
    build_setup_plan,
    detect_hosts,
)
from daemons_companion.setup.install_session import (
    CompanionInstallLayout,
    prepare_installation,
)
from daemons_companion.desktop.package import verify_package_manifest
from daemons_companion.runtime.acquisition import review_host_setup, target_platform
from daemons_companion.runtime.release import CompanionRuntimeLayout
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.setup.transaction import public_plan_digest


REQUEST_SCHEMA = "daemon.companion-request.v1"
RESPONSE_SCHEMA = "daemon.companion-response.v1"
_MAX_REQUEST_BYTES = 64 * 1024
_REQUEST_KEYS = {
    "inspect": frozenset({"schema", "action"}),
    "host_setup": frozenset({"schema", "action", "host"}),
    "plan": frozenset(
        {"schema", "action", "host", "daemon_name", "daemon_gender", "provider"}
    ),
    "prepare": frozenset(
        {"schema", "action", "host", "daemon_name", "daemon_gender", "provider"}
    ),
}
_PROVIDER_KEYS = frozenset({"kind", "model", "base_url", "credential_available"})
_CONTEXT_ENVIRONMENT = frozenset(
    {
        "DAEMON_HARNESS_COMPANION_PACKAGE_ROOT",
        "DAEMON_HARNESS_COMPANION_INSTALL_HOME",
        "DAEMON_HARNESS_COMPANION_BUNDLE_ROOT",
        "DAEMON_HARNESS_COMPANION_UV_EXECUTABLE",
        "DAEMON_HARNESS_COMPANION_UV_SHA256",
        "DAEMON_HARNESS_COMPANION_TARGET_OS",
        "DAEMON_HARNESS_COMPANION_OPENCLAW_API_PORT",
        "DAEMON_HARNESS_COMPANION_OPENCLAW_GATEWAY_PORT",
    }
)


class CompanionAppRequestError(ValueError):
    """Safe request error whose message may be shown by a native shell."""


@dataclass(frozen=True)
class CompanionPreparationContext:
    package_root: Path
    companion_home: Path
    bundle_root: Path
    uv_executable: Path
    uv_sha256: str
    target_os: str
    openclaw_api_port: int = 8000
    openclaw_gateway_port: int = 18789
    installation_scope: InstallationScope | None = None
    root_folder_memory: bool = False

    def install_layout(self, host: HostKind, profile: Path) -> CompanionInstallLayout:
        self.validate()
        scope = self.installation_scope
        if scope is not None and (host.value != scope.host_kind or profile != scope.profile):
            raise CompanionAppRequestError("Installation scope does not authorize this host profile.")
        common = {
            "runtime": CompanionRuntimeLayout(
                scope.home if scope else self.companion_home, self.bundle_root, scope,
                root_folder_memory=self.root_folder_memory,
            ),
            "job_namespace": scope.installation_id if scope else None,
            "uv_executable": self.uv_executable,
            "uv_sha256": self.uv_sha256,
            "target_os": self.target_os,
            "openclaw_api_port": self.openclaw_api_port,
            "openclaw_gateway_port": self.openclaw_gateway_port,
        }
        if host == HostKind.HERMES:
            return CompanionInstallLayout(**common, hermes_home=profile)
        return CompanionInstallLayout(**common, openclaw_state=profile)

    def validate(self) -> None:
        if self.installation_scope is not None:
            scope = self.installation_scope
            if (not isinstance(scope, InstallationScope)
                    or scope.attempt != self.companion_home / "hosts" / scope.host_kind / "installations" / scope.installation_id):
                raise CompanionAppRequestError("Installation scope is outside the verified package destination.")
            scope.receipt()
        paths = (
            self.package_root,
            self.companion_home,
            self.bundle_root,
            self.uv_executable,
        )
        if any(not _safe_absolute(path) for path in paths):
            raise CompanionAppRequestError("Companion preparation paths are invalid.")
        if (
            self.package_root.is_symlink()
            or not self.package_root.is_dir()
            or self.bundle_root != self.package_root / "payload"
            or self.uv_executable
            != self.package_root / "bin" / ("uv.exe" if self.target_os == "windows" else "uv")
            or not self.bundle_root.is_dir()
            or self.bundle_root.is_symlink()
            or not self.uv_executable.is_file()
            or self.uv_executable.is_symlink()
        ):
            raise CompanionAppRequestError("Companion package context is invalid.")
        if self.target_os not in {"macos", "windows"}:
            raise CompanionAppRequestError("Companion target OS is unsupported.")
        if not _digest(self.uv_sha256) or _sha256(self.uv_executable) != self.uv_sha256:
            raise CompanionAppRequestError("Companion uv verification failed.")
        if self.companion_home == self.package_root or self.companion_home.is_relative_to(
            self.package_root
        ):
            raise CompanionAppRequestError("Companion install home must remain outside the package.")
        if not 1024 <= self.openclaw_api_port <= 65535 or not 1024 <= self.openclaw_gateway_port <= 65535:
            raise CompanionAppRequestError("Companion OpenClaw ports are invalid.")
        try:
            verify_package_manifest(self.package_root)
        except (ValueError, RuntimeError) as error:
            raise CompanionAppRequestError("Companion package integrity verification failed.") from error


def handle_request(
    payload: object,
    *,
    probes: Sequence[HostProbe] | None = None,
    finder: Callable[[str], str | None] = shutil.which,
    preparation_context: CompanionPreparationContext | None = None,
    environment: Mapping[str, str] | None = None,
    target_os: str | None = None,
) -> dict[str, object]:
    """Return one public response without mutating an installation."""

    request = _request_object(payload)
    action = request.get("action")
    if action not in _REQUEST_KEYS:
        raise CompanionAppRequestError("Unsupported companion action.")
    allowed = _REQUEST_KEYS[action]
    if action in {"plan", "prepare"} and "user_preferences" in request:
        allowed = allowed | {"user_preferences"}
    _exact_keys(request, allowed, "Companion request")
    available_hosts = tuple(probes) if probes is not None else detect_hosts(finder=finder)
    _validate_probes(available_hosts)
    codex_available = bool(finder("codex"))
    platform = target_os or target_platform()
    if action == "inspect":
        return _response(
            action,
            inspection=_inspection(available_hosts, codex_available, platform),
        )

    try:
        host = HostKind(_text(request["host"], "Host", 32))
    except ValueError as error:
        raise CompanionAppRequestError("Host must be Hermes or OpenClaw.") from error
    selected = next(probe for probe in available_hosts if probe.host == host)
    host_setup = _host_setup(selected, platform)
    if action == "host_setup":
        return _response(action, host_setup=host_setup)
    provider = _provider(request["provider"])
    if selected.usable and not host_setup["recognized_version"]:
        raise CompanionAppRequestError("Host version is not recognized. Review host setup before preparing plugin changes.")
    try:
        plan = build_setup_plan(
            SetupRequest(
                host=host,
                daemon_name=_text(request["daemon_name"], "Daemon name", 80),
                daemon_gender=_text(request["daemon_gender"], "Daemon gender", 16),
                provider=provider,
                user_preferences=preferences_from_request(request),
            ),
            available_hosts,
            codex_available=codex_available,
        )
    except ValueError as error:
        raise CompanionAppRequestError(str(error)) from error
    if action == "plan":
        return _response(
            action,
            plan=plan.to_public_dict(),
            confirmation_digest=public_plan_digest(plan),
        )
    context = preparation_context or preparation_context_from_environment(
        os.environ if environment is None else environment
    )
    try:
        prepared = prepare_installation(
            plan,
            context.install_layout(host, Path(selected.config_path)),
        )
    except ValueError as error:
        raise CompanionAppRequestError(str(error)) from error
    return _response(
        action,
        reviewed_plan=plan.to_public_dict(),
        plan=prepared.plan.to_public_dict(),
        confirmation_digest=prepared.confirmation_digest,
        review_text=_review_text(plan),
    )


def _review_text(plan: SetupPlan) -> str:
    """Readable introduction; the unchanged exact plan stays available separately."""
    host = "Hermes Agent" if plan.selected_host.host == HostKind.HERMES else "OpenClaw"
    provider = {
        "codex_cli": "your official Codex CLI connection",
        "openai_api": "an OpenAI API key",
        "mistral_api": "a Mistral AI API or Vibe key",
        "anthropic_api": "an Anthropic API key",
        "xai_api": "a SpaceXSI API key",
        "openai_compatible_api": "your API provider",
    }[plan.provider["kind"]]
    settings = plan.provider["settings"]
    model = next(settings[key] for key in ("OPENAI_MODEL", "MISTRAL_MODEL", "ANTHROPIC_MODEL", "XAI_MODEL", "LOCAL_LLM_MODEL", "CODEX_CLI_MODEL") if key in settings)
    lines = [
        f"Set up {plan.daemon_name} on {host}",
        f"Grammatical agreement: {plan.daemon_gender}. The canonical voice portrait will not be rewritten.",
        f"Background memory work will use {model} through {provider}.",
        "Planned changes:",
        *[f"{index}. {step.description}" for index, step in enumerate(plan.steps, 1)],
        "Your host keeps its tools, channels and permissions. This plan does not upgrade the host.",
        "Background memory maintenance uses model calls and can incur usage or charges.",
        "Open Technical details to inspect the exact paths, settings and confirmation reference.",
        "This review has not applied the plan. A separate confirmation is required for execution.",
    ]
    if plan.user_preferences:
        preferences = plan.user_preferences
        lines.insert(2, f"Call you: {preferences.preferred_name}. Pronouns: {preferences.pronouns or 'not specified'}.")
    return "\n\n".join(lines)


def preparation_context_from_environment(
    environment: Mapping[str, str],
) -> CompanionPreparationContext:
    unknown = {
        key
        for key in environment
        if key.startswith("DAEMON_HARNESS_COMPANION_INSTALL_")
        and key not in _CONTEXT_ENVIRONMENT
    }
    if unknown:
        raise CompanionAppRequestError("Companion preparation environment is invalid.")
    missing = [key for key in _CONTEXT_ENVIRONMENT if not environment.get(key, "").strip()]
    if missing:
        raise CompanionAppRequestError("Verified companion preparation context is unavailable.")
    try:
        context = CompanionPreparationContext(
            package_root=Path(environment["DAEMON_HARNESS_COMPANION_PACKAGE_ROOT"]),
            companion_home=Path(environment["DAEMON_HARNESS_COMPANION_INSTALL_HOME"]),
            bundle_root=Path(environment["DAEMON_HARNESS_COMPANION_BUNDLE_ROOT"]),
            uv_executable=Path(environment["DAEMON_HARNESS_COMPANION_UV_EXECUTABLE"]),
            uv_sha256=environment["DAEMON_HARNESS_COMPANION_UV_SHA256"],
            target_os=environment["DAEMON_HARNESS_COMPANION_TARGET_OS"],
            openclaw_api_port=int(
                environment["DAEMON_HARNESS_COMPANION_OPENCLAW_API_PORT"]
            ),
            openclaw_gateway_port=int(
                environment["DAEMON_HARNESS_COMPANION_OPENCLAW_GATEWAY_PORT"]
            ),
        )
    except (TypeError, ValueError) as error:
        raise CompanionAppRequestError("Companion preparation environment is invalid.") from error
    context.validate()
    return context


def read_request(stream) -> object:
    raw = stream.buffer.read(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES:
        raise CompanionAppRequestError("Companion request is too large.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompanionAppRequestError("Companion request must be valid UTF-8 JSON.") from error


def _inspection(
    probes: Sequence[HostProbe],
    codex_available: bool,
    platform: str,
) -> dict[str, object]:
    setups = {probe.host.value: _host_setup(probe, platform) for probe in probes}
    usable = [probe.host.value for probe in probes if probe.usable and setups[probe.host.value]["recognized_version"]]
    if HostKind.HERMES.value in usable:
        recommended = HostKind.HERMES.value
    elif HostKind.OPENCLAW.value in usable:
        recommended = HostKind.OPENCLAW.value
    else:
        recommended = HostKind.HERMES.value
    return {
        "hosts": [probe.to_public_dict() for probe in probes],
        "host_setup_options": setups,
        "usable_hosts": usable,
        "recommended_host": recommended,
        "selection_is_automatic": len(usable) == 1,
        "host_required": not usable,
        "codex_cli_available": codex_available,
        "provider_kinds": [kind.value for kind in ProviderKind],
        "writes_performed": False,
    }


def _host_setup(probe: HostProbe, platform: str) -> dict[str, object]:
    return review_host_setup(
        host=probe.host.value, detected=probe.detected, usable=probe.usable,
        version=probe.version, version_status=probe.version_status, platform=platform,
    )


def _provider(value: object) -> ProviderSelection:
    if not isinstance(value, dict):
        raise CompanionAppRequestError("Provider must be an object.")
    _exact_keys(value, _PROVIDER_KEYS, "Provider")
    try:
        kind = ProviderKind(_text(value["kind"], "Provider kind", 64))
    except ValueError as error:
        raise CompanionAppRequestError("Provider kind is unsupported.") from error
    credential_available = value["credential_available"]
    if not isinstance(credential_available, bool):
        raise CompanionAppRequestError("Provider credential availability must be boolean.")
    return ProviderSelection(
        kind=kind,
        model=_text(value["model"], "Model", 160),
        base_url=_text(value["base_url"], "Provider URL", 512, empty=True),
        credential_available=credential_available,
    )


def _request_object(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise CompanionAppRequestError("Companion request must be an object.")
    if payload.get("schema") != REQUEST_SCHEMA:
        raise CompanionAppRequestError("Companion request schema is unsupported.")
    return payload


def _exact_keys(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise CompanionAppRequestError(f"{label} fields are invalid.")


def _validate_probes(probes: Sequence[HostProbe]) -> None:
    if tuple(probe.host for probe in probes) != (HostKind.HERMES, HostKind.OPENCLAW):
        raise CompanionAppRequestError("Host inspection is incomplete.")


def _text(value: object, label: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise CompanionAppRequestError(f"{label} must be text.")
    cleaned = value.strip()
    if (
        (not cleaned and not empty)
        or len(cleaned) > maximum
        or any(character in cleaned for character in "\r\n\x00")
    ):
        raise CompanionAppRequestError(f"{label} is invalid.")
    return cleaned


def _safe_absolute(path: Path) -> bool:
    return path.is_absolute() and ".." not in path.parts and "." not in path.parts


def _digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _response(action: str, **contents: object) -> dict[str, object]:
    return {
        "schema": RESPONSE_SCHEMA,
        "ok": True,
        "action": action,
        **contents,
    }


def _error_response(error: CompanionAppRequestError) -> dict[str, object]:
    return {
        "schema": RESPONSE_SCHEMA,
        "ok": False,
        "error": {"code": "invalid_request", "message": str(error)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Daemon Harness companion backend.")
    parser.add_argument("command", choices=("request",))
    parser.parse_args()
    try:
        response = handle_request(read_request(sys.stdin))
    except CompanionAppRequestError as error:
        response = _error_response(error)
        status = 2
    else:
        status = 0
    json.dump(response, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CompanionAppRequestError",
    "CompanionPreparationContext",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "handle_request",
    "main",
    "preparation_context_from_environment",
    "read_request",
]
