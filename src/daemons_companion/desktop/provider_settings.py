"""Show and change the API key, model and provider of a daemon created by the app.

One JSON request in, one JSON line out. A new key goes only to the provider's
fixed endpoint and to the operating-system credential store; it is never
written to a file or returned. Only installations owned by the app are
handled: a host profile the user installed themself is refused.

A change keeps the previous files and credentials until the desktop app has
restarted the daemon's gateway and confirmed it (``commit``). Any failure, or
a change left unfinished, is reverted to the exact previous state (``revert``).

A change of provider (``switch``) also rewrites the connection the daemon's own
processes read, and is run by the guided session in hosts/provider_change.py
with the daemon's services stopped; the services captured before are recorded,
so a revert from anywhere restarts them exactly.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shutil
import stat
import sys
import uuid

from daemon_core.api_providers import (
    API_PROVIDERS,
    ApiConnectionError,
    hermes_model_config,
    hermes_provider_entries,
    validate_key,
)
from daemons_companion.setup.provider_api import model_catalog, verify_model
from daemon_core.memory.integrity import memory_file_try_lock
from daemons_companion.hosts.creation import native_config
from daemons_companion.openclaw.host_install import checked_file, direct
from daemons_companion.runtime.installation_scope import InstallationScope
from daemons_companion.setup.credentials import CredentialStore, system_credential_store
from daemons_companion.setup.plan import ProviderKind, ProviderSelection
from daemons_companion.setup.provider import (
    _atomic_write,
    _credential_name,
    _credential_payload,
    _json_bytes,
    _parse_credential_payload,
    _provider_core,
    _read_existing_config,
)

SCHEMA = "daemon.provider-change.v1"
CHANGE_DIR = "provider-change"
LIMIT = 2 * 1024 * 1024
# Saved before a change, restored by a revert. None of them holds a secret.
SAVED = ("provider.json", "host-config", "connection.json", "openclaw-connection.json")
SUBSCRIPTION = "codex_cli"


class ProviderSettingsError(ValueError):
    """A bounded code (or a fixed provider message) safe to show the user."""


class OwnedDaemon:
    def __init__(self, profile: Path, store: CredentialStore | None = None):
        profile = direct(profile)
        self.scope = InstallationScope(profile.parent)
        self.scope.validate_profile(profile)
        if self.scope.host_kind not in {"openclaw", "hermes"}:
            raise ProviderSettingsError("provider.unsupported")
        self.store = store or system_credential_store()
        self.openclaw = self.scope.host_kind == "openclaw"
        self.provider_file = self.scope.home / "data/state/config/provider.json"
        self.host_config = self.scope.profile / ("openclaw.json" if self.openclaw else "config.yaml")
        self.connection = self.scope.attempt / "openclaw-connection.json"
        self.private_connection = self.scope.attempt / "connection.json"
        self.change_dir = self.scope.attempt / CHANGE_DIR

    # Reading ------------------------------------------------------------

    def provider(self) -> dict:
        config = _read_existing_config(self.provider_file)
        if config is None:
            raise ProviderSettingsError("provider.unsupported")
        return config

    def paths(self) -> dict[str, Path]:
        paths = {"provider.json": self.provider_file, "host-config": self.host_config,
                 "connection.json": self.private_connection}
        if self.openclaw:
            paths["openclaw-connection.json"] = self.connection
        return paths

    def revision(self) -> str:
        digest = hashlib.sha256()
        for name, path in self.paths().items():
            digest.update(name.encode() + b"\0" + checked_file(path, LIMIT) + b"\0")
        return digest.hexdigest()

    def connection_record(self) -> dict:
        record = json.loads(checked_file(self.connection, 8192))
        if (
            not isinstance(record, dict)
            or set(record) != {"schema", "credential", "kind"}
            or record.get("schema") != "daemon.openclaw-connection.v1"
        ):
            raise ProviderSettingsError("provider.unsupported")
        return record

    def connection_secret(self, reference: str) -> dict:
        secret = json.loads(self.store.get(reference) or "{}")
        if not isinstance(secret, dict) or secret.get("installation_id") != self.scope.installation_id:
            raise ProviderSettingsError("provider.unsupported")
        return secret

    def current_key(self, config: dict) -> str:
        credential = config.get("credential")
        if not isinstance(credential, dict) or not isinstance(credential.get("reference"), str):
            return ""
        parsed = _parse_credential_payload(self.store.get(credential["reference"]))
        return parsed["value"] if parsed else ""

    def host_matches(self, kind: str, model: str) -> bool:
        """The host uses exactly the provider and model the daemon records."""
        if self.openclaw:
            config = json.loads(checked_file(self.host_config, LIMIT))
            try:
                expected = native_config(kind, model, "0", 1)
            except (KeyError, ValueError):
                return False  # A provider or model the app does not configure.
            name = next(iter(expected["models"]["providers"]))
            return (
                self.connection_record()["kind"] == kind
                and config.get("agents", {}).get("defaults", {}).get("model")
                == expected["agents"]["defaults"]["model"]
                and config.get("models", {}).get("providers", {}).get(name)
                == expected["models"]["providers"][name]
            )
        import yaml

        config = yaml.safe_load(checked_file(self.host_config, LIMIT).decode("utf-8"))
        if kind == SUBSCRIPTION:
            return isinstance(config, dict) and config.get("model") == _subscription_model(model)
        return (
            isinstance(config, dict)
            and config.get("model") == hermes_model_config(kind, model)
            and all(
                config.get("providers", {}).get(name) == entry
                for name, entry in hermes_provider_entries(kind, model).items()
            )
        )

    def describe(self) -> dict:
        config = self.provider()
        kind, model = config["kind"], config["model"]
        editable = kind in API_PROVIDERS and self.host_matches(kind, model)
        from daemons_companion.setup import model_access

        return {
            "kind": kind,
            "model": model,
            "editable": editable,
            # Another provider can be chosen when the host uses exactly what the
            # daemon records; only the providers offered to new daemons.
            "switchable": kind in {*API_PROVIDERS, SUBSCRIPTION} and self.host_matches(kind, model),
            # A subscription daemon changes its model through the same guided
            # session (hosts/provider_change.py), not the API key form.
            "model_changeable": kind == SUBSCRIPTION and self.host_matches(kind, model),
            "offers": list(model_access.options()[0]),
            "key": bool(self.current_key(config)) if kind in API_PROVIDERS else None,
            "pending": self.pending() is not None,
            "revision": self.revision(),
        }

    def check(self, key: str | None) -> dict:
        config = self.provider()
        kind = config["kind"]
        if kind not in API_PROVIDERS:
            raise ProviderSettingsError("provider.unsupported")
        value = validate_key(key) if key is not None else self.current_key(config)
        if not value:
            raise ProviderSettingsError("provider.key-missing")
        models = model_catalog(kind, value)
        return {"models": list(models), "available": config["model"] in models}

    # Changing -----------------------------------------------------------

    def pending(self) -> dict | None:
        record = self.change_dir / "record.json"
        if not self.change_dir.exists():
            return None
        if not record.exists():
            # The folder was created but nothing else happened yet.
            return {"schema": SCHEMA, "status": "empty", "new_references": [], "old_references": []}
        data = json.loads(checked_file(record, 65536))
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ProviderSettingsError("provider.recovery")
        return data

    def change(self, revision: str, model: str, key: str | None) -> dict:
        if self.pending() is not None:
            raise ProviderSettingsError("provider.pending")
        if revision != self.revision():
            raise ProviderSettingsError("provider.changed")
        config = self.provider()
        kind, before_model = config["kind"], config["model"]
        if kind not in API_PROVIDERS or not self.host_matches(kind, before_model):
            raise ProviderSettingsError("provider.unsupported")
        current = self.current_key(config)
        new_key = validate_key(key) if key is not None else None
        if new_key == current:
            new_key = None
        value = new_key or current
        if not value:
            raise ProviderSettingsError("provider.key-missing")
        if not isinstance(model, str) or model not in model_catalog(kind, value):
            raise ProviderSettingsError("provider.model-unavailable")
        if model == before_model and new_key is None:
            return {"status": "unchanged"}
        # The same short test as at creation, with the key and model to be saved.
        verify_model(kind, value, model)

        self.change_dir.mkdir(mode=0o700)
        for name, path in self.paths().items():
            _atomic_write(self.change_dir / name, checked_file(path, LIMIT), 0o600)
        modes = {name: stat.S_IMODE(path.stat().st_mode) for name, path in self.paths().items()}
        provider_ref = connection_ref = None
        old_refs: list[str] = []
        if new_key is not None:
            provider_ref = "provider-" + str(uuid.uuid4())
            old_refs.append(config["credential"]["reference"])
            if self.openclaw:
                connection_ref = "provider-" + str(uuid.uuid4())
                old_refs.append(self.connection_record()["credential"])
        record = {
            "schema": SCHEMA,
            "status": "prepared",
            "modes": modes,
            "new_references": [r for r in (provider_ref, connection_ref) if r],
            "old_references": old_refs,
        }
        # Written before any credential, so a revert always knows what to remove.
        _atomic_write(self.change_dir / "record.json", _json_bytes(record), 0o600)
        try:
            if provider_ref:
                self.store.put(provider_ref, _credential_payload(str(uuid.uuid4()), new_key))
            if connection_ref:
                connection = self.connection_record()
                secret = self.connection_secret(connection["credential"])
                self.store.put(connection_ref, json.dumps({**secret, "key": new_key}))
                _atomic_write(self.connection, _json_bytes_plain({**connection, "credential": connection_ref}),
                              modes["openclaw-connection.json"])
            if model != before_model:
                _atomic_write(self.host_config, self.host_config_with(kind, model), modes["host-config"])
            updated = {**config, "model": model}
            if provider_ref:
                updated["credential"] = {**config["credential"], "reference": provider_ref}
            # Last: the daemon's own processes reload this file on their next call.
            _atomic_write(self.provider_file, _json_bytes(updated), modes["provider.json"])
            if not self.host_matches(kind, model):
                raise RuntimeError("The host configuration was not updated exactly.")
        except BaseException:
            self.revert()
            raise
        record["status"] = "applied"
        _atomic_write(self.change_dir / "record.json", _json_bytes(record), 0o600)
        return {"status": "applied"}

    def switch(self, revision: str, kind: str, model: str, key: str | None,
               codex: Path | None, services: dict[str, bytes | None]) -> dict:
        """Move the daemon to another provider; the caller has stopped its services.

        ``key`` is a verified API key, or None for the ChatGPT subscription,
        whose native sign-ins and private Codex (``codex``) are already done.
        A subscription daemon may also stay on the subscription: its model and
        private Codex change, with the same snapshot, reply check and revert.
        ``services`` are the definitions captured before they were stopped.
        """
        from daemons_companion.setup import model_access
        from daemons_companion.setup.model_selection import SUPPORTED_MODELS

        if self.pending() is not None:
            raise ProviderSettingsError("provider.pending")
        if revision != self.revision():
            raise ProviderSettingsError("provider.changed")
        config = self.provider()
        before = config["kind"]
        if not self.host_matches(before, config["model"]):
            raise ProviderSettingsError("provider.unsupported")
        if kind not in model_access.options()[0] or (kind == before and kind != SUBSCRIPTION):
            raise ProviderSettingsError("provider.unsupported")
        if kind == SUBSCRIPTION:
            if key is not None or model not in {m.identifier for m in SUPPORTED_MODELS}:
                raise ProviderSettingsError("provider.model-unavailable")
            codex = direct(Path(codex)) if codex is not None else None
            if codex is None or not codex.is_file() or not codex.is_relative_to(direct(self.scope.attempt / "codex")):
                raise ProviderSettingsError("provider.unsupported")
        else:
            key = validate_key(key)
            if model not in model_catalog(kind, key):
                raise ProviderSettingsError("provider.model-unavailable")
            # The same short test as at creation, with the key and model to be saved.
            verify_model(kind, key, model)
        selection = ProviderSelection(ProviderKind(kind), model, credential_available=kind != SUBSCRIPTION)
        user = json.loads(checked_file(self.private_connection, 8192)).get("user_id")
        if not isinstance(user, str) or not user.isascii() or not user.isdecimal():
            raise ProviderSettingsError("provider.unsupported")

        self.change_dir.mkdir(mode=0o700)
        for name, path in self.paths().items():
            _atomic_write(self.change_dir / name, checked_file(path, LIMIT), 0o600)
        modes = {name: stat.S_IMODE(path.stat().st_mode) for name, path in self.paths().items()}
        provider_ref = "provider-" + str(uuid.uuid4()) if kind != SUBSCRIPTION else None
        connection_ref = "provider-" + str(uuid.uuid4()) if self.openclaw else None
        old_refs = [config["credential"]["reference"]] if isinstance(config.get("credential"), dict) else []
        if self.openclaw:
            old_refs.append(self.connection_record()["credential"])
        record = {
            "schema": SCHEMA,
            "status": "prepared",
            "modes": modes,
            "new_references": [r for r in (provider_ref, connection_ref) if r],
            "old_references": old_refs,
            # A list: the order (gateway first) survives the sorted record.
            "services": [[code, base64.b64encode(value).decode() if value else None]
                         for code, value in services.items()],
        }
        # Written before any credential, so a revert always knows what to remove.
        _atomic_write(self.change_dir / "record.json", _json_bytes(record), 0o600)
        try:
            if provider_ref:
                self.store.put(provider_ref, _credential_payload(str(uuid.uuid4()), key))
            if connection_ref:
                connection = self.connection_record()
                secret = self.connection_secret(connection["credential"])
                self.store.put(connection_ref, json.dumps({**secret, "key": key or ""}))
                _atomic_write(self.connection, _json_bytes_plain(
                    {"schema": connection["schema"], "credential": connection_ref, "kind": kind}),
                    modes["openclaw-connection.json"])
            private = ({"schema": "daemon.private-connection.v1", "codex": str(codex),
                        "codex_sha256": hashlib.sha256(codex.read_bytes()).hexdigest(), "user_id": user}
                       if kind == SUBSCRIPTION else
                       {"schema": "daemon.private-connection.v2", "provider_kind": kind, "user_id": user})
            # The same serialization as the app's first write of this file.
            _atomic_write(self.private_connection, (json.dumps(private) + "\n").encode("utf-8"),
                          modes["connection.json"])
            _atomic_write(self.host_config, self.host_config_with(kind, model, provider=True), modes["host-config"])
            name = _credential_name(selection)
            updated = {**_provider_core(selection),
                       "credential": {"name": name, "reference": provider_ref} if name else None}
            _atomic_write(self.provider_file, _json_bytes(updated), modes["provider.json"])
            if not self.host_matches(kind, model):
                raise RuntimeError("The host configuration was not updated exactly.")
        except BaseException:
            # The caller still holds the captured services and restarts them.
            self._restore(self.pending())
            raise
        record["status"] = "applied"
        _atomic_write(self.change_dir / "record.json", _json_bytes(record), 0o600)
        return {"status": "applied"}

    def host_config_with(self, kind: str, model: str, *, provider: bool = False) -> bytes:
        if self.openclaw:
            config = json.loads(checked_file(self.host_config, LIMIT))
            expected = native_config(kind, model, "0", 1)
            name = next(iter(expected["models"]["providers"]))
            if provider:
                # Another provider: its own entry and web tools, as at creation.
                config["models"]["providers"] = expected["models"]["providers"]
                tools = config.setdefault("tools", {})
                if "web" in expected.get("tools", {}):
                    tools["web"] = expected["tools"]["web"]
                else:
                    tools.pop("web", None)
            config["models"]["providers"][name] = expected["models"]["providers"][name]
            config["agents"]["defaults"]["model"] = expected["agents"]["defaults"]["model"]
            # Same serialization as the app's own first write of this file.
            return (json.dumps(config, sort_keys=True) + "\n").encode("utf-8")
        import yaml

        config = yaml.safe_load(checked_file(self.host_config, LIMIT).decode("utf-8"))
        if provider:
            # The custom entries the app writes for another provider go with it.
            managed = {name for other in API_PROVIDERS
                       for name in hermes_provider_entries(other, "managed")}
            providers = {k: v for k, v in (config.get("providers") or {}).items() if k not in managed}
            if providers or "providers" in config:
                config["providers"] = providers
        if kind == SUBSCRIPTION:
            config["model"] = _subscription_model(model)
            return yaml.safe_dump(config, sort_keys=False).encode("utf-8")
        config["model"] = hermes_model_config(kind, model)
        entries = hermes_provider_entries(kind, model)
        if entries:
            config.setdefault("providers", {}).update(entries)
        return yaml.safe_dump(config, sort_keys=False).encode("utf-8")

    def commit(self) -> dict:
        record = self.pending()
        if record is None or record.get("status") != "applied":
            raise ProviderSettingsError("provider.recovery")
        for reference in record["old_references"]:
            self.store.delete(reference)
        shutil.rmtree(self.change_dir)
        return {"status": "committed"}

    def revert(self, scheduler=None) -> dict:
        record = self.pending()
        if record is None:
            return {"status": "unchanged"}
        services = dict(record.get("services") or [])
        if services:
            # A provider switch: stop what runs on the new settings (the gateway
            # first), and restart the captured services on the previous ones.
            if scheduler is None:
                raise ProviderSettingsError("provider.recovery")
            for code in services:
                scheduler.restore(code, None)
        self._restore(record)
        for code, capture in reversed(list(services.items())):
            scheduler.restore(code, base64.b64decode(capture) if capture else None)
        return {"status": "reverted"}

    def _restore(self, record: dict) -> None:
        """Previous files and credentials back exactly; the change folder goes."""
        modes = record.get("modes", {})
        for name, path in self.paths().items():
            saved = self.change_dir / name
            if saved.exists():
                _atomic_write(path, checked_file(saved, LIMIT), int(modes.get(name, 0o600)))
        for reference in record["new_references"]:
            if self.store.get(reference) is not None:
                self.store.delete(reference)
        shutil.rmtree(self.change_dir)


def _subscription_model(model: str) -> dict:
    # The Hermes model entry first install writes for a ChatGPT subscription.
    return {"provider": "openai-codex", "default": model}


def service_scheduler(profile: Path, base: Path):
    """The scheduler of an owned installation's services, as its removal sees them."""
    from daemons_companion.hosts.change import launch_agents
    from daemons_companion.removal.removal import Removal

    return Removal(profile, base, launch_agents())


