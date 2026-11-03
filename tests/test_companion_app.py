from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from daemons_companion.desktop.app import (
    CompanionAppRequestError,
    CompanionPreparationContext,
    REQUEST_SCHEMA,
    handle_request,
    preparation_context_from_environment,
)
from daemons_companion.setup.plan import HostKind, HostProbe
from daemons_companion.desktop.package import write_package_manifest


def _probes(
    *, hermes: bool = True, openclaw: bool = True
) -> tuple[HostProbe, HostProbe]:
    return (
        HostProbe(
            HostKind.HERMES,
            hermes,
            hermes,
            "/fixture/hermes",
            "/fixture/.hermes" if hermes else "",
            "Hermes 0.21.0" if hermes else "",
            "verified" if hermes else "unavailable",
        ),
        HostProbe(
            HostKind.OPENCLAW,
            openclaw,
            openclaw,
            "/fixture/openclaw",
            "/fixture/.openclaw" if openclaw else "",
            "OpenClaw 2026.9.1-beta.1" if openclaw else "",
            "verified" if openclaw else "unavailable",
        ),
    )


def _inspect(**kwargs) -> dict[str, object]:
    return handle_request(
        {"schema": REQUEST_SCHEMA, "action": "inspect"},
        finder=lambda name: "/fixture/codex" if name == "codex" else None,
        **kwargs,
    )


def _plan(
    provider: dict[str, object] | None = None, *, host: str = "hermes", action: str = "plan"
) -> dict[str, object]:
    return {
        "schema": REQUEST_SCHEMA,
        "action": action,
        "host": host,
        "daemon_name": "Aster",
        "daemon_gender": "feminine",
        "provider": provider
        or {
            "kind": "codex_cli",
            "model": "gpt-5.6-terra",
            "base_url": "",
            "credential_available": False,
        },
    }


