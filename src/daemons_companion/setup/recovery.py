"""Retain an unpublished legacy Python build before a fresh private attempt.

This is not a successful rollback: the old journal and every remaining byte
are retained. Only the known pre-service Windows failure is eligible. No host
configuration, memory, credentials or native task is written by this owner.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from daemons_companion.runtime.services import (
    AFTER_TURN_JOB,
    NIGHTLY_JOB,
    HERMES_GATEWAY_JOB,
    EMBEDDINGS_JOB,
    scoped_job_code,
)
from daemons_companion.hermes.ready_composition import HERMES_READY_STEP_CODES
from daemons_companion.runtime.bootstrap import _source_io_path as _io_path
from daemons_companion.runtime.environment import _path_descriptor, _is_junction
from daemons_companion.setup.transaction import _validate_journal
from daemon_core.memory.integrity import atomic_write_text

_RECEIPT_SCHEMA = "daemon.retained-python-recovery.v1"


def _direct(path: Path) -> Path:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or _is_junction(path)
        or path.resolve() != path
    ):
        raise ValueError("Recovery requires direct private paths.")
    return path


def _read(path: Path, *, maximum: int = 65536):
    _direct(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Recovery evidence is missing or too large.")
    data = path.read_bytes()
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Recovery evidence is invalid.")
    return value, hashlib.sha256(data).hexdigest()


def _absent(path):
    if _path_descriptor(path) != {"kind": "absent"}:
        raise RuntimeError("Recovery found an existing runtime; it is preserved.")


def retain_failed_python_build(scope, journal_path: Path, scheduler) -> Path | None:
    """Called under setup-owner and install-owner locks after plan confirmation.

    Atomically move the leftover direct tree out of the build namespace, without
    traversing junction targets. A receipt written first makes either side of a
    crash resumable; hashes reject concurrent edits and ambiguous dual trees.
    """
    scope.validate_home(scope.home)
    scope.validate_namespace(scheduler.job_namespace)
    if scheduler.target_os != "windows":
        raise RuntimeError(
            "Automatic recovery is limited to the legacy Windows Python build."
        )
    root = _direct(scope.home / "transactions")
    transaction = _direct(journal_path.parent)
    if transaction.parent != root or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", transaction.name
    ):
        raise ValueError("Recovery journal is outside the owned installation.")
    if journal_path != transaction / "journal.json":
        raise ValueError("Recovery journal path is invalid.")
    journal, journal_hash = _read(journal_path)
    _validate_journal(journal)
    if not (
        journal["transaction_id"] == transaction.name
        and journal["host"] == "hermes"
        and journal["status"] == "rollback_failed"
        and journal["step_codes"] == list(HERMES_READY_STEP_CODES)
        and journal["snapshotted_steps"] == ["runtime-layout", "runtime-environment"]
        and journal["completed_steps"] == ["runtime-layout"]
        and journal["rolled_back_steps"] == ["runtime-layout"]
        and journal["rollback_failed_steps"] == ["runtime-environment"]
    ):
        raise RuntimeError(
            "This interrupted installation requires separate recovery; its files and jobs are preserved."
        )
    snapshots = _direct(transaction / "snapshots")
    runtime_snapshot, runtime_hash = _read(
        snapshots / "runtime-layout/manifest.json", maximum=4 * 1024 * 1024
    )
    environment, environment_hash = _read(
        snapshots / "runtime-environment/manifest.json"
    )
    release = environment.get("release_id", "")
    if not isinstance(release, str) or not re.fullmatch(r"[0-9a-f]{64}", release):
        raise ValueError("Recovery release identifier is invalid.")
    if (
        runtime_snapshot.get("schema") != "daemon.companion-runtime-snapshot.v1"
        or runtime_snapshot.get("release_id") != release
        or environment.get("schema") != "daemon.companion-environment-snapshot.v1"
        or environment.get("target_os") != "windows"
        or environment.get("compact_paths") is not None
    ):
        raise RuntimeError(
            "Recovery is not an unpublished legacy Windows Python build."
        )
    for evidence, keys in (
        (runtime_snapshot, ("current", "release", "stage")),
        (environment, ("final", "stage")),
    ):
        if any(
            evidence.get("before", {}).get(key) != {"kind": "absent"} for key in keys
        ):
            raise RuntimeError("Recovery cannot replace a previous runtime.")
    runtime = _direct(scope.home / "data/runtime")
    for path in (
        runtime / "current.json",
        runtime / "releases" / release,
        runtime / (".staging-" + release),
        runtime / "environments" / release,
        runtime / "environments" / release[:16],
    ):
        _direct(path)
        _absent(path)
    # The journal's pre-service boundary is checked independently against the
    # scoped native jobs. Never stop or overwrite an existing job.
    for code in (AFTER_TURN_JOB, NIGHTLY_JOB, HERMES_GATEWAY_JOB, EMBEDDINGS_JOB):
        if scheduler.capture(scoped_job_code(code, scope.installation_id)) is not None:
            raise RuntimeError(
                "Recovery found an existing private service; it is preserved."
            )
    source = _direct(runtime / (".environment-" + release))
    target = _direct(runtime / (".retained-" + transaction.name))
    receipt_path = _direct(transaction / "retained-python.json")
    evidence = {
        "schema": _RECEIPT_SCHEMA,
        "journal_sha256": journal_hash,
        "runtime_snapshot_sha256": runtime_hash,
        "environment_snapshot_sha256": environment_hash,
        "source": source.name,
        "destination": target.name,
    }
    if receipt_path.exists():
        receipt, _ = _read(receipt_path)
        if {key: value for key, value in receipt.items() if key != "tree"} != evidence:
            raise RuntimeError(
                "Recovery evidence changed; the retained files are preserved."
            )
    else:
        _absent(target)
        tree = _path_descriptor(source, recursive=True)
        if tree["kind"] not in {"absent", "directory"}:
            raise RuntimeError("Failed Python build is not a direct directory.")
        receipt = {**evidence, "tree": tree}
        atomic_write_text(receipt_path, json.dumps(receipt, sort_keys=True) + "\n")
    tree = receipt.get("tree")
    if not isinstance(tree, dict) or tree.get("kind") not in {"absent", "directory"}:
        raise ValueError("Retained Python receipt is invalid.")
    source_now = _path_descriptor(source, recursive=True)
    target_now = _path_descriptor(target, recursive=True)
    if tree["kind"] == "absent":
        if source_now != tree or target_now != tree:
            raise RuntimeError(
                "Recovery files changed concurrently; nothing was removed."
            )
        return None
    if source_now == tree and target_now == {"kind": "absent"}:
        os.replace(_io_path(source), _io_path(target))
    elif source_now != {"kind": "absent"} or target_now != tree:
        raise RuntimeError("Recovery files changed concurrently; nothing was removed.")
    if _path_descriptor(target, recursive=True) != tree:
        raise RuntimeError(
            "Retained Python files changed; they are preserved for review."
        )
    return target
