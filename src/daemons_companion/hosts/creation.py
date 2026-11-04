"""Resumable private OpenClaw provisioning, before any daemon association.

Public state contains only technical progress and model/bot labels. Credentials
belong to the new host, in the OS store; the shared daemon runtime is read-only.
"""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import uuid
from urllib.request import Request, build_opener

from daemon_core import api_providers as api
from daemons_companion.runtime.bootstrap import _source_io_path
from daemons_companion.setup.credentials import system_credential_store
from daemons_companion.openclaw.host_install import (
    OpenClawHostLayout,
    checked_file,
    direct,
    install,
    plan,
    verify_installed,
)
from daemon_core.memory.integrity import atomic_write_text, memory_file_try_lock

SCHEMA = "daemon.host-creation.v1"
IMMUTABLE = (
    "schema",
    "installation_id",
    "profile",
    "source",
    "binding",
    "acquisition",
    "credential",
    "gateway_port",
)
TOKEN = r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def record_path(host):
    return direct(
        host.companion_home / "host-setups" / (host.installation_id + ".json")
    )


def read_record(host):
    data = json.loads(checked_file(record_path(host), 65536))
    if (
        data.get("schema") != SCHEMA
        or data.get("installation_id") != host.installation_id
        or data.get("profile") != str(host.profile)
        or data.get("acquisition") != plan(host)["revision"]
        or not re.fullmatch(r"provider-[a-f0-9-]{36}", data.get("credential", ""))
    ):
        raise ValueError("Host setup ownership changed.")
    original = {key: data[key] for key in IMMUTABLE} | {"phase": "download"}
    if data.get("revision") != fingerprint(original) or data.get("phase") not in {
        "download",
        "key",
        "model",
        "telegram",
        "configuring",
        "ready",
    }:
        raise ValueError("Host setup metadata changed.")
    return data


def save(host, data):
    path = record_path(host)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    direct(path.parent)
    atomic_write_text(path, json.dumps(data, sort_keys=True) + "\n")


def public(data):
    return {
        k: data[k]
        for k in ("installation_id", "phase", "kind", "models", "model", "bot", "error", "catalog")
        if k in data
    } | {"revision": data["revision"]}


def credentials(host, data, store):
    value = store.get(data["credential"])
    if value is None:
        return {}
    secret = json.loads(value)
    if secret.get("installation_id") != host.installation_id:
        raise ValueError("Host credential ownership changed.")
    return secret


def credential_environment(host, *, store=None):
    if not record_path(host).exists():
        return {}
    data = read_record(host)
    if data["phase"] != "ready":
        raise ValueError("Host credentials are not ready.")
    verify_installed(host)
    secret = credentials(host, data, store or system_credential_store())
    provider = {} if data["kind"] == "codex_cli" else {
        api.API_PROVIDERS[data["kind"]].secret_name: api.validate_key(secret["key"])}
    return {
        **provider,
        "TELEGRAM_BOT_TOKEN": secret["token"],
        "DAEMONS_GATEWAY_TOKEN": secret["gateway"],
    }


def credential_environment_for_profile(profile):
    if (profile.parent / 'openclaw-connection.json').exists():
        from daemons_companion.openclaw.gateway import host_environment
        from daemons_companion.runtime.installation_scope import InstallationScope
        from daemon_core.api_providers import API_PROVIDERS
        allowed = {spec.secret_name for spec in API_PROVIDERS.values()} | {'TELEGRAM_BOT_TOKEN', 'DAEMONS_GATEWAY_TOKEN'}
        return {k: v for k, v in host_environment(InstallationScope(profile.parent)).items() if k in allowed}
    # Legacy prepared destinations have no creation record and retain native auth.
    home = profile.parents[4]
    record = home / "host-setups" / (profile.parent.name + ".json")
    if not record.exists():
        return {}
    from daemons_companion.hosts.attachment_command import native_target

    # The owned host retains its exact pinned npm lock after acquisition.
    host = OpenClawHostLayout(
        home,
        profile.parent.name,
        native_target(),
        profile.parent / "host",
    )
    return credential_environment(host)


