"""Explicit credential environment for companion-owned processes only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from daemons_companion.runtime.bootstrap import clean_environment, private_home_environment
from daemons_companion.runtime.installation_scope import InstallationScope


def private_process_environment(scope: InstallationScope, *, hermes=False, credentials=True):
    scope.validate_home(scope.home)
    config = scope.attempt / "connection.json"
    if config.is_symlink() or config.resolve() != config or config.stat().st_size > 8192:
        raise ValueError("Private connection settings are invalid.")
    data = json.loads(config.read_text(encoding="utf-8"))
    is_api = data.get('schema') == 'daemon.private-connection.v2'
    if is_api:
        from daemon_core.api_providers import API_PROVIDERS
        if set(data) != {'schema', 'provider_kind', 'user_id'} or data['provider_kind'] not in API_PROVIDERS:
            raise ValueError("Private API connection settings are unsupported.")
        binary = None
    else:
        if set(data) != {"schema", "codex", "codex_sha256", "user_id"} or data["schema"] != "daemon.private-connection.v1":
            raise ValueError("Private connection settings are unsupported.")
        binary = Path(data["codex"])
        if (not binary.is_absolute() or binary.resolve() != binary or not binary.is_file()
                or hashlib.sha256(binary.read_bytes()).hexdigest() != data["codex_sha256"]):
            raise ValueError("The selected Codex executable changed.")
    user_id = data["user_id"]
    if not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdecimal() or not 1 <= len(user_id) <= 20:
        raise ValueError("Telegram owner must be one numeric user id.")
    private_home = scope.attempt / "os-home"
    auth = private_home / (".hermes-codex" if hermes else ".codex")
    for path in ((private_home,) if is_api else (private_home, auth)):
        if path.is_symlink() or path.resolve() != path or not path.is_dir():
            raise ValueError("Native sign-in directory must remain private and direct.")
    environment = clean_environment(scope.attempt)
    environment.update(private_home_environment(private_home))
    from daemon_core.installation_profile import CONTINUITY_RECEIPTS, SCENE_CONTINUITY
    environment.update({
        "HERMES_HOME": str(scope.profile),
        **SCENE_CONTINUITY,
        "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_USER_IDS": user_id,
        "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS": "telegram",
        **CONTINUITY_RECEIPTS,
        "DAEMON_CORE_CONTINUITY_RECEIPT_DIR": str(scope.home / "data/state/receipts"),
    })
    if scope.host_kind == "openclaw":
        environment.pop("HERMES_HOME", None)
    if not is_api:
        environment.update({"CODEX_HOME": str(auth), "CODEX_CLI_BIN": str(binary),
                            "PATH": str(binary.parent) + os.pathsep + environment.get("PATH", os.defpath)})
    else:
        provider_path = scope.home / 'data/state/config/provider.json'
        environment['DAEMON_CORE_PROVIDER_CONFIG'] = str(provider_path)
        if hermes and credentials:
            # Only the native host needs an environment credential. The shared
            # daemon loader passes keys directly, never through os.environ.
            from daemons_companion.setup.provider_runtime import managed_provider_config
            from daemon_core.api_providers import hermes_model_config, hermes_provider_entries
            import yaml
            configured = scope.profile / 'config.yaml'
            if configured.is_symlink() or configured.resolve() != configured or configured.stat().st_size > 1024 * 1024:
                raise ValueError('Private host configuration is invalid.')
            host_config = yaml.safe_load(configured.read_text(encoding='utf-8'))
            managed = managed_provider_config(environment)
            spec = API_PROVIDERS[data['provider_kind']]
            model_key = data['provider_kind'].removesuffix('_api').upper() + '_MODEL'
            model = managed.get(model_key)
            expected = hermes_model_config(data['provider_kind'], model)
            if not model or not isinstance(host_config, dict) or host_config.get('model') != expected:
                raise ValueError('The native host model differs from the managed API connection.')
            for name, entry in hermes_provider_entries(data['provider_kind'], model).items():
                if host_config.get('providers', {}).get(name) != entry:
                    raise ValueError('The native host API endpoint differs from the managed connection.')
            secret = managed.secret(spec.secret_name)
            if not secret:
                raise ValueError('The private API credential is unavailable.')
            environment[spec.secret_name] = secret
    from daemons_companion.setup.embeddings import client_environment
    environment.update(client_environment(scope, user_id))
    from daemon_core.memory.library_layout import vault_root
    from daemon_core.installation_profile import VAULT_FORMAT, installed_profile_environment
    if vault_root(scope.home / 'data/memory/.daemon') is not None:
        environment.update(VAULT_FORMAT)
    environment.update(installed_profile_environment(scope, user_id))
    return environment
