"""Move a daemon created by the app to another model provider, on the same host.

A guided session, like a first install: the person chooses among the providers
offered to new daemons, connects it (an API key, or the ChatGPT sign-ins) and
chooses an approved model. Nothing changes before that. The daemon's services
are then stopped once its memory work is done, its settings switched
(desktop/provider_settings.py) and its services restarted. The previous
settings are kept until the person has seen the daemon answer on Telegram;
going back, an error or an unfinished session restores them exactly.

A daemon on the ChatGPT subscription can also change its model the same way
(``model_only``): its sign-ins are reused, the current private Codex is
installed when the daemon has an older one, and the models offered are those
its account and its host list.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from types import SimpleNamespace

from daemons_companion.desktop.provider_settings import (
    SUBSCRIPTION,
    OwnedDaemon,
    ProviderSettingsError,
    service_scheduler,
)
from daemons_companion.desktop.terminal import Back, Quit
from daemons_companion.setup import model_access

FIRST_REPLY_SECONDS = 300
PROVIDER_NAMES = {
    "openai_api": "OpenAI",
    "anthropic_api": "Anthropic",
    "mistral_api": "Mistral",
    "xai_api": "SpaceXSI",
    SUBSCRIPTION: "ChatGPT",
}


@dataclass
class Connection:
    kind: str
    model: str
    key: str | None = None
    codex: Path | None = None


def run(ui, profile: Path, base: Path, name: str, context, *, model_only: bool = False) -> bool:
    """True once the daemon answered on its new provider and the change was kept."""
    daemon = OwnedDaemon(profile)
    info = daemon.describe()
    if info["pending"]:
        raise ProviderSettingsError("provider.pending")
    if model_only:
        return _change_model(ui, daemon, base, info, name, context)
    if not info["switchable"]:
        raise ProviderSettingsError("provider.unsupported")
    connection = _connect(ui, daemon, info, name, context)
    if connection is None:
        return False
    provider = PROVIDER_NAMES[connection.kind]
    choice = ui.choose(f"Switch {name} to {provider} with {connection.model}?",
                       (f"Switch to {provider}", "Cancel"))
    if choice != 0:
        raise Quit()
    switched_at = _switch(ui, daemon, base, connection, info["revision"])
    return _conclude(ui, daemon, base, name, provider, switched_at)


def _change_model(ui, daemon, base, info, name, context) -> bool:
    import json
    from daemons_companion.openclaw.host_install import checked_file

    if not info.get("model_changeable"):
        raise ProviderSettingsError("provider.unsupported")
    current = model_label(info["model"])
    ui.phase("model")
    ui.say(f"{name} uses ChatGPT with {current}. Its memory, its identity and its Telegram bot stay the same; "
           "only the model changes. Nothing changes before you confirm.")
    connection = _subscription(ui, daemon, context, signed_in=True)
    codex = json.loads(checked_file(daemon.private_connection, 8192)).get("codex")
    if connection.model == info["model"] and str(connection.codex) == codex:
        ui.say(f"{name} already uses {current}. Nothing was changed.")
        return False
    chosen = model_label(connection.model)
    if ui.choose(f"Change {name}'s model to {chosen}?", (f"Change to {chosen}", "Cancel")) != 0:
        raise Quit()
    switched_at = _switch(ui, daemon, base, connection, info["revision"])
    return _conclude(ui, daemon, base, name, chosen, switched_at, previous=current)


def model_label(identifier: str) -> str:
    from daemons_companion.setup.model_selection import SUPPORTED_MODELS

    return next((m.label for m in SUPPORTED_MODELS if m.identifier == identifier), identifier)


# Connection --------------------------------------------------------------


def _connect(ui, daemon, info, name, context) -> Connection | None:
    host = daemon.scope.host_kind
    current = PROVIDER_NAMES.get(info["kind"], info["kind"])
    ui.phase("model")
    ui.say(f"{name} uses {current} with {info['model']}. Its memory, its identity and its Telegram bot stay "
           "the same; only the model connection changes. Nothing changes before you confirm.")
    kinds, labels = model_access.options()
    offered = [(kind, label) for kind, label in zip(kinds, labels) if kind != info["kind"]]
    while True:
        try:
            kind = offered[ui.choose("New model access", tuple(label for _, label in offered))][0]
            if kind == SUBSCRIPTION:
                return _subscription(ui, daemon, context)
            return _api(ui, host, kind)
        except Back:
            continue


def _api(ui, host, kind) -> Connection:
    from daemon_core import api_providers as api
    from daemons_companion.setup import provider_api
    from daemons_companion.setup.api_setup import ApiConnection, choose_api_model

    ui.say("Your API key is used for conversations and memory maintenance. API usage is billed by your provider, "
           "separately from subscriptions. It is stored in your operating system credential store.")
    while True:
        try:
            key = api.validate_key(ui.secret("Paste your API key (hidden)"))
            with ui.activity("Checking your API key and available models..."):
                models = provider_api.model_catalog(kind, key)
            break
        except api.ApiConnectionError as error:
            ui.say(str(error))
            if ui.choose("API connection not ready", ("Enter API key again", "Back to model access")) == 1:
                raise Back from None
    # The same selection and connection test as a first installation.
    owner = SimpleNamespace(api_connection=ApiConnection(kind, "", key, tuple(models)), selected_host=host)
    model = choose_api_model(owner, ui)
    return Connection(kind, model, key=owner.api_connection.key)


def _subscription(ui, daemon, context, *, signed_in: bool = False) -> Connection:
    if daemon.openclaw:
        from daemons_companion.openclaw.gateway import host_for
        from daemons_companion.openclaw.subscription import first_connection

        codex, model = first_connection(host_for(daemon.scope), ui, {},
                                        current=daemon.provider()["model"] if signed_in else "")
        return Connection(SUBSCRIPTION, model, codex=codex)
    from daemons_companion.runtime.host_install import HostInstallLayout
    from daemons_companion.setup.codex_install import install_private_codex
    from daemons_companion.setup.model_selection import select_model
    from daemons_companion.setup.subscription import PrivateSubscriptionSetup

    layout = HostInstallLayout(daemon.scope.profile.parents[4], context.uv_executable, context.uv_sha256,
                               daemon.scope.installation_id, context.target_os)
    layout.validate()
    if layout.profile != daemon.scope.profile:
        raise ProviderSettingsError("provider.unsupported")
    if not signed_in:
        # A model change reuses both sign-ins; sign_in() asks only for a missing one.
        ui.say("Connect your ChatGPT account in the browser. No API key is needed.")
        ui.say("Hermes and the daemon keep separate native sessions, so two sign-ins may be needed: "
               "the first in your browser, the second with a code shown here. Complete both to continue.")
        if hasattr(ui, "emit"):
            ui.emit("connection-start")
    with ui.activity("Preparing ChatGPT access...") as progress:
        codex = install_private_codex(layout, progress)
    runner = getattr(ui, "subscription_runner", None)
    operation = PrivateSubscriptionSetup(layout, codex=codex, **({"runner": runner} if runner else {}))
    status = operation.sign_in(ui.say, activity=ui.activity)
    if not status.codex or not status.hermes:
        raise RuntimeError("Both native ChatGPT sign-ins must be confirmed before continuing.")
    ui.say("ChatGPT sign-ins are confirmed.")
    bridge = context.bundle_root / "integrations/hermes_plugin/companion_models.py"
    current = daemon.provider()["model"] if signed_in else ""
    model = select_model(ui, lambda: operation.model_choices(bridge), current)
    return Connection(SUBSCRIPTION, model, codex=codex)


# Switch ------------------------------------------------------------------


def _switch(ui, daemon, base, connection, revision) -> float:
    from daemons_companion.backup.live import QUEUE_WAIT_SECONDS, WAIT_SECONDS, pending_work, stalled
    from daemons_companion.removal.errors import Refused

    removal = service_scheduler(daemon.scope.profile, base)
    scheduler = removal.scheduler
    spool = daemon.scope.home / "data/state/after-turn-spool"
    ui.phase("installation")
    with ui.activity("Switching the model connection..."):
        try:
            captures = removal.service_snapshot(daemon.scope.home / "data/memory")
        except Refused:
            raise ProviderSettingsError("provider.unsupported") from None
        # The local search service does not depend on the provider and keeps running.
        services = {code: value for code, value in captures.items() if not code.endswith("embeddings")}
        stopped = []
        try:
            # The gateway first: no new turn reaches the daemon after this.
            gateway = next(iter(services))
            scheduler.restore(gateway, None)
            stopped.append(gateway)
            waited = 0.0
            while pending_work(spool) and not stalled(spool):
                if waited >= QUEUE_WAIT_SECONDS:
                    raise ProviderSettingsError("provider.busy")
                time.sleep(WAIT_SECONDS)
                waited += WAIT_SECONDS
            for code in list(services)[1:]:
                scheduler.restore(code, None)
                stopped.append(code)
            switched_at = time.time()
            daemon.switch(revision, connection.kind, connection.model, connection.key, connection.codex, services)
        except BaseException:
            for code in reversed(stopped):
                scheduler.restore(code, services[code])
            raise
        try:
            for code in reversed(list(services)):
                scheduler.restore(code, services[code])
        except BaseException:
            daemon.revert(scheduler)
            raise
    return switched_at


# Conclusion --------------------------------------------------------------


def _answered(daemon, since: float) -> bool:
    import json
    from daemons_companion.hermes.first_install import first_exchange
    from daemons_companion.openclaw.host_install import checked_file

    user = json.loads(checked_file(daemon.private_connection, 8192))["user_id"]
    receipts, platform = ((daemon.scope.home / "data/state/continuity/shared", "openclaw") if daemon.openclaw
                          else (daemon.scope.home / "data/state/receipts", "telegram"))
    deadline = time.monotonic() + FIRST_REPLY_SECONDS
    while True:
        if first_exchange(receipts, since=since, user_id=user, platform=platform):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def _conclude(ui, daemon, base, name, provider, switched_at, *, previous: str | None = None) -> bool:
    scheduler = service_scheduler(daemon.scope.profile, base).scheduler
    previous = previous or PROVIDER_NAMES.get(_previous_kind(daemon), "its previous provider")
    ui.phase("conversation")
    ui.say(f"{name} now uses {provider}. Send it a message on Telegram, in the same conversation as before.")
    while True:
        with ui.activity("Waiting for your Telegram reply and memory work..."):
            received = _answered(daemon, switched_at)
        if received:
            ui.say(f"{name} answered with {provider}.")
            choice = ui.choose(f"Did {name}'s reply arrive in Telegram?",
                               (f"Yes — keep {provider}", "Not yet — wait again", f"Go back to {previous}"))
            choice = {0: "keep", 1: "wait", 2: "revert"}[choice]
        else:
            ui.say(f"No reply from {name} with {provider} was confirmed yet.")
            choice = {0: "wait", 1: "revert"}[ui.choose(f"{name} has not answered with {provider} yet",
                                                        ("Wait again", f"Go back to {previous}"))]
        if choice == "keep":
            with ui.activity("Saving the new model connection..."):
                daemon.commit()
            ui.say(f"{name} now uses {provider}. The previous API key, if any, was removed from this computer.")
            return True
        if choice == "revert":
            with ui.activity(f"Going back to {previous}..."):
                daemon.revert(scheduler)
            ui.say(f"{name} uses {previous} again, exactly as before.")
            return False


def _previous_kind(daemon) -> str:
    import json
    from daemons_companion.openclaw.host_install import checked_file

    saved = daemon.change_dir / "provider.json"
    try:
        return json.loads(checked_file(saved, 65536)).get("kind", "")
    except (OSError, ValueError):
        return ""


def abandon(profile: Path, base: Path) -> None:
    """A session that ended before the person kept the change goes back."""
    daemon = OwnedDaemon(profile)
    if daemon.pending() is not None:
        daemon.revert(service_scheduler(profile, base).scheduler)


__all__ = ["abandon", "run"]

