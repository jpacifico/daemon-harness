"""Load companion provider state without exporting protected credentials."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
import threading
from typing import Mapping

from daemons_companion.setup.credentials import CredentialStore, system_credential_store
from daemons_companion.setup.plan import ProviderKind, ProviderSelection
from daemons_companion.setup.provider import PROVIDER_STATE_SCHEMA


PROVIDER_CONFIG_ENV = "DAEMON_CORE_PROVIDER_CONFIG"
_CREDENTIAL_SCHEMA = "daemon.companion-credential.v1"
_KINDS = {kind.value for kind in ProviderKind}
_REASONING = {"routing": "medium", "conversation": "medium", "memory": "high"}


@dataclass(frozen=True)
class ManagedProviderConfig:
    values: Mapping[str, str]
    credential_name: str = ""
    credential_value: str = field(default="", repr=False)

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def secret(self, name: str) -> str | None:
        if name == self.credential_name:
            return self.credential_value
        return None


class ManagedProviderLoader:
    def __init__(self, *, store_factory=system_credential_store) -> None:
        self._store_factory = store_factory
        self._lock = threading.Lock()
        self._cache_key: tuple[str, int, int, int] | None = None
        self._cached: ManagedProviderConfig | None = None

    def load(self, environ: Mapping[str, str] | None = None) -> ManagedProviderConfig | None:
        env = os.environ if environ is None else environ
        configured = env.get(PROVIDER_CONFIG_ENV, "").strip()
        if not configured:
            return None
        path = Path(configured)
        _validate_config_path(path)
        file_stat = path.stat()
        key = (str(path), file_stat.st_mtime_ns, file_stat.st_size, file_stat.st_ino)
        with self._lock:
            if key == self._cache_key and self._cached is not None:
                return self._cached
            loaded = _load_config(path, self._store_factory)
            self._cache_key = key
            self._cached = loaded
            return loaded

    def clear(self) -> None:
        with self._lock:
            self._cache_key = None
            self._cached = None


_LOADER = ManagedProviderLoader()


def managed_provider_config(
    environ: Mapping[str, str] | None = None,
) -> ManagedProviderConfig | None:
    return _LOADER.load(environ)


def _load_config(path: Path, store_factory) -> ManagedProviderConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise RuntimeError("Managed provider configuration is malformed.") from error
    required = {"schema", "kind", "model", "base_url", "reasoning", "credential"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise RuntimeError("Managed provider configuration has an unknown format.")
    if payload.get("schema") != PROVIDER_STATE_SCHEMA or payload.get("kind") not in _KINDS:
        raise RuntimeError("Managed provider configuration is unsupported.")
    model = payload.get("model")
    base_url = payload.get("base_url")
    if not isinstance(model, str) or not model or len(model) > 160:
        raise RuntimeError("Managed provider model is invalid.")
    if not isinstance(base_url, str) or len(base_url) > 2048:
        raise RuntimeError("Managed provider base URL is invalid.")
    if payload.get("reasoning") != _REASONING:
        raise RuntimeError("Managed provider reasoning defaults are invalid.")

    kind = str(payload["kind"])
    provider = {
        "openai_api": "openai",
        "mistral_api": "mistral",
        "anthropic_api": "anthropic",
        "xai_api": "xai",
        "openai_compatible_api": "local",
        "codex_cli": "codex_cli",
    }[kind]
    if kind in {"openai_api", "mistral_api", "anthropic_api", "xai_api"} and base_url:
        raise RuntimeError("Managed OpenAI configuration must not set a base URL.")
    if kind == "codex_cli" and base_url:
        raise RuntimeError("Managed Codex CLI configuration must not set a base URL.")
    if kind == "openai_compatible_api" and not base_url:
        raise RuntimeError("Managed compatible API configuration requires a base URL.")

    values = {
        "LLM_PROVIDER": provider,
        "ROUTING_LLM_PROVIDER": provider,
        "CONVERSATION_LLM_PROVIDER": provider,
        "MEMORY_LLM_PROVIDER": provider,
        "ROUTING_REASONING_EFFORT": "medium",
        "CONVERSATION_REASONING_EFFORT": "medium",
        "MEMORY_REASONING_EFFORT": "high",
    }
    model_names = {
        "mistral_api": ("MISTRAL_MODEL", "ROUTING_MISTRAL_MODEL", "CONVERSATION_MISTRAL_MODEL", "MEMORY_MISTRAL_MODEL"),
        "anthropic_api": ("ANTHROPIC_MODEL", "ROUTING_ANTHROPIC_MODEL", "CONVERSATION_ANTHROPIC_MODEL", "MEMORY_ANTHROPIC_MODEL"),
        "xai_api": ("XAI_MODEL", "ROUTING_XAI_MODEL", "CONVERSATION_XAI_MODEL", "MEMORY_XAI_MODEL"),
        "openai_api": (
            "OPENAI_MODEL",
            "ROUTING_OPENAI_MODEL",
            "CONVERSATION_OPENAI_MODEL",
            "MEMORY_OPENAI_MODEL",
        ),
        "openai_compatible_api": (
            "LOCAL_LLM_MODEL",
            "ROUTING_LOCAL_LLM_MODEL",
            "CONVERSATION_LOCAL_LLM_MODEL",
            "MEMORY_LOCAL_LLM_MODEL",
        ),
        "codex_cli": (
            "CODEX_CLI_MODEL",
            "ROUTING_CODEX_MODEL",
            "CONVERSATION_CODEX_MODEL",
            "MEMORY_CODEX_MODEL",
        ),
    }[kind]
    values.update({name: model for name in model_names})
    if kind == "openai_compatible_api":
        values["LOCAL_LLM_BASE_URL"] = base_url

    expected_credential = {
        "openai_api": "OPENAI_API_KEY",
        "mistral_api": "MISTRAL_API_KEY",
        "anthropic_api": "ANTHROPIC_API_KEY",
        "xai_api": "XAI_API_KEY",
        "openai_compatible_api": "LOCAL_LLM_API_KEY",
        "codex_cli": None,
    }[kind]
    credential = payload.get("credential")
    try:
        ProviderSelection(
            ProviderKind(kind),
            model,
            base_url=base_url,
            credential_available=credential is not None,
        ).validated(codex_available=True)
    except ValueError as error:
        raise RuntimeError("Managed provider selection is invalid.") from error
    if credential is None:
        if expected_credential in {"OPENAI_API_KEY", "MISTRAL_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY"}:
            raise RuntimeError("Managed API credential reference is missing.")
        return ManagedProviderConfig(values)
    if (
        expected_credential is None
        or not isinstance(credential, dict)
        or set(credential) != {"name", "reference"}
        or credential.get("name") != expected_credential
        or not isinstance(credential.get("reference"), str)
    ):
        raise RuntimeError("Managed provider credential reference is invalid.")
    store: CredentialStore = store_factory()
    protected = store.get(str(credential["reference"]))
    secret = _unwrap_credential(protected)
    if not secret:
        raise RuntimeError("Managed provider credential is unavailable.")
    return ManagedProviderConfig(values, expected_credential, secret)


def _unwrap_credential(protected: str | None) -> str:
    if protected is None:
        return ""
    try:
        payload = json.loads(protected)
    except (json.JSONDecodeError, TypeError):
        return ""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "owner_token", "value"}
        or payload.get("schema") != _CREDENTIAL_SCHEMA
        or not isinstance(payload.get("owner_token"), str)
        or not payload["owner_token"]
        or not isinstance(payload.get("value"), str)
    ):
        return ""
    return payload["value"]


def _validate_config_path(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeError("Managed provider config must be an absolute regular file.")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("Managed provider config must be private.")


__all__ = [
    "ManagedProviderConfig",
    "ManagedProviderLoader",
    "PROVIDER_CONFIG_ENV",
    "managed_provider_config",
]
