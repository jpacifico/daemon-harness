"""Bounded desktop transport for the existing private installer.

Only the parent application supplies package paths. The renderer answers one
identified prompt at a time; it cannot submit commands, paths or a setup plan.
Stdout is exclusively protocol data. Native OAuth retains credential ownership.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
import _thread
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from daemons_companion.desktop.terminal import Back, Quit, run_wizard, safe_text

SCHEMA = "daemon.desktop.v1"
MAX_FRAME = 65536

_ACTIVITY_CODES = {'Updating your daemon. This can take a few minutes...': 'upgrade-apply', 'Downloading and verifying OpenClaw...': 'create-download', 'Connect ChatGPT for OpenClaw conversations...': 'sign-in', 'Saving your model choice...': 'create-model', "Starting OpenClaw and connecting it to your daemon's memory...": 'host-attach', 'Checking private Hermes setup...': 'host-check', 'Preparing the Hermes installation plan...': 'host-plan', 'Verifying installer files...': 'package', 'Preparing the private Hermes installation...': 'host-download', 'Preparing your ChatGPT connection...': 'sign-in', 'Checking private model access...': 'model-check', 'Preparing ChatGPT access...': 'sign-in', 'Checking the ChatGPT component...': 'sign-in', 'Complete the ChatGPT connection in your browser…': 'sign-in', 'Checking your private installation...': 'host-check', 'Checking your API key and available models...': 'api-key', 'Testing your model connection...': 'model-test', 'Checking your installation and model access...': 'model-check', 'Checking previous installation and preparing recovery...': 'recovery', 'Verifying your saved Telegram connection...': 'telegram-token', 'Preparing local memory search...': 'embeddings', 'Installing runtime, plugins and private services...': 'runtime-install', 'Waiting for your Telegram reply and memory work...': 'first-exchange', 'Preparing private installation...': 'host-check', 'Checking previous setup...': 'recovery', 'Checking and repairing private Windows service paths...': 'recovery', 'Waiting for the private Telegram gateway to reconnect...': 'gateway', 'Checking the previous Telegram conversation...': 'first-exchange', 'Verifying your Telegram bot...': 'telegram-token'}


# Exception class names come from code, never from people or hosts, so the
# diagnostic may keep them; anything else is reported as UnexpectedError.
_ERROR_CLASS = re.compile(
    r"(?:[A-Z][A-Za-z0-9]{0,47})?(?:Error|Exception|Refused|Timeout|Expired|Stopped|Failure|Unavailable|Iteration|Back|Quit)"
)


def private_input(stream):
    """Keep the answer pipe for the reader; give every child process NUL.

    On Windows a child inherits the process's standard input. While the reader
    thread waits in readline() on that synchronous pipe, a child that queries
    the handle at startup blocks until the next answer arrives: OpenClaw probes
    then hung idle until their timeout. The reader gets a private duplicate and
    fd 0, with the Windows standard input handle, becomes NUL.
    """
    if os.name != "nt":
        return stream
    private = os.dup(stream.fileno())
    null = os.open(os.devnull, os.O_RDONLY)
    try:
        # The C runtime also points STD_INPUT_HANDLE at the new descriptor.
        os.dup2(null, stream.fileno())
    finally:
        os.close(null)
    return open(private, "r", encoding=stream.encoding, errors=stream.errors, closefd=True)


def error_category(error):
    name = type(error).__name__
    return name if _ERROR_CLASS.fullmatch(name) else "UnexpectedError"


def allowed_link(value):
    if not isinstance(value, str) or len(value) > 8192:
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.hostname in {
            "auth.openai.com", "web.telegram.org", "t.me"
        } and url.port in (None, 443) and not url.username and not url.password)
    except ValueError:
        return False


class DesktopUI:
    allow_preview = False

    def __init__(self, incoming, outgoing):
        self.incoming, self.outgoing = incoming, outgoing
        self.queue = queue.Queue(maxsize=4)
        self.lock = threading.RLock()
        self.next_id = 0
        self.pending = None
        self.cancelled = False
        self.finished = False
        self.guidance = []

    def emit(self, event_type, **data):
        with self.lock:
            self.outgoing.write(json.dumps({"schema": SCHEMA, "type": event_type, **data}, ensure_ascii=True) + "\n")
            self.outgoing.flush()

    def start(self):
        threading.Thread(target=self._read, daemon=True).start()

    def _cancel(self):
        with self.lock:
            if self.cancelled or self.finished:
                return
            self.cancelled = True
        # One interrupt only. A second one could interrupt the rollback itself.
        _thread.interrupt_main()

    def _read(self):
        while not self.finished:
            line = self.incoming.readline(MAX_FRAME + 1)
            if not line or len(line.encode("utf-8")) > MAX_FRAME:
                self._cancel()
                return
            try:
                frame = json.loads(line)
                if not isinstance(frame, dict) or frame.get("schema") != SCHEMA:
                    raise ValueError()
                if set(frame) == {"schema", "type"} and frame["type"] == "cancel":
                    self._cancel()
                    return
                with self.lock:
                    prompt = self.pending
                    if (set(frame) != {"schema", "type", "id", "action", "value"}
                            or frame["type"] != "answer" or prompt is None
                            or type(frame["id"]) is not int or frame["id"] != prompt["id"]):
                        raise ValueError()
                    action, value = frame["action"], frame["value"]
                    if action not in {"submit", "back", "quit"}:
                        raise ValueError()
                    if action != "submit":
                        if value is not None:
                            raise ValueError()
                    elif prompt["kind"] == "choice":
                        if type(value) is not int or not 0 <= value < len(prompt["options"]):
                            raise ValueError()
                    elif (not isinstance(value, str) or not value.strip()
                          or len(value) > prompt["maximum"] or any(ord(c) < 32 for c in value)):
                        raise ValueError()
                    self.pending = None  # consumes the capability, including double clicks
                self.queue.put_nowait((action, value))
            except (ValueError, TypeError, queue.Full):
                self.emit("input-rejected", message="That answer is no longer valid. Use the current screen.")

    def welcome(self):
        self.phase("host")

    def phase(self, name):
        self.guidance = []
        self.emit("phase", name=name)

    def say(self, value):
        message = safe_text(value)[:12000]
        self.guidance.append(message)
        self.guidance = self.guidance[-12:]
        while len(json.dumps(self.guidance, ensure_ascii=True)) > 24000:
            self.guidance.pop(0)
        self.emit("message", message=message)
        for link in re.findall(r"https://[^\s<>]+", message):
            link = link.rstrip(".,)")
            if allowed_link(link):
                bot = re.fullmatch(r"https://t\.me/([A-Za-z0-9_]{5,32})", link)
                if bot:
                    self.emit("link", url="https://web.telegram.org/k/#@" + bot[1])
                self.emit("link", url=link)

    def _prompt(self, **data):
        if self.cancelled:
            raise Quit()
        with self.lock:
            self.next_id += 1
            self.pending = {"id": self.next_id, **data, "guidance": self.guidance}
            self.guidance = []
            self.emit("prompt", **self.pending)
        while True:
            if self.cancelled:
                raise Quit()
            try:
                action, value = self.queue.get(timeout=.1)
                break
            except queue.Empty:
                continue
        if action == "back":
            raise Back()
        if action == "quit":
            raise Quit()
        return value

    def choose(self, title, options, default=0):
        return self._prompt(kind="choice", title=title, options=list(options), default=default)

    def field(self, label, previous="", maximum=160):
        return self._prompt(kind="field", title=label, previous=previous, maximum=maximum)

    def first_meeting_preferences(self):
        # Only explicit form input is saved; no profile or personal fact is inferred.
        # Propose the app's current language, or the choice already made in this
        # session. A language saved by an earlier attempt only lets setup skip this step.
        initial = getattr(self, "_language_chosen", None) or os.environ.get("DAEMONS_CONVERSATION_LANGUAGE", "en")
        self.conversation_language = self._language_chosen = ("en", "fr")[self.choose(
            "Conversation language", ("English", "Français"), default=int(initial == "fr"))]

    def user_preferences(self, previous=None):
        from daemon_core.onboarding_user import UserPreferences
        name = previous.preferred_name if previous else ""
        while True:
            try:
                self.say("Your first name or nickname is enough. You can correct it later in conversation.")
                name = self.field("What should your daemon call you?", name, maximum=40)
                return UserPreferences(name, previous.pronouns if previous else "").validated()
            except ValueError as error:
                self.say(str(error))

    def secret(self, prompt):
        # Never include an earlier secret in the protocol, errors or a retry.
        return self._prompt(kind="secret", title=prompt, previous="", maximum=1500)

    def open_link(self, url):
        if not allowed_link(url):
            return False
        # An explicit wizard action must open the browser, unlike a URL merely
        # mentioned in instructions or emitted by the native OAuth process.
        self.emit("link", url=url, open=True)
        return True

    def diagnostic(self, operation, status, error=None, transaction_phase=None):
        data = {"operation": operation, "status": status}
        if transaction_phase is not None:
            data["transaction_phase"] = transaction_phase
        if error is not None:
            data["exception_type"] = error_category(error)
            code = getattr(error, "code", None)
            if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,40}", code):
                data["refusal"] = code
            for _ in range(8):
                if getattr(error, "host_stage", None) in {"prepare", "node-download", "node-extract", "node-check", "dependencies", "host-check"}:
                    data["host_stage"] = error.host_stage
                if error.__cause__ is None:
                    break
                error = error.__cause__
            if type(getattr(error, "timeout_seconds", None)) in (int, float):
                data["timeout_seconds"] = int(error.timeout_seconds)
            data["error_code"] = error_category(error)
            for key in ("errno", "winerror", "exit_code"):
                if type(getattr(error, key, None)) is int:
                    data[key] = getattr(error, key)
        self.emit("diagnostic", **data)

    @contextmanager
    def activity(self, label):
        self.emit("activity", active=True, message=label)
        operation = _ACTIVITY_CODES.get(label, "activity")
        self.diagnostic(operation, "started")
        try:
            def progress(value):
                if value == "Installer output received.":
                    self.emit("activity", active=True, detail=value)
                else:
                    self.emit("activity", active=True, message=safe_text(value)[:2000])
            yield progress
        except (Quit, KeyboardInterrupt):
            self.diagnostic(operation, "cancelled")
            raise
        except Exception as error:
            self.diagnostic(operation, "failed", error)
            raise
        else:
            self.diagnostic(operation, "completed")
        finally:
            self.emit("activity", active=False, message=label)

    def subscription_runner(self, command, environment, *, interactive=False):
        from daemons_companion.setup.subscription import native_command
        if not interactive:
            return native_command(command, environment)
        self.emit("connection-start")
        # Native commands still open the usual browser themselves. Pipe their
        # output so a missing terminal cannot strand the OAuth instructions.
        process = subprocess.Popen(command, env={**environment, "PYTHONUNBUFFERED": "1"},
            cwd=environment["HOME"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        def read():
            tail = ""
            seen = set()
            while True:
                chunk = process.stdout.read1(4096)
                if not chunk:
                    return
                tail = (tail + chunk.decode("utf-8", errors="replace"))[-16384:]
                # Forward only complete official authorization URLs, not raw
                # output (which can contain credentials or private diagnostics).
                for match in re.finditer(r"https://auth\.openai\.com/[^\s\x1b<>]+(?=[\s\x1b<>])", tail):
                    url = match.group(0)
                    if allowed_link(url) and url not in seen:
                        seen.add(url)
                        self.emit("link", url=url)
                # A device sign-in prints a one-time code to enter on that page.
                plain = re.sub(r"\x1b\[[0-9;]*m", "", tail)
                for match in re.finditer(r"Enter this code:\s+([A-Z0-9-]{4,32})\s", plain):
                    code = match.group(1)
                    if code not in seen:
                        seen.add(code)
                        self.emit("chatgpt-code", code=code)
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.activity("Complete the ChatGPT connection in your browser…"):
                while process.poll() is None:
                    if self.cancelled:
                        raise Quit()
                    if time.monotonic() - started > 900:
                        raise RuntimeError("Sign-in timed out. You can retry; completed private sign-ins are retained.")
                    time.sleep(.1)
            return subprocess.CompletedProcess(command, process.returncode, "", "")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            thread.join(timeout=2)
            process.stdout.close()
            # Whatever its outcome, this browser sign-in is over: its link no longer serves.
            self.emit("sign-in-ended")


def serve(ui, owner):
    change = None
    try:
        from daemons_companion.hosts.change import pending_change
        change = pending_change()
        if change is not None:
            return serve_change(ui, owner, change)
        result = run_wizard(ui, backend=owner.backend, packaged=True, host_setup=owner, supported_only=True)
        ui.emit("finished", result="installed" if owner.daemon_installed else "unfinished", exit_code=result)
        return result
    except (Quit, KeyboardInterrupt, EOFError):
        if change is not None:
            return stopped_change(ui, change)
        if owner.daemon_installed:
            ui.emit("finished", result="installed", message=(
                "Your private daemon is installed and keeps running. The first conversation still needs confirmation. "
                "Reopen setup to check it; it will not reinstall the daemon."))
            return 130
        ui.emit("finished", result="cancelled", message=(
            "Setup stopped. Completed private downloads and connections are retained. "
            "Any installation recovery details are shown above. Existing personal services were not changed."))
        return 130
    except Exception as error:
        if change is not None:
            from daemons_companion.hosts.change import message
            ui.emit("finished", result="error", error_code="ChangeError", message=message(error))
            stopped_change(ui, change, emit=False)
            return 1
        # Do not send exception text, traceback, locals or subprocess output:
        # any of them may contain credentials. Preserve only a bounded category.
        category = error_category(error)
        ui.emit("finished", result="error", error_code=category,
                message=f"Setup could not complete (reference: {category}). Reopen setup to resume; keep its private recovery journal.")
        return 1
    finally:
        ui.finished = True


def serve_change(ui, owner, change):
    """A setup that moves a daemon to its other host, then concludes the move."""
    from daemons_companion.hosts.change import HOST_NAMES
    if change.state in {"finished", "reverted"}:
        ui.emit("finished", result="unfinished", message="This host change has ended. Start a new one from the daemon's settings.")
        return 0
    change.resume()
    if change.state != "installed":
        run_wizard(ui, backend=owner.backend, packaged=True, host_setup=owner, supported_only=True)
    if change.state != "installed":
        change.abandon()
        ui.emit("finished", result="unfinished", message=(
            f"{change.identity['name']} keeps running on {HOST_NAMES[change.source]}. Nothing was uninstalled."))
        return 1
    moved = change.conclude(ui)
    host = HOST_NAMES[change.target if moved else change.source]
    ui.emit("finished", result="installed", exit_code=0,
            message=f"{change.identity['name']} runs on {host}.")
    return 0


def stopped_change(ui, change, *, emit=True):
    """Cancelled before the new host runs: the daemon stays on its previous host."""
    from daemons_companion.hosts.change import HOST_NAMES
    try:
        state = change.state
        if state in {"switching", "switched"}:
            change.abandon()
    except Exception:  # noqa: BLE001 - the record keeps what recovery needs.
        state = "recovery"
    if emit:
        ui.emit("finished", result="cancelled", message=(
            "The host change awaits your check. Reopen it to finish or go back." if state == "installed" else
            f"{change.identity['name']} keeps running on {HOST_NAMES[change.source]}. Nothing was uninstalled."))
    return 130


def serve_host_addition(ui, request):
    """Guided Add a host session; the parent supplies only owned paths and a name."""
    from pathlib import Path
    from daemons_companion.hosts.wizard import HostAdditionStopped, run

    try:
        if (
            not isinstance(request, dict)
            or set(request) != {"source_profile", "base", "name"}
            or not all(isinstance(v, str) and v for v in request.values())
        ):
            raise ValueError("Invalid host addition request.")
        run(ui, Path(request["source_profile"]), Path(request["base"]), safe_text(request["name"])[:80])
        ui.emit("finished", result="installed")
        return 0
    except HostAdditionStopped:
        ui.emit("finished", result="unfinished", message="Nothing was changed for OpenClaw. Your daemon keeps working.")
        return 0
    except (Quit, Back, KeyboardInterrupt, EOFError):
        ui.emit("finished", result="cancelled", message=(
            "OpenClaw setup stopped. What was already prepared is kept: Add a host resumes it. "
            "Your daemon keeps working."))
        return 130
    except Exception as error:
        category = error_category(error)
        ui.diagnostic("add-host", "failed", error)
        ui.emit("finished", result="error", error_code=category, message=(
            f"OpenClaw could not be added (reference: {category}). Your daemon keeps working. "
            "Add a host resumes from the last completed step; export the diagnostic to report it."))
        return 1
    finally:
        ui.finished = True


def serve_daemon_update(ui, request):
    """Guided runtime update from the daemon's page; the parent supplies only owned paths and a name."""
    from pathlib import Path
    from daemons_companion.hosts.update import UpdateStopped, run

    try:
        if (
            not isinstance(request, dict)
            or set(request) != {"source_profile", "base", "name"}
            or not all(isinstance(v, str) and v for v in request.values())
        ):
            raise ValueError("Invalid daemon update request.")
        name = safe_text(request["name"])[:80]
        updated = run(ui, Path(request["source_profile"]), Path(request["base"]), name)
        if updated:
            ui.emit("finished", result="installed", message=(
                f"The update is finished. {name}'s services restarted and were checked."
            ))
        else:
            ui.emit("finished", result="unfinished", message=f"{name} already runs the version of this app.")
        return 0
    except UpdateStopped:
        ui.emit("finished", result="unfinished", message="Your daemon keeps working as it was.")
        return 0
    except (Quit, Back, KeyboardInterrupt, EOFError):
        ui.emit("finished", result="cancelled", message="The update was not started. Your daemon keeps working as it was.")
        return 130
    except Exception as error:
        category = error_category(error)
        ui.diagnostic("daemon-update", "failed", error)
        ui.emit("finished", result="error", error_code=category, message=(
            f"The update could not be completed (reference: {category}). Open the daemon's page again: "
            "an interrupted update is restored from there. Export the diagnostic to report it."))
        return 1
    finally:
        ui.finished = True


