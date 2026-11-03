"""Disposable provider state with credentials delegated to an OS store."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Callable
import uuid

from daemons_companion.setup.plan import PlanStep, ProviderKind, ProviderSelection
from daemons_companion.setup.credentials import CredentialStore
from daemons_companion.runtime.release import validate_companion_home
from daemons_companion.runtime.installation_scope import InstallationScope


PROVIDER_STATE_STEP = "provider-state"
PROVIDER_STATE_SCHEMA = "daemon.companion-provider.v1"
_SNAPSHOT_SCHEMA = "daemon.companion-provider-snapshot.v1"
_CREDENTIAL_SCHEMA = "daemon.companion-credential.v1"


@dataclass(frozen=True)
class CompanionProviderLayout:
    companion_home: Path
    installation_scope: InstallationScope | None = None

    @property
    def state_root(self) -> Path:
        return self.companion_home / "data" / "state"

    @property
    def config_dir(self) -> Path:
        return self.state_root / "config"

    @property
    def provider_file(self) -> Path:
        return self.config_dir / "provider.json"


class CompanionProviderAdapter:
    """Write non-secret provider state and an owned protected credential."""

    def __init__(
        self,
        layout: CompanionProviderLayout,
        *,
        selection: ProviderSelection,
        credential_store: CredentialStore | None,
        credential_value: str | None = None,
        codex_available: bool = False,
        mutation_hook: Callable[[str], None] | None = None,
        _recovery_only: bool = False,
    ) -> None:
        self.layout = layout
        self.selection = selection.validated(codex_available=codex_available)
        self.credential_store = credential_store
        self.credential_value = credential_value
        self._mutation_hook = mutation_hook or (lambda _artifact: None)
        self._snapshot_dirs: dict[str, Path] = {}
        required_name = _credential_name(self.selection)
        if required_name:
            if credential_store is None:
                raise ValueError("API provider requires an operating-system credential store.")
            if (
                not _recovery_only
                and (not isinstance(credential_value, str) or not credential_value)
            ):
                raise ValueError("API provider credential is required.")
            if credential_value is not None and len(credential_value.encode("utf-8")) > 1_500:
                raise ValueError("API provider credential is too large.")
        elif credential_value is not None:
            raise ValueError("This provider selection does not accept a credential value.")

    @classmethod
    def for_recovery(
        cls,
        layout: CompanionProviderLayout,
        *,
        selection: ProviderSelection,
        credential_store: CredentialStore,
        codex_available: bool = False,
    ) -> "CompanionProviderAdapter":
        """Build a rollback-only adapter without asking for the API key again."""

        return cls(
            layout,
            selection=selection,
            credential_store=credential_store,
            credential_value=None,
            codex_available=codex_available,
            _recovery_only=True,
        )

    def snapshot(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        self._validate_layout()
        _validate_snapshot_dir(snapshot_dir)
        before = _path_descriptor(self.layout.provider_file)
        config_before = _read_existing_config(self.layout.provider_file)
        if before["kind"] == "file":
            _atomic_write(
                snapshot_dir / "provider.before",
                self.layout.provider_file.read_bytes(),
                0o600,
            )

        core = _provider_core(self.selection)
        credential = self._credential_plan(config_before, core)
        expected_config = {
            **core,
            "credential": (
                None
                if credential["action"] == "none"
                else {
                    "name": _credential_name(self.selection),
                    "reference": credential["reference"],
                }
            ),
        }
        expected_bytes = _json_bytes(expected_config)
        manifest = {
            "schema": _SNAPSHOT_SCHEMA,
            "before": before,
            "config_dir_before": _path_descriptor(self.layout.config_dir),
            "expected": _file_descriptor_bytes(expected_bytes, 0o600),
            "expected_config": expected_config,
            "credential": credential,
        }
        _atomic_write(snapshot_dir / "manifest.json", _json_bytes(manifest), 0o600)
        self._snapshot_dirs[step.code] = snapshot_dir

    def apply(self, step: PlanStep) -> None:
        self._validate_step(step)
        self._validate_layout()
        snapshot_dir = self._snapshot_dirs.get(step.code)
        if snapshot_dir is None:
            raise RuntimeError("Provider snapshot is required before apply.")
        manifest = _load_manifest(snapshot_dir)
        if _path_descriptor(self.layout.provider_file) != manifest["before"]:
            raise RuntimeError("Provider state changed after its snapshot.")
        credential = manifest["credential"]
        if credential["action"] == "create":
            if self.credential_store is None or self.credential_value is None:
                raise RuntimeError("Recovery-only provider adapter cannot apply settings.")
            payload = _credential_payload(
                str(credential["owner_token"]),
                self.credential_value,
            )
            if self.credential_store.get(str(credential["reference"])) is not None:
                raise RuntimeError("New credential reference is already occupied.")
            self.credential_store.put(str(credential["reference"]), payload)
            self._mutation_hook("credential")
        _atomic_write(
            self.layout.provider_file,
            _json_bytes(manifest["expected_config"]),
            0o600,
        )
        self._mutation_hook("config")

    def verify(self, step: PlanStep) -> None:
        self._validate_step(step)
        self._validate_layout()
        snapshot_dir = self._snapshot_dirs.get(step.code)
        if snapshot_dir is None:
            raise RuntimeError("Provider snapshot is required before verify.")
        manifest = _load_manifest(snapshot_dir)
        if _path_descriptor(self.layout.provider_file) != manifest["expected"]:
            raise RuntimeError("Provider state was not applied exactly.")
        credential = manifest["credential"]
        if credential["action"] != "none":
            assert self.credential_store is not None
            payload = self.credential_store.get(str(credential["reference"]))
            parsed = _parse_credential_payload(payload)
            if parsed is None:
                raise RuntimeError("Provider credential is unavailable.")
            if self.credential_value is not None and parsed["value"] != self.credential_value:
                raise RuntimeError("Provider credential does not match this setup.")

    def rollback(self, step: PlanStep, snapshot_dir: Path) -> None:
        self._validate_step(step)
        self._validate_layout()
        manifest = _load_manifest(snapshot_dir)
        current = _path_descriptor(self.layout.provider_file)
        if current not in (manifest["before"], manifest["expected"]):
            raise RuntimeError("Provider state has concurrent changes; rollback was refused.")
        credential = manifest["credential"]
        if credential["action"] == "create":
            assert self.credential_store is not None
            payload = self.credential_store.get(str(credential["reference"]))
            if payload is not None:
                parsed = _parse_credential_payload(payload)
                if parsed is None or parsed["owner_token"] != credential["owner_token"]:
                    raise RuntimeError(
                        "Provider credential has concurrent changes; rollback was refused."
                    )

        _restore_provider_file(
            self.layout.provider_file,
            manifest["before"],
            snapshot_dir / "provider.before",
        )
        if credential["action"] == "create":
            assert self.credential_store is not None
            self.credential_store.delete(str(credential["reference"]))
        if (
            manifest["config_dir_before"]["kind"] == "absent"
            and self.layout.config_dir.exists()
        ):
            self.layout.config_dir.rmdir()

    def _credential_plan(
        self,
        current: dict[str, object] | None,
        core: dict[str, object],
    ) -> dict[str, object]:
        name = _credential_name(self.selection)
        if name is None:
            return {"action": "none", "reference": "", "owner_token": ""}
        if self.credential_store is None or self.credential_value is None:
            raise RuntimeError("Recovery-only provider adapter cannot prepare settings.")
        if current is not None:
            existing_core = {key: current[key] for key in core}
            credential = current.get("credential")
            if existing_core == core and isinstance(credential, dict):
                reference = credential.get("reference")
                if credential.get("name") == name and isinstance(reference, str):
                    payload = self.credential_store.get(reference)
                    parsed = _parse_credential_payload(payload)
                    if parsed is not None and parsed["value"] == self.credential_value:
                        return {
                            "action": "reuse",
                            "reference": reference,
                            "owner_token": parsed["owner_token"],
                        }
        return {
            "action": "create",
            "reference": f"provider-{uuid.uuid4()}",
            "owner_token": str(uuid.uuid4()),
        }

    def _validate_step(self, step: PlanStep) -> None:
        if step.code != PROVIDER_STATE_STEP:
            raise ValueError("Provider adapter accepts only the provider-state step.")

    def _validate_layout(self) -> None:
        home = validate_companion_home(self.layout.companion_home, self.layout.installation_scope)
        state = self.layout.state_root
        if state.is_symlink() or not state.is_dir():
            raise ValueError("Provider adapter requires the existing shared state root.")
        try:
            state.relative_to(home)
        except ValueError as error:  # pragma: no cover - fixed layout property.
            raise ValueError("Provider state root escapes the companion home.") from error
        if self.layout.config_dir.is_symlink() or self.layout.provider_file.is_symlink():
            raise ValueError("Provider configuration paths must not be symbolic links.")


def _provider_core(selection: ProviderSelection) -> dict[str, object]:
    return {
        "schema": PROVIDER_STATE_SCHEMA,
        "kind": selection.kind.value,
        "model": selection.model,
        "base_url": selection.base_url,
        "reasoning": {
            "routing": "medium",
            "conversation": "medium",
            "memory": "high",
        },
    }


def _credential_name(selection: ProviderSelection) -> str | None:
    from daemon_core.api_providers import API_PROVIDERS
    if selection.kind.value in API_PROVIDERS:
        return API_PROVIDERS[selection.kind.value].secret_name
    if selection.kind == ProviderKind.OPENAI_COMPATIBLE_API and selection.credential_available:
        return "LOCAL_LLM_API_KEY"
    return None


def _credential_payload(owner_token: str, value: str) -> str:
    return json.dumps(
        {"schema": _CREDENTIAL_SCHEMA, "owner_token": owner_token, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_credential_payload(payload: str | None) -> dict[str, str] | None:
    if payload is None:
        return None
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"schema", "owner_token", "value"}
        or parsed.get("schema") != _CREDENTIAL_SCHEMA
        or not isinstance(parsed.get("owner_token"), str)
        or not isinstance(parsed.get("value"), str)
    ):
        return None
    return parsed


def _read_existing_config(path: Path) -> dict[str, object] | None:
    descriptor = _path_descriptor(path)
    if descriptor["kind"] == "absent":
        return None
    if descriptor["kind"] != "file":
        raise ValueError("Provider state must be a regular file.")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError("Existing provider state is malformed.") from error
    required = {"schema", "kind", "model", "base_url", "reasoning", "credential"}
    if not isinstance(parsed, dict) or set(parsed) != required:
        raise ValueError("Existing provider state has an unknown format.")
    if parsed.get("schema") != PROVIDER_STATE_SCHEMA:
        raise ValueError("Existing provider state version is unsupported.")
    return parsed


def _restore_provider_file(path: Path, before: dict[str, object], saved: Path) -> None:
    if before["kind"] == "absent":
        path.unlink(missing_ok=True)
        return
    if before["kind"] != "file" or saved.is_symlink() or not saved.is_file():
        raise RuntimeError("Previous provider state cannot be restored safely.")
    _atomic_write(path, saved.read_bytes(), int(before.get("mode", 0o600)))


def _path_descriptor(path: Path) -> dict[str, object]:
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if not path.exists():
        return {"kind": "absent"}
    if path.is_file():
        return _file_descriptor_bytes(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    if path.is_dir():
        descriptor: dict[str, object] = {"kind": "directory"}
        if os.name != "nt":
            descriptor["mode"] = stat.S_IMODE(path.stat().st_mode)
        return descriptor
    return {"kind": "special"}


def _file_descriptor_bytes(data: bytes, mode: int) -> dict[str, object]:
    descriptor: dict[str, object] = {
        "kind": "file",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if os.name != "nt":
        descriptor["mode"] = mode
    return descriptor


def _load_manifest(snapshot_dir: Path) -> dict[str, object]:
    path = snapshot_dir / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Provider snapshot manifest is unavailable.")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or set(loaded) != {
        "schema",
        "before",
        "config_dir_before",
        "expected",
        "expected_config",
        "credential",
    }:
        raise RuntimeError("Provider snapshot manifest is invalid.")
    if loaded["schema"] != _SNAPSHOT_SCHEMA:
        raise RuntimeError("Provider snapshot version is unsupported.")
    return loaded


def _validate_snapshot_dir(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("Provider snapshot directory must be direct and absolute.")


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


__all__ = [
    "CompanionProviderAdapter",
    "CompanionProviderLayout",
    "PROVIDER_STATE_SCHEMA",
    "PROVIDER_STATE_STEP",
]
