"""Synthetic ownership, interruption and preservation checks for setup discard."""

import base64
import json
from pathlib import Path
import plistlib
import tempfile
import unittest

from daemons_companion.setup.discard import Discard, MARKER, Refused
from daemons_companion.runtime.services import (
    HERMES_GATEWAY_JOB,
    scoped_job_code,
)


class Scheduler:
    target_os = "macos"

    def __init__(self):
        self.jobs = {}
        self.fail = False

    def capture(self, code):
        raw = self.jobs.get(code)
        return (
            None
            if raw is None
            else json.dumps({"definition": base64.b64encode(raw).decode()}).encode()
        )

    def require_exclusive(self, attempt, memory, codes):
        pass

    def restore(self, code, capture):
        if self.fail:
            raise RuntimeError("synthetic failure")
        self.jobs.pop(code, None)


class DiscardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "instance-ABC123"
        self.attempt = self.root / "hosts/hermes/installations" / ("a" * 32)
        self.attempt.mkdir(parents=True)
        self.owner = {
            "schema": "daemon.companion-host-install.v1",
            "installation_id": self.attempt.name,
            "version": "0.21.5",
            "revision": "b" * 40,
        }
        (self.attempt / "owner.json").write_text(json.dumps(self.owner))
        self.source = self.attempt / "source"
        self.source.mkdir()
        (self.source / "downloaded").write_text("program")
        self.memory = self.attempt / "daemon/data/memory"
        self.memory.mkdir(parents=True)
        (self.memory / "exact.md").write_bytes(b"Exact synthetic memory.\n")
        self.scheduler = Scheduler()
        self.operation = Discard(
            self.root,
            self.base,
            launch_agents=self.base / "LaunchAgents",
            scheduler_factory=lambda _: self.scheduler,
            processes=lambda: "",
        )
        self.addCleanup(self.temp.cleanup)

    def apply(self):
        return self.operation.apply(self.operation.inspect()["revision"])

    def test_partial_host_without_pointer_is_removed_and_memory_retained(self):
        result = self.apply()
        self.assertTrue(result["done"])
        self.assertFalse(self.source.exists())
        self.assertEqual(
            (self.memory / "exact.md").read_bytes(), b"Exact synthetic memory.\n"
        )
        self.assertEqual(json.loads((self.root / MARKER).read_text())["status"], "done")
        self.assertTrue((self.attempt / "owner.json").is_file())

    def test_root_before_host_acquisition(self):
        root = self.base / "instance-DEF456"
        root.mkdir()
        op = Discard(root, self.base, processes=lambda: "")
        self.assertTrue(op.apply(op.inspect()["revision"])["done"])

    def test_sibling_is_untouched(self):
        sibling = self.base / "instance-DEF456"
        sibling.mkdir()
        (sibling / "keep").write_text("keep")
        self.apply()
        self.assertEqual((sibling / "keep").read_text(), "keep")

    def test_completed_receipt_refused(self):
        (self.attempt / "daemon-setup.json").write_text("{}")
        with self.assertRaisesRegex(Refused, "completed"):
            self.apply()
        self.assertTrue(self.source.exists())

    def test_committed_daemon_without_receipt_refused(self):
        folder = self.attempt / "daemon/transactions/t"
        folder.mkdir(parents=True)
        (folder / "journal.json").write_text('{"status":"committed"}')
        with self.assertRaisesRegex(Refused, "completed"):
            self.apply()

    def test_missing_owner_refused(self):
        (self.attempt / "owner.json").unlink()
        with self.assertRaises(Refused):
            self.apply()

    def test_changed_plan_requires_new_confirmation(self):
        revision = self.operation.inspect()["revision"]
        (self.attempt / "python").mkdir()
        with self.assertRaisesRegex(Refused, "changed"):
            self.operation.apply(revision)
        self.assertTrue(self.source.exists())

    def test_program_link_is_not_traversed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.attempt / "python").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(Refused, "unsafe"):
            self.apply()
        self.assertTrue(outside.exists())

    def test_nested_link_target_survives(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        (self.source / "link").symlink_to(outside, target_is_directory=True)
        self.apply()
        self.assertEqual((outside / "keep").read_text(), "keep")

    def test_memory_cannot_overlap_programs(self):
        profile = self.attempt / "profile"
        profile.mkdir()
        (profile / ".env").write_text(
            "DAEMON_CORE_MEMORY_ROOT=" + str(self.source) + "\n"
        )
        with self.assertRaisesRegex(Refused, "memory"):
            self.apply()

    def job(self):
        code = scoped_job_code(HERMES_GATEWAY_JOB, self.attempt.name)
        self.scheduler.jobs[code] = plistlib.dumps(
            {
                "Label": code,
                "ProgramArguments": [str(self.source / "python")],
                "WorkingDirectory": str(self.attempt),
            }
        )
        return code

    def test_owned_service_stopped_before_program_delete(self):
        code = self.job()
        self.operation.hook = lambda step: self.assertNotIn(code, self.scheduler.jobs)
        self.apply()
        self.assertEqual(self.scheduler.jobs, {})

    def test_failed_service_cleanup_preserves_program_and_can_retry(self):
        self.job()
        self.scheduler.fail = True
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertTrue(self.source.exists())
        self.assertEqual(
            json.loads((self.root / MARKER).read_text())["status"], "failed"
        )
        self.scheduler.fail = False
        self.assertTrue(self.apply()["done"])

    def test_foreign_service_definition_refused(self):
        code = self.job()
        self.scheduler.jobs[code] = plistlib.dumps(
            {
                "Label": code,
                "ProgramArguments": ["/usr/bin/python3"],
                "WorkingDirectory": str(self.attempt),
            }
        )
        with self.assertRaisesRegex(Refused, "services"):
            self.apply()

    def test_partial_file_removal_can_retry(self):
        (self.attempt / "python").mkdir()

        def fail(step):
            if step == "program-removed":
                raise OSError("synthetic")

        self.operation.hook = fail
        with self.assertRaises(OSError):
            self.apply()
        self.assertFalse(self.source.exists())
        self.operation.hook = lambda _: None
        self.assertTrue(self.apply()["done"])
        self.assertEqual(
            (self.memory / "exact.md").read_bytes(), b"Exact synthetic memory.\n"
        )

    def test_running_process_preserves_programs(self):
        self.operation.processes = lambda: str(self.attempt / "source/python")
        with self.assertRaisesRegex(Refused, "running"):
            self.apply()
        self.assertTrue(self.source.exists())

    def test_windows_definition_checks_and_cleanup(self):
        from daemons_companion.runtime.services import (
            BackgroundJob,
            render_job_definition,
        )

        self.scheduler.target_os = "windows"
        self.operation.user_sid = "S-1-5-21-123"
        code = scoped_job_code(HERMES_GATEWAY_JOB, self.attempt.name)
        self.scheduler.jobs[code] = render_job_definition(
            BackgroundJob(
                code,
                str(self.source / "python.exe"),
                (),
                str(self.attempt),
                str(self.attempt / "log"),
                True,
            ),
            "windows",
        )
        from xml.etree import ElementTree as ET

        node = ET.fromstring(self.scheduler.jobs[code])
        ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
        principal = node.find(ns + "Principals/" + ns + "Principal")
        ET.SubElement(principal, ns + "UserId").text = self.operation.user_sid
        self.scheduler.jobs[code] = ET.tostring(node)
        self.apply()
        self.assertEqual(self.scheduler.jobs, {})

    def test_reviewed_directory_replacement_requires_new_review(self):
        revision = self.operation.inspect()["revision"]
        self.source.rename(self.attempt / "old-source")
        self.source.mkdir()
        (self.source / "new").write_text("replacement")
        with self.assertRaisesRegex(Refused, "changed"):
            self.operation.apply(revision)
        self.assertTrue((self.source / "new").exists())

    def test_service_reappearing_after_stop_blocks_deletion(self):
        self.job()
        self.operation.hook = (
            lambda step: self.job() if step == "services-stopped" else None
        )
        with self.assertRaisesRegex(Refused, "services"):
            self.apply()
        self.assertTrue(self.source.exists())
        self.assertEqual(
            json.loads((self.root / MARKER).read_text())["status"], "failed"
        )

    def test_windows_locked_file_preserves_memory_and_retries(self):
        from unittest.mock import patch
        import shutil

        self.operation.target_os = "windows"
        with patch(
            "daemons_companion.setup.discard.remove_tree",
            side_effect=PermissionError("locked"),
        ):
            with self.assertRaises(PermissionError):
                self.apply()
        self.assertTrue(self.source.exists())
        self.assertEqual(
            json.loads((self.root / MARKER).read_text())["status"], "failed"
        )
        with patch(
            "daemons_companion.setup.discard.remove_tree",
            side_effect=lambda path, identity: shutil.rmtree(path),
        ):
            self.assertTrue(self.apply()["done"])
        self.assertEqual(
            (self.memory / "exact.md").read_bytes(), b"Exact synthetic memory.\n"
        )

    def test_foreign_windows_task_preserves_files(self):
        self.scheduler.target_os = "windows"

        def shared(*args):
            raise ValueError("shared")

        self.scheduler.require_exclusive = shared
        with self.assertRaisesRegex(Refused, "shared"):
            self.apply()
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
