from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Mapping

from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.paths import MEMORY_ROOT


@dataclass(frozen=True)
class GroundZeroReference:
    """Stable byte reference to one exact entry in the Markdown L0."""

    path: str
    offset: int
    length: int
    sha256: str
    role: str
    turn_id: str = ""

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "offset": self.offset,
            "length": self.length,
            "sha256": self.sha256,
            "role": self.role,
            "turn_id": self.turn_id,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "GroundZeroReference":
        if not isinstance(value, dict):
            raise ValueError("ground zero reference must be an object")
        return cls(
            path=str(value.get("path", "")),
            offset=int(value.get("offset", -1)),
            length=int(value.get("length", 0)),
            sha256=str(value.get("sha256", "")),
            role=str(value.get("role", "")),
            turn_id=str(value.get("turn_id", "")),
        )


@dataclass(frozen=True)
class GroundZeroRecord:
    """Verified L0 entry with its exact timestamp, role, and body."""

    timestamp: str
    role: str
    body: str


@dataclass(frozen=True)
class GroundZeroReconciliation:
    """Result of one bounded exact host-transcript reconciliation."""

    scanned: int
    already_recorded: int
    restored: int


def safe_memory_user_id(user_id: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in user_id)


def _ground_zero_datetime(occurred_at: float = 0.0) -> datetime:
    if occurred_at > 0:
        return datetime.fromtimestamp(occurred_at)
    return datetime.now()


def ground_zero_conversation_path(
    user_id: str,
    *,
    occurred_at: float = 0.0,
) -> Path:
    date = _ground_zero_datetime(occurred_at).strftime("%Y-%m-%d")
    return MEMORY_ROOT / "levels" / "L0_ground_zero" / "conversations" / f"{safe_memory_user_id(user_id)}_{date}.md"


def user_memory_dir(user_id: str) -> Path:
    return MEMORY_ROOT / "users" / safe_memory_user_id(user_id)


def user_context_path(user_id: str) -> Path:
    return user_memory_dir(user_id) / "context.md"


def user_preferences_path(user_id: str) -> Path:
    return user_memory_dir(user_id) / "preferences.md"


def user_agenda_path(user_id: str) -> Path:
    return user_memory_dir(user_id) / "agenda.md"


def active_context_path(user_id: str) -> Path:
    return user_memory_dir(user_id) / "contexte_actif.md"


def trajectory_path(user_id: str) -> Path:
    return user_memory_dir(user_id) / "trajectoire.md"


def stance_register_path(user_id: str) -> Path:
    return user_memory_dir(user_id) / "stance_register.md"


def legacy_carnet_path(user_id: str) -> Path:
    return user_memory_dir(user_id) / "carnet.md"


def carnet_path(user_id: str) -> Path:
    """Legacy API name. New writes go to the Stance Register file."""
    return stance_register_path(user_id)


def write_stance_register(user_id: str, content: str) -> Path:
    path = stance_register_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def write_carnet(user_id: str, content: str) -> Path:
    return write_stance_register(user_id, content)


def write_trajectory(user_id: str, content: str) -> Path:
    path = trajectory_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def immediate_focus_path(user_id: str) -> Path:
    return user_memory_dir(user_id) / "focus_immediat.md"


def conversation_state_path(user_id: str) -> Path:
    return user_memory_dir(user_id) / "conversation_state.md"


def legacy_current_discussion_path(user_id: str) -> Path:
    return user_memory_dir(user_id) / "current_discussion.md"


def legacy_moment_memory_path(user_id: str) -> Path:
    return user_memory_dir(user_id) / "moment.md"


def current_discussion_path(user_id: str) -> Path:
    return immediate_focus_path(user_id)


def moment_memory_path(user_id: str) -> Path:
    return active_context_path(user_id)


def souvenir_recalls_path() -> Path:
    return MEMORY_ROOT / "levels" / "capsule_recalls.md"