def verify_bot(token):
    if not re.fullmatch(TOKEN, token):
        raise ValueError("Invalid bot token.")
    opener = build_opener(api._NoRedirects())

    def request(method):
        try:
            with opener.open(
                Request("https://api.telegram.org/bot" + token + "/" + method),
                timeout=20,
            ) as response:
                value = json.loads(response.read(65537))
            if value.get("ok") is not True or not isinstance(value.get("result"), dict):
                raise ValueError()
            return value["result"]
        except Exception:
            raise ValueError("Telegram verification failed.") from None

    bot = request("getMe")
    if (
        str(bot.get("id")) != token.split(":", 1)[0]
        or bot.get("is_bot") is not True
        or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot.get("username", ""))
    ):
        raise ValueError("Invalid Telegram bot identity.")
    if request("getWebhookInfo").get("url"):
        raise ValueError("This bot already has a webhook.")
    return bot["username"]


def native_config(kind, model, user, port):
    if kind == "codex_cli":
        from daemons_companion.setup.model_selection import SUPPORTED_MODELS
        if model not in {item.identifier for item in SUPPORTED_MODELS}:
            raise ValueError("Unsupported subscription model.")
        config = native_config("openai_api", model, user, port)
        # Keep the canonical native OpenClaw loop and its daemon hooks.
        config["models"] = {"providers": {"openai": {
            "baseUrl": "https://chatgpt.com/backend-api/codex", "api": "openai-chatgpt-responses",
            "auth": "oauth", "agentRuntime": {"id": "openclaw"}, "models": []}}}
        config["agents"]["defaults"]["model"]["primary"] = "openai/" + model
        # No managed search provider is configured without a key; the
        # subscription's hosted search is opt-in and cached unless live.
        config["tools"]["web"] = {
            "search": {"enabled": True, "openaiCodex": {"enabled": True, "mode": "live"}}
        }
        return config
    spec = api.API_PROVIDERS[kind]
    provider = "daemons-" + kind.removesuffix("_api")
    model_info = {
        "id": model,
        "name": model,
        "input": ["text"],
        "contextWindow": 128000,
        "maxTokens": 8192,
    }
    if kind in ("mistral_api", "xai_api"):
        model_info["compat"] = {"supportsDeveloperRole": False}
    protocol = {
        "mistral_api": "openai-completions",
        "openai_api": "openai-responses",
        "anthropic_api": "anthropic-messages",
        "xai_api": "openai-completions",
    }[kind]
    config = {
        **_reference_conversation(),
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "port": port,
            "auth": {"mode": "token", "token": "${DAEMONS_GATEWAY_TOKEN}"},
        },
        "models": {
            "providers": {
                provider: {
                    "baseUrl": spec.base_url,
                    "api": protocol,
                    "apiKey": "${" + spec.secret_name + "}",
                    "models": [model_info],
                }
            }
        },
        "agents": {
            "defaults": {
                "model": {"primary": provider + "/" + model},
                "heartbeat": {"every": "0m"},
            }
        },
        "channels": {
            "telegram": {
                "enabled": True,
                "botToken": "${TELEGRAM_BOT_TOKEN}",
                "dmPolicy": "allowlist",
                "allowFrom": [user],
                "groupPolicy": "disabled",
                "streaming": {
                    "mode": "progress",
                    "preview": {"toolProgress": True, "commandText": "status"},
                    "progress": {"commentary": True, "label": False},
                },
            }
        },
    }
    _require_command_approval(config, user)
    return config


def _require_command_approval(config, user):
    """Host commands wait for the owner's approval in their Telegram DM.

    OpenClaw otherwise runs host commands without asking. Its approvers are
    not taken from ``allowFrom``, so the owner is named explicitly. Without an
    answer, OpenClaw's default fallback refuses the command.
    """
    config["tools"]["exec"] = {"mode": "ask"}
    config["channels"]["telegram"]["execApprovals"] = {
        "enabled": True,
        "approvers": [user],
        "target": "dm",
    }


