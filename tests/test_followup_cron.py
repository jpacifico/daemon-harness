import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "integrations" / "hermes_plugin"))

import followup_cron
from daemon_core.followup import TZ, load_intent, new_followup
from daemon_core.providers.base import LLMProvider


@pytest.fixture(autouse=True)
def _tmp_memory(monkeypatch, tmp_path):
    monkeypatch.setattr("daemon_core.paths.MEMORY_ROOT", tmp_path / "memory")


class FakeCreateJob:
    def __init__(self, job_id="job123"):
        self.job_id = job_id
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return {"id": self.job_id}


def test_schedule_writes_script_and_creates_job(tmp_path):
    hermes_home = tmp_path / "hermes"
    fu = new_followup(
        user_id="u1",
        fire_at="2026-06-30T18:00",
        topic="restitution",
        evidence="vendredi libération",
        origin={"platform": "telegram", "chat_id": "1234567890"},
    )
    fake = FakeCreateJob()
    job_id = followup_cron.schedule_followup(fu, hermes_home=str(hermes_home), _create_job=fake)

    assert job_id == "job123"
    # Job created with the expected parameters.
    assert fake.kwargs["no_agent"] is True
    assert fake.kwargs["deliver"] == "origin"
    assert fake.kwargs["origin"] == {"platform": "telegram", "chat_id": "1234567890"}
    assert fake.kwargs["schedule"] == "2026-06-30T18:00"
    assert fake.kwargs["repeat"] == 1
    assert fake.kwargs["script"] == f"followup_{fu.id}.sh"

    # Relay script exists, is executable and calls the correct entry point.
    script = hermes_home / "scripts" / f"followup_{fu.id}.sh"
    assert script.exists()
    body = script.read_text()
    assert "followup_cron fire" in body and fu.id in body
    assert str(ROOT / "src") in body
    assert 'FOLLOWUP_LLM_PROVIDER:-codex_cli' in body
    assert sys.executable in body
    assert str(hermes_home / "hermes-agent") not in body

    # Job ID persisted on the intent.
    assert load_intent(fu.id).job_id == "job123"


def test_schedule_failure_cleans_script_and_returns_none(tmp_path):
    hermes_home = tmp_path / "hermes"
    fu = new_followup(user_id="u1", fire_at="2026-06-30T18:00", topic="t", evidence="e")

    def boom(**kwargs):
        raise RuntimeError("cron down")

    job_id = followup_cron.schedule_followup(fu, hermes_home=str(hermes_home), _create_job=boom)
    assert job_id is None
    assert not (hermes_home / "scripts" / f"followup_{fu.id}.sh").exists()
    assert load_intent(fu.id).job_id is None


def test_cancel_removes_job_script_and_marks_cancelled(tmp_path):
    hermes_home = tmp_path / "hermes"
    fu = new_followup(user_id="u1", fire_at="2026-06-30T18:00", topic="t", evidence="e")
    followup_cron.schedule_followup(fu, hermes_home=str(hermes_home), _create_job=FakeCreateJob("jX"))
    fu = load_intent(fu.id)  # Reload with the job ID.

    removed_ids = []

    def fake_remove(job_id):
        removed_ids.append(job_id)
        return True

    ok = followup_cron.cancel_followup(fu, hermes_home=str(hermes_home), _remove_job=fake_remove)
    assert ok is True
    assert removed_ids == ["jX"]
    assert not (hermes_home / "scripts" / f"followup_{fu.id}.sh").exists()
    assert load_intent(fu.id).state == "cancelled"


class RescheduleProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        return json.dumps(
            {
                "action": "reschedule",
                "message": "",
                "fire_at": "2026-06-30T19:15",
                "reason": "moment plus naturel",
            }
        )


class UnexpectedProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        raise AssertionError("un tir obsolète ne doit pas rappeler le LLM")


def test_fire_and_reconcile_creates_new_job_after_reschedule(tmp_path):
    hermes_home = tmp_path / "hermes"
    fu = new_followup(
        user_id="u1",
        fire_at="2026-06-30T18:00",
        topic="restitution",
        evidence="vendredi libération",
        origin={"platform": "telegram", "chat_id": "1234567890"},
    )
    fu.job_id = "old-job"
    followup_cron.fu_mod.save_intent(fu)
    fake = FakeCreateJob("new-job")

    message = followup_cron.fire_and_reconcile(
        fu.id,
        hermes_home=str(hermes_home),
        provider=RescheduleProvider(),
        now=datetime(2026, 6, 30, 18, 0, tzinfo=TZ),
        _create_job=fake,
    )

    assert message == ""
    updated = load_intent(fu.id)
    assert updated.state == "pending"
    assert updated.fire_at == "2026-06-30T19:15"
    assert updated.job_id == "new-job"
    assert fake.kwargs["schedule"] == "2026-06-30T19:15"
    assert (hermes_home / "scripts" / f"followup_{fu.id}.sh").exists()


