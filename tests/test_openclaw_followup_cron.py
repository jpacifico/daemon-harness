from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from daemon_core.followup import TZ, load_intent, new_followup
from daemon_core.providers.base import LLMProvider


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "openclaw_plugin"
    / "followup_cron.py"
)
SPEC = importlib.util.spec_from_file_location(
    "test_openclaw_followup_cron_module",
    MODULE_PATH,
)
assert SPEC is not None and SPEC.loader is not None
followup_cron = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(followup_cron)


@pytest.fixture(autouse=True)
def _tmp_memory(monkeypatch, tmp_path):
    monkeypatch.setattr("daemon_core.paths.MEMORY_ROOT", tmp_path / "memory")


class FakeCompleted:
    def __init__(self, payload, returncode=0):
        self.returncode = returncode
        self.stdout = json.dumps(payload)
        self.stderr = ""


class FakeRunner:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return FakeCompleted(self.payload)


def _intent():
    return new_followup(
        user_id="u1",
        fire_at="2026-08-31T18:30",
        topic="prendre des nouvelles",
        evidence="on se reparle ce soir",
        origin={
            "platform": "openclaw",
            "channel": "telegram",
            "to": "telegram:12345",
            "account_id": "default",
            "thread_id": "77",
        },
    )


def test_schedule_uses_idempotent_native_command_delivery(monkeypatch):
    followup = _intent()
    runner = FakeRunner({"created": True, "job": {"id": "job-1"}})
    monkeypatch.setattr(followup_cron, "_openclaw_bin", lambda: "/openclaw")

    assert followup_cron.schedule_followup(followup, _run=runner) == "job-1"

    argv, kwargs = runner.calls[0]
    assert argv[:3] == ["/openclaw", "cron", "add"]
    assert argv[argv.index("--declaration-key") + 1].endswith(
        f":{followup.id}:r0"
    )
    command = json.loads(argv[argv.index("--command-argv") + 1])
    assert command[-3:] == ["followup_cron", "fire", followup.id]
    assert argv[argv.index("--channel") + 1] == "telegram"
    assert argv[argv.index("--to") + 1] == "telegram:12345"
    assert argv[argv.index("--thread-id") + 1] == "77"
    assert "--delete-after-run" in argv
    assert "--announce" in argv
    assert kwargs["check"] is False
    assert load_intent(followup.id).job_id == "job-1"


def test_schedule_without_exact_destination_stays_inspectable():
    followup = _intent()
    followup.origin = {"platform": "openclaw", "session_id": "opaque"}
    followup_cron.fu_mod.save_intent(followup)

    assert followup_cron.schedule_followup(followup) is None
    assert load_intent(followup.id).state == "pending"
    assert load_intent(followup.id).job_id is None


class RescheduleProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        return json.dumps(
            {
                "action": "reschedule",
                "message": "",
                "fire_at": "2026-08-31T19:15",
                "reason": "moment plus naturel",
            }
        )


def test_reschedule_creates_next_idempotence_generation(monkeypatch):
    followup = _intent()
    followup.job_id = "old-job"
    followup_cron.fu_mod.save_intent(followup)
    runner = FakeRunner({"created": True, "job": {"id": "new-job"}})
    monkeypatch.setattr(followup_cron, "_openclaw_bin", lambda: "/openclaw")

    message = followup_cron.fire_and_reconcile(
        followup.id,
        provider=RescheduleProvider(),
        now=datetime(2026, 8, 31, 18, 30, tzinfo=TZ),
        _run=runner,
    )

    assert message == ""
    updated = load_intent(followup.id)
    assert updated.state == "pending"
    assert updated.reschedule_count == 1
    assert updated.job_id == "new-job"
    argv = runner.calls[0][0]
    assert argv[argv.index("--declaration-key") + 1].endswith(
        f":{followup.id}:r1"
    )


def test_cancel_removes_native_job_and_closes_intent(monkeypatch):
    followup = _intent()
    followup.job_id = "job-1"
    followup_cron.fu_mod.save_intent(followup)
    runner = FakeRunner({"ok": True})
    monkeypatch.setattr(followup_cron, "_openclaw_bin", lambda: "/openclaw")

    assert followup_cron.cancel_followup(followup, _run=runner) is True
    assert runner.calls[0][0][-2:] == ["--json", "job-1"]
    assert load_intent(followup.id).state == "cancelled"