def _reference_conversation():
    """Conversation settings of the reference daemon, explicit on new hosts.

    Telegram shows the voice's brief sentence and tool progress before the
    final reply; no acknowledgement reaction or unsolicited heartbeat turn.
    """
    return {
        "messages": {
            "visibleReplies": "automatic",
            "ackReactionScope": "off",
            "statusReactions": {"enabled": False},
        },
        "session": {"dmScope": "per-channel-peer"},
        "tools": {"profile": "coding"},
    }


class Creation:
    def __init__(
        self,
        host,
        source_profile,
        binding,
        *,
        store=None,
        installer=install,
        catalog=api.model_catalog,
        model_check=api.verify_model,
        bot_check=verify_bot,
        events=None,
        progress=None,
    ):
        # The guided app shows ChatGPT sign-in events and download progress;
        # the JSON transport relays sign-in events on stderr instead.
        self.events, self.progress = events, progress
        self.host, self.source, self.binding = host, source_profile, binding
        self.store = store or system_credential_store()
        self.installer, self.catalog, self.model_check, self.bot_check = (
            installer,
            catalog,
            model_check,
            bot_check,
        )

    def start(self, port):
        if record_path(self.host).exists() or self.host.attempt.exists():
            raise ValueError("Use the existing setup record.")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Invalid private gateway port.")
        data = {
            "schema": SCHEMA,
            "installation_id": self.host.installation_id,
            "profile": str(self.host.profile),
            "source": str(self.source),
            "binding": fingerprint(self.binding.descriptor()),
            "acquisition": plan(self.host)["revision"],
            "credential": "provider-" + str(uuid.uuid4()),
            "gateway_port": port,
            "phase": "download",
        }
        data["revision"] = fingerprint(data)
        save(self.host, data)
        return public(data)

    def current(self):
        data = read_record(self.host)
        if data["source"] != str(self.source) or data["binding"] != fingerprint(
            self.binding.descriptor()
        ):
            raise ValueError("The source daemon changed; review the setup.")
        # A crash between the config write and phase update is completed exactly.
        config = self.host.profile / "openclaw.json"
        if data["phase"] == "configuring":
            if (
                config.exists()
                and hashlib.sha256(checked_file(config, 65536)).hexdigest()
                == data["config_sha256"]
            ):
                data["phase"] = "ready"
                save(self.host, data)
            else:
                data["phase"] = "telegram"
                save(self.host, data)
        return data

    def perform(self, operation, revision, values):
        with memory_file_try_lock(
            record_path(self.host).with_suffix(".lock")
        ) as acquired:
            if not acquired:
                raise RuntimeError("Host setup is already running.")
            data = self.current()
            if revision != data["revision"]:
                raise ValueError("Host setup changed.")
            try:
                self._step(data, operation, values)
                data.pop("error", None)
                save(self.host, data)
                return public(data)
            except Exception:
                # Never persist raw exception/provider text, arguments or keys.
                data["error"] = operation
                save(self.host, data)
                raise

    def _step(self, data, operation, values):
        if operation == "download" and data["phase"] == "download":
            if self.progress is None:
                self.installer(self.host, data["acquisition"], resume=True)
            else:
                self.installer(
                    self.host, data["acquisition"], resume=True, progress=self.progress
                )
            data["phase"] = "key"
            return
        verify_installed(self.host)
        if operation == "key" and data["phase"] in {"key", "model"}:
            kind = values.get("kind")
            if kind == "codex_cli":
                from daemons_companion.openclaw.subscription import connect
                import sys
                def event(value):
                    if "catalog" in value:
                        data["catalog"] = value["catalog"]
                    if self.events is not None:
                        self.events(value)
                    else:
                        print("DAEMONS_CHATGPT " + json.dumps(value), file=sys.stderr, flush=True)
                models = connect(self.host, event)
                self.store.put(data["credential"], json.dumps({"installation_id": self.host.installation_id}))
                data.update(kind=kind, models=list(models), phase="model")
                return
            if kind not in api.API_PROVIDERS:
                raise ValueError("Unsupported provider.")
            data.pop("catalog", None)
            key = api.validate_key(values.get("key", ""))
            models = self.catalog(kind, key)
            self.store.put(
                data["credential"],
                json.dumps({"installation_id": self.host.installation_id, "key": key}),
            )
            data.update(kind=kind, models=list(models), phase="model")
            return
        secret = credentials(self.host, data, self.store)
        if operation == "model" and data["phase"] == "model":
            model = values.get("model")
            if model not in data["models"]:
                raise ValueError("Choose a listed model.")
            if data["kind"] == "codex_cli":
                from daemons_companion.openclaw.subscription import connect
                if model not in connect(self.host, catalog_only=True):
                    raise ValueError("The model is no longer available to this account.")
            else:
                self.model_check(data["kind"], secret["key"], model)
            data.update(model=model, phase="telegram")
            return
        if operation == "telegram" and data["phase"] == "telegram":
            from dotenv import dotenv_values

            token = values.get("token", "").strip()
            original = dotenv_values(self.source / ".env").get("TELEGRAM_BOT_TOKEN")
            if (
                not isinstance(original, str)
                or not re.fullmatch(TOKEN, original)
                or token.split(":", 1)[0] == original.split(":", 1)[0]
            ):
                raise ValueError(
                    "Use a separate bot; same-bot transfer is not enabled."
                )
            bot = self.bot_check(token)
            config = self.host.profile / "openclaw.json"
            raw = (
                json.dumps(
                    native_config(
                        data["kind"],
                        data["model"],
                        self.binding.user_id,
                        data["gateway_port"],
                    ),
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            if config.exists() and checked_file(config, 65536) != raw:
                raise ValueError("Target configuration changed; do not overwrite it.")
            secret.update(
                token=token, gateway=secret.get("gateway") or secrets.token_urlsafe(32)
            )
            self.store.put(data["credential"], json.dumps(secret))
            data.update(
                phase="configuring",
                config_sha256=hashlib.sha256(raw).hexdigest(),
                bot=bot,
            )
            save(self.host, data)
            atomic_write_text(config, raw.decode())
            data["phase"] = "ready"
            return
        raise ValueError("This operation is unavailable at the current step.")


def dispatch(request):
    from daemons_companion.hosts.attachment_command import (
        Refused,
        source_binding,
        native_target,
    )

    target_os = native_target()
    if set(request) - {
        "operation",
        "source_profile",
        "base",
        "installation_id",
        "port",
        "revision",
        "values",
    }:
        raise Refused("request")
    base, source = (
        direct(Path(request["base"])),
        direct(Path(request["source_profile"])),
    )
    if not source.is_relative_to(base):
        raise Refused("ownership")
    scope, runtime, binding = source_binding(source)
    if not _source_io_path(binding.source / "src/daemons_companion/hosts/creation.py").is_file():
        raise Refused("upgrade")
    home = base / "Companion/private"
    host = OpenClawHostLayout.existing(
        home,
        request["installation_id"],
        target_os,
        Path(os.environ["DAEMON_HARNESS_COMPANION_BUNDLE_ROOT"])
        / "apps/companion/openclaw-runtime",
    )
    host.validate()
    creation = Creation(host, source, binding)
    operation = request["operation"].removeprefix("create-")
    try:
        if operation == "start":
            return creation.start(request["port"])
        if operation == "status":
            with memory_file_try_lock(
                record_path(host).with_suffix(".lock")
            ) as acquired:
                if not acquired:
                    raise RuntimeError("Host setup is already running.")
                return public(creation.current())
        return creation.perform(
            operation, request["revision"], request.get("values", {})
        )
    except (api.ApiConnectionError, ValueError, RuntimeError, OSError):
        raise Refused(
            "creation-" + operation
            if operation in {"key", "model", "telegram"}
            else "creation"
        ) from None
