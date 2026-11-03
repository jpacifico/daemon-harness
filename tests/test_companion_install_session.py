from __future__ import annotations

import json
from pathlib import Path

import pytest

from daemons_companion.setup.plan import (
    HostKind,
    HostProbe,
    ProviderKind,
    ProviderSelection,
    SetupRequest,
    build_setup_plan,
)
from daemons_companion.setup.install_session import (
    CompanionInstallLayout,
    execute_prepared_installation,
    prepare_installation,
)
from daemons_companion.openclaw.profile import (
    create_disposable_openclaw_marker,
)
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    create_disposable_companion_marker,
)
from daemons_companion.setup.transaction import TransactionApplyError


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


def _layout(tmp_path: Path, host: HostKind, *, api_port: int = 8000):
    companion = (tmp_path / "companion").absolute()
    create_disposable_companion_marker(companion)
    runtime = CompanionRuntimeLayout(companion, _bundle(tmp_path / "bundle"))
    uv = tmp_path / "uv"
    uv.write_bytes(b"synthetic uv")
    uv.chmod(0o755)
    if host == HostKind.HERMES:
        profile = (tmp_path / "hermes").absolute()
        profile.mkdir()
        (profile / ".daemon-harness-disposable-profile").write_text(
            "daemon-harness disposable Hermes profile v1\n", encoding="utf-8"
        )
        install = tmp_path / "hermes-install"
        (install / "venv/bin").mkdir(parents=True)
        (install / "venv/bin/python").write_bytes(b"python")
        (install / "venv/bin/python").chmod(0o755)
        return CompanionInstallLayout(
            runtime, uv, "a" * 64, "macos", hermes_home=profile
        ), profile
    profile = (tmp_path / "openclaw").absolute()
    create_disposable_openclaw_marker(profile)
    return CompanionInstallLayout(
        runtime,
        uv,
        "a" * 64,
        "macos",
        openclaw_state=profile,
        openclaw_api_port=api_port,
        openclaw_gateway_port=18789,
    ), profile


def _reviewed(host: HostKind, profile: Path):
    provider = ProviderSelection(
        ProviderKind.OPENAI_COMPATIBLE_API,
        "synthetic-model",
        base_url="http://127.0.0.1:18888/v1",
    )
    return build_setup_plan(
        SetupRequest(host, "Aster", "feminine", provider),
        (
            HostProbe(
                HostKind.HERMES,
                True,
                True,
                "/fixture/hermes",
                str(profile) if host == HostKind.HERMES else "/fixture/hermes-state",
                "Hermes 0.21.0",
                "verified",
            ),
            HostProbe(
                HostKind.OPENCLAW,
                True,
                True,
                "/fixture/openclaw",
                str(profile) if host == HostKind.OPENCLAW else "/fixture/openclaw-state",
                "OpenClaw 2026.9.1-beta.1",
                "verified",
            ),
        ),
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [(HostKind.HERMES, 7), (HostKind.OPENCLAW, 8)],
)
def test_prepare_exposes_exact_existing_composition(
    tmp_path: Path, host: HostKind, expected: int
) -> None:
    layout, profile = _layout(tmp_path, host)
    prepared = prepare_installation(_reviewed(host, profile), layout)
    public = prepared.public_dict()
    assert len(prepared.plan.steps) == expected
    assert public["confirmation_digest"] == prepared.confirmation_digest
    assert len(prepared.confirmation_digest) == 64
    rendered = json.dumps(public)
    assert "synthetic-model" in rendered
    assert "secret-value" not in rendered
    assert str(profile) in rendered


def test_openclaw_port_changes_confirmation_digest(tmp_path: Path) -> None:
    first_layout, profile = _layout(tmp_path / "first", HostKind.OPENCLAW, api_port=18000)
    second_layout, second_profile = _layout(
        tmp_path / "second", HostKind.OPENCLAW, api_port=18001
    )
    first = prepare_installation(_reviewed(HostKind.OPENCLAW, profile), first_layout)
    second = prepare_installation(
        _reviewed(HostKind.OPENCLAW, second_profile), second_layout
    )
    assert first.confirmation_digest != second.confirmation_digest


