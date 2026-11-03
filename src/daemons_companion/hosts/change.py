"""Move a daemon created by the app to its other host.

The app writes a request naming the daemon's profile into a new setup's
folder and passes its path in DAEMONS_HOST_CHANGE. That setup installs the
other host while the daemon keeps answering, and reuses its identity, its
model connection and its Telegram bot. It then switches: it captures and stops
the previous host's services, waits for the after-turn queue, archives the
memory in Move mode and restores it into the new host before that host
starts. Once the user has seen the daemon answer on the new host, the previous
host is uninstalled; going back uninstalls the new host and restores the
previous services exactly. A record beside the request names the step reached.

Secrets are read from the previous host's own storage when the new host needs
them and are stored by the new host under its own entries, as at creation.
They are never written to the record, a file or the environment.
"""

from __future__ import annotations

import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sys
import time

from daemon_core.daemon_identity import VALID_GENDERS, validate_name
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemons_companion.backup.archive import BackupError, create_archive
from daemons_companion.backup.live import QUEUE_WAIT_SECONDS, WAIT_SECONDS, pending_work, stalled
from daemons_companion.openclaw.host_install import checked_file, direct
from daemons_companion.removal.errors import Refused
from daemons_companion.removal.removal import Removal, definition

ENVIRONMENT = "DAEMONS_HOST_CHANGE"
REQUEST_SCHEMA = "daemon.host-change-request.v1"
SCHEMA = "daemon.host-change.v1"
RECORD = "record.json"
ARCHIVE = "host-change.zip"
# prepared: nothing changed yet; switching: the previous services are being
# stopped; switched: the memory belongs to the new host; installed: the new
# host runs and awaits the user's check; finished and reverted are final.
STATES = ("prepared", "switching", "switched", "installed", "finished", "reverted")
HOST_NAMES = {"hermes": "Hermes Agent", "openclaw": "OpenClaw"}
FIRST_REPLY_SECONDS = 300
TOKEN = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}")
USER = re.compile(r"[1-9][0-9]{0,19}")


class ChangeError(ValueError):
    """A bounded code safe to show; never a path, secret or exception text."""


MESSAGES = {
    "change.not-owned": "Only a daemon created by this app can change host.",
    "change.added-host": "This daemon already runs on both hosts. Moving it is not available yet.",
    "change.daemon-busy": "Your daemon's memory was still filing a recent exchange after several minutes. Try again later.",
    "change.recovery": "The host change needs recovery. Nothing else was changed.",
}


def message(error: BaseException) -> str:
    code = str(error) if isinstance(error, ChangeError) else ""
    if code.startswith("change.uninstall-"):
        return "A host could not be uninstalled. Both hosts are kept; open the setup again to retry."
    return MESSAGES.get(code, "The host change could not continue.")


def launch_agents() -> Path:
    return Path.home() / "Library/LaunchAgents"


def write_request(folder: Path, source_profile: Path, base: Path) -> Path:
    """The request the app writes into a new setup's folder (tests use it too)."""
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / "request.json"
    atomic_write_text(path, json.dumps({"schema": REQUEST_SCHEMA, "source_profile": str(source_profile),
                                        "base": str(base)}) + "\n")
    return path