def record_souvenir_recalls(names: list[str], when: datetime | None = None) -> Path | None:
    """Record each memory's latest recall time and count.

    Dreaming uses these records when judging consolidation and dormancy."""
    if not names:
        return None
    path = souvenir_recalls_path()
    entries = _parse_souvenir_recalls(path)
    day = (when or datetime.now()).date().isoformat()
    for name in names:
        _, count = entries.get(name, ("", 0))
        entries[name] = (day, count + 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"- {name}: dernier rappel {last} | rappels {count}" for name, (last, count) in sorted(entries.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _parse_souvenir_recalls(path: Path) -> dict[str, tuple[str, int]]:
    entries: dict[str, tuple[str, int]] = {}
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        name, sep, rest = stripped[2:].partition(":")
        if not sep:
            continue
        last, _, count_part = rest.partition("|")
        last = last.replace("dernier rappel", "").strip()
        digits = "".join(ch for ch in count_part if ch.isdigit())
        entries[name.strip()] = (last, int(digits) if digits else 0)
    return entries


def append_ground_zero(
    user_id: str,
    role: str,
    content: str,
    *,
    turn_id: str = "",
    idempotency_key: str = "",
    occurred_at: float = 0.0,
    return_reference: bool = False,
) -> Path | GroundZeroReference:
    if idempotency_key.strip():
        reference = _append_ground_zero_idempotent(
            user_id=user_id,
            role=role,
            content=content,
            turn_id=turn_id,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
        )
        return reference if return_reference else _reference_path(reference)

    instant = _ground_zero_datetime(occurred_at)
    path = ground_zero_conversation_path(
        user_id,
        occurred_at=occurred_at,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = instant.isoformat(timespec="seconds")
    encoded = f"\n## {timestamp} — {role}\n\n{content}\n".encode("utf-8")
    with memory_file_lock(path):
        with path.open("ab") as handle:
            offset = handle.tell()
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    reference = GroundZeroReference(
        path=path.resolve().relative_to(MEMORY_ROOT.resolve()).as_posix(),
        offset=offset,
        length=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
        role=role,
        turn_id=turn_id,
    )
    return reference if return_reference else path


def reconcile_ground_zero_messages(
    user_id: str,
    messages: Iterable[Mapping[str, object]],
    *,
    source: str,
    session_id: str,
) -> GroundZeroReconciliation:
    """Append only exact transcript messages absent from durable L0 receipts.

    The host transcript is a recovery source, never a second memory. Existing
    top-level L0 entries are identified through their verified idempotency
    receipts instead of reparsing Markdown bodies, which may themselves quote
    older L0 headings. Replays remain safe across process restarts because each
    restored host event receives its own stable idempotency key.
    """

    candidates = []
    for value in messages:
        role = str(value.get("role", "")).strip().lower()
        content = str(value.get("content", ""))
        event_id = str(value.get("event_id", "")).strip()
        try:
            occurred_at = float(value.get("occurred_at", 0.0))
        except (TypeError, ValueError):
            occurred_at = 0.0
        if (
            role not in {"user", "assistant"}
            or not content
            or not event_id
            or occurred_at <= 0
        ):
            raise ValueError("invalid ground zero reconciliation message")
        timestamp = _ground_zero_datetime(occurred_at).isoformat(
            timespec="seconds"
        )
        candidates.append(
            {
                "role": role,
                "content": content,
                "event_id": event_id,
                "occurred_at": occurred_at,
                "timestamp": timestamp,
                "payload_sha256": _ground_zero_payload_sha256(
                    role,
                    content,
                ),
            }
        )

    if not candidates:
        return GroundZeroReconciliation(0, 0, 0)

    target_timestamps = {item["timestamp"] for item in candidates}
    existing = _ground_zero_receipt_counts(
        user_id,
        target_timestamps=target_timestamps,
    )
    already_recorded = 0
    restored = 0
    safe_source = "".join(
        character
        for character in str(source).strip().lower()
        if character.isalnum() or character in {"-", "_"}
    )
    if not safe_source:
        raise ValueError("ground zero reconciliation source missing")
    safe_session = hashlib.sha256(
        str(session_id).encode("utf-8")
    ).hexdigest()[:24]

    for item in candidates:
        fingerprint = (item["timestamp"], item["payload_sha256"])
        if existing[fingerprint] > 0:
            existing[fingerprint] -= 1
            already_recorded += 1
            continue
        append_ground_zero(
            user_id=user_id,
            role=item["role"],
            content=item["content"],
            idempotency_key=(
                f"{safe_source}:transcript:{safe_session}:"
                f"{item['event_id']}:{item['role']}"
            ),
            occurred_at=item["occurred_at"],
            return_reference=True,
        )
        existing[fingerprint] += 1
        restored += 1

    return GroundZeroReconciliation(
        scanned=len(candidates),
        already_recorded=already_recorded,
        restored=restored,
    )


def _append_ground_zero_idempotent(
    *,
    user_id: str,
    role: str,
    content: str,
    turn_id: str,
    idempotency_key: str,
    occurred_at: float,
) -> GroundZeroReference:
    receipt_path = _ground_zero_receipt_path(
        user_id,
        idempotency_key,
    )
    payload_sha256 = _ground_zero_payload_sha256(role, content)
    with memory_file_lock(receipt_path):
        receipt = _read_ground_zero_receipt(receipt_path)
        if receipt is not None:
            if receipt.get("payload_sha256") != payload_sha256:
                raise ValueError(
                    "ground zero idempotency key reused for another payload"
                )
            reference = GroundZeroReference.from_dict(
                receipt.get("reference", {})
            )
            timestamp = str(receipt.get("timestamp", ""))
            encoded = _ground_zero_entry(timestamp, role, content)
            if hashlib.sha256(encoded).hexdigest() != reference.sha256:
                raise ValueError(
                    "ground zero idempotency receipt payload mismatch"
                )
            path = _reference_path(reference)
            with memory_file_lock(path):
                if _reference_bytes_match(path, reference):
                    if receipt.get("status") != "completed":
                        _write_ground_zero_receipt(
                            receipt_path,
                            receipt,
                            status="completed",
                        )
                    return reference
                _append_at_expected_offset(
                    path,
                    reference.offset,
                    encoded,
                )
            _write_ground_zero_receipt(
                receipt_path,
                receipt,
                status="completed",
            )
            return reference

        instant = _ground_zero_datetime(occurred_at)
        path = ground_zero_conversation_path(
            user_id,
            occurred_at=occurred_at,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = instant.isoformat(timespec="seconds")
        encoded = _ground_zero_entry(timestamp, role, content)
        with memory_file_lock(path):
            offset = path.stat().st_size if path.exists() else 0
            reference = GroundZeroReference(
                path=path.resolve().relative_to(MEMORY_ROOT.resolve()).as_posix(),
                offset=offset,
                length=len(encoded),
                sha256=hashlib.sha256(encoded).hexdigest(),
                role=role,
                turn_id=turn_id,
            )
            receipt = {
                "schema": "daemon.l0-idempotency.v1",
                "status": "pending",
                "payload_sha256": payload_sha256,
                "timestamp": timestamp,
                "reference": reference.as_dict(),
            }
            _write_ground_zero_receipt(
                receipt_path,
                receipt,
                status="pending",
            )
            _append_at_expected_offset(path, offset, encoded)
        _write_ground_zero_receipt(
            receipt_path,
            receipt,
            status="completed",
        )
        return reference


def _ground_zero_entry(
    timestamp: str,
    role: str,
    content: str,
) -> bytes:
    if not timestamp:
        raise ValueError("ground zero entry timestamp missing")
    return f"\n## {timestamp} — {role}\n\n{content}\n".encode("utf-8")


def _ground_zero_payload_sha256(role: str, content: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "role": role,
                "content": content,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _ground_zero_receipt_counts(
    user_id: str,
    *,
    target_timestamps: set[str],
) -> Counter[tuple[str, str]]:
    root = (
        MEMORY_ROOT
        / "levels"
        / "L0_ground_zero"
        / "idempotency"
        / safe_memory_user_id(user_id)
    )
    counts: Counter[tuple[str, str]] = Counter()
    seen_references = set()
    if not root.exists():
        return counts
    for path in root.glob("*.json"):
        try:
            receipt = _read_ground_zero_receipt(path)
            if receipt is None or receipt.get("status") != "completed":
                continue
            timestamp = str(receipt.get("timestamp", ""))
            if timestamp not in target_timestamps:
                continue
            reference = GroundZeroReference.from_dict(
                receipt.get("reference", {})
            )
            reference_key = (
                reference.path,
                reference.offset,
                reference.length,
                reference.sha256,
            )
            if reference_key in seen_references:
                continue
            reference_path = _reference_path(reference)
            if not _reference_bytes_match(reference_path, reference):
                continue
            payload_sha256 = str(receipt.get("payload_sha256", ""))
            if not payload_sha256:
                continue
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            continue
        seen_references.add(reference_key)
        counts[(timestamp, payload_sha256)] += 1
    return counts


def _ground_zero_receipt_path(
    user_id: str,
    idempotency_key: str,
) -> Path:
    digest = hashlib.sha256(
        idempotency_key.encode("utf-8")
    ).hexdigest()
    return (
        MEMORY_ROOT
        / "levels"
        / "L0_ground_zero"
        / "idempotency"
        / safe_memory_user_id(user_id)
        / f"{digest}.json"
    )


def _read_ground_zero_receipt(path: Path) -> dict | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("schema") != "daemon.l0-idempotency.v1"
        or data.get("status") not in {"pending", "completed"}
    ):
        raise ValueError("invalid ground zero idempotency receipt")
    return data


def _write_ground_zero_receipt(
    path: Path,
    receipt: dict,
    *,
    status: str,
) -> None:
    updated = dict(receipt)
    updated["status"] = status
    atomic_write_text(
        path,
        json.dumps(
            updated,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )
    path.chmod(0o600)


def _reference_path(
    reference: GroundZeroReference,
    *,
    memory_root: Path | None = None,
) -> Path:
    relative = Path(reference.path)
    if relative.is_absolute():
        raise ValueError("absolute ground zero reference")
    root = (MEMORY_ROOT if memory_root is None else Path(memory_root)).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "ground zero reference outside memory root"
        ) from exc
    return path


def _reference_bytes_match(
    path: Path,
    reference: GroundZeroReference,
) -> bool:
    try:
        with path.open("rb") as handle:
            handle.seek(reference.offset)
            encoded = handle.read(reference.length)
    except FileNotFoundError:
        return False
    return (
        len(encoded) == reference.length
        and hashlib.sha256(encoded).hexdigest() == reference.sha256
    )


def _append_at_expected_offset(
    path: Path,
    offset: int,
    encoded: bytes,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    size = path.stat().st_size if path.exists() else 0
    if size != offset:
        raise ValueError(
            "ground zero changed after idempotency receipt preparation"
        )
    with path.open("ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def read_ground_zero_reference(
    reference: GroundZeroReference | dict,
    *,
    memory_root: Path | None = None,
) -> str:
    """Read and verify the exact message body addressed by an L0 reference."""

    return read_ground_zero_record(reference, memory_root=memory_root).body


def read_ground_zero_record(
    reference: GroundZeroReference | dict,
    *,
    memory_root: Path | None = None,
) -> GroundZeroRecord:
    """Read and verify one complete L0 entry addressed by a stable reference."""

    ref = (
        reference
        if isinstance(reference, GroundZeroReference)
        else GroundZeroReference.from_dict(reference)
    )
    if (
        not ref.path
        or ref.offset < 0
        or ref.length <= 0
        or not ref.sha256
    ):
        raise ValueError("incomplete ground zero reference")
    path = _reference_path(ref, memory_root=memory_root)
    with path.open("rb") as handle:
        handle.seek(ref.offset)
        encoded = handle.read(ref.length)
    if len(encoded) != ref.length:
        raise ValueError("truncated ground zero reference")
    if hashlib.sha256(encoded).hexdigest() != ref.sha256:
        raise ValueError("ground zero reference hash mismatch")
    text = encoded.decode("utf-8")
    heading, separator, body = text.lstrip("\n").partition("\n\n")
    if not separator or not heading.startswith("## ") or " — " not in heading:
        raise ValueError("invalid ground zero entry")
    timestamp, actual_role = heading[3:].rsplit(" — ", 1)
    timestamp = timestamp.strip()
    actual_role = actual_role.strip()
    if not timestamp or not actual_role:
        raise ValueError("invalid ground zero entry heading")
    if ref.role and actual_role != ref.role:
        raise ValueError("ground zero reference role mismatch")
    return GroundZeroRecord(
        timestamp=timestamp,
        role=actual_role,
        body=body[:-1] if body.endswith("\n") else body,
    )


def write_user_context(user_id: str, content: str) -> Path:
    path = user_context_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def write_user_preferences(user_id: str, content: str) -> Path:
    path = user_preferences_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def write_user_agenda(user_id: str, content: str) -> Path:
    path = user_agenda_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def write_active_context(user_id: str, content: str) -> Path:
    path = active_context_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def write_immediate_focus(user_id: str, content: str) -> Path:
    path = immediate_focus_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def write_conversation_state(user_id: str, content: str) -> Path:
    path = conversation_state_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def write_current_discussion(user_id: str, content: str) -> Path:
    return write_immediate_focus(user_id, content)


def write_moment_memory(user_id: str, content: str) -> Path:
    return write_active_context(user_id, content)


def reset_ground_zero_conversation(user_id: str) -> tuple[Path, bool]:
    path = ground_zero_conversation_path(user_id)
    if path.exists():
        path.unlink()
        return path, True
    return path, False


def reset_user_context(user_id: str) -> tuple[Path, bool]:
    path = user_context_path(user_id)
    if path.exists():
        path.unlink()
        return path, True
    return path, False


def reset_user_preferences(user_id: str) -> tuple[Path, bool]:
    path = user_preferences_path(user_id)
    if path.exists():
        path.unlink()
        return path, True
    return path, False


def reset_user_agenda(user_id: str) -> tuple[Path, bool]:
    path = user_agenda_path(user_id)
    if path.exists():
        path.unlink()
        return path, True
    return path, False


def reset_immediate_focus(user_id: str) -> tuple[Path, bool]:
    path = immediate_focus_path(user_id)
    legacy_path = legacy_current_discussion_path(user_id)
    deleted = False
    if path.exists():
        path.unlink()
        deleted = True
    if legacy_path.exists():
        legacy_path.unlink()
        deleted = True
    return path, deleted


def reset_active_context(user_id: str) -> tuple[Path, bool]:
    path = active_context_path(user_id)
    legacy_path = legacy_moment_memory_path(user_id)
    deleted = False
    if path.exists():
        path.unlink()
        deleted = True
    if legacy_path.exists():
        legacy_path.unlink()
        deleted = True
    return path, deleted


def reset_current_discussion(user_id: str) -> tuple[Path, bool]:
    return reset_immediate_focus(user_id)


def reset_moment_memory(user_id: str) -> tuple[Path, bool]:
    return reset_active_context(user_id)
