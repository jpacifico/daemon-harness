"""First-install OpenClaw onboarding using the common transaction and memory."""

from __future__ import annotations
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import socket
import time
import uuid

from daemons_companion.openclaw.host_install import (
    OpenClawHostLayout,
    checked_file,
    install,
    plan,
    OPENCLAW_VERSION,
)
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemons_companion.runtime.installation_scope import InstallationScope


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pointer_path(context):
    return context.companion_home / "hosts/openclaw/current.json"


def load_host(context):
    pointer = json.loads(checked_file(pointer_path(context), 8192))
    if (
        pointer.get("schema") != "daemon.openclaw-first.v1"
        or pointer.get("target_os") != context.target_os
    ):
        raise ValueError("The saved OpenClaw setup changed.")
    host = OpenClawHostLayout.existing(
        context.companion_home,
        pointer["installation_id"],
        context.target_os,
        context.bundle_root / "apps/companion/openclaw-runtime",
    )
    if plan(host)["revision"] != pointer.get("acquisition"):
        raise ValueError(
            "The OpenClaw package changed; the previous setup is retained."
        )
    return host, pointer


def acquire(context, ui):
    context.validate()
    if pointer_path(context).exists():
        host, pointer = load_host(context)
    else:
        host = OpenClawHostLayout(
            context.companion_home,
            uuid.uuid4().hex,
            context.target_os,
            context.bundle_root / "apps/companion/openclaw-runtime",
        )
        reviewed = plan(host)
        api_port, gateway_port = free_port(), free_port()
        while api_port == gateway_port:
            gateway_port = free_port()
        pointer = dict(
            schema="daemon.openclaw-first.v1",
            installation_id=host.installation_id,
            acquisition=reviewed["revision"],
            target_os=context.target_os,
            api_port=api_port,
            gateway_port=gateway_port,
        )
        ui.say(
            "OpenClaw will be installed separately, with its own memory, model connection and Telegram bot. Hermes Agent is not required."
        )
        ui.say(
            "Connect your ChatGPT subscription, or an OpenAI or Anthropic API key."
        )
        if (
            ui.choose("Install OpenClaw?", ("Install OpenClaw", "Back to host choice"))
            != 0
        ):
            from daemons_companion.desktop.terminal import Back

            raise Back
        host.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_text(pointer_path(context), json.dumps(pointer) + "\n")
    with ui.activity("Downloading and verifying OpenClaw...") as progress:
        install(host, pointer["acquisition"], progress=progress, resume=True)
        from daemons_companion.openclaw.gateway import private_npm_environment

        private_npm_environment(host, prepare=True)
    return host, pointer


def reconnect_api(owner, ui, kind):
    from daemon_core import api_providers as api
    from daemons_companion.setup import provider_api
    from daemons_companion.setup.api_setup import ApiConnection

    host, _ = load_host(owner.context)
    key = api.validate_key(ui.secret("Paste your API key (hidden)"))
    with ui.activity("Checking your API key and available models..."):
        models = provider_api.model_catalog(kind, key)
    owner.api_connection = ApiConnection(kind, str(host.attempt), key, models)
    return True


def connect_api(owner, ui, host, saved=None):
    from daemon_core import api_providers as api
    from daemons_companion.setup import provider_api
    from daemons_companion.setup.api_setup import ApiConnection, choose_api_model
    from daemons_companion.setup.progress import save
    from daemons_companion.desktop.terminal import Back, Quit

    from daemons_companion.setup.model_access import options

    from daemons_companion.setup.api_setup import reused_key
    from daemons_companion.hosts.change import pending_change

    kinds, labels = options()
    saved = saved or {}
    change = pending_change()
    if change is not None and not saved:
        # The daemon keeps its provider and model; OpenClaw checks them again.
        saved = {"provider_kind": change.identity["kind"], "model": change.identity["model"]}
    if change is not None and change.identity["kind"] not in kinds:
        # A provider no longer offered to new daemons stays the moved daemon's own.
        kinds += (change.identity["kind"],)
    tried = False
    kind = saved.get("provider_kind")
    scope = InstallationScope(host.attempt)
    while True:
        if kind not in kinds:
            kind = kinds[ui.choose("3. Model access to prepare", labels)]
        save(scope, "openclaw", provider_kind=kind)
        try:
            if kind == "codex_cli":
                from daemons_companion.openclaw.subscription import first_connection
                try:
                    owner.openclaw_codex, model = first_connection(host, ui, saved)
                except RuntimeError as error:
                    from daemons_companion.openclaw.subscription import SubscriptionFailure
                    if isinstance(error, SubscriptionFailure) and hasattr(ui, "emit"):
                        ui.emit("diagnostic", phase="model", status="failed",
                                operation="model-check" if error.stage in {"authenticated", "catalog"} else "sign-in",
                                auth_stage=error.stage, error_code="RuntimeError")
                    ui.say(str(error) if isinstance(error, SubscriptionFailure) else
                           "ChatGPT setup could not finish this step. Your progress is retained. Retry or export the diagnostic.")
                    choice = ui.choose("ChatGPT setup needs attention",
                                       ("Retry this step", "Choose another provider", "Quit"))
                    owner.api_connection = None
                    if choice == 2:
                        raise Quit()
                    if choice == 1:
                        kind, saved = None, {}
                    continue
                owner.api_connection = ApiConnection(kind, str(host.attempt), "", (model,))
                save(scope, "openclaw", model=model)
                return model
            # The current host's key is tried once; a rejected key is asked again.
            reused, tried = ("" if tried else reused_key(ui, kind)), True
            key = api.validate_key(reused or ui.secret("Paste your API key (hidden)"))
            with ui.activity("Checking your API key and available models..."):
                models = provider_api.model_catalog(kind, key)
            owner.api_connection = ApiConnection(kind, str(host.attempt), key, models)
            model = choose_api_model(owner, ui, saved.get("model", ""), resume=bool(saved.get("model")))
            save(scope, "openclaw", model=model)
            return model
        except Back:
            kind, saved = None, {}
            owner.api_connection = None
        except api.ApiConnectionError as error:
            ui.say(str(error))