class HostChange:
    @classmethod
    def from_request(cls, request: Path, **options) -> "HostChange":
        """The change a setup performs, named by the request the app wrote."""
        request = direct(Path(request))
        data = json.loads(checked_file(request, 8192))
        if (not isinstance(data, dict) or set(data) != {"schema", "source_profile", "base"}
                or data["schema"] != REQUEST_SCHEMA
                or not all(isinstance(data[k], str) and Path(data[k]).is_absolute()
                           for k in ("source_profile", "base"))):
            raise ChangeError("change.invalid-request")
        return cls(Path(data["source_profile"]), Path(data["base"]), request.parent, **options)

    def __init__(self, source_profile: Path, base: Path, folder: Path | None = None, *, removal=None,
                 target_removal=None, store=None, sleep=time.sleep, clock=time.time,
                 queue_wait=QUEUE_WAIT_SECONDS):
        # Without a folder the change can only be inspected: it has no record.
        self.folder = folder
        self.record_path = folder / RECORD if folder is not None else None
        self.source_profile, self.base = Path(source_profile), Path(base)
        try:
            self.removal = removal or Removal(self.source_profile, self.base, launch_agents())
        except Refused:
            raise ChangeError("change.not-owned") from None
        self.target_removal = target_removal or (lambda profile: Removal(profile, self.base, launch_agents()))
        self.source = "openclaw" if self.removal.openclaw else "hermes"
        self.target = "hermes" if self.removal.openclaw else "openclaw"
        self.scope = self.removal.scope
        self._store, self.sleep, self.clock, self.queue_wait = store, sleep, clock, queue_wait
        self._identity = None

    # Record --------------------------------------------------------------

    def record(self) -> dict:
        if self.record_path is None:
            raise ChangeError("change.invalid-request")
        if not self.record_path.exists() and not self.record_path.is_symlink():
            return {"schema": SCHEMA, "state": "prepared", "source_profile": str(self.source_profile)}
        data = json.loads(checked_file(self.record_path, 256 * 1024))
        if (not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("state") not in STATES
                or data.get("source_profile") != str(self.source_profile)):
            raise ChangeError("change.recovery")
        return data

    def save(self, data: dict) -> None:
        atomic_write_text(self.record_path, json.dumps(data, sort_keys=True) + "\n")

    @property
    def state(self) -> str:
        return self.record()["state"]

    # What the new host reuses -------------------------------------------

    def inspect(self) -> dict:
        """Check the daemon can move; read only what the new setup needs."""
        try:
            self.removal.ownership()
            review = self.removal.inspect("uninstall")
        except Refused as error:
            raise ChangeError("change.not-owned" if error.code in {"ownership", "removed"}
                              else "change.unavailable") from None
        if review.get("addedHosts"):
            raise ChangeError("change.added-host")
        connection = json.loads(checked_file(self.scope.attempt / "connection.json", 8192))
        owner = connection.get("user_id") if isinstance(connection, dict) else None
        if not isinstance(owner, str) or USER.fullmatch(owner) is None:
            raise ChangeError("change.unavailable")
        memory = Path(review["memory"])
        try:
            identity = json.loads(checked_file(memory / ".daemon/core/daemon_identity.json", 8192))
            name, gender = validate_name(identity.get("name")), identity.get("gender")
        except (OSError, ValueError, AttributeError):
            raise ChangeError("change.unavailable") from None
        if gender not in VALID_GENDERS:
            raise ChangeError("change.unavailable")
        language = None
        meeting = memory / ".daemon/users" / owner / "first_meeting.json"
        if meeting.is_file():
            try:
                value = json.loads(checked_file(meeting, 8192)).get("language")
                language = value if value in {"en", "fr"} else None
            except (OSError, ValueError, AttributeError):
                language = None
        kind, model = self.provider(connection)
        self._identity = {"name": name, "gender": gender, "language": language, "owner": owner,
                          "memory": str(memory), "kind": kind, "model": model}
        return {"name": name, "source": self.source, "target": self.target, "kind": kind}

    @property
    def identity(self) -> dict:
        if self._identity is None:
            self.inspect()
        return self._identity

    def provider(self, connection: dict) -> tuple[str, str]:
        from daemons_companion.desktop.provider_settings import OwnedDaemon, ProviderSettingsError

        try:
            config = OwnedDaemon(self.source_profile, self._store).provider()
            return config["kind"], config["model"]
        except (ProviderSettingsError, OSError, ValueError, KeyError):
            pass
        # A ChatGPT subscription daemon records no provider file; it signs in again.
        if connection.get("schema") == "daemon.private-connection.v1":
            return "codex_cli", ""
        raise ChangeError("change.unavailable")

    def store(self):
        if self._store is None:
            from daemons_companion.setup.credentials import system_credential_store

            self._store = system_credential_store()
        return self._store

    def key(self) -> str:
        """The previous host's API key, or "" when none can be reused."""
        from daemons_companion.desktop.provider_settings import OwnedDaemon

        try:
            daemon = OwnedDaemon(self.source_profile, self.store())
            return daemon.current_key(daemon.provider())
        except Exception:  # noqa: BLE001 - the form asks for a key instead.
            return ""

    def token(self) -> str:
        """The previous host's bot token, or "" when it cannot be read."""
        try:
            if self.source == "openclaw":
                from daemons_companion.desktop.provider_settings import OwnedDaemon

                daemon = OwnedDaemon(self.source_profile, self.store())
                value = daemon.connection_secret(daemon.connection_record()["credential"]).get("token", "")
            else:
                from daemons_companion.hermes.profile import _read_env_values

                value = _read_env_values(direct(self.source_profile / ".env")).get("TELEGRAM_BOT_TOKEN", "")
                value = value.strip().strip("'\"")
        except Exception:  # noqa: BLE001 - the form asks for a token instead.
            return ""
        return value if isinstance(value, str) and TOKEN.fullmatch(value) else ""

    # Switch ---------------------------------------------------------------

    def spool(self) -> Path:
        return self.scope.home / "data/state/after-turn-spool"

    def switch(self, target_scope):
        """Stop the previous host and hand its memory to the new one.

        Returns the archive to restore, or None when the memory was already
        restored by an earlier run of this setup.
        """
        from daemons_companion.backup.restore import read_source

        data = self.record()
        archive = target_scope.home / "restore" / ARCHIVE
        if data["state"] == "switched":
            return replace(read_source(archive), move=True) if archive.is_file() else None
        if data["state"] == "switching":
            self.restart_source(data)
            data = self.record()
        if data["state"] != "prepared":
            raise ChangeError("change.recovery")
        identity = self.identity
        memory = Path(identity["memory"])
        with memory_file_lock(self.scope.home / "install-owner"):
            try:
                captures = self.removal.service_snapshot(memory)
            except Refused:
                raise ChangeError("change.unavailable") from None
            data.update(state="switching", target_attempt=str(target_scope.attempt), switched_at=self.clock(),
                        services={code: base64.b64encode(value).decode() if value else None
                                  for code, value in captures.items()})
            self.save(data)
            try:
                scheduler, codes = self.removal.scheduler, self.removal.codes
                # The gateway first: no new turn reaches the memory after this.
                scheduler.restore(codes[0], None)
                waited = 0.0
                while pending_work(self.spool()) and not stalled(self.spool()):
                    if waited >= self.queue_wait:
                        raise ChangeError("change.daemon-busy")
                    self.sleep(WAIT_SECONDS)
                    waited += WAIT_SECONDS
                for code in codes[1:]:
                    scheduler.restore(code, None)
                self.removal.require_stopped()
                archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                archive.unlink(missing_ok=True)
                create_archive(memory, archive)
                source = replace(read_source(archive), move=True)
                if source.owner != identity["owner"]:
                    raise ChangeError("change.unavailable")
            except BaseException as error:
                archive.unlink(missing_ok=True)
                self.restart_source(data)
                if isinstance(error, (ChangeError, KeyboardInterrupt)):
                    raise
                code = str(error) if isinstance(error, BackupError) else "change.switch-failed"
                raise ChangeError(code) from None
            data["state"] = "switched"
            self.save(data)
            return source

    def restart_source(self, data: dict, *, final: bool = False) -> None:
        """Restore the previous host's services exactly as they were captured."""
        scheduler = self.removal.scheduler
        for code, capture in reversed(list(data.get("services", {}).items())):
            original = base64.b64decode(capture) if capture else None
            current = scheduler.capture(code)
            if current is not None and definition(current) != definition(original):
                raise ChangeError("change.recovery")
            if current is None or original is None:
                scheduler.restore(code, original)
        data["state"] = "reverted" if final else "prepared"
        if not final:
            data.pop("services", None)
            data.pop("switched_at", None)
        self.save(data)

    def abandon(self) -> None:
        """The setup ended before the new host was installed: restart the previous one."""
        data = self.record()
        if data["state"] in {"switching", "switched"}:
            self.restart_source(data, final=True)

    def resume(self) -> None:
        """A setup stopped right after installing the new host resumes at the check."""
        data = self.record()
        attempt = Path(data.get("target_attempt", "/"))
        if data["state"] == "switched" and (attempt / "daemon-setup.json").is_file():
            self.installed(attempt / "profile")

    def installed(self, target_profile: Path) -> None:
        data = self.record()
        if data["state"] != "switched":
            raise ChangeError("change.recovery")
        data.update(state="installed", target_profile=str(target_profile))
        self.save(data)

    # Conclusion -----------------------------------------------------------

    def receipts(self, data: dict):
        from daemons_companion.runtime.installation_scope import InstallationScope

        home = InstallationScope(Path(data["target_profile"]).parent).home
        if self.target == "openclaw":
            return home / "data/state/continuity/shared", "openclaw"
        return home / "data/state/receipts", "telegram"

    def answered(self, data: dict) -> bool:
        from daemons_companion.hermes.first_install import first_exchange

        receipts, platform = self.receipts(data)
        deadline = time.monotonic() + FIRST_REPLY_SECONDS
        while True:
            if first_exchange(receipts, since=data["switched_at"], user_id=self.identity["owner"],
                              platform=platform):
                return True
            if time.monotonic() >= deadline:
                return False
            self.sleep(1)

    def conclude(self, ui) -> bool:
        """Ask the user once the daemon answered; uninstall the host they leave."""
        data = self.record()
        if data["state"] != "installed":
            raise ChangeError("change.recovery")
        name, old, new = self.identity["name"], HOST_NAMES[self.source], HOST_NAMES[self.target]
        ui.phase("conversation")
        ui.say(f"{name} now runs on {new}. Send it a message on Telegram, in the same conversation as before.")
        while True:
            with ui.activity("Waiting for your Telegram reply and memory work..."):
                received = self.answered(data)
            if received:
                ui.say(f"{name} answered on {new} and its memory work completed.")
                choice = ui.choose(f"Did {name}'s reply arrive in Telegram?",
                                   (f"Yes — uninstall {old}", "Not yet — wait again", f"Go back to {old}"))
                choice = {0: "finish", 1: "wait", 2: "revert"}[choice]
            else:
                ui.say(f"No reply from {name} on {new} was confirmed yet.")
                choice = ui.choose(f"{name} has not answered on {new} yet",
                                   ("Wait again", f"Go back to {old}"))
                choice = {0: "wait", 1: "revert"}[choice]
            if choice == "finish":
                with ui.activity(f"Uninstalling {old}..."):
                    self.finish()
                ui.say(f"{old} is uninstalled. {name} continues on {new}.")
                ui.say("The previous memory folder is kept as an archive and is no longer used: " + self.identity["memory"])
                return True
            if choice == "revert":
                with ui.activity(f"Going back to {old}..."):
                    self.revert()
                ui.say(f"{name} is back on {old}, as before the change. {new} is uninstalled.")
                ui.say("Messages exchanged on the new host stay in its memory folder, which is kept: "
                       + str(self._target_memory(data)))
                return False

    def finish(self) -> None:
        data = self.record()
        if data["state"] != "installed":
            raise ChangeError("change.recovery")
        try:
            review = self.removal.inspect("uninstall")
            self.removal.apply("uninstall", review["revision"], review["name"])
        except Refused as error:
            raise ChangeError("change.uninstall-" + error.code) from None
        data["state"] = "finished"
        self.save(data)

    def revert(self) -> None:
        data = self.record()
        if data["state"] != "installed":
            raise ChangeError("change.recovery")
        try:
            target = self.target_removal(Path(data["target_profile"]))
            review = target.inspect("uninstall")
            target.apply("uninstall", review["revision"], review["name"])
        except Refused as error:
            raise ChangeError("change.uninstall-" + error.code) from None
        self.restart_source(data, final=True)

    def _target_memory(self, data):
        from daemons_companion.runtime.installation_scope import InstallationScope

        return InstallationScope(Path(data["target_profile"]).parent).home / "data/memory"