def test_stale_duplicate_after_reschedule_preserves_new_job_and_relay(tmp_path):
    """Reproduce duplicate Hermes delivery of an old one-shot job."""
    hermes_home = tmp_path / "hermes"
    fu = new_followup(
        user_id="u1",
        fire_at="2026-06-30T18:00",
        topic="restitution",
        evidence="vendredi libération",
        origin={"platform": "telegram", "chat_id": "1234567890"},
    )
    fu.job_id = "old-job"
    followup_cron.fu_mod.save_intent(fu)
    fake = FakeCreateJob("new-job")

    first = followup_cron.fire_and_reconcile(
        fu.id,
        hermes_home=str(hermes_home),
        provider=RescheduleProvider(),
        now=datetime(2026, 6, 30, 18, 0, tzinfo=TZ),
        _create_job=fake,
    )
    relay = hermes_home / "scripts" / f"followup_{fu.id}.sh"

    second = followup_cron.fire_and_reconcile(
        fu.id,
        hermes_home=str(hermes_home),
        provider=UnexpectedProvider(),
        now=datetime(2026, 6, 30, 18, 0, 5, tzinfo=TZ),
        _create_job=fake,
    )

    updated = load_intent(fu.id)
    assert first == second == ""
    assert updated.state == "pending"
    assert updated.fire_at == "2026-06-30T19:15"
    assert updated.job_id == "new-job"
    assert updated.reschedule_count == 1
    assert relay.exists()


def test_relay_runs_with_detached_profile_and_private_log(tmp_path, monkeypatch):
    """Execute the relay, not only its text; no provider or real host is used."""
    import os
    import shlex
    import subprocess
    from datetime import timedelta

    home = tmp_path / "profile with 'quotes' and $literal"
    home.mkdir()
    memory = tmp_path / "memory"
    log = tmp_path / "private logs"
    settings = {"DAEMON_CORE_MEMORY_ROOT": str(memory), "DAEMON_CORE_LOG_DIR": str(log)}
    (home / ".env").write_text("\n".join(k + "=" + shlex.quote(v) for k, v in settings.items()) + "\n")
    monkeypatch.delenv("DAEMON_CORE_PYTHON", raising=False)
    fu = new_followup(user_id="isolated-test", fire_at=(datetime.now(TZ) + timedelta(days=1)).isoformat(),
                      topic="Synthetic future follow-up", evidence="Synthetic source")
    fu.job_id = "synthetic-already-scheduled"
    followup_cron.fu_mod.save_intent(fu)
    before = (memory / "followups/intents" / (fu.id + ".json")).read_bytes()
    name = followup_cron._write_relay_script(fu.id, str(home))
    result = subprocess.run(["bash", str(home / "scripts" / name)], text=True,
                            capture_output=True, timeout=15,
                            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert (log / "followups.log").exists()
    assert (memory / "followups/intents" / (fu.id + ".json")).read_bytes() == before
    assert not (home / "hermes-agent").exists()


def test_relay_preserves_venv_entrypoint_and_host_import_path(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import shlex

    python = tmp_path / "venv entry"
    python.symlink_to(sys.executable)
    monkeypatch.setenv("DAEMON_CORE_PYTHON", str(python))
    host = tmp_path / "Hermes install"
    monkeypatch.setattr(followup_cron.importlib.util, "find_spec",
                        lambda _: SimpleNamespace(submodule_search_locations=[str(host / "cron")]))
    body = followup_cron._relay_script_body("synthetic-followup", str(tmp_path / "profile"))
    assert shlex.quote(str(python)) + " -m followup_cron" in body
    assert str(host) in body
    assert "PYTHONPATH:+" in body


def test_relay_rejects_missing_configured_interpreter(tmp_path, monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_PYTHON", str(tmp_path / "missing-python"))
    with pytest.raises(ValueError, match="interpreter is unavailable"):
        followup_cron._relay_script_body("synthetic-followup", str(tmp_path))