def run(owner, ui):
    # The parent owns the root; a second app cannot concurrently provision it.
    with memory_file_lock(owner.context.companion_home / "openclaw-setup-owner"):
        return _run(owner, ui)


def _run(owner, ui):
    from daemons_companion.setup.plan import (
        HostKind,
        HostProbe,
        ProviderKind,
        ProviderSelection,
        SetupRequest,
        build_setup_plan,
    )
    from daemons_companion.setup.credentials import system_credential_store
    from daemons_companion.hosts.creation import native_config, verify_bot
    from daemons_companion.setup.telegram import (
        prepare_telegram,
        verify_telegram_token,
        TelegramSetupError,
    )
    from daemons_companion.hermes.first_install import (
        initialize_private_memory,
        first_exchange,
    )
    from daemons_companion.runtime.services import (
        MacLaunchdSchedulerBackend,
        WindowsTaskSchedulerBackend,
    )
    from daemons_companion.setup.install_session import (
        prepare_installation,
        execute_prepared_installation,
    )
    from daemons_companion.openclaw.ready_composition import (
        OpenClawReadyCompositionAdapter,
        OpenClawReadyCompositionLayout,
    )
    from daemons_companion.openclaw.gateway import PrivateOpenClawControl
    from daemons_companion.desktop.install_diagnostics import (
        InstallationProgress,
        report_installation_failure,
    )
    from daemons_companion.setup.transaction import TransactionEngine, TransactionError
    from daemons_companion.runtime.environment import CommandResult
    from daemons_companion.runtime.bootstrap import run as command

    owner.selected_host = "openclaw"
    host, pointer = acquire(owner.context, ui)
    owner.changed = True
    scope = InstallationScope(host.attempt)
    scope.initialize()
    completed = scope.attempt / "daemon-setup.json"
    if completed.exists():
        data = json.loads(checked_file(completed, 8192))
        owner.daemon_installed = True
        ui.say("Your private daemon is already installed. It will not be reinstalled.")
        ui.say("Open https://t.me/" + data["username"])
        return 0
    for journal in sorted((scope.home / "transactions").glob("*/journal.json")):
        if json.loads(checked_file(journal, 65536)).get("status") != "rolled_back":
            raise RuntimeError(
                "An interrupted OpenClaw transaction needs recovery. Its files and diagnostic are retained."
            )
    from daemons_companion.setup.progress import load, save
    saved = load(scope, "openclaw")
    from daemons_companion.desktop.terminal import Back, ask_daemon_name

    name, gender = saved.get("name", ""), saved.get("gender", "feminine")
    preferences = None
    language_ui = hasattr(ui, "first_meeting_preferences") or hasattr(ui, "user_preferences")
    if "language" in saved:
        ui.conversation_language = saved["language"]
    step = next((n for n, key in ((0, "name"), (1, "gender"), (2, "language"))
                 if key not in saved), 3)
    from daemons_companion.hosts.change import confirm, inherited_identity, pending_change

    change = pending_change()
    inherited = inherited_identity()
    if inherited is not None and step < 3:
        name, gender, language, message = inherited
        values = {"name": name, "gender": gender}
        if language:
            ui.conversation_language = values["language"] = language
        save(scope, "openclaw", **values)
        ui.say(message)
        step = 3 if language else 2

    def check(token):
        try:
            username = verify_bot(token)
        except ValueError:
            raise TelegramSetupError(
                "Telegram could not verify this bot. Check the token and try again."
            ) from None
        return {"username": username, "token": token}

    # All editable form navigation stays before credentials/config publication.
    # In particular a resumed setup must not leak Back to the desktop's generic
    # error handler or start downloading another host in the same attempt.
    while True:
        try:
            ui.phase("identity" if step <= 2 else "model" if step == 3 else "telegram")
            if step == 0:
                name = ask_daemon_name(ui, name)
                save(scope, "openclaw", name=name)
            elif step == 1:
                gender = ("feminine", "masculine")[ui.choose(
                    "How should your daemon refer to itself?", ("She / her", "He / him"),
                    default=int(gender == "masculine"))]
                save(scope, "openclaw", gender=gender)
            elif step == 2:
                if hasattr(ui, "first_meeting_preferences"):
                    ui.first_meeting_preferences()
                    save(scope, "openclaw", language=getattr(ui, "conversation_language", "en"))
                elif hasattr(ui, "user_preferences"):
                    preferences = ui.user_preferences(preferences)
            elif step == 3:
                model = connect_api(owner, ui, host, saved)
            elif step == 4:
                user_id = (confirm(ui, change) if change is not None
                           else prepare_telegram(ui, scope.home, host="openclaw"))
            else:
                identity = None
                token = change.token() if change is not None else ""
                if token:
                    # The same bot: Telegram checks it without disturbing its running gateway.
                    try:
                        with ui.activity("Verifying your Telegram bot..."):
                            identity = check(token)
                        ui.say("Telegram verified @" + identity["username"] + ", the same bot as before.")
                    except TelegramSetupError as error:
                        ui.say(str(error))
                    finally:
                        del token
                identity = identity or verify_telegram_token(ui, check)
                break
            step += 1
        except Back:
            step = max(0, step - 1)
            if step == 2 and not language_ui:
                step = 1
            # A deliberate return to the model form must offer editable choices.
            saved = {}
    connection = owner.api_connection
    selection = ProviderSelection(
        ProviderKind(connection.kind), model, credential_available=connection.kind != "codex_cli"
    )
    store = system_credential_store()
    config = host.profile / "openclaw.json"
    raw = (
        json.dumps(
            native_config(connection.kind, model, user_id, pointer["gateway_port"]),
            sort_keys=True,
        )
        + "\n"
    )
    if config.exists() and checked_file(config, 2 * 1024 * 1024) != raw.encode():
        raise ValueError(
            "OpenClaw configuration changed. Its files are retained for recovery."
        )
    credentials_path = host.attempt / "openclaw-connection.json"
    if credentials_path.exists():
        saved = json.loads(checked_file(credentials_path, 8192))
        if (
            not isinstance(saved, dict)
            or set(saved) != {"schema", "credential", "kind"}
            or saved.get("schema") != "daemon.openclaw-connection.v1"
        ):
            raise ValueError("Saved OpenClaw connection changed.")
        credential = saved["credential"]
        previous = json.loads(store.get(credential) or "{}")
        if previous.get("installation_id") != host.installation_id:
            raise ValueError("Saved credential belongs to another installation.")
    else:
        credential = "provider-" + str(uuid.uuid4())
    store.put(
        credential,
        json.dumps(
            dict(
                installation_id=host.installation_id,
                key=connection.key,
                token=identity["token"],
                gateway=secrets.token_urlsafe(32),
            )
        ),
    )
    atomic_write_text(
        credentials_path,
        json.dumps(
            dict(
                schema="daemon.openclaw-connection.v1",
                credential=credential,
                kind=connection.kind,
            )
        )
        + "\n",
    )
    atomic_write_text(config, raw)
    connection_record = dict(schema="daemon.private-connection.v2", provider_kind=connection.kind, user_id=user_id)
    if connection.kind == "codex_cli":
        import hashlib
        binary = owner.openclaw_codex
        connection_record = dict(schema="daemon.private-connection.v1", codex=str(binary),
                                 codex_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), user_id=user_id)
    atomic_write_text(
        host.attempt / "connection.json",
        json.dumps(
connection_record
        )
        + "\n",
    )
    ui.phase("installation")
    context = replace(
        owner.context,
        installation_scope=scope,
        openclaw_api_port=pointer["api_port"],
        openclaw_gateway_port=pointer["gateway_port"],
    )
    layout = context.install_layout(HostKind.OPENCLAW, host.profile)
    probe = HostProbe(
        HostKind.OPENCLAW,
        True,
        True,
        str(host.entry),
        str(host.profile),
        OPENCLAW_VERSION,
        "verified",
    )
    reviewed = build_setup_plan(
        SetupRequest(HostKind.OPENCLAW, name, gender, selection, preferences), (probe,), codex_available=connection.kind == "codex_cli"
    )
    prepared = prepare_installation(reviewed, layout)
    scheduler = (
        WindowsTaskSchedulerBackend(job_namespace=scope.installation_id)
        if host.target_os == "windows"
        else MacLaunchdSchedulerBackend(
            Path.home() / "Library/LaunchAgents",
            user_id=os.getuid(),
            job_namespace=scope.installation_id,
        )
    )
    from daemons_companion.setup.embeddings import prepare as prepare_embeddings

    with ui.activity("Preparing local memory search...") as progress:
        prepare_embeddings(scope, host.target_os, progress)
    initialize_private_memory(
        scope,
        context.bundle_root,
        user_id,
        root_folder_memory=context.root_folder_memory,
        conversation_language=getattr(ui, "conversation_language", None),
    )
    composition = OpenClawReadyCompositionLayout(
        layout.runtime,
        host.profile,
        context.uv_executable,
        context.uv_sha256,
        host.target_os,
        pointer["api_port"],
        pointer["gateway_port"],
        scope.installation_id,
    )
    control = PrivateOpenClawControl(
        host,
        layout.runtime,
        composition.runtime_environment,
        scheduler,
        pointer["gateway_port"],
    )
    adapter = OpenClawReadyCompositionAdapter(
        composition,
        plan=prepared.plan,
        provider_selection=selection,
        credential_store=store,
        credential_value=connection.key or None,
        codex_available=connection.kind == "codex_cli",
        user_id=user_id,
        scheduler=scheduler,
        control=control,
        runner=lambda cmd, env: CommandResult(
            0, command(list(cmd), dict(env), timeout=900)
        ),
    )
    observed = InstallationProgress(
        adapter, lambda _: None, getattr(ui, "diagnostic", None)
    )
    transactions = scope.home / "transactions"
    # Unknown/partial prior mutations are never overwritten by another plan.
    for journal in sorted(transactions.glob("*/journal.json")):
        status = json.loads(checked_file(journal, 65536)).get("status")
        if status != "rolled_back":
            raise RuntimeError(
                "An interrupted OpenClaw transaction needs recovery before another installation. Its diagnostic is retained."
            )
    transaction_id = uuid.uuid4().hex
    started = time.time()
    try:
        with ui.activity(
            "Installing runtime, plugins and private services..."
        ) as progress:
            observed.progress = progress
            try:
                result = execute_prepared_installation(
                    prepared,
                    adapter=observed,
                    transactions_root=transactions,
                    transaction_id=transaction_id,
                    confirmed_digest=prepared.confirmation_digest,
                )
            except KeyboardInterrupt:
                if (transactions / transaction_id / "journal.json").is_file():
                    TransactionEngine(transactions).recover(
                        prepared.plan,
                        observed,
                        transaction_id=transaction_id,
                        confirmed_plan_digest=prepared.confirmation_digest,
                    )
                raise
    except TransactionError as error:
        report_installation_failure(ui, scope, observed, error, transaction_id)
        raise
    owner.daemon_installed = True
    owner.api_connection = None
    atomic_write_text(
        completed,
        json.dumps(
            dict(
                schema="daemon.private-setup.v1",
                username=identity["username"],
                user_id=user_id,
                transaction_id=result.transaction_id,
            )
        )
        + "\n",
    )
    if change is not None:
        # The user checks the new host; the change session concludes it.
        change.installed(host.profile)
        return 0
    ui.phase("conversation")
    ui.say(
        "Your private gateway and memory worker are ready. Open https://t.me/"
        + identity["username"]
    )
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
                layout.runtime.state_root / "continuity/shared",
                since=started,
                user_id=user_id,
                platform="openclaw",
            ):
                # The waiting instructions no longer apply; the Telegram links stay.
                ui.phase("conversation")
                ui.say(
                    "A Telegram reply and its completed memory work have been verified."
                )
                break
            time.sleep(1)
        else:
            ui.say(
                "Your daemon is installed, but its first reply and memory work have not been confirmed yet. Check its health in the app after sending a message."
            )
    ui.say("Your daemon is installed. You can continue talking on Telegram.")
    ui.say(
        "To talk with your daemon from anywhere, install the Telegram app on your iPhone or Android phone and sign in to the same account."
    )
    return 0