def _json_bytes_plain(value: dict) -> bytes:
    # The OpenClaw connection record is written as plain JSON by first install.
    return (json.dumps(value) + "\n").encode("utf-8")


def handle_request(request: object, *, store: CredentialStore | None = None) -> dict:
    if not isinstance(request, dict) or not isinstance(request.get("profile"), str):
        raise ProviderSettingsError("provider.invalid-request")
    action = request.get("action")
    fields = {
        "describe": {"action", "profile"},
        "check": {"action", "profile", "key"},
        "change": {"action", "profile", "revision", "model", "key"},
        "commit": {"action", "profile"},
        "revert": {"action", "profile"},
    }.get(action)
    if action == "revert" and "base" in request:
        fields = {"action", "profile", "base"}
    if fields is None or set(request) != fields:
        raise ProviderSettingsError("provider.invalid-request")
    key = request.get("key")
    if key is not None and not isinstance(key, str):
        raise ProviderSettingsError("provider.invalid-request")
    daemon = OwnedDaemon(Path(request["profile"]), store)
    if action == "describe":
        return daemon.describe()
    if action == "check":
        return daemon.check(key)
    with memory_file_try_lock(daemon.scope.home / "install-owner") as acquired:
        if not acquired:
            raise ProviderSettingsError("provider.busy")
        if action == "change":
            return daemon.change(request["revision"], request["model"], key)
        if action == "commit":
            return daemon.commit()
        record = daemon.pending()
        scheduler = None
        if record and record.get("services"):
            if not isinstance(request.get("base"), str):
                raise ProviderSettingsError("provider.recovery")
            scheduler = service_scheduler(daemon.scope.profile, Path(request["base"])).scheduler
        return daemon.revert(scheduler)


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise ProviderSettingsError("provider.invalid-request")
        result = {"ok": True, "result": handle_request(json.loads(raw))}
    except ProviderSettingsError as error:
        result = {"ok": False, "code": str(error)}
    except ApiConnectionError as error:
        # Fixed, translated provider messages; never the key or a response body.
        result = {"ok": False, "code": str(error)}
    except Exception as error:  # noqa: BLE001 - only the class name leaves.
        result = {"ok": False, "code": "provider.unavailable", "diagnostic": {"type": type(error).__name__}}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
