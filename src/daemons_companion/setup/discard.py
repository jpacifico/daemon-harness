"""Discard an unfinished Companion setup, retaining memory and recovery records.

Only application-selected roots and receipt-owned program directories qualify.
This controller never imports the partially installed host or daemon code.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time

from daemons_companion.runtime.services import (
    AFTER_TURN_JOB, NIGHTLY_JOB, EMBEDDINGS_JOB, HERMES_GATEWAY_JOB, OPENCLAW_API_JOB, OPENCLAW_GATEWAY_JOB,
    MacLaunchdSchedulerBackend, scoped_job_code,
)
from daemons_companion.runtime.bootstrap import _source_io_path
from daemons_companion.removal.windows import WindowsRemovalScheduler, validate_task, remove_tree
from daemon_core.platform_support import _user_sid
from daemons_companion.removal.removal import (
    PROGRAMS, Refused, direct, document, read, save, encoded, digest, removable_tree,
)
from daemon_core.memory.integrity import memory_file_try_lock

SCHEMA = "daemon.companion-setup-discard.v1"
MARKER = "setup-discard.json"
CODES = (HERMES_GATEWAY_JOB, AFTER_TURN_JOB, NIGHTLY_JOB, EMBEDDINGS_JOB, OPENCLAW_GATEWAY_JOB, OPENCLAW_API_JOB)


def inside(value, root):
    # Native Windows definitions can contain extended-length paths.
    value = str(value)
    if value.startswith("\\\\?\\"):
        value = value[4:]
    path = Path(value)
    return path.is_absolute() and path.is_relative_to(root) and path.resolve().is_relative_to(root)


def native_processes():
    if sys.platform == "darwin":
        command = ["/bin/ps", "-axo", "pid=,command="]
    elif sys.platform == "win32":
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   "$ErrorActionPreference='Stop'; Get-CimInstance Win32_Process | ForEach-Object { $_.ExecutablePath; $_.CommandLine }"]
    else:
        raise Refused("platform")
    return subprocess.run(command, capture_output=True, text=True, check=True, timeout=20).stdout


class Discard:
    def __init__(self, root, base, *, launch_agents=None, scheduler_factory=None,
                 processes=native_processes, hook=lambda _: None, target_os=None, user_sid=None):
        self.root, self.base = direct(Path(root)), direct(Path(base))
        if self.root.parent != self.base or not re.fullmatch(r"instance-[A-Za-z0-9]{6}|private", self.root.name):
            raise Refused("ownership")
        if not self.root.is_dir():
            raise Refused("ownership")
        self.target_os = target_os or ("windows" if sys.platform == "win32" else "macos")
        if self.target_os not in {"macos", "windows"}:
            raise Refused("platform")
        self.user_sid = user_sid
        self.marker = self.root / MARKER
        self.launch_agents = direct(Path(launch_agents or Path.home() / "Library/LaunchAgents"))
        self.processes, self.hook = processes, hook
        self.factory = scheduler_factory or self.scheduler

    def scheduler(self, namespace):
        if self.target_os == "macos":
            return MacLaunchdSchedulerBackend(self.launch_agents, user_id=os.getuid(), job_namespace=namespace)
        if self.target_os == "windows":
            return WindowsRemovalScheduler(job_namespace=namespace)
        raise Refused("platform")

    def attempts(self):
        if (self.root / "hosts/openclaw/current.json").exists():
            return self.openclaw_attempts()
        folder = direct(self.root / "hosts/hermes/installations")
        if not folder.exists():
            return []
        entries = sorted(folder.iterdir())
        if len(entries) > 128:
            raise Refused("unsafe")
        result = []
        for attempt in entries:
            direct(attempt)
            if not attempt.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", attempt.name):
                raise Refused("ownership")
            owner = document(attempt / "owner.json")
            if (set(owner) != {"schema", "installation_id", "version", "revision"}
                    or owner["schema"] != "daemon.companion-host-install.v1"
                    or owner["installation_id"] != attempt.name
                    or not isinstance(owner["version"], str)
                    or not re.fullmatch(r"[a-f0-9]{40}", owner["revision"])):
                raise Refused("ownership")
            # A missing/malformed completion receipt is never permission to
            # discard a successfully committed daemon transaction.
            if (attempt / "daemon-setup.json").exists() or (attempt / "daemon-setup.json").is_symlink():
                raise Refused("completed")
            transactions = direct(attempt / "daemon/transactions")
            if transactions.exists():
                for entry in transactions.iterdir():
                    direct(entry)
                    journal = entry / "journal.json"
                    if journal.exists() and document(journal).get("status") == "committed":
                        raise Refused("completed")
            result.append((attempt, owner))
        pointer = self.root / "hosts/hermes/current.json"
        if pointer.exists() and document(pointer) not in [owner for _, owner in result]:
            raise Refused("ownership")
        return result

    def openclaw_attempts(self):
        from daemons_companion.openclaw.host_install import OpenClawHostLayout, plan as acquisition_plan
        pointer = document(self.root / 'hosts/openclaw/current.json')
        ident = pointer.get('installation_id', '')
        if (pointer.get('schema') != 'daemon.openclaw-first.v1' or not re.fullmatch(r'[a-f0-9]{32}', ident)
                or pointer.get('target_os') != self.target_os):
            raise Refused('ownership')
        # The bundled lock remains available after a partially removed host.
        bundle = Path(__file__).resolve().parents[3]
        host = OpenClawHostLayout.existing(self.root, ident, self.target_os, bundle / 'apps/companion/openclaw-runtime')
        expected = acquisition_plan(host)
        if pointer.get('acquisition') != expected['revision']:
            raise Refused('ownership')
        attempt = direct(host.attempt)
        if not attempt.exists():
            return []
        owner = document(host.receipt)
        if any(owner.get(k) != v for k, v in expected.items()):
            raise Refused('ownership')
        if owner.get('command_active'):
            raise Refused('running')
        if (attempt / 'daemon-setup.json').exists() or (attempt / 'daemon-setup.json').is_symlink():
            raise Refused('completed')
        for journal in (attempt / 'daemon/transactions').glob('*/journal.json'):
            if document(journal).get('status') == 'committed':
                raise Refused('completed')
        return [(attempt, owner)]

    def services(self, attempt):
        scheduler = self.factory(attempt.name)
        codes = [scoped_job_code(code, attempt.name) for code in CODES]
        found = {}
        if scheduler.target_os == "macos":
            for file in self.launch_agents.glob("*.plist"):
                raw = read(file)
                try:
                    data = plistlib.loads(raw)
                except Exception:
                    if str(attempt).encode() in raw:
                        raise Refused("shared") from None
                    continue
                if str(attempt) in repr(data) and data.get("Label") not in codes:
                    raise Refused("shared")
        for code in codes:
            capture = scheduler.capture(code)
            if capture is None:
                found[code] = None
                continue
            record = json.loads(capture)
            raw = base64.b64decode(record["definition"], validate=True)
            if scheduler.target_os == "macos":
                data = plistlib.loads(raw)
                args = data.get("ProgramArguments", [])
                if args[:2] == ["/usr/bin/caffeinate", "-s"]:
                    args = args[2:]
                if (data.get("Label") != code or not args or data.get("UserName") or data.get("GroupName")
                        or not inside(args[0], attempt) or not inside(data.get("WorkingDirectory", ""), attempt)):
                    raise Refused("services")
            else:
                validate_task(raw, attempt, self.user_sid or _user_sid())
            # Bind the confirmation to the definition, not transient running state.
            found[code] = digest(raw)
        if scheduler.target_os == "windows":
            try:
                scheduler.require_exclusive(attempt, attempt / "daemon/data/memory", codes)
            except ValueError as error:
                raise Refused("shared" if str(error) == "shared" else "services") from None
        return scheduler, found

    def plan(self):
        attempts, programs, memories, services, identities = [], [], [], {}, {}
        for attempt, owner in self.attempts():
            attempts.append(owner)
            memory = direct(attempt / "daemon/data/memory")
            if memory.exists():
                memories.append(str(memory))
            env = attempt / "profile/.env"
            if env.exists():
                from daemons_companion.hermes.profile import _read_env_values
                read(env)
                configured = _read_env_values(env).get("DAEMON_CORE_MEMORY_ROOT")
                if configured:
                    target = direct(Path(configured))
                    if target.name == ".daemon":
                        target = target.parent
                    if str(target) not in memories:
                        memories.append(str(target))
            candidates = list(PROGRAMS)
            if attempt.parent.parent.name == "openclaw":
                candidates.extend(("node", "host", "npm-cache", "setup-libraries", "tools"))
            candidates.extend(p.name for p in attempt.iterdir() if re.fullmatch(r"download-[A-Za-z0-9_-]+", p.name))
            for relative in candidates:
                target = direct(attempt / relative)
                if any(inside(target, Path(m)) or inside(m, target) for m in memories):
                    raise Refused("memory")
                if target.exists():
                    if not target.is_dir():
                        raise Refused("unsafe")
                    if self.target_os != "windows":
                        removable_tree(_source_io_path(target))
                    relative = str(target.relative_to(self.root))
                    programs.append(relative)
                    info = _source_io_path(target).stat()
                    identities[relative] = [info.st_dev, info.st_ino]
            _, captures = self.services(attempt)
            services.update(captures)
        # A later attempt can reference an earlier attempt's files. Check every
        # preserved memory against the complete cleanup set, not just its owner.
        for relative in programs:
            target = self.root / relative
            if any(inside(target, Path(m)) or inside(m, target) for m in memories):
                raise Refused("memory")
        result = dict(schema=SCHEMA, root=str(self.root), attempts=attempts,
                      programs=programs, memories=memories, services=services, identities=identities)
        result["revision"] = digest(encoded(result))
        return result

    def inspect(self):
        plan = self.plan()
        return {"revision": plan["revision"], "memory": plan["memories"],
                "retained": str(self.root), "programs": plan["programs"],
                "services": [key for key, value in plan["services"].items() if value], "done": False}

    def stopped(self):
        processes = self.processes()
        if str(self.root).casefold() in processes.casefold():
            raise Refused("running")

    def apply(self, revision):
        with ExitStack() as stack:
            locks = [self.root / "discard-owner", self.root / "hosts/hermes/install-owner",
                     self.root / "hosts/hermes/setup-owner"]
            locks += [self.root / "openclaw-setup-owner", self.root / "hosts/openclaw/.install.lock"]
            locks += [a / "daemon/install-owner" for a, _ in self.attempts()]
            for lock in locks:
                if not stack.enter_context(memory_file_try_lock(lock)):
                    raise Refused("running")
            plan = self.plan()
            if plan["revision"] != revision:
                raise Refused("changed")
            # Mark before mutation: interrupted deletion can only be retried,
            # never accidentally resumed as a normal installation.
            save(self.marker, {"schema": SCHEMA, "status": "cleaning"})
            try:
                for attempt, _ in self.attempts():
                    scheduler, captures = self.services(attempt)
                    for code, fingerprint in captures.items():
                        if fingerprint != plan["services"][code]:
                            raise Refused("changed")
                        if fingerprint:
                            scheduler.restore(code, None)
                        if scheduler.capture(code) is not None:
                            raise Refused("services")
                for _ in range(30):
                    try:
                        self.stopped()
                        break
                    except Refused:
                        time.sleep(0.1)
                else:
                    raise Refused("running")
                self.hook("services-stopped")
                for attempt, _ in self.attempts():
                    _, captures = self.services(attempt)
                    if any(captures.values()):
                        raise Refused("services")
                for relative in plan["programs"]:
                    target = direct(self.root / relative)
                    info = _source_io_path(target).stat()
                    if [info.st_dev, info.st_ino] != plan["identities"][relative]:
                        raise Refused("changed")
                    if self.target_os == "windows":
                        remove_tree(target, plan["identities"][relative])
                    else:
                        removable_tree(_source_io_path(target))
                        shutil.rmtree(_source_io_path(target))
                    self.hook("program-removed")
                # Leave profile, sign-ins, snapshots, diagnostics and exact memory
                # in place, just as the existing complete-install uninstall does.
                pointer = direct(self.root / "hosts/hermes/current.json")
                pointer.unlink(missing_ok=True)
                direct(self.root / "hosts/openclaw/current.json").unlink(missing_ok=True)
                save(self.marker, {"schema": SCHEMA, "status": "done"})
            except Exception as error:
                save(self.marker, {"schema": SCHEMA, "status": "failed",
                                   "code": error.code if isinstance(error, Refused) else "cleanup"})
                raise
            return {"revision": revision, "memory": plan["memories"], "retained": str(self.root), "done": True}


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(32769))
        if not isinstance(request, dict) or set(request) - {"root", "base", "action", "revision", "launch_agents"}:
            raise Refused("request")
        # Enabled in the explicitly selected preview; native Windows acceptance
        # remains pending. Stable r31 is unchanged.
        if sys.platform not in {"darwin", "win32"}:
            raise Refused("platform")
        operation = Discard(request["root"], request["base"], launch_agents=request.get("launch_agents"))
        if request["action"] == "review":
            result = operation.inspect()
        elif request["action"] == "apply":
            result = operation.apply(request["revision"])
        else:
            raise Refused("request")
        output = {"ok": True, "result": result}
    except Exception as error:
        output = {"ok": False, "code": error.code if isinstance(error, Refused) else "cleanup"}
    print(json.dumps(output))


if __name__ == "__main__":
    main()