_PENDING: dict[str, HostChange] = {}


def pending_change() -> HostChange | None:
    """The host change this setup performs, or None for an ordinary setup."""
    value = os.environ.get(ENVIRONMENT, "").strip()
    if not value:
        return None
    if value not in _PENDING:
        _PENDING[value] = HostChange.from_request(Path(value))
    return _PENDING[value]


def inherited_identity():
    """Name, gender and language carried over by a restore or a host change."""
    from daemons_companion.backup.restore import pending_restore

    change = pending_change()
    if change is not None:
        value = change.identity
        return value["name"], value["gender"], value["language"], (
            f"{value['name']} keeps its name, voice and memories on its new host.")
    restored = pending_restore()
    if restored is not None:
        return restored.name, restored.gender, restored.language, (
            f"{restored.name} is installed from your backup, with its name and memories.")
    return None


def confirm(ui, change: HostChange) -> str:
    """The one screen before the switch; returns the Telegram account kept."""
    from daemons_companion.desktop.terminal import Quit

    value = change.identity
    name, old, new = value["name"], HOST_NAMES[change.source], HOST_NAMES[change.target]
    ui.say(f"{name} will stop on {old}, then continue on {new} with the same Telegram bot and account {value['owner']}.")
    ui.say("Its memory moves to the new host before the new host starts. Avoid writing to it during the switch, which takes a few minutes.")
    ui.say(f"{old} is uninstalled only after you have seen {name} answer on {new}. Until then you can go back to {old} unchanged.")
    if ui.choose(f"Move {name} to {new}?", (f"Move to {new}", "Quit")) != 0:
        raise Quit
    return value["owner"]


def handle_request(request: object) -> dict:
    """The app's check before it opens a host change session."""
    if (not isinstance(request, dict) or set(request) != {"action", "profile", "base"}
            or request["action"] != "inspect"
            or not all(isinstance(request[k], str) and Path(request[k]).is_absolute() for k in ("profile", "base"))):
        raise ChangeError("change.invalid-request")
    return HostChange(Path(request["profile"]), Path(request["base"])).inspect()


def main() -> None:
    try:
        if sys.platform not in ("darwin", "win32"):
            raise ChangeError("change.unavailable")
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise ChangeError("change.invalid-request")
        result = {"ok": True, "result": handle_request(json.loads(raw))}
    except ChangeError as error:
        result = {"ok": False, "code": str(error)}
    except Exception as error:  # noqa: BLE001 - only the class name leaves.
        result = {"ok": False, "code": "change.unavailable", "diagnostic": {"type": type(error).__name__}}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
