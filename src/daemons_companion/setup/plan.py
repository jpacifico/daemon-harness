"""Platform-neutral planning core for the Daemon Harness companion app.

This module detects and reviews. Host configuration remains owned by the
existing integration installers; no host is installed or upgraded here.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Sequence
from urllib.parse import urlparse

from daemon_core.daemon_identity import validate_name
from daemon_core.onboarding_user import UserPreferences


class HostKind(str, Enum):
    HERMES = "hermes"
    OPENCLAW = "openclaw"


class ProviderKind(str, Enum):
    MISTRAL_API = "mistral_api"
    ANTHROPIC_API = "anthropic_api"
    XAI_API = "xai_api"
    OPENAI_API = "openai_api"
    OPENAI_COMPATIBLE_API = "openai_compatible_api"
    CODEX_CLI = "codex_cli"


@dataclass(frozen=True)
class HostProbe:
    host: HostKind
    detected: bool
    usable: bool
    executable: str = ""
    config_path: str = ""
    version: str = ""
    version_status: str = "unavailable"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "host": self.host.value,
            "detected": self.detected,
            "usable": self.usable,
            "executable": self.executable,
            "config_path": self.config_path,
            "version": self.version,
            "version_status": self.version_status,
        }


@dataclass(frozen=True)
class ProviderSelection:
    kind: ProviderKind
    model: str
    base_url: str = ""
    credential_available: bool = False

    def validated(self, *, codex_available: bool = False) -> "ProviderSelection":
        model = _bounded_text(self.model, "Model", 160)
        base_url = self.base_url.strip()
        if self.kind in {ProviderKind.MISTRAL_API, ProviderKind.ANTHROPIC_API, ProviderKind.XAI_API}:
            if base_url:
                raise ValueError("Named API providers use their official endpoint.")
            if not self.credential_available:
                raise ValueError("An API credential is required.")
        elif self.kind == ProviderKind.OPENAI_API:
            if base_url:
                raise ValueError("OpenAI API does not accept a custom base URL.")
            if not self.credential_available:
                raise ValueError("An OpenAI API credential is required.")
        elif self.kind == ProviderKind.OPENAI_COMPATIBLE_API:
            _validate_provider_url(base_url)
            if not self.credential_available and not _is_loopback_url(base_url):
                raise ValueError("A credential is required for a remote provider.")
        elif self.kind == ProviderKind.CODEX_CLI:
            if base_url or self.credential_available:
                raise ValueError("Codex CLI uses its official login, not an API credential.")
            if not codex_available:
                raise ValueError("Codex CLI is not available on this computer.")
        else:  # pragma: no cover - Enum protects construction.
            raise ValueError("Unsupported provider.")
        return ProviderSelection(
            kind=self.kind,
            model=model,
            base_url=base_url,
            credential_available=self.credential_available,
        )

    def public_configuration(self) -> dict[str, object]:
        """Return non-secret runtime settings suitable for review or logs."""

        if self.kind in {ProviderKind.MISTRAL_API, ProviderKind.ANTHROPIC_API, ProviderKind.XAI_API}:
            from daemon_core.api_providers import API_PROVIDERS
            provider = self.kind.value.removesuffix('_api')
            settings = {"LLM_PROVIDER": provider, provider.upper() + "_MODEL": self.model}
            secret_names = [API_PROVIDERS[self.kind.value].secret_name]
        elif self.kind == ProviderKind.OPENAI_API:
            settings = {"LLM_PROVIDER": "openai", "OPENAI_MODEL": self.model}
            secret_names = ["OPENAI_API_KEY"]
        elif self.kind == ProviderKind.OPENAI_COMPATIBLE_API:
            settings = {
                "LLM_PROVIDER": "local",
                "LOCAL_LLM_BASE_URL": self.base_url,
                "LOCAL_LLM_MODEL": self.model,
            }
            secret_names = ["LOCAL_LLM_API_KEY"] if self.credential_available else []
        else:
            settings = {"LLM_PROVIDER": "codex_cli", "CODEX_CLI_MODEL": self.model}
            secret_names = []
        return {
            "kind": self.kind.value,
            "settings": settings,
            "required_secret_names": secret_names,
            "credential_configured": self.credential_available,
        }


@dataclass(frozen=True)
class SetupRequest:
    host: HostKind
    daemon_name: str
    daemon_gender: str
    provider: ProviderSelection
    user_preferences: UserPreferences | None = None


@dataclass(frozen=True)
class PlanStep:
    code: str
    description: str
    changes: tuple[str, ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "description": self.description,
            "changes": list(self.changes),
        }


@dataclass(frozen=True)
class SetupPlan:
    selected_host: HostProbe
    daemon_name: str
    daemon_gender: str
    provider: dict[str, object]
    steps: tuple[PlanStep, ...]
    installs_host: bool = False
    requires_confirmation: bool = True
    user_preferences: UserPreferences | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": "daemon.companion-plan.v1",
            "selected_host": self.selected_host.to_public_dict(),
            "identity": {
                "name": self.daemon_name,
                "gender": self.daemon_gender,
                "persona_modified": False,
                "conversation_modified": False,
            },
            **({"user_preferences": self.user_preferences.to_dict()} if self.user_preferences else {}),
            "provider": self.provider,
            "steps": [step.to_public_dict() for step in self.steps],
            "installs_host": self.installs_host,
            "requires_confirmation": self.requires_confirmation,
        }


def detect_hosts(
    *,
    home: Path | None = None,
    finder: Callable[[str], str | None] = shutil.which,
    version_reader: Callable[[str], tuple[str, str]] | None = None,
) -> tuple[HostProbe, HostProbe]:
    """Read-only host discovery. No configuration or package manager is invoked."""

    root = (home or Path.home()).expanduser()
    reader = version_reader or _read_version
    return tuple(
        _probe_host(host, root, finder=finder, version_reader=reader)
        for host in (HostKind.HERMES, HostKind.OPENCLAW)
    )  # type: ignore[return-value]


def build_setup_plan(
    request: SetupRequest,
    probes: Sequence[HostProbe],
    *,
    codex_available: bool = False,
) -> SetupPlan:
    by_host = {probe.host: probe for probe in probes}
    selected = by_host.get(request.host)
    if selected is None or not selected.detected or not selected.usable:
        raise ValueError(f"{request.host.value} is not installed and usable.")
    name = validate_name(request.daemon_name)
    if request.daemon_gender not in {"feminine", "masculine"}:
        raise ValueError("Gender must be feminine or masculine.")
    provider = request.provider.validated(codex_available=codex_available)
    steps = _COMMON_STEPS + _HOST_STEPS[request.host] + _VERIFY_STEPS
    return SetupPlan(
        selected_host=selected,
        daemon_name=name,
        daemon_gender=request.daemon_gender,
        provider=provider.public_configuration(),
        steps=steps,
        user_preferences=request.user_preferences.validated() if request.user_preferences else None,
    )


_COMMON_STEPS = (
    PlanStep(
        "review",
        "Review and confirm every change before writing.",
        ("No host installation or upgrade", "Back up only settings that will change"),
    ),
    PlanStep(
        "runtime",
        "Install the private Daemon Harness runtime and shared maintenance services.",
        ("Local runtime", "Shared memory root", "After-turn worker", "Dreaming/REM schedule"),
    ),
    PlanStep(
        "identity",
        "Store the chosen name and grammatical agreement separately from the voice portrait.",
        ("daemon_identity.json", "No persona.md change", "No conversation.md change"),
    ),
    PlanStep(
        "provider",
        "Configure non-secret provider settings and connect the credential through protected storage.",
        ("One model", "Medium/high internal defaults", "No credential in logs or plan"),
    ),
)


_HOST_STEPS = {
    HostKind.HERMES: (
        PlanStep(
            "hermes-plugin",
            "Install the Hermes memory and voice plugins without changing Hermes source code.",
            ("Select daemon_core memory", "Disable competing native personal memory review", "Keep host tools and channels"),
        ),
    ),
    HostKind.OPENCLAW: (
        PlanStep(
            "openclaw-plugin",
            "Install the OpenClaw context-engine plugin and its loopback API.",
            ("Select daemon-harness context engine", "Use minimal workspace", "Keep host tools and channels"),
        ),
    ),
}


_VERIFY_STEPS = (
    PlanStep(
        "verify",
        "Restart only affected services and verify voice, memory tools and durable after-turn work.",
        ("Host plugin active", "Memory root readable", "Ready attestation", "Rollback available"),
    ),
)


def _probe_host(
    host: HostKind,
    home: Path,
    *,
    finder: Callable[[str], str | None],
    version_reader: Callable[[str], tuple[str, str]],
) -> HostProbe:
    executable = finder(host.value) or ""
    config = home / (".hermes" if host == HostKind.HERMES else ".openclaw")
    detected = bool(executable or config.exists())
    version = ""
    status = "unavailable"
    if executable:
        version, status = version_reader(executable)
    return HostProbe(
        host=host,
        detected=detected,
        usable=bool(executable),
        executable=executable,
        config_path=str(config) if config.exists() else "",
        version=version,
        version_status=status,
    )


def _read_version(executable: str) -> tuple[str, str]:
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", "failed"
    output = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not output:
        return "", "failed"
    return output[0][:200], "verified"


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum or any(char in cleaned for char in "\r\n\x00"):
        raise ValueError(f"{label} is invalid.")
    return cleaned


def _validate_provider_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Provider URL must be an HTTP(S) origin without credentials.")
    if parsed.path not in {"", "/", "/v1", "/v1/"} or parsed.query or parsed.fragment:
        raise ValueError("Provider URL must be a provider origin or its /v1 endpoint.")
    if parsed.scheme == "http" and not _is_loopback_url(value):
        raise ValueError("Remote provider URLs must use HTTPS.")


def _is_loopback_url(value: str) -> bool:
    return (urlparse(value).hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}


def _main() -> None:
    parser = argparse.ArgumentParser(description="Inspect Daemon Harness companion prerequisites.")
    parser.add_argument("command", choices=("inspect",))
    args = parser.parse_args()
    if args.command == "inspect":
        print(json.dumps(
            {"schema": "daemon.companion-inspection.v1", "hosts": [
                probe.to_public_dict() for probe in detect_hosts()
            ]},
            ensure_ascii=False,
            indent=2,
        ))


if __name__ == "__main__":
    _main()
