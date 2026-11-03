from __future__ import annotations

import json
from pathlib import Path

import pytest

import daemons_companion.setup.plan as module
from daemons_companion.setup.plan import (
    HostKind,
    HostProbe,
    ProviderKind,
    ProviderSelection,
    SetupRequest,
    build_setup_plan,
    detect_hosts,
)


def test_version_probe_is_bounded_and_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def run(arguments, **kwargs):
        observed["arguments"] = arguments
        observed.update(kwargs)
        return type("Result", (), {"returncode": 0, "stdout": "host 1.2.3\n", "stderr": ""})()

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module._read_version("/safe/bin/host") == ("host 1.2.3", "verified")
    assert observed == {
        "arguments": ["/safe/bin/host", "--version"],
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 3,
    }


def test_detection_is_read_only_and_handles_both_hosts(tmp_path: Path) -> None:
    (tmp_path / ".hermes").mkdir()
    version_calls: list[str] = []

    def finder(name: str) -> str | None:
        return {"hermes": "/safe/bin/hermes", "openclaw": "/safe/bin/openclaw"}.get(name)

    def version_reader(executable: str) -> tuple[str, str]:
        version_calls.append(executable)
        return ("synthetic 1.2.3", "verified")

    hermes, openclaw = detect_hosts(
        home=tmp_path,
        finder=finder,
        version_reader=version_reader,
    )
    assert hermes == HostProbe(
        host=HostKind.HERMES,
        detected=True,
        usable=True,
        executable="/safe/bin/hermes",
        config_path=str(tmp_path / ".hermes"),
        version="synthetic 1.2.3",
        version_status="verified",
    )
    assert openclaw.detected is True
    assert openclaw.usable is True
    assert openclaw.config_path == ""
    assert version_calls == ["/safe/bin/hermes", "/safe/bin/openclaw"]
    assert sorted(path.name for path in tmp_path.iterdir()) == [".hermes"]


def test_config_marker_without_executable_is_detected_but_not_usable(tmp_path: Path) -> None:
    (tmp_path / ".openclaw").mkdir()
    probes = detect_hosts(
        home=tmp_path,
        finder=lambda _name: None,
        version_reader=lambda _path: pytest.fail("version probe must not run"),
    )
    by_host = {probe.host: probe for probe in probes}
    assert by_host[HostKind.HERMES].detected is False
    assert by_host[HostKind.OPENCLAW].detected is True
    assert by_host[HostKind.OPENCLAW].usable is False


def test_plan_refuses_to_install_or_target_an_absent_host() -> None:
    request = SetupRequest(
        host=HostKind.HERMES,
        daemon_name="Iris",
        daemon_gender="feminine",
        provider=ProviderSelection(
            ProviderKind.OPENAI_API,
            model="gpt-test",
            credential_available=True,
        ),
    )
    with pytest.raises(ValueError, match="not installed and usable"):
        build_setup_plan(request, ())


def test_plan_is_exact_public_and_contains_no_credential_value() -> None:
    probe = HostProbe(
        host=HostKind.OPENCLAW,
        detected=True,
        usable=True,
        executable="/safe/bin/openclaw",
    )
    request = SetupRequest(
        host=HostKind.OPENCLAW,
        daemon_name="Noé",
        daemon_gender="masculine",
        provider=ProviderSelection(
            ProviderKind.OPENAI_COMPATIBLE_API,
            model="grok-compatible",
            base_url="https://provider.example/v1",
            credential_available=True,
        ),
    )
    public = build_setup_plan(request, (probe,)).to_public_dict()
    encoded = json.dumps(public, sort_keys=True)
    assert public["installs_host"] is False
    assert public["requires_confirmation"] is True
    assert public["identity"] == {
        "name": "Noé",
        "gender": "masculine",
        "persona_modified": False,
        "conversation_modified": False,
    }
    assert "openclaw-plugin" in [step["code"] for step in public["steps"]]
    assert "api-key-value-for-test" not in encoded
    assert public["provider"]["required_secret_names"] == ["LOCAL_LLM_API_KEY"]


@pytest.mark.parametrize(
    "selection,codex_available,match",
    [
        (ProviderSelection(ProviderKind.OPENAI_API, "model"), False, "credential"),
        (ProviderSelection(ProviderKind.CODEX_CLI, "model"), False, "not available"),
        (
            ProviderSelection(
                ProviderKind.OPENAI_COMPATIBLE_API,
                "model",
                base_url="http://remote.example/v1",
                credential_available=True,
            ),
            False,
            "HTTPS",
        ),
        (
            ProviderSelection(
                ProviderKind.OPENAI_COMPATIBLE_API,
                "model",
                base_url="https://user:password@example.test/v1",
                credential_available=True,
            ),
            False,
            "without credentials",
        ),
    ],
)
def test_provider_validation_fails_closed(
    selection: ProviderSelection,
    codex_available: bool,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        selection.validated(codex_available=codex_available)


def test_loopback_compatible_provider_can_use_no_secret() -> None:
    selection = ProviderSelection(
        ProviderKind.OPENAI_COMPATIBLE_API,
        "local-model",
        base_url="http://127.0.0.1:1234/v1",
    ).validated()
    assert selection.public_configuration()["required_secret_names"] == []


def test_codex_plan_uses_official_login_without_secret() -> None:
    selection = ProviderSelection(
        ProviderKind.CODEX_CLI,
        "gpt-test",
    ).validated(codex_available=True)
    assert selection.public_configuration() == {
        "kind": "codex_cli",
        "settings": {"LLM_PROVIDER": "codex_cli", "CODEX_CLI_MODEL": "gpt-test"},
        "required_secret_names": [],
        "credential_configured": False,
    }