def serve_provider_change(ui, request, owner):
    """Guided provider change; the parent supplies only owned paths and a name."""
    from pathlib import Path
    from daemons_companion.desktop.provider_settings import ProviderSettingsError
    from daemons_companion.hosts.provider_change import abandon, run

    profile = base = None
    try:
        if (
            not isinstance(request, dict)
            or set(request) != {"profile", "base", "name"}
            or not all(isinstance(v, str) and v for v in request.values())
        ):
            raise ValueError("Invalid provider change request.")
        profile, base = Path(request["profile"]), Path(request["base"])
        kept = run(ui, profile, base, safe_text(request["name"])[:80], owner.context)
        if kept:
            ui.emit("finished", result="installed")
        else:
            ui.emit("finished", result="unfinished",
                    message="Your daemon uses its previous model connection, exactly as before.")
        return 0
    except (Quit, Back, KeyboardInterrupt, EOFError):
        _abandon_provider_change(abandon, profile, base)
        ui.emit("finished", result="cancelled", message=(
            "The model connection was not changed. Your daemon keeps its previous provider."))
        return 130
    except ProviderSettingsError as error:
        _abandon_provider_change(abandon, profile, base)
        ui.emit("finished", result="error", error_code=str(error), message={
            "provider.busy": "Your daemon's memory was still filing a recent exchange. Nothing was changed; try again in a few minutes.",
            "provider.pending": "An earlier model change was not finished. Open the daemon's settings once, then try again.",
            "provider.model-unavailable": "This model is not available with this connection. Nothing was changed.",
        }.get(str(error), "This daemon's model connection cannot be changed here. Nothing was changed."))
        return 1
    except Exception as error:
        _abandon_provider_change(abandon, profile, base)
        category = error_category(error)
        ui.diagnostic("provider-change", "failed", error)
        ui.emit("finished", result="error", error_code=category, message=(
            f"The model connection could not be changed (reference: {category}). Your daemon keeps its "
            "previous provider; export the diagnostic to report it."))
        return 1
    finally:
        ui.finished = True


