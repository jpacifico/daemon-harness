"""Opt-in real launchd upgrade transitions with synthetic foreground services.

No model, Telegram, credentials or personal profile is used. The fixture checks
native service ownership and release selection, not Hermes conversation quality.
"""

import json
import os
import sys
import time

import pytest
from daemons_companion.runtime.upgrade import Upgrade
from daemons_companion.runtime.release import verify_current_release
from daemons_companion.runtime.services import (
    BackgroundJob,
    MacLaunchdSchedulerBackend,
    render_job_definition,
)
from tests.test_companion_source_upgrade import setup as isolated_setup

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("DAEMONS_TEST_NATIVE_UPGRADE") != "1",
    reason="Opt-in disposable launchd qualification",
)


@pytest.mark.parametrize("failure", [False, True])
def test_native_upgrade_restart_and_rollback(tmp_path, monkeypatch, failure):
    factory, initial, _, memory, provider, _ = isolated_setup.__wrapped__(
        tmp_path, monkeypatch
    )
    scheduler = MacLaunchdSchedulerBackend(
        tmp_path / "launchagents",
        user_id=os.getuid(),
        job_namespace=initial.host.installation_id,
    )
    codes = [j.code for j in initial.jobs()]
    assert all(scheduler.capture(code) is None for code in codes)
    health = tmp_path / "health"
    health.mkdir()

    class NativeUpgrade(Upgrade):
        def jobs(self):
            release = verify_current_release(self.runtime)
            jobs = []
            for code in codes:
                script = (
                    "import json,os,time;from pathlib import Path;"
                    f"Path({str(health / code)!r}).write_text(json.dumps({{'release':{release.name!r},'pid':os.getpid()}}));"
                    "time.sleep(600)"
                )
                jobs.append(
                    BackgroundJob(
                        code,
                        sys.executable,
                        ("-I", "-B", "-c", script),
                        str(tmp_path),
                        str(tmp_path / (code + ".log")),
                        True,
                    )
                )
            return tuple(jobs)

        def health(self):
            revision = verify_current_release(self.runtime).name
            try:
                return all(
                    json.loads((health / c).read_text())["release"] == revision
                    for c in codes
                )
            except (ValueError, OSError):
                return False

    old = NativeUpgrade(
        initial.composition, initial.host, scheduler, "777001", runner=initial.runner
    )
    candidate = factory()

    def fail(phase):
        if phase == "upgrade-start" and failure:
            raise RuntimeError("Synthetic post-start failure")

    upgrade = NativeUpgrade(
        candidate.composition,
        initial.host,
        scheduler,
        "777001",
        runner=initial.runner,
        hook=fail,
    )
    started = time.monotonic()
    try:
        for j in old.jobs():
            scheduler.install(j, render_job_definition(j, "macos"))
        limit = time.monotonic() + 20
        while not old.health():
            assert time.monotonic() < limit
            time.sleep(0.1)
        old_release = verify_current_release(old.runtime)
        review = upgrade.review()
        result = upgrade.run(review["revision"])
        assert result["status"] == ("rolled_back" if failure else "committed")
        expected = old_release.name if failure else review["to"]
        assert verify_current_release(upgrade.runtime).name == expected
        assert all(
            scheduler.matches(j, render_job_definition(j, "macos"))
            for j in upgrade.jobs()
        )
        assert memory.read_text() == "Synthetic canonical memory remains exact.\n"
        assert provider.read_text() == '{"kind":"synthetic-account"}'
    finally:
        for code in codes:
            scheduler.restore(code, None)
        assert all(scheduler.capture(code) is None for code in codes)
    print(
        json.dumps(
            {
                "native_upgrade_seconds": round(time.monotonic() - started, 3),
                "failure": failure,
                "cleanup_verified": True,
            }
        )
    )
