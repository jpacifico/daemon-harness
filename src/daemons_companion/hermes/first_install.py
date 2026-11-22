"""Connect the confirmed private Hermes plan to its existing transaction owners."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import hashlib
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from daemons_companion.runtime.services import MacLaunchdSchedulerBackend, WindowsTaskSchedulerBackend
from daemons_companion.setup.plan import HostKind, ProviderKind, ProviderSelection, SetupRequest, build_setup_plan
from daemons_companion.hermes.composition import HermesCompositionLayout
from daemons_companion.hermes.ready_composition import HermesReadyCompositionAdapter
from daemons_companion.runtime.host_install import installed_host
from daemons_companion.setup.install_session import prepare_installation, execute_prepared_installation
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.hermes.gateway import PrivateHermesControl, wait_private_gateway_ready
from daemons_companion.setup.subscription import PrivateSubscriptionSetup
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemons_companion.setup.transaction import TransactionEngine, TransactionError
from daemons_companion.desktop.install_diagnostics import InstallationProgress, report_installation_failure
from daemons_companion.setup.telegram import TelegramSetupError, prepare_telegram, verify_telegram_token
from daemons_companion.runtime.bootstrap import run
from daemons_companion.runtime.environment import CommandResult
from daemons_companion.setup.dependencies import ensure_setup_dependencies
from daemons_companion.setup.recovery import retain_failed_python_build

# Runs with the pinned host's interpreter. No token appears in argv, stdout,
# exception output or transaction metadata. Hermes remains the Telegram client.
_TELEGRAM_CONFIGURE = r'''
import asyncio, json, sys
from pathlib import Path
from telegram import Bot
from telegram.error import InvalidToken, NetworkError, TelegramError
from dotenv import set_key, dotenv_values
import yaml
from hermes_constants import get_hermes_home

async def configure():
    data=json.load(sys.stdin)
    home=get_hermes_home()
    if data['token'] is None:
        saved=dotenv_values(home/'.env')
        if saved.get('TELEGRAM_ALLOWED_USERS') != data['user_id']:
            sys.exit(25)
        data['token']=saved.get('TELEGRAM_BOT_TOKEN', '')
    async with Bot(data['token']) as bot:
        identity=await bot.get_me()
        webhook=await bot.get_webhook_info()
        if webhook.url:
            sys.exit(22)
    home=get_hermes_home()
    settings=home/'config.yaml'
    config=yaml.safe_load(settings.read_text(encoding='utf-8')) if settings.exists() else {}
    config=config or {}
    config['model']=data['model_config']
    if data.get('providers'):
        config.setdefault('providers',{}).update(data['providers'])
    config['platforms']={'telegram':{'enabled':True}}
    settings.write_text(yaml.safe_dump(config,sort_keys=False),encoding='utf-8')
    env=home/'.env'
    env.touch(mode=0o600,exist_ok=True)
    for key,value in {'TELEGRAM_BOT_TOKEN':data['token'],
                      'TELEGRAM_ALLOWED_USERS':data['user_id'],
                      'TELEGRAM_HOME_CHANNEL':data['user_id']}.items():
        set_key(str(env),key,value)
    print(json.dumps({'username':identity.username,'bot_id':identity.id}))
try:
    asyncio.run(configure())
except InvalidToken:
    sys.exit(20)
except NetworkError:
    sys.exit(21)
except TelegramError:
    sys.exit(24)
except Exception:
    sys.exit(23)
'''


def configure_telegram(layout, codex, *, token, user_id, model, provider_kind="codex_cli"):
    scope = InstallationScope.from_verified_host(layout)
    scope.validate_home(scope.home)
    if re.fullmatch(r"[1-9][0-9]{0,19}", user_id) is None:
        raise ValueError("Enter your own numeric Telegram user ID.")
    if token is not None and not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token):
        raise TelegramSetupError("The token format is invalid. Copy the entire token from BotFather, including the colon. You can paste it again here.")
    for name in ("config.yaml", ".env"):
        path = layout.profile / name
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("Native profile settings must be direct files.")
    from daemon_core.api_providers import hermes_model_config, hermes_provider_entries
    if provider_kind == "codex_cli":
        environment = PrivateSubscriptionSetup(layout, codex=codex).environment(hermes=True)
        model_config = {'provider': 'openai-codex', 'default': model}
        providers = {}
    else:
        from daemons_companion.runtime.bootstrap import clean_environment, private_home_environment
        environment = clean_environment(layout.attempt)
        environment.update(private_home_environment(layout.attempt / 'os-home'))
        environment['HERMES_HOME'] = str(layout.profile)
        model_config = hermes_model_config(provider_kind, model)
        providers = hermes_provider_entries(provider_kind, model)
    try:
        result = subprocess.run([layout.launch_python, "-B", "-c", _TELEGRAM_CONFIGURE],
                                input=json.dumps({"token": token, "user_id": user_id, "model_config": model_config, "providers": providers}),
                                capture_output=True, text=True, env=environment,
                                cwd=layout.attempt, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        raise TelegramSetupError("Telegram did not respond in time. Check your internet connection and try again here.") from None
    if result.returncode:
        messages = {
            25: "The saved Telegram account does not match this installation. Enter the bot token again to confirm this account.",
            20: "Telegram rejected this token. Copy the current token from BotFather and paste it again here.",
            21: "Cannot reach Telegram. Check your internet connection and try again here.",
            22: "This bot already has a webhook connected to another service. Create a new dedicated bot in BotFather and paste its token here.",
            24: "Telegram could not verify this bot. Check the bot in BotFather, then try again here.",
            23: "Telegram setup could not save the private configuration. Check that the private installation folder is writable, then try again.",
        }
        raise TelegramSetupError(messages.get(result.returncode,
            "The private Telegram setup process could not start correctly. Close the assistant and reopen it from the extracted download."))
    try:
        identity = json.loads(result.stdout)
        if (not isinstance(identity, dict)
                or not isinstance(identity.get("username"), str)
                or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", identity["username"])
                or type(identity.get("bot_id")) is not int or identity["bot_id"] <= 0):
            raise ValueError
    except (ValueError, TypeError):
        raise TelegramSetupError("Telegram verification returned an incomplete bot identity. Please try again.") from None
    return identity


def daemon_receipt_dirs(state_root: Path) -> tuple[Path, Path]:
    """Where a Companion daemon's turn receipts are written.

    The after-turn worker writes to ``receipts``; an OpenClaw daemon's live
    service writes the turn's identity and reply to ``continuity/shared``.
    A turn is only complete when both are read together.
    """
    return state_root / "receipts", state_root / "continuity/shared"


def first_exchange(receipts, *, since: float, user_id: str, platform: str = "telegram"):
    """Read bounded technical receipts; never poll Telegram or copy dialogue.

    ``receipts`` is one directory or several; a turn's events are merged across
    all of them by turn id.
    """
    directories = [receipts] if isinstance(receipts, Path) else list(receipts)
    files = []
    for directory in directories:
        if not directory.is_dir() or directory.is_symlink():
            continue
        files.extend(directory.glob("*.jsonl"))
    turns = {}
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
        if path.is_symlink() or path.stat().st_mtime < since or path.stat().st_size > 2 * 1024 * 1024:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                at = datetime.fromisoformat(event.get("at", "").replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError, TypeError):
                continue
            if event.get("schema") != "daemon.continuity-receipt.event.v1" or at < since:
                continue
            turn_id = event.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                continue
            turn = turns.setdefault(turn_id, {"identity": {}, "stages": {}, "degraded": False})
            if isinstance(event.get("identity"), dict):
                turn["identity"].update(event["identity"])
            if not isinstance(event.get("stage"), str):
                continue
            turn["stages"][event["stage"]] = event.get("status")
            if isinstance(event.get("details"), dict) and event["details"].get("degraded") is True:
                turn["degraded"] = True
    for turn in turns.values():
        identity, stages = turn["identity"], turn["stages"]
        if (not turn["degraded"] and "failed" not in stages.values()
                and identity.get("user_ref") == hashlib.sha256(user_id.encode()).hexdigest()[:16]
                and identity.get("platform") == platform
                and all(stages.get(name) == "completed" for name in ("response", "l0", "cycle"))):
            return True
    return False


def _saved_telegram_user(scope):
    """Only non-secret connection metadata is read by the installer process."""
    path = scope.attempt / "connection.json"
    if not path.exists():
        return None
    if path.is_symlink() or path.resolve() != path or not path.is_file() or path.stat().st_size > 8192:
        raise ValueError("Saved connection must be a direct private file.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") not in {"daemon.private-connection.v1", "daemon.private-connection.v2"}
            or not isinstance(data.get("user_id"), str)
            or re.fullmatch(r"[1-9][0-9]{0,19}", data['user_id']) is None):
        raise ValueError("Saved Telegram account is invalid.")
    return data['user_id']


def _complete_private_setup(owner, ui, request, response):
    from daemon_core.api_providers import API_PROVIDERS
    kind = request['provider']['kind']
    if request['host'] != 'hermes' or kind not in {'codex_cli', *API_PROVIDERS}:
        ui.say("The private installation requires Hermes and a supported model connection.")
        return False
    with ui.activity('Checking your installation and model access...'):
        layout = owner.layout()
        host = installed_host(layout)
        if host is None:
            ui.say("Your existing personal host is unchanged. Select a separate private Hermes to install the daemon.")
            return False
        layout = replace(layout, installation_id=Path(host.config_path).parent.name)
        codex, credential_store, credential_value = None, None, None
        if kind == 'codex_cli':
            codex = owner.codex_executable(layout)
            if codex is None:
                raise ValueError("Complete the private Codex installation and sign-in first.")
            status = PrivateSubscriptionSetup(layout, codex=codex).status()
            if not status.codex or not status.hermes:
                raise ValueError("Complete both native ChatGPT sign-ins first.")
        else:
            connection = getattr(owner, 'api_connection', None)
            if (connection is None or connection.kind != kind or connection.attempt != str(layout.attempt)
                    or connection.verified_model != request['provider']['model']):
                raise ValueError("Verify your API key and model before continuing.")
            from daemons_companion.setup.credentials import system_credential_store
            credential_store, credential_value = system_credential_store(), connection.key
        scope = InstallationScope.from_verified_host(layout)
        context = replace(owner.context, installation_scope=scope)
        selection = ProviderSelection(ProviderKind(kind), request['provider']['model'], credential_available=kind != 'codex_cli')
        from daemon_core.onboarding_user import preferences_from_request
        reviewed = build_setup_plan(SetupRequest(HostKind.HERMES, request['daemon_name'],
                                    request['daemon_gender'], selection, preferences_from_request(request)), (host,), codex_available=True)
        install_layout = context.install_layout(HostKind.HERMES, layout.profile)
        prepared = prepare_installation(reviewed, install_layout)
        if prepared.confirmation_digest != response['confirmation_digest']:
            raise ValueError("The installation plan changed. Review it again.")
    scheduler = (WindowsTaskSchedulerBackend(job_namespace=scope.installation_id)
                 if layout.target_os == 'windows' else MacLaunchdSchedulerBackend(
                     Path.home() / 'Library/LaunchAgents', user_id=os.getuid(), job_namespace=scope.installation_id))
    recovered = []
    with ui.activity('Checking previous installation and preparing recovery...'):
        context.validate()
        scope.initialize()
        if (scope.attempt / "companion-removal.json").exists():
            raise ValueError("This installation has a removal record. Manage it from Installations or create a separate new installation.")
        completed = scope.attempt / "daemon-setup.json"
        if completed.exists():
            ui.say("A daemon setup already exists here. It is preserved; use its bot rather than reinstalling.")
            return False
        for journal in sorted((scope.home / "transactions").glob("*/journal.json")):
            if journal.is_symlink() or journal.stat().st_size > 65536:
                raise ValueError("Installation journal is invalid.")
            status = json.loads(journal.read_text()).get("status")
            if status == "committed":
                raise RuntimeError("A committed installation is missing its completion receipt. Recovery is required; its configuration and jobs are preserved.")
            if status != "rolled_back":
                with memory_file_lock(scope.home / "install-owner"):
                    retained = retain_failed_python_build(scope, journal, scheduler)
                recovered.append(retained)
        ensure_setup_dependencies(layout, context.bundle_root, lambda _: None)
    if recovered:
        ui.say("Recovery prepared. The previous failed Python build and its journal are retained; your connections and memory are unchanged.")
        for retained in recovered:
            if retained:
                ui.say("Preserved build: " + str(retained))
    if hasattr(ui, 'phase'):
        ui.phase('telegram')
    from daemons_companion.hosts.change import confirm, pending_change
    change = pending_change()
    saved_user_id = None if change is not None else _saved_telegram_user(scope)
    reuse = False
    if saved_user_id:
        choice = ui.choose("Continue with your saved Telegram account " + saved_user_id + "?",
                           ("Use saved connection and install", "Change Telegram details", "Quit"))
        if choice == 2:
            from daemons_companion.desktop.terminal import Quit
            raise Quit
        reuse = choice == 0
    if change is not None:
        user_id = confirm(ui, change)
    else:
        user_id = saved_user_id if reuse else prepare_telegram(ui, scope.home)
    provider_arguments = {} if kind == 'codex_cli' else {'provider_kind': kind}
    identity = None
    token = change.token() if change is not None else ''
    if token:
        # The same bot: Telegram checks it without disturbing its running gateway.
        try:
            with ui.activity('Verifying your Telegram bot...'):
                identity = configure_telegram(layout, codex, token=token, user_id=user_id, model=selection.model, **provider_arguments)
            ui.say('Telegram verified @' + identity['username'] + ', the same bot as before.')
        except TelegramSetupError as error:
            ui.say(str(error))
        finally:
            del token
    if reuse:
        try:
            with ui.activity('Verifying your saved Telegram connection...'):
                identity = configure_telegram(layout, codex, token=None, user_id=user_id, model=selection.model, **provider_arguments)
            ui.say('Saved connection verified — Telegram @' + identity['username'] + '. Installing your daemon next.')
        except TelegramSetupError as error:
            ui.say(str(error))
    if identity is None:
        identity = verify_telegram_token(ui, lambda token: configure_telegram(
            layout, codex, token=token, user_id=user_id, model=selection.model, **provider_arguments))
    connection = scope.attempt / "connection.json"
    if connection.is_symlink() or connection.resolve() != connection:
        raise ValueError("Connection settings must be a direct private file.")
    connection_data = ({"schema": "daemon.private-connection.v1", "codex": str(codex),
                        "codex_sha256": hashlib.sha256(codex.read_bytes()).hexdigest(), "user_id": user_id}
                       if codex is not None else
                       {"schema": "daemon.private-connection.v2", "provider_kind": kind, "user_id": user_id})
    atomic_write_text(connection, json.dumps(connection_data) + "\n")
    if hasattr(ui, 'phase'):
        ui.phase('installation')
    from daemons_companion.setup.embeddings import prepare as prepare_embeddings
    ui.say('Local memory search uses a private Ollama service and a 639 MB model. The first download can take several minutes.')
    with ui.activity('Preparing local memory search...') as progress:
        prepare_embeddings(scope, layout.target_os, progress)
    composition = HermesCompositionLayout(install_layout.runtime, layout.profile, layout.uv_executable,
                                         layout.uv_sha256, layout.target_os, scope.installation_id)
    control = PrivateHermesControl(layout, install_layout.runtime, scheduler)
    initialize_private_memory(scope, context.bundle_root, user_id,
                              root_folder_memory=context.root_folder_memory,
                              conversation_language=getattr(ui, "conversation_language", None))
    adapter = HermesReadyCompositionAdapter(composition, plan=prepared.plan, provider_selection=selection,
                credential_store=credential_store, credential_value=credential_value, user_id=user_id, scheduler=scheduler,
                control=control, codex_available=True,
                runner=lambda command, environment: CommandResult(0, run(list(command), dict(environment), timeout=900)),
                mutation_hook=lambda _: None)
    ui.say("Installing runtime, plugins and private services. This can take several minutes...")
    started = time.time()
    transaction_id = uuid.uuid4().hex
    transactions = scope.home / "transactions"
    observed = InstallationProgress(adapter, lambda _: None, getattr(ui, "diagnostic", None))
    try:
        with ui.activity('Installing runtime, plugins and private services...') as progress:
            observed.progress = progress
            with memory_file_lock(scope.home / "install-owner"):
                try:
                    result = execute_prepared_installation(prepared, adapter=observed,
                        transactions_root=transactions, transaction_id=transaction_id,
                        confirmed_digest=prepared.confirmation_digest)
                except KeyboardInterrupt:
                    if (transactions / transaction_id / "journal.json").is_file():
                        TransactionEngine(transactions).recover(prepared.plan, observed,
                            transaction_id=transaction_id, confirmed_plan_digest=prepared.confirmation_digest)
                    raise
    except TransactionError as error:
        report_installation_failure(ui, scope, observed, error, transaction_id)
        raise
    if result.status != "committed":
        raise RuntimeError("Daemon installation did not complete. Recovery information is retained privately.")
    owner.daemon_installed = True
    owner.api_connection = None
    atomic_write_text(completed, json.dumps({"schema": "daemon.private-setup.v1", "username": identity['username'],
                     "user_id": user_id, "transaction_id": result.transaction_id}) + "\n")
    if change is not None:
        # The user checks the new host; the change session concludes it.
        change.installed(layout.profile)
        return True
    if hasattr(ui, 'phase'):
        ui.phase('conversation')
    link = 'https://t.me/' + identity['username']
    ui.say("Your private gateway and memory worker are ready. Open " + link)
    ui.say("Press Start, then wait: your daemon writes to you first. Its first message can take up to a minute, so there is no need to type anything.")
    ui.say("If nothing arrives after a minute, send it a first message in your own words. Waiting for a real reply and its memory work...")
    deadline = time.monotonic() + 300
    received = False
    with ui.activity("Waiting for your Telegram reply and memory work..."):
        while time.monotonic() < deadline:
            if first_exchange(install_layout.runtime.state_root / 'receipts', since=started, user_id=user_id):
                received = True
                break
            time.sleep(1)
    if received:
        if hasattr(ui, 'phase'):
            # The waiting instructions no longer apply; the Telegram links stay.
            ui.phase('conversation')
        ui.say("A Telegram reply and its completed memory work have been verified.")
        if ui.choose("Did you receive the reply in Telegram?", ("Not yet", "Yes, I received it"), default=1) == 1:
            ui.say("Setup complete. You can continue talking with your daemon on Telegram.")
            ui.say("To talk with your daemon from anywhere, install the Telegram app on your iPhone or Android phone and sign in to the same account.")
            return True
    ui.say("Services are installed, but the first conversation is not confirmed yet. The private setup is retained.")
    ui.say("Bot: " + link)
    return False


def initialize_private_memory(scope, bundle, user_id, *, root_folder_memory=False, conversation_language=None):
    """Seed only canonical versioned voice, never facts or an existing file.

    The persona and the way of conversing are fixed texts; the daemon's name
    and grammatical gender come from its identity settings at every reply.
    """
    scope.validate_home(scope.home)
    templates = bundle / "memory/templates/users"
    for name in ("persona.md", "conversation.md"):
        if (templates / name).is_symlink() or not (templates / name).is_file():
            raise ValueError("Canonical voice template is missing.")
    if re.fullmatch(r"[1-9][0-9]{0,19}", user_id) is None:
        raise ValueError("Memory owner must be a numeric Telegram user id.")
    root = scope.home / "data/memory"
    from daemons_companion.backup.restore import pending_restore, restore_memory
    from daemons_companion.hosts.change import pending_change
    change = pending_change()
    # A host change stops the previous host here and hands its memory over.
    restored = change.switch(scope) if change is not None else pending_restore()
    if restored is not None:
        if not root_folder_memory:
            raise ValueError("A backup can only be restored into a new root-folder memory.")
        record = scope.attempt / "restored-memory.json"
        expected = json.dumps({"schema": 1, "manifest_sha256": restored.manifest_sha256}) + "\n"
        # A resumed setup keeps the memory it already restored from this backup.
        if not (record.is_file() and not record.is_symlink() and record.read_text(encoding="utf-8") == expected):
            restore_memory(restored, root, user_id)
            atomic_write_text(record, expected)
        # The memory folder now holds these bytes; keep no second plain copy.
        restored.archive.unlink(missing_ok=True)
    fresh_memory = not root.exists() or (root.is_dir() and not any(root.iterdir()))
    if root_folder_memory:
        from daemons_companion.desktop.memory import initialize_empty_vault
        root = initialize_empty_vault(root)
        from daemon_core.installation_profile import initialize_profile
        initialize_profile(scope, user_id)
    files = {root / "users" / user_id / name: (templates / name).read_bytes()
             for name in ("persona.md", "conversation.md")}
    for path, content in files.items():
        if path.resolve() != path or path.is_symlink():
            raise ValueError("Initial voice path must be direct.")
        if path.exists():
            if not path.is_file():
                raise ValueError("Initial voice path is not a file.")
            continue
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as stream:
            stream.write(content)
        path.chmod(0o600)
    if fresh_memory and conversation_language is not None:
        from daemon_core.first_meeting import initialize
        initialize(root, user_id, conversation_language)


def complete_private_setup(owner, ui, request, response):
    with ExitStack() as locks:
        with ui.activity("Preparing private installation..."):
            layout = owner.layout()
            locks.enter_context(memory_file_lock(layout.root / "setup-owner"))
        return _complete_private_setup(owner, ui, request, response)


def resume_private_setup(owner, ui):
    layout = owner.layout()
    if not layout.pointer.exists() and not layout.pointer.is_symlink():
        return False
    with memory_file_lock(layout.root / "setup-owner"):
        return _resume_private_setup(owner, ui)


def _resume_private_setup(owner, ui):
    with ui.activity('Checking previous setup...'):
        layout = owner.layout()
        host = installed_host(layout)
        if host is None:
            return False
        layout = replace(layout, installation_id=Path(host.config_path).parent.name)
        scope = InstallationScope.from_verified_host(layout)
        owner._progress_scope = scope
        removal = scope.attempt / "companion-removal.json"
        if removal.exists() or removal.is_symlink():
            from daemons_companion.removal.removal import document
            if document(removal).get("status") != "rolled_back":
                raise ValueError("This installation was detached or removed. Manage it from Installations or create a separate new installation.")
        marker = scope.attempt / "daemon-setup.json"
        if not marker.exists():
            return False
        if marker.is_symlink() or marker.stat().st_size > 8192:
            raise ValueError("Private setup receipt is invalid.")
        data = json.loads(marker.read_text(encoding="utf-8"))
        if (data.get("schema") != "daemon.private-setup.v1"
                or re.fullmatch(r"[A-Za-z0-9_]{5,32}", data.get("username", "")) is None
                or re.fullmatch(r"[1-9][0-9]{0,19}", data.get("user_id", "")) is None):
            raise ValueError("Private setup receipt is invalid.")
        scope.validate_home(scope.home)
    owner.daemon_installed = True
    ui.say("Your private daemon is already installed. It will not be reinstalled.")
    if ui.choose("Existing private daemon", ("Open my Telegram conversation", "Review setup choices")) == 1:
        return False
    repaired = False
    if layout.target_os == "windows":
        from daemons_companion.runtime.gateway_repair import repair_gateway_launch
        context = replace(owner.context, installation_scope=scope)
        runtime = context.install_layout(HostKind.HERMES, layout.profile).runtime
        scheduler = WindowsTaskSchedulerBackend(job_namespace=scope.installation_id)
        with ui.activity("Checking and repairing private Windows service paths..."):
            repaired = repair_gateway_launch(layout, runtime, scheduler)
        with ui.activity("Waiting for the private Telegram gateway to reconnect..."):
            wait_private_gateway_ready(layout)
        if repaired:
            ui.say("The private Windows service paths are repaired. Your connections and memory are preserved.")
    started = time.time() if repaired else 0
    ui.say("Open https://t.me/" + data["username"] + " and send a message.")
    with ui.activity("Waiting for your Telegram reply and memory work..." if repaired else "Checking the previous Telegram conversation..."):
        deadline = time.monotonic() + (300 if repaired else 0)
        while True:
            received = first_exchange(scope.home / "data/state/receipts", since=started, user_id=data['user_id'])
            if received or time.monotonic() >= deadline:
                break
            time.sleep(1)
    if received:
        ui.say("A Telegram reply and its completed memory work are confirmed.")
    else:
        ui.say("The first conversation is not yet confirmed. The existing installation is preserved.")
    return True