def _abandon_provider_change(abandon, profile, base):
    if profile is None:
        return
    try:
        abandon(profile, base)
    except Exception:
        pass  # The app reverts an unfinished change when the daemon's settings open.


def overview(ui, owner):
    """Read only the exact companion-owned setup; never adopt detected hosts."""
    from pathlib import Path
    from daemons_companion.runtime.host_install import installed_host
    from daemons_companion.runtime.installation_scope import InstallationScope
    from daemons_companion.hermes.first_install import first_exchange
    with ui.activity('Checking your private daemon…'):
        layout = owner.layout()
        host = installed_host(layout)
        if host is None:
            ui.emit('overview', installed=False)
            return
        layout = replace(layout, installation_id=Path(host.config_path).parent.name)
        scope = InstallationScope.from_verified_host(layout)
        receipt = scope.attempt / 'daemon-setup.json'
        if not receipt.exists():
            ui.emit('overview', installed=False)
            return
        scope.validate_home(scope.home)
        if receipt.is_symlink() or receipt.resolve() != receipt or receipt.stat().st_size > 8192:
            raise ValueError('Invalid private setup receipt.')
        data = json.loads(receipt.read_text(encoding='utf-8'))
        if (data.get('schema') != 'daemon.private-setup.v1'
                or re.fullmatch(r'[A-Za-z0-9_]{5,32}', data.get('username', '')) is None
                or re.fullmatch(r'[1-9][0-9]{0,19}', data.get('user_id', '')) is None):
            raise ValueError('Invalid private setup receipt.')
        vault = scope.home / 'data/memory'
        if vault.resolve() != vault or not vault.is_dir():
            raise ValueError('The private memory directory is unavailable.')
        verified = first_exchange(scope.home / 'data/state/receipts', since=0, user_id=data['user_id'])
        link = 'https://t.me/' + data['username']
        ui.emit('link', url='https://web.telegram.org/k/#@' + data['username'])
        ui.emit('link', url=link)
        ui.emit('overview', installed=True, first_exchange_verified=verified,
                vault=str(vault), bot=link)