def test_crossed_profile_is_refused_before_execution(tmp_path: Path) -> None:
    layout, profile = _layout(tmp_path, HostKind.OPENCLAW)
    crossed = _reviewed(HostKind.OPENCLAW, profile.parent / "other")
    with pytest.raises(ValueError, match="profile differs"):
        prepare_installation(crossed, layout)


class RecordingAdapter:
    def __init__(self, *, fail_code: str = "") -> None:
        self.events: list[str] = []
        self.fail_code = fail_code

    def snapshot(self, step, _snapshot_dir: Path) -> None:
        self.events.append(f"snapshot:{step.code}")

    def apply(self, step) -> None:
        self.events.append(f"apply:{step.code}")
        if step.code == self.fail_code:
            raise RuntimeError("synthetic failure containing secret-value")

    def verify(self, step) -> None:
        self.events.append(f"verify:{step.code}")

    def rollback(self, step, _snapshot_dir: Path) -> None:
        self.events.append(f"rollback:{step.code}")


def test_execute_requires_technical_digest_before_snapshot(tmp_path: Path) -> None:
    layout, profile = _layout(tmp_path, HostKind.HERMES)
    reviewed = _reviewed(HostKind.HERMES, profile)
    prepared = prepare_installation(reviewed, layout)
    adapter = RecordingAdapter()
    with pytest.raises(ValueError, match="differs"):
        execute_prepared_installation(
            prepared,
            adapter=adapter,
            transactions_root=tmp_path / "transactions",
            transaction_id="stale",
            confirmed_digest="0" * 64,
        )
    assert adapter.events == []


def test_execute_delegates_success_and_rollback_without_leaking_error(
    tmp_path: Path,
) -> None:
    layout, profile = _layout(tmp_path, HostKind.OPENCLAW)
    prepared = prepare_installation(_reviewed(HostKind.OPENCLAW, profile), layout)
    successful = RecordingAdapter()
    result = execute_prepared_installation(
        prepared,
        adapter=successful,
        transactions_root=tmp_path / "success-transactions",
        transaction_id="success",
        confirmed_digest=prepared.confirmation_digest,
    )
    assert result.status == "committed"
    assert len([event for event in successful.events if event.startswith("apply:")]) == 8

    failing = RecordingAdapter(fail_code=prepared.plan.steps[2].code)
    with pytest.raises(TransactionApplyError) as caught:
        execute_prepared_installation(
            prepared,
            adapter=failing,
            transactions_root=tmp_path / "failed-transactions",
            transaction_id="failed",
            confirmed_digest=prepared.confirmation_digest,
        )
    assert "secret-value" not in str(caught.value)
    journal = (tmp_path / "failed-transactions/failed/journal.json").read_text()
    assert "secret-value" not in journal
    assert any(event.startswith("rollback:") for event in failing.events)


@pytest.mark.parametrize("host", [HostKind.HERMES, HostKind.OPENCLAW])
@pytest.mark.parametrize("target_os", ["macos", "windows"])
def test_prepared_plan_preserves_explicit_scheduler_namespace(tmp_path, host, target_os):
    from dataclasses import replace
    layout, profile = _layout(tmp_path, host)
    namespace = "c" * 32
    layout = replace(layout, target_os=target_os, job_namespace=namespace)
    scoped = prepare_installation(_reviewed(host, profile), layout)
    default = prepare_installation(_reviewed(host, profile), replace(layout, job_namespace=None))
    assert scoped.confirmation_digest != default.confirmation_digest
    serialized = json.dumps(scoped.plan.to_public_dict())
    assert "com.daemonharness.test." + namespace + ".after-turn-worker" in serialized
    assert "com.daemonharness.test." + namespace + ".dreaming" in serialized
    assert "com.daemonharness.after-turn-worker" not in serialized
    if host == HostKind.OPENCLAW:
        assert "com.daemonharness.test." + namespace + ".openclaw-api" in serialized