def _bundle(root: Path) -> Path:
    files = {
        "requirements-runtime.lock": "# synthetic\n",
        "requirements.txt": "# synthetic\n",
        "src/daemon_core/__init__.py": "\n",
        "src/daemons_companion/runtime/service_entry.py": "\n",
        "src/daemon_core/nightly_maintenance.py": "\n",
        "src/daemons_companion/runtime/services.py": "\n",
        "integrations/hermes_plugin/__init__.py": "\n",
        "integrations/hermes_voice_plugin/__init__.py": "\n",
        "integrations/openclaw_plugin/index.js": "export default {};\n",
        "integrations/openclaw_plugin/openclaw.plugin.json": '{"id":"daemon-harness"}\n',
        "integrations/openclaw_plugin/package.json": '{"name":"daemon-harness"}\n',
        "integrations/openclaw_plugin/workspace/AGENTS.md": "# Workspace\n",
        "integrations/after_turn_service/service.sh": "#!/bin/sh\n",
        "integrations/dreaming_service/service.sh": "#!/bin/sh\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _preparation(tmp_path: Path, *, target_os: str = "macos") -> CompanionPreparationContext:
    package = (tmp_path / "package").absolute()
    bundle = _bundle(package / "payload")
    uv = package / "bin" / ("uv.exe" if target_os == "windows" else "uv")
    uv.parent.mkdir(parents=True)
    uv.write_bytes(b"synthetic uv")
    uv.chmod(0o755)
    digest = hashlib.sha256(uv.read_bytes()).hexdigest()
    write_package_manifest(package)
    return CompanionPreparationContext(
        package_root=package,
        companion_home=(tmp_path / "install-home").absolute(),
        bundle_root=bundle,
        uv_executable=uv,
        uv_sha256=digest,
        target_os=target_os,
    )


@pytest.mark.parametrize(
    ("hermes", "openclaw", "recommended", "automatic", "required"),
    [
        (False, False, "hermes", False, True),
        (True, False, "hermes", True, False),
        (False, True, "openclaw", True, False),
        (True, True, "hermes", False, False),
    ],
)
def test_inspection_recommends_without_writing(
    hermes: bool,
    openclaw: bool,
    recommended: str | None,
    automatic: bool,
    required: bool,
) -> None:
    result = _inspect(probes=_probes(hermes=hermes, openclaw=openclaw))
    inspection = result["inspection"]
    assert isinstance(inspection, dict)
    assert inspection["recommended_host"] == recommended
    assert inspection["selection_is_automatic"] is automatic
    assert inspection["host_required"] is required
    assert inspection["writes_performed"] is False
    assert inspection["codex_cli_available"] is True


def test_plan_is_exact_stable_and_contains_no_secret() -> None:
    first = handle_request(
        _plan(), probes=_probes(), finder=lambda _name: "/fixture/codex"
    )
    second = handle_request(
        _plan(), probes=_probes(), finder=lambda _name: "/fixture/codex"
    )
    assert first == second
    assert first["ok"] is True
    assert len(first["confirmation_digest"]) == 64
    plan = first["plan"]
    assert isinstance(plan, dict)
    assert plan["identity"] == {
        "name": "Aster",
        "gender": "feminine",
        "persona_modified": False,
        "conversation_modified": False,
    }
    rendered = json.dumps(first)
    assert "secret-value" not in rendered
    assert plan["installs_host"] is False
    assert plan["requires_confirmation"] is True


@pytest.mark.parametrize("target_os", ["macos", "windows"])
@pytest.mark.parametrize("host", ["hermes", "openclaw"])
def test_host_setup_without_host_name_or_credentials(host, target_os) -> None:
    request = {"schema": REQUEST_SCHEMA, "action": "host_setup", "host": host}
    response = handle_request(request, probes=_probes(hermes=False, openclaw=False), target_os=target_os)
    assert response["host_setup"]["action"] == "install_qualified"
    assert response["host_setup"]["execution_enabled"] is False
    assert response["host_setup"]["target_os"] == target_os
    with pytest.raises(CompanionAppRequestError):
        handle_request({**request, "url": "https://untrusted.example"}, probes=_probes())
    with pytest.raises(CompanionAppRequestError):
        handle_request({**request, "action": "download"}, probes=_probes())


def test_unknown_existing_host_not_approved_for_plugin_preparation() -> None:
    probes = (HostProbe(HostKind.HERMES, True, True, "/fixture/hermes", "/fixture/profile", "Hermes 0.99.0", "verified"), _probes()[1])
    inspection = _inspect(probes=probes)["inspection"]
    assert inspection["usable_hosts"] == ["openclaw"]
    assert inspection["host_setup_options"]["hermes"]["action"] == "review_existing"
    for action in ("plan", "prepare"):
        with pytest.raises(CompanionAppRequestError, match="not recognized"):
            handle_request(_plan(action=action), probes=probes)


@pytest.mark.parametrize(
    ("host", "expected"),
    [("hermes", 7), ("openclaw", 8)],
)
def test_prepare_uses_verified_context_for_exact_technical_plan(
    tmp_path: Path, host: str, expected: int
) -> None:
    profile = (tmp_path / host).absolute()
    probes = (
        HostProbe(HostKind.HERMES, True, True, "/fixture/hermes", str(profile if host == "hermes" else tmp_path / "hermes"), "Hermes 0.21.0", "verified"),
        HostProbe(HostKind.OPENCLAW, True, True, "/fixture/openclaw", str(profile if host == "openclaw" else tmp_path / "openclaw"), "OpenClaw 2026.9.1-beta.1", "verified"),
    )
    result = handle_request(
        _plan(host=host, action="prepare"),
        probes=probes,
        finder=lambda _name: "/fixture/codex",
        preparation_context=_preparation(tmp_path),
    )
    assert result["ok"] is True
    assert result["action"] == "prepare"
    summary = result["review_text"]
    assert "Set up Aster on" in summary
    assert "feminine" in summary
    assert "separate confirmation" in summary
    assert "Background memory maintenance" in summary
    assert "Technical details" in summary
    assert str(profile) not in summary
    assert "secret-value" not in summary
    for step in result["reviewed_plan"]["steps"]:
        assert step["description"] in summary
    assert len(result["plan"]["steps"]) == expected
    assert len(result["reviewed_plan"]["steps"]) == 6
    assert len(result["confirmation_digest"]) == 64
    assert result["confirmation_digest"] == hashlib.sha256(
        json.dumps(result["plan"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    rendered = json.dumps(result)
    assert str(profile) in rendered
    assert "secret-value" not in rendered


def test_prepare_rejects_missing_unknown_or_tampered_context(tmp_path: Path) -> None:
    with pytest.raises(CompanionAppRequestError, match="unavailable"):
        handle_request(
            _plan(action="prepare"),
            probes=_probes(),
            finder=lambda _name: "/fixture/codex",
            environment={},
        )
    context = _preparation(tmp_path)
    environment = {
        "DAEMON_HARNESS_COMPANION_PACKAGE_ROOT": str(context.package_root),
        "DAEMON_HARNESS_COMPANION_INSTALL_HOME": str(context.companion_home),
        "DAEMON_HARNESS_COMPANION_BUNDLE_ROOT": str(context.bundle_root),
        "DAEMON_HARNESS_COMPANION_UV_EXECUTABLE": str(context.uv_executable),
        "DAEMON_HARNESS_COMPANION_UV_SHA256": context.uv_sha256,
        "DAEMON_HARNESS_COMPANION_TARGET_OS": context.target_os,
        "DAEMON_HARNESS_COMPANION_OPENCLAW_API_PORT": "8000",
        "DAEMON_HARNESS_COMPANION_OPENCLAW_GATEWAY_PORT": "18789",
    }
    environment["DAEMON_HARNESS_COMPANION_INSTALL_UNKNOWN"] = "refuse"
    with pytest.raises(CompanionAppRequestError, match="environment"):
        preparation_context_from_environment(environment)
    environment.pop("DAEMON_HARNESS_COMPANION_INSTALL_UNKNOWN")
    context.uv_executable.write_bytes(b"altered")
    with pytest.raises(CompanionAppRequestError, match="uv verification"):
        preparation_context_from_environment(environment)


@pytest.mark.parametrize(
    "provider",
    [
        {
            "kind": "openai_api",
            "model": "gpt-5.6-terra",
            "base_url": "",
            "credential_available": True,
        },
        {
            "kind": "openai_compatible_api",
            "model": "grok-capable-model",
            "base_url": "https://provider.example/v1",
            "credential_available": True,
        },
        {
            "kind": "codex_cli",
            "model": "gpt-5.6-terra",
            "base_url": "",
            "credential_available": False,
        },
    ],
)
def test_plan_accepts_only_qualified_provider_shapes(
    provider: dict[str, object],
) -> None:
    result = handle_request(
        _plan(provider), probes=_probes(), finder=lambda _name: "/fixture/codex"
    )
    assert result["ok"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        lambda request: request.update({"secret": "secret-value"}),
        lambda request: request.update({"bundle_root": "/untrusted"}),
        lambda request: request.update({"companion_home": "/untrusted"}),
        lambda request: request["provider"].update({"api_key": "secret-value"}),
        lambda request: request.update({"daemon_name": "Aster\nInjected"}),
        lambda request: request.update({"daemon_gender": "neutral"}),
        lambda request: request.update({"host": "unknown"}),
    ],
)
def test_unknown_sensitive_or_invalid_fields_are_rejected(mutation) -> None:
    request = _plan()
    mutation(request)
    with pytest.raises(CompanionAppRequestError):
        handle_request(
            request, probes=_probes(), finder=lambda _name: "/fixture/codex"
        )


def test_plan_refuses_absent_host_and_missing_codex() -> None:
    with pytest.raises(CompanionAppRequestError, match="not installed"):
        handle_request(
            _plan(), probes=_probes(hermes=False), finder=lambda _name: None
        )
    with pytest.raises(CompanionAppRequestError, match="Codex CLI"):
        handle_request(_plan(), probes=_probes(), finder=lambda _name: None)


def test_cli_reads_json_from_stdin_and_returns_only_public_json() -> None:
    request = json.dumps({"schema": REQUEST_SCHEMA, "action": "inspect"})
    result = subprocess.run(
        [sys.executable, "-m", "daemons_companion.desktop.app", "request"],
        input=request,
        check=True,
        capture_output=True,
        text=True,
    )
    response = json.loads(result.stdout)
    assert response["schema"] == "daemon.companion-response.v1"
    assert response["ok"] is True
    assert response["action"] == "inspect"
    assert result.stderr == ""


def test_cli_returns_a_bounded_error_without_echoing_input() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "daemons_companion.desktop.app", "request"],
        input='{"schema":"wrong","action":"inspect","secret":"do-not-echo"}',
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "do-not-echo" not in result.stdout
    response = json.loads(result.stdout)
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert result.stderr == ""
