"""Add OpenClaw to an existing daemon, through the same guided steps as a first install.

The desktop process supplies the owned source profile and base; the person
answers one screen at a time. Durable state stays where it was: the host
creation record (download, model, Telegram) and the attachment transaction
(services, rollback). A restarted session resumes the unfinished creation.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
import time
import uuid
from pathlib import Path

from daemons_companion.desktop.terminal import Back, Quit
from daemons_companion.hosts import creation as host_creation
from daemons_companion.openclaw.first_install import free_port
from daemons_companion.openclaw.host_install import OpenClawHostLayout, checked_file, direct
from daemons_companion.setup import model_access
from daemons_companion.setup.telegram import TelegramSetupError, create_bot, verify_telegram_token



class HostAdditionStopped(RuntimeError):
    """A plain explanation was shown; nothing is left to do in this session."""

    def __init__(self, message, code=None):
        self.code = code
        super().__init__(message)


@contextmanager
def _step(ui, code):
    """Name, in the diagnostic, the steps that show no activity of their own."""
    report = getattr(ui, "diagnostic", lambda *_args: None)
    report(code, "started")
    try:
        yield
    except (Quit, Back, HostAdditionStopped):
        raise
    except Exception as error:
        report(code, "failed", error)
        raise
    report(code, "completed")


def run(ui, source_profile: Path, base: Path, name: str) -> bool:
    import os
    from daemons_companion.hosts.attachment_command import native_target, source_binding

    source_profile, base = direct(source_profile), direct(base)
    if not source_profile.is_relative_to(base):
        raise ValueError("The daemon is not an owned private installation.")
    ui.phase("host")
    ui.say(f"{name} will also answer you from OpenClaw, through a second Telegram bot.")
    ui.say("Its memory stays the same: nothing is copied, and Hermes Agent keeps running with its own bot.")
    if ui.choose("Add OpenClaw?", ("Continue", "Cancel")) != 0:
        raise Quit
    with _step(ui, "source-check"):
        _update_if_needed(ui, source_profile, base, name)
        _scope, _runtime, binding = source_binding(source_profile)
    with _step(ui, "host-prepare"):
        host = OpenClawHostLayout.existing(
            base / "Companion/private",
            _resumable_creation(base, source_profile, binding.state) or uuid.uuid4().hex,
            native_target(),
            Path(os.environ["DAEMON_HARNESS_COMPANION_BUNDLE_ROOT"]) / "apps/companion/openclaw-runtime",
        )
        host.validate()
        creation = host_creation.Creation(
            host, source_profile, binding, model_check=lambda *_: None
        )
        data = (
            creation.current()
            if host_creation.record_path(host).exists()
            else creation.start(free_port())
        )
    if data["phase"] == "download":
        with ui.activity("Downloading and verifying OpenClaw...") as progress:
            creation.progress = progress
            data = creation.perform("download", data["revision"], {})
            creation.progress = None
    data = _connect_model(ui, creation, data)
    data = _connect_telegram(ui, creation, data)
    ui.phase("installation")
    with ui.activity("Starting OpenClaw and connecting it to your daemon's memory..."):
        _attach(source_profile, base, host)
    _first_message(ui, binding, data["bot"], name)
    return True


def _update_if_needed(ui, source_profile, base, name):
    from daemons_companion.runtime.upgrade import current_status, from_request

    request = {"source_profile": str(source_profile), "base": str(base)}
    upgrade = from_request(request)
    status = current_status(upgrade)
    if status["status"] == "current":
        return
    recover = status["status"] not in {"required", "committed", "rolled_back"}
    if recover:
        ui.say(f"A previous update of {name} was interrupted. The app can restore its previous version safely.")
    else:
        ui.say(f"{name} first needs the update that comes with this app version.")
        ui.say("Its memory, model account and Telegram bot do not change. Its services restart for a few seconds.")
    if ui.choose(
        "Update your daemon?" if not recover else "Restore the previous version?",
        ("Update and continue" if not recover else "Restore and continue", "Cancel"),
    ) != 0:
        raise Quit
    with ui.activity("Updating your daemon. This can take a few minutes..."):
        if recover:
            result = upgrade.run(status["revision"], recover=True)
        else:
            try:
                reviewed = upgrade.review()
            except ValueError as error:
                code = getattr(error, "code", None) or "review"
                ui.say(_REVIEW_REFUSALS.get(code, _REVIEW_REFUSALS["review"]).format(name=name))
                raise HostAdditionStopped("Daemon update refused.", code) from None
            result = upgrade.run(reviewed["revision"])
    if result.get("status") != "committed":
        ui.say(
            f"The update did not complete. {name} was restored to its previous version and keeps working. "
            "Export the diagnostic if you want to report it."
        )
        raise HostAdditionStopped("Update rolled back.")
    ui.say(f"{name} is up to date.")


_TOO_OLD = (
    "{name} was created with an older version of the app, which cannot be updated automatically. "
    "To use OpenClaw, create a new daemon with this version."
)
# Plain explanations for each review refusal code of runtime.upgrade.
_REVIEW_REFUSALS = {
    "protected-runtime": _TOO_OLD,
    "portability": _TOO_OLD,
    "attached-hosts": (
        "{name} already has another host attached, and this version cannot update both together. "
        "To use OpenClaw, create a new daemon with this version."
    ),
    "services-changed": (
        "{name}'s background services are not running as they were installed. "
        "Restart your computer, check that {name} answers on Telegram, then try again."
    ),
    "hermes-profile": (
        "{name}'s Hermes Agent settings changed since installation, so the update was not started. "
        "Your daemon keeps working."
    ),
    "host-config": (
        "{name}'s Hermes Agent settings need a migration that this version does not do yet. "
        "Your daemon keeps working."
    ),
    "review": "{name} could not be checked for its update. Your daemon keeps working; export the diagnostic to report it.",
}


def _resumable_creation(base, source_profile, state):
    """An OpenClaw prepared for this daemon but never attached, such as after a failure."""
    from daemons_companion.hosts.attachment import host_reference

    root = base / "Companion/private/host-setups"
    if not root.is_dir():
        return None
    for path in sorted(root.glob("*.json")):
        if not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
            continue
        data = json.loads(checked_file(path, 65536))
        if data.get("source") != str(source_profile):
            continue
        record = state / ("host-attachment-" + host_reference(Path(data["profile"])) + ".json")
        if record.exists() and json.loads(checked_file(record, 12 * 1024 * 1024)).get("status") == "active":
            continue
        return path.stem
    return None


def _connect_model(ui, creation, data):
    from daemon_core import api_providers as api

    if data["phase"] not in {"key", "model"}:
        return data
    ui.phase("model")
    while True:
        try:
            kinds, labels = model_access.options()
            kind = kinds[ui.choose("Model access for OpenClaw", labels)]
            if kind == "codex_cli":
                from daemons_companion.openclaw.subscription import chatgpt_events

                ui.say("OpenClaw uses its own ChatGPT connection. Your daemon keeps its memory connection.")
                if hasattr(ui, "emit"):
                    ui.emit("connection-start")
                creation.events = chatgpt_events(ui)
                try:
                    with ui.activity("Connect ChatGPT for OpenClaw conversations..."):
                        data = creation.perform("key", data["revision"], {"kind": kind})
                finally:
                    creation.events = None
                ui.say("ChatGPT connection verified.")
                model = _choose_subscription_model(ui, data["models"])
            else:
                key = api.validate_key(ui.secret("Paste your API key (hidden)"))
                with ui.activity("Checking your API key and available models..."):
                    data = creation.perform("key", data["revision"], {"kind": kind, "key": key})
                model = _choose_api_model(ui, kind, key, data["models"])
            with ui.activity("Saving your model choice..."):
                return creation.perform("model", data["revision"], {"model": model})
        except Back:
            data = creation.current()
        except (api.ApiConnectionError, ValueError, RuntimeError) as error:
            ui.say(
                str(error)
                if isinstance(error, api.ApiConnectionError)
                else "The model connection could not be completed. Your progress is kept; try again or choose another provider."
            )
            data = creation.current()


def _choose_subscription_model(ui, models):
    from daemons_companion.openclaw.subscription import SUPPORTED_MODELS
    from daemons_companion.setup.model_selection import select_model

    return select_model(
        ui,
        lambda: tuple(m for m in SUPPORTED_MODELS if m.identifier in models),
        "",
        resume=False,
        show_unavailable=False,
    )


def _choose_api_model(ui, kind, key, models):
    from types import SimpleNamespace
    from daemons_companion.setup.api_setup import ApiConnection, choose_api_model

    # The same selection and connection test as a first installation.
    owner = SimpleNamespace(
        api_connection=ApiConnection(kind, "", key, tuple(models)), selected_host="openclaw"
    )
    return choose_api_model(owner, ui)


def _connect_telegram(ui, creation, data):
    if data["phase"] != "telegram":
        return data
    ui.phase("telegram")
    ui.say("Create a new bot for OpenClaw: the Hermes Agent bot stays in place. Your Telegram account is already known.")
    create_bot(ui)

    def check(token):
        try:
            result = creation.perform("telegram", data["revision"], {"token": token})
            return result | {"username": result["bot"]}
        except ValueError as error:
            if "separate bot" in str(error):
                raise TelegramSetupError(
                    "This is your Hermes Agent bot. Create a new bot with BotFather for OpenClaw."
                ) from None
            raise TelegramSetupError(
                "Telegram could not verify this bot. Check the token and try again."
            ) from None

    return verify_telegram_token(ui, check)


def _attach(source_profile, base, host):
    from daemons_companion.hosts.attachment import apply, inspect
    from daemons_companion.hosts.attachment_command import layouts

    layout, controller = layouts(
        {
            "source_profile": str(source_profile),
            "target_profile": str(host.profile),
            "base": str(base),
            "api_port": free_port(),
        }
    )
    plan = inspect(layout)
    controller.snapshot()
    if not controller.probe("maintenance"):
        raise RuntimeError("The daemon's memory maintenance is not running.")
    return apply(layout, plan["revision"], controller)


def _first_message(ui, binding, bot, name):
    from daemons_companion.hermes.first_install import first_exchange

    started = time.time()
    ui.phase("conversation")
    ui.say(f"OpenClaw is ready. Open https://t.me/{bot}")
    ui.say(
        "Press Start, then wait: your daemon writes to you first. Its first message can take up to a minute, so there is no need to type anything."
    )
    ui.say(
        "If nothing arrives after a minute, send it a first message in your own words. Waiting for a real reply and its memory work..."
    )
    with ui.activity("Waiting for your Telegram reply and memory work..."):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if first_exchange(
                binding.state / "continuity/shared",
                since=started,
                user_id=binding.user_id,
                platform="openclaw",
            ):
                ui.say("A Telegram reply and its completed memory work have been verified.")
                break
            time.sleep(1)
        else:
            ui.say(
                "OpenClaw is installed, but its first reply has not been confirmed yet. Check its health in the app after sending a message."
            )
    ui.say(f"{name} now answers you from both bots. Use whichever you prefer.")
    ui.say(
        "To talk with your daemon from anywhere, install the Telegram app on your iPhone or Android phone and sign in to the same account."
    )
