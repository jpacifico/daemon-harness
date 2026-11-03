from __future__ import annotations

from datetime import datetime

import pytest

from daemon_core.common_ground_library import (
    COMMON_GROUND_CONSUMPTION_CONTRACT,
    COMMON_GROUND_LIBRARY_ENV,
    COMMON_GROUND_LIBRARY_PLATFORMS_ENV,
    COMMON_GROUND_LIBRARY_USER_IDS_ENV,
    common_ground_library_enabled,
    resolve_common_ground_library,
)
from daemon_core.memory import writer
from daemon_core.memory.writer import (
    GroundZeroRecord,
    GroundZeroReference,
    read_ground_zero_record,
)
from daemon_core.sovereign_library import (
    isolated_active_memory_library,
    read_exact_hot_memory,
)


USER_ID = "person-1"
CANDIDATE_ID = "a" * 64


def _reference(
    turn_id: str,
    role: str,
    *,
    user_id: str = USER_ID,
    offset: int = 0,
) -> GroundZeroReference:
    return GroundZeroReference(
        path=(
            "levels/L0_ground_zero/conversations/"
            f"{user_id}_2026-07-28.md"
        ),
        offset=offset,
        length=100,
        sha256=turn_id.ljust(64, "0")[:64],
        role=role,
        turn_id=turn_id,
    )


def _register_entry(
    *,
    state: str = "reinforced",
    candidate_id: str = CANDIDATE_ID,
    origin_user: GroundZeroReference | None = None,
    latest_user: GroundZeroReference | None = None,
) -> dict:
    assistant = _reference("assistant-origin", "assistant", offset=10)
    origin_user = origin_user or _reference("user-origin", "user", offset=20)
    latest_user = latest_user or _reference("user-latest", "user", offset=30)
    action = "revise" if state == "revised" else "reinforce"
    return {
        "candidate_id": candidate_id,
        "state": state,
        "shared_expression": "atelier nocturne",
        "shared_meaning": "Un espace de création partagé le soir.",
        "origin": {
            "event_id": "origin-event",
            "provenance": {
                "prior_assistant_l0": assistant.as_dict(),
                "current_user_l0": origin_user.as_dict(),
            },
        },
        "latest_evidence": {
            "event_id": "latest-event",
            "action": action,
            "provenance": {
                "current_user_l0": latest_user.as_dict(),
            },
        },
        "evidence_event_ids": ["origin-event", "latest-event"],
    }


def _record_reader(reference: GroundZeroReference | dict) -> GroundZeroRecord:
    ref = (
        reference
        if isinstance(reference, GroundZeroReference)
        else GroundZeroReference.from_dict(reference)
    )
    records = {
        "assistant-origin": GroundZeroRecord(
            timestamp="2026-07-28T20:40:00+02:00",
            role="assistant",
            body="On pourrait appeler cet espace notre atelier nocturne.",
        ),
        "user-origin": GroundZeroRecord(
            timestamp="2026-07-28T20:40:10+02:00",
            role="user",
            body="Oui, notre atelier nocturne.",
        ),
        "user-latest": GroundZeroRecord(
            timestamp="2026-07-28T21:38:37+02:00",
            role="user",
            body="Je serais mieux au frais dans notre atelier nocturne.",
        ),
    }
    return records[ref.turn_id]


def test_resolves_only_consolidated_lineage_with_exact_l0_episode():
    resolution = resolve_common_ground_library(
        user_id=USER_ID,
        register=[_register_entry()],
        read_record=_record_reader,
    )

    assert resolution.rejections == ()
    assert resolution.ignored_entries == 0
    assert len(resolution.entries) == 1
    entry = resolution.entries[0]
    assert entry.library_name == f"common_ground/shared/{CANDIDATE_ID}"
    assert "On pourrait appeler cet espace notre atelier nocturne." in entry.content
    assert "Je serais mieux au frais dans notre atelier nocturne." in entry.content
    assert "2026-07-28T20:40:00+02:00" in entry.content
    assert 'an independent source for each element' in entry.content
    assert "the origin" in COMMON_GROUND_CONSUMPTION_CONTRACT
    assert "materially change its relational meaning" in (
        COMMON_GROUND_CONSUMPTION_CONTRACT
    )
    assert "local reading exhausts its meaning" in (
        COMMON_GROUND_CONSUMPTION_CONTRACT
    )
    assert "The current\nmessage" in COMMON_GROUND_CONSUMPTION_CONTRACT
    assert "do not read the episode" in COMMON_GROUND_CONSUMPTION_CONTRACT


