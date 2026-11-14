"""Update an existing daemon's runtime to the one this app carries.

The reviewed transaction is runtime.upgrade: services stopped, the new copy
installed beside the old one, the host profile pointed at it, services
restarted and checked, everything restored if a step fails. Only Companion code
may change; the engine, memory, model connection and Telegram bot stay as they
are. This module only guides the person through it, from the daemon's page or
before Add a host.
"""
from __future__ import annotations

from dataclasses import dataclass


class UpdateStopped(RuntimeError):
    """A plain explanation was shown; nothing was changed."""

    def __init__(self, message, code=None):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class UpdateTexts:
    """What the person reads; each text takes the daemon's name."""

    needed: str
    scope: str
    question: str
    confirm: str
    restore_question: str
    restore: str
    refusals: dict


_KEEPS_WORKING = "Your daemon keeps working."
_COMMON_REFUSALS = {
    "services-changed": (
        "{name}'s background services are not running as they were installed. "
        "Restart your computer, check that {name} answers on Telegram, then try again."
    ),
    "hermes-profile": (
        "{name}'s Hermes Agent settings changed since installation, so the update was not started. "
        + _KEEPS_WORKING
    ),
    "openclaw-profile": (
        "{name}'s OpenClaw settings changed since installation, so the update was not started. "
        + _KEEPS_WORKING
    ),
    "host-config": (
        "{name}'s Hermes Agent settings need a migration that this version does not do yet. "
        + _KEEPS_WORKING
    ),
    "review": "{name} could not be checked for its update. Your daemon keeps working; export the diagnostic to report it.",
}

_TOO_OLD_FOR_HOST = (
    "{name} was created with an older version of the app, which cannot be updated automatically. "
    "To use OpenClaw, create a new daemon with this version."
)
# Before Add a host: the wording the host addition has always used.
ADD_HOST = UpdateTexts(
    needed="{name} first needs the update that comes with this app version.",
    scope="Its memory, model account and Telegram bot do not change. Its services restart for a few seconds.",
    question="Update your daemon?",
    confirm="Update and continue",
    restore_question="Restore the previous version?",
    restore="Restore and continue",
    refusals={
        **_COMMON_REFUSALS,
        "protected-runtime": _TOO_OLD_FOR_HOST,
        "portability": _TOO_OLD_FOR_HOST,
        "attached-hosts": (
            "{name} already has another host attached, and this version cannot update both together. "
            "To use OpenClaw, create a new daemon with this version."
        ),
    },
)

_ENGINE = (
    "The update that comes with this app version also changes the daemon's engine, which an existing "
    "daemon keeps. {name} keeps working as it is. To get it, back up {name}'s memory, then install a "
    "new daemon from that backup."
)
# From the daemon's page.
STANDALONE = UpdateTexts(
    needed="An update for {name} comes with this app version. It changes how the app installs and runs {name}, not what it says or remembers.",
    scope="Its memory, model account and Telegram bot do not change. It does not answer on Telegram until its services have restarted.",
    question="Update {name} now?",
    confirm="Update",
    restore_question="Restore the previous version?",
    restore="Restore",
    refusals={
        **_COMMON_REFUSALS,
        "protected-runtime": _ENGINE,
        "portability": "{name} cannot be updated automatically by this version of the app. " + _KEEPS_WORKING,
        "attached-hosts": (
            "{name} also answers from another host, and this version cannot update both hosts together yet. "
            + _KEEPS_WORKING
        ),
    },
)

INTERRUPTED = "A previous update of {name} was interrupted. The app can restore its previous version safely."
UPDATING = "Updating your daemon. This can take a few minutes..."
ROLLED_BACK = (
    "The update did not complete. {name} was restored to its previous version and keeps working. "
    "Export the diagnostic if you want to report it."
)
UP_TO_DATE = "{name} is up to date."


def ensure_current(ui, source_profile, base, name, texts=STANDALONE, *, cancel):
    """Bring the daemon to the app's runtime, or explain why not.

    Returns False when it already was, True once an update or a restoration
    committed. `cancel` is raised when the person declines.
    """
    from daemons_companion.runtime.upgrade import current_status, from_request

    upgrade = from_request({"source_profile": str(source_profile), "base": str(base)})
    status = current_status(upgrade)
    if status["status"] == "current":
        return False
    recover = status["status"] not in {"required", "committed", "rolled_back"}
    if recover:
        ui.say(INTERRUPTED.format(name=name))
    else:
        ui.say(texts.needed.format(name=name))
        ui.say(texts.scope.format(name=name))
    question = texts.restore_question if recover else texts.question
    action = texts.restore if recover else texts.confirm
    if ui.choose(question.format(name=name), (action, "Cancel")) != 0:
        raise cancel
    with ui.activity(UPDATING):
        if recover:
            result = upgrade.run(status["revision"], recover=True)
        else:
            try:
                reviewed = upgrade.review()
            except ValueError as error:
                code = getattr(error, "code", None) or "review"
                ui.say(texts.refusals.get(code, texts.refusals["review"]).format(name=name))
                raise UpdateStopped("Daemon update refused.", code) from None
            result = upgrade.run(reviewed["revision"])
    if result.get("status") != "committed":
        ui.say(ROLLED_BACK.format(name=name))
        raise UpdateStopped("Update rolled back.")
    ui.say(UP_TO_DATE.format(name=name))
    return True


def run(ui, source_profile, base, name):
    """The guided update started from the daemon's page."""
    from daemons_companion.desktop.terminal import Quit
    from daemons_companion.openclaw.host_install import direct

    source_profile, base = direct(source_profile), direct(base)
    if not source_profile.is_relative_to(base):
        raise ValueError("The daemon is not an owned private installation.")
    ui.phase("installation")
    return ensure_current(ui, source_profile, base, name, STANDALONE, cancel=Quit)
