"""Versioned continuity contract for newly installed, private PARGA daemons.

This selects already qualified faculties; it does not change their decisions.
An explicit installation marker keeps older installations on their own profile.
No credential, personal note or host configuration is copied into this contract.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import re

from daemon_core.memory.integrity import atomic_write_text
from daemon_core.memory.library_layout import vault_root
from daemon_core.memory.path_identity import path_identity


PROFILE_ENV = "DAEMON_CORE_INSTALLATION_PROFILE"
MARKER = "continuity-profile.json"


def _switches(**values: str) -> dict[str, str]:
    return {"DAEMON_CORE_" + key: value for key, value in values.items()}


# Behavior switches written by Companion-owned processes. Callers import these
# groups instead of repeating literals; installations without a continuity
# marker keep exactly the groups their process already received.
DURABLE_AFTER_TURN = _switches(AFTER_TURN_OWNER="external", AFTER_TURN_SPOOL="on")
HOST_VOICE = _switches(SOVEREIGN_VOICE="on", FOLLOWUPS="on")
CONTINUITY_RECEIPTS = _switches(CONTINUITY_RECEIPTS="on")
SCENE_CONTINUITY = _switches(SCENE_COALESCING="on", NARRATIVE_AUTHORITY_HIERARCHY="on")
VAULT_FORMAT = _switches(MEMORY_STRUCTURE="v1", OBSIDIAN_FORMAT="1")
EMBEDDING_SELECTION = _switches(ATLAS_EMBEDDING_TOP12="canary")

_V0 = _switches(
    MEMORY_STRUCTURE="v1",
    OBSIDIAN_FORMAT="1",
    MEMORY_RELOCATION="v1",
    SCENE_MODEL="canary",
    CURRENT_TOPICS="canary",
    PERSONAL_CONTEXT="canary",
    MEMORY_SITUATIONS="canary",
    TEMPORAL_CURATOR="on",
    REM_EPISODIC_SCENES="canary",
    SHARED_WORLD_STATE="on",
    SCENE_COALESCING="on",
    NARRATIVE_AUTHORITY_HIERARCHY="on",
    RADAR_FINAL_SELECTION="on",
    SITUATED_CONTEXT="on",
    SOVEREIGN_VOICE="on",
    FOLLOWUPS="on",
)
_V0_SCOPED = (
    "SCENE_MODEL", "CURRENT_TOPICS", "PERSONAL_CONTEXT", "MEMORY_SITUATIONS",
    "TEMPORAL_CURATOR", "REM_EPISODIC_SCENE", "NARRATIVE_AUTHORITY_HIERARCHY",
)
_V0_PLATFORMS = ("PERSONAL_CONTEXT", "REM_EPISODIC_SCENE", "NARRATIVE_AUTHORITY_HIERARCHY")

# v1 carries every switch that shapes model input or memory writes in the
# qualified reference daemon, so each process receives one complete set.
_V1 = {
    **DURABLE_AFTER_TURN, **HOST_VOICE, **CONTINUITY_RECEIPTS, **SCENE_CONTINUITY,
    **VAULT_FORMAT, **_V0,
    **_switches(
        COMMON_GROUND_SHADOW="shadow",
        COMMON_GROUND_LIBRARY="on",
        MEMORY_RELATION_SHADOW="shadow",
        MEMORY_COLLECTION_BOOTSTRAP_SHADOW="shadow",
        MEMORY_TOPOLOGY_BUNDLE_SHADOW="shadow",
        MEMORY_PERFORMATIVE_RELATION_RETRACTIONS="canary",
        HERMES_PROGRESS_PREAMBLE="canary",
        HERMES_PROGRESS_PREAMBLE_PLATFORMS="telegram",
        RECEIPT_REACTION="off",
    ),
    # Nightly Dreaming rereads the same 48-hour window as the reference daemon.
    "DREAMING_SINCE_HOURS": "48",
}
_V1_SCOPED = _V0_SCOPED + (
    "AFTER_TURN", "COMMON_GROUND_SHADOW", "COMMON_GROUND_LIBRARY",
    "MEMORY_RELATION_SHADOW", "MEMORY_COLLECTION_BOOTSTRAP_SHADOW",
    "MEMORY_TOPOLOGY_BUNDLE_SHADOW", "MEMORY_PERFORMATIVE_RELATION_RETRACTIONS",
)
_V1_PLATFORMS = _V0_PLATFORMS + ("COMMON_GROUND_LIBRARY",)

_PROFILES = {
    "parga-v0": (_V0, _V0_SCOPED, _V0_PLATFORMS),
    "parga-v1": (_V1, _V1_SCOPED, _V1_PLATFORMS),
}
PROFILE = "parga-v1"  # written for fresh installations; older markers keep theirs


def _ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def profile_environment(user_id: str, memory_root: Path, *, profile: str = PROFILE) -> dict[str, str]:
    if profile not in _PROFILES:
        raise ValueError("The continuity profile is unknown.")
    if not isinstance(user_id, str) or re.fullmatch(r"[1-9][0-9]{0,19}", user_id) is None:
        raise ValueError("The continuity profile requires one numeric owner.")
    root = Path(memory_root)
    if root.is_symlink() or not root.is_absolute() or root.resolve() != root or vault_root(root) != root.parent:
        raise ValueError("The continuity profile requires its exact private PARGA vault.")
    switches, scoped, platforms = _PROFILES[profile]
    values = dict(switches)
    values.update({"DAEMON_CORE_" + key + "_USER_IDS": user_id for key in scoped})
    values.update({"DAEMON_CORE_" + key + "_PLATFORMS": "telegram,openclaw" for key in platforms})
    values.update({PROFILE_ENV: profile,
                   PROFILE_ENV + "_USER_ID": user_id,
                   PROFILE_ENV + "_MEMORY_ROOT": str(root)})
    return values


def _marker_value(scope, user_id: str, profile: str = PROFILE) -> dict:
    root = scope.home / "data/memory/.daemon"
    profile_environment(user_id, root, profile=profile)
    return {"schema": "daemon.installation-profile.v1", "profile": profile,
            "user_ref": _ref(user_id), "memory_root_ref": _ref(path_identity(str(root)))}


def initialize_profile(scope, user_id: str) -> None:
    """Called by fresh setup only, after the empty owned vault is initialized."""
    scope.validate_home(scope.home)
    value = _marker_value(scope, user_id)
    path = scope.attempt / MARKER
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("The continuity profile marker must remain private and direct.")
    if path.exists():
        installed_profile_environment(scope, user_id)
        return
    atomic_write_text(path, json.dumps(value, sort_keys=True) + "\n")
    path.chmod(0o600)


def installed_profile_environment(scope, user_id: str) -> dict[str, str]:
    scope.validate_home(scope.home)
    path = scope.attempt / MARKER
    if not path.exists() and not path.is_symlink():
        return {}  # Existing installations are not silently upgraded.
    if path.is_symlink() or path.resolve() != path or not path.is_file() or path.stat().st_size > 8192:
        raise ValueError("The continuity profile marker is invalid.")
    recorded = json.loads(path.read_text(encoding="utf-8"))
    # Each marker keeps the profile it was created with; none is rewritten.
    profile = recorded.get("profile") if isinstance(recorded, dict) else None
    if profile not in _PROFILES:
        raise ValueError("The continuity profile does not match this installation and owner.")
    expected = _marker_value(scope, user_id, profile)
    # Accept a historical marker hashed with this exact extended spelling too;
    # no arbitrary alternate root or owner is accepted.
    legacy = dict(expected, memory_root_ref=_ref(str(scope.home / "data/memory/.daemon")))
    if recorded not in (expected, legacy):
        raise ValueError("The continuity profile does not match this installation and owner.")
    return profile_environment(user_id, scope.home / "data/memory/.daemon", profile=profile)


def profile_status(environment: Mapping[str, str], *, user_ids: Sequence[str] | None = None,
                   memory_root: Path | None = None) -> dict | None:
    """Attest the process environment, not just a generated configuration file."""
    name = environment.get(PROFILE_ENV)
    if name is None:
        return None
    owner = environment.get(PROFILE_ENV + "_USER_ID", "")
    root = Path(environment.get(PROFILE_ENV + "_MEMORY_ROOT", ""))
    failures = []
    try:
        expected = profile_environment(owner, root, profile=name)
        failures.extend(key for key, value in expected.items() if environment.get(key) != value)
    except (OSError, ValueError):
        failures.append("profile_scope_invalid")
    if user_ids is not None and tuple(user_ids) != (owner,):
        failures.append("profile_owner_mismatch")
    if memory_root is not None:
        from daemon_core.platform_support import io_path as _source_io_path
        if _source_io_path(Path(memory_root)).resolve() != _source_io_path(root).resolve():
            failures.append("profile_memory_mismatch")
    return {"profile": name, "ok": not failures, "failures": sorted(failures),
            "user_ref": _ref(owner), "memory_root_ref": _ref(path_identity(str(root)))}


def require_profile(environment: Mapping[str, str], *, user_ids: Sequence[str], memory_root: Path) -> None:
    status = profile_status(environment, user_ids=user_ids, memory_root=memory_root)
    if status is not None and not status["ok"]:
        raise ValueError("The installed continuity profile is incomplete: " + ", ".join(status["failures"]))