def test_candidate_is_ignored_and_cross_user_reference_is_rejected():
    candidate = _register_entry(state="candidate")
    cross_user = _register_entry(
        origin_user=_reference(
            "user-origin",
            "user",
            user_id="another-person",
            offset=20,
        )
    )

    resolution = resolve_common_ground_library(
        user_id=USER_ID,
        register=[candidate, cross_user],
        read_record=_record_reader,
    )

    assert resolution.entries == ()
    assert resolution.ignored_entries == 1
    assert [item.reason_code for item in resolution.rejections] == [
        "cross_user_l0_reference"
    ]


def test_zero_limit_reads_nothing():
    called = False

    def reader(_reference):
        nonlocal called
        called = True
        return _record_reader(_reference)

    resolution = resolve_common_ground_library(
        user_id=USER_ID,
        register=[_register_entry()],
        read_record=reader,
        limit=0,
    )

    assert resolution.entries == ()
    assert resolution.ignored_entries == 1
    assert called is False


def test_read_canary_is_bounded_by_identity_and_platform(monkeypatch):
    monkeypatch.setenv(COMMON_GROUND_LIBRARY_ENV, "canary")
    monkeypatch.setenv(COMMON_GROUND_LIBRARY_USER_IDS_ENV, "person-1")
    monkeypatch.setenv(COMMON_GROUND_LIBRARY_PLATFORMS_ENV, "telegram")

    assert common_ground_library_enabled("person-1", "telegram") is True
    assert common_ground_library_enabled("person-1", "openclaw") is False
    assert common_ground_library_enabled("another-person", "telegram") is False


def test_unresolvable_l0_rejects_entry_without_summary_fallback():
    def unavailable(_reference):
        raise ValueError("hash mismatch")

    resolution = resolve_common_ground_library(
        user_id=USER_ID,
        register=[_register_entry()],
        read_record=unavailable,
    )

    assert resolution.entries == ()
    assert [item.reason_code for item in resolution.rejections] == [
        "unresolvable_l0_reference"
    ]


def test_revised_lineage_requires_matching_latest_action():
    valid = _register_entry(state="revised")
    invalid = _register_entry(state="revised", candidate_id="b" * 64)
    invalid["latest_evidence"]["action"] = "reinforce"

    resolution = resolve_common_ground_library(
        user_id=USER_ID,
        register=[valid, invalid],
        read_record=_record_reader,
    )

    assert [entry.state for entry in resolution.entries] == ["revised"]
    assert [item.reason_code for item in resolution.rejections] == [
        "state_action_mismatch"
    ]


def test_sourced_entry_can_join_the_existing_exact_library(tmp_path):
    resolution = resolve_common_ground_library(
        user_id=USER_ID,
        register=[_register_entry()],
        read_record=_record_reader,
    )

    with isolated_active_memory_library(
        tmp_path,
        additional_entries=resolution.library_entries,
    ) as library:
        assert library.souvenir_names == (
            f"common_ground/shared/{CANDIDATE_ID}",
        )
        assert "subjects: atelier nocturne" in library.catalog_text
        exact = read_exact_hot_memory(library, list(library.souvenir_names))
        assert 'Exact Original Episode' in exact.text


def test_additional_library_entry_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError, match="Invalid active-memory"):
        with isolated_active_memory_library(
            tmp_path,
            additional_entries=(("../outside", "body"),),
        ):
            pass


def test_ground_zero_record_exposes_verified_timestamp_role_and_body(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(writer, "MEMORY_ROOT", tmp_path)
    reference = writer.append_ground_zero(
        USER_ID,
        "user",
        "Une parole exacte.",
        turn_id="turn-1",
        return_reference=True,
    )

    record = read_ground_zero_record(reference)

    assert record.timestamp
    assert record.role == "user"
    assert record.body == "Une parole exacte."


def test_ground_zero_write_can_preserve_original_occurrence_date(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(writer, "MEMORY_ROOT", tmp_path)
    occurred_at = datetime(2026, 8, 28, 5, 15, 20).timestamp()

    reference = writer.append_ground_zero(
        USER_ID,
        "user",
        "Une parole historique.",
        occurred_at=occurred_at,
        return_reference=True,
    )
    record = read_ground_zero_record(reference)

    assert reference.path.endswith("_2026-08-28.md")
    assert record.timestamp == "2026-08-28T05:15:20"