def main():
    ui = DesktopUI(private_input(sys.stdin), sys.stdout)
    # Prevent an incidental library print from becoming a protocol frame.
    with open(os.devnull, "w") as discarded, redirect_stdout(discarded):
        from daemons_companion.desktop.host_terminal import PackagedHostSetup
        owner = PackagedHostSetup.from_environment(os.environ)
        owner.context = replace(owner.context, root_folder_memory=True)
        ui.start()
        if sys.argv[1:] == ['--add-host']:
            return serve_host_addition(ui, json.loads(os.environ.get("DAEMONS_ADD_HOST", "null")))
        if sys.argv[1:] == ['--update-daemon']:
            return serve_daemon_update(ui, json.loads(os.environ.get("DAEMONS_UPDATE_DAEMON", "null")))
        if sys.argv[1:] == ['--change-provider']:
            return serve_provider_change(ui, json.loads(os.environ.get("DAEMONS_CHANGE_PROVIDER", "null")), owner)
        if sys.argv[1:] == ['--status']:
            try:
                overview(ui, owner)
                ui.emit('finished', result='status')
                return 0
            except (Quit, KeyboardInterrupt):
                ui.emit('finished', result='cancelled', message='Status check cancelled.')
                return 130
            except (OSError, RuntimeError, ValueError):
                ui.emit('finished', result='error', message='Private setup could not be verified. Reopen setup to check its recovery state.')
                return 1
            finally:
                ui.finished = True
        return serve(ui, owner)


if __name__ == "__main__":
    raise SystemExit(main())
