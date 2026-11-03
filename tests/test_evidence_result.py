import pytest

from daemon_core.evidence_result import (
    EVIDENCE_RESULT_SCHEMA,
    EvidenceItem,
    EvidenceResult,
    project_evidence_tool_result,
)


def test_souvenir_projection_preserves_exact_bodies_paths_and_partial_gap():
    projected = project_evidence_tool_result(
        "daemon_read_souvenirs",
        arguments={"paths": ["theme/family", "theme/missing"]},
        result={
            "status": "partial",
            "capsules": [
                {"path": "theme/family", "content": "# Family\nExact fact."}
            ],
            "rejected_paths": ["theme/missing"],
            "coverage_checkpoint": "Keep the unresolved participant explicit.",
        },
    )

    assert projected.status == "partial"
    assert projected.evidence == (
        EvidenceItem(
            kind="personal_memory",
            source_ref="theme/family",
            content="# Family\nExact fact.",
            authority="exact_capsule",
        ),
    )
    assert projected.source_refs == ("theme/family",)
    assert projected.unresolved == ("theme/missing",)
    assert projected.response_constraint.startswith("Keep the unresolved")


def test_complete_souvenir_projection_has_no_unresolved_piece():
    projected = project_evidence_tool_result(
        "daemon_read_souvenirs",
        arguments={"paths": ["theme/a"]},
        result={
            "status": "ok",
            "capsules": [{"path": "theme/a", "content": "Exact."}],
            "rejected_paths": [],
        },
    )

    assert projected.status == "complete"
    assert projected.unresolved == ()
    assert projected.as_dict()["schema_version"] == EVIDENCE_RESULT_SCHEMA


def test_l0_preflight_souvenirs_use_the_same_evidence_shape():
    projected = project_evidence_tool_result(
        "daemon_recall_l0",
        arguments={"need": "old preference"},
        result={
            "status": "capsule_sources",
            "reason": "Read durable sources before L0.",
            "capsules": [
                {"path": "theme/preferences", "content": "Exact preference."}
            ],
            "rejected_paths": [],
        },
    )

    assert projected.status == "complete"
    assert projected.evidence[0].authority == "exact_capsule"
    assert projected.response_constraint == "Read durable sources before L0."


def test_connected_source_projection_separates_snapshot_and_truncated_tail():
    projected = project_evidence_tool_result(
        "daemon_read_source",
        arguments={"source": "professional_calendar"},
        result={
            "status": "partial",
            "source": "professional_calendar",
            "locator": "agenda_pro.md",
            "content": "# Agenda\n- 15:00 Review",
            "truncated": True,
            "boundary": "Do not infer events outside this window.",
        },
    )

    assert projected.status == "partial"
    assert projected.evidence[0].kind == "connected_source_snapshot"
    assert projected.evidence[0].content.endswith("15:00 Review")
    assert projected.source_refs == ("agenda_pro.md",)
    assert projected.unresolved == ("agenda_pro.md#truncated-tail",)


def test_retired_professional_calendar_bridge_is_not_an_evidence_tool():
    projected = project_evidence_tool_result(
        "read_professional_calendar",
        arguments={},
        result={
            "status": "ok",
            "kind": "professional_calendar_entries",
            "source": "outlook_calendar",
            "content": "# Agenda\n\n## Aujourd'hui\n- 10:00 Daily",
            "truncated": False,
        },
    )

    assert projected.status == "error"
    assert projected.error_code == "unknown_evidence_tool"
    assert projected.evidence == ()
    assert projected.unresolved == ("read_professional_calendar",)


def test_l0_projection_keeps_exact_quote_separate_from_resolution():
    projected = project_evidence_tool_result(
        "daemon_recall_l0",
        arguments={"need": "where the final would be watched"},
        result={
            "status": "found",
            "evidence_kind": "personal_fact",
            "source_file": "u_2026-07-19.md",
            "evidence": "I will watch it at the airport.",
            "relation_status": "not_established",
            "conclusion": "The airport is established, not the plane.",
            "response_constraint": "Do not confirm the plane.",
        },
    )

    assert projected.status == "complete"
    assert projected.evidence[0].content == "I will watch it at the airport."
    assert projected.evidence[0].authority == "exact_user_l0"
    assert projected.resolution.relation_status == "not_established"
    assert projected.resolution.conclusion.endswith("not the plane.")
    assert projected.resolution.conclusion not in projected.evidence[0].content


def test_partial_l0_projection_keeps_quote_and_marks_need_unresolved():
    projected = project_evidence_tool_result(
        "daemon_recall_l0",
        arguments={"need": "return destination"},
        result={
            "status": "partial",
            "evidence_kind": "personal_fact",
            "source_file": "u_2026-07-01.md",
            "evidence": "We are returning.",
            "relation_status": "ambiguous",
            "conclusion": "",
            "response_constraint": "Use only the exact quote.",
        },
    )

    assert projected.status == "partial"
    assert projected.evidence[0].content == "We are returning."
    assert projected.unresolved == ("return destination",)


@pytest.mark.parametrize("legacy_status", ["not_found", "invalid_evidence", "abstained"])
def test_unproven_l0_statuses_never_create_evidence(legacy_status):
    projected = project_evidence_tool_result(
        "daemon_recall_l0",
        arguments={"need": "missing fact"},
        result={
            "status": legacy_status,
            "source_file": "private.md",
            "evidence": "UNTRUSTED",
            "relation_status": "not_established",
            "conclusion": "",
        },
    )

    assert projected.status == "not_found"
    assert projected.evidence == ()
    assert projected.source_refs == ()
    assert projected.unresolved == ("missing fact",)


def test_errors_and_unavailability_remain_material_errors():
    failed = project_evidence_tool_result(
        "daemon_recall_l0",
        arguments={"need": "missing fact"},
        result={
            "status": "error",
            "error_code": "forensic_lookup_failed",
            "response_constraint": "Do not infer it.",
        },
    )
    unavailable = project_evidence_tool_result(
        "daemon_read_source",
        arguments={"source": "professional_calendar"},
        result={"status": "unavailable"},
    )

    assert failed.status == "error"
    assert failed.error_code == "forensic_lookup_failed"
    assert failed.evidence == ()
    assert unavailable.status == "error"
    assert unavailable.error_code == "tool_unavailable"


def test_malformed_success_fails_closed_instead_of_claiming_completion():
    projected = project_evidence_tool_result(
        "daemon_read_source",
        arguments={"source": "professional_calendar"},
        result={"status": "ok", "content": ""},
    )

    assert projected.status == "error"
    assert projected.error_code == "invalid_tool_result"
    assert projected.evidence == ()


def test_result_rejects_a_not_found_state_that_carries_evidence():
    with pytest.raises(ValueError, match="cannot carry evidence"):
        EvidenceResult(
            status="not_found",
            evidence=(
                EvidenceItem(
                    kind="personal_memory",
                    source_ref="theme/a",
                    content="Exact.",
                    authority="exact_capsule",
                ),
            ),
            source_refs=("theme/a",),
        )
