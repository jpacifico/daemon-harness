import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.continuity import read_continuity_receipt
from daemon_core.evidence_observability import (
    observe_evidence_tool_result,
    observe_prepared_evidence,
)


def _enable_receipts(monkeypatch, tmp_path):
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPTS", "shadow")
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPT_DIR", str(tmp_path))


def _brief():
    blocks = (
        SimpleNamespace(function="identity", raw_span="IDENTITE PRIVEE"),
        SimpleNamespace(function="relationship", raw_span="RELATION PRIVEE"),
        SimpleNamespace(function="situation", raw_span="SITUATION PRIVEE"),
        SimpleNamespace(function="procedure", raw_span="PROCEDURE"),
        SimpleNamespace(function="evidence", raw_span="PREUVE PRIVEE"),
        SimpleNamespace(function="resource", raw_span="CATALOGUE PRIVE"),
    )
    near_blocks = (SimpleNamespace(function="procedure", raw_span="SEUIL DU TOUR"),)
    return SimpleNamespace(
        leading_blocks=blocks,
        near_turn_blocks=near_blocks,
        scene=SimpleNamespace(
            revision=17,
            claims=(object(), object()),
            dependencies=(object(),),
            sources=(object(), object(), object()),
            unresolved_source_ids=("source-missing",),
            dialogue_watermark_source_id="private-watermark",
        ),
        turn_evidence=SimpleNamespace(
            schema_version="daemon.turn-evidence.v1",
            present_claims=(object(),),
            open_dependencies=(object(),),
            material_unknowns=(),
            unabsorbed_dialogue=(object(),),
            opened_evidence=(object(), object()),
            available_sources=(object(), object()),
            capability_handles=(object(),),
            material_failures=(),
            source_unit_count=19,
            coverage_complete=True,
            renderer_ready=True,
        ),
        dialogue_coverage=SimpleNamespace(
            status="scene_delta_covered",
            recent_turn_ids=("recent-1", "recent-2"),
            scene_delta_turn_ids=("delta-1",),
            visible_turn_ids=("recent-1", "recent-2", "delta-1"),
            expected_turn_ids=("recent-1", "recent-2", "delta-1"),
            missing_turn_ids=(),
        ),
        near_term_timeline=SimpleNamespace(
            entries=(object(), object()),
            sources=(object(),),
            complete=True,
        ),
        evidence=SimpleNamespace(
            authoritative_sources=(SimpleNamespace(text="AGENDA PRIVE"),),
            opened_souvenirs=(
                SimpleNamespace(name="theme/famille", text="CAPSULE PRIVEE"),
            ),
        ),
        resources=SimpleNamespace(
            atlas=SimpleNamespace(
                souvenir_names=("theme/famille", "theme/projet"),
                rendered_chars=321,
            ),
        ),
        recent_dialogue=(SimpleNamespace(content="DIALOGUE PRIVE"),),
        temporal_frame=SimpleNamespace(raw="TEMPS PRIVE"),
        current_message=SimpleNamespace(content="MESSAGE PRIVE"),
    )


def test_prepared_evidence_receipt_records_topology_without_content(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)

    assert observe_prepared_evidence(
        "trace-evidence-prepared",
        _brief(),
        prompt_variant="voice_brief_reference",
    )

    receipt = read_continuity_receipt(
        "trace-evidence-prepared",
        directory=tmp_path,
    )
    details = receipt["stages"]["evidence_preparation"]["details"]
    assert details["scene_revision"] == 17
    assert details["scene_claim_count"] == 2
    assert details["scene_dependency_count"] == 1
    assert details["evidence_schema"] == "daemon.turn-evidence.v1"
    assert details["evidence_present_claim_count"] == 1
    assert details["evidence_dependency_count"] == 1
    assert details["evidence_unknown_count"] == 0
    assert details["evidence_failure_count"] == 0
    assert details["evidence_coverage_complete"] is True
    assert details["evidence_renderer_ready"] is True
    assert details["dialogue_delta_turns"] == 1
    assert details["dialogue_missing_turns"] == 0
    assert details["atlas_capsule_count"] == 2
    assert details["atlas_chars"] == 321
    assert details["context_paths"] == ["theme/famille"]
    assert details["near_term_entry_count"] == 2
    assert details["near_term_complete"] is True
    assert details["current_message_chars"] == len("MESSAGE PRIVE")

    raw = (tmp_path / "continuity_receipts.jsonl").read_text(encoding="utf-8")
    for private in (
        "IDENTITE PRIVEE",
        "AGENDA PRIVE",
        "CAPSULE PRIVEE",
        "DIALOGUE PRIVE",
        "MESSAGE PRIVE",
        "private-watermark",
    ):
        assert private not in raw


def test_tool_evidence_receipt_keeps_status_paths_and_no_evidence(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)

    assert observe_evidence_tool_result(
        "trace-evidence-tool",
        tool_name="daemon_read_souvenirs",
        arguments={"paths": ["theme/famille", "theme/missing"]},
        result={
            "status": "partial",
            "capsules": [{"path": "theme/famille", "content": "FAIT PERSONNEL PRIVE"}],
            "rejected_paths": ["theme/missing"],
            "total_chars": 42,
            "response_constraint": "BORNE PRIVEE",
        },
    )

    receipt = read_continuity_receipt(
        "trace-evidence-tool",
        directory=tmp_path,
    )
    stage = receipt["stages"]["evidence_tool"]
    assert stage["status"] == "completed"
    details = stage["details"]
    assert details["tool_name"] == "daemon_read_souvenirs"
    assert details["tool_status"] == "partial"
    assert details["requested_path_count"] == 2
    assert details["opened_path_count"] == 1
    assert details["rejected_path_count"] == 1
    assert details["read_paths"] == ["theme/famille"]
    assert details["rejected_paths"] == ["theme/missing"]
    assert details["result_chars"] == 42
    assert details["evidence_present"] is True
    assert details["response_constraint_present"] is True
    assert details["canonical_schema"] == "daemon.evidence-result.v1"
    assert details["canonical_status"] == "partial"
    assert details["canonical_evidence_count"] == 1
    assert details["canonical_source_ref_count"] == 1
    assert details["canonical_unresolved_count"] == 1
    assert details["canonical_response_constraint_present"] is True

    raw = (tmp_path / "continuity_receipts.jsonl").read_text(encoding="utf-8")
    assert "FAIT PERSONNEL PRIVE" not in raw
    assert "BORNE PRIVEE" not in raw


def test_tool_error_is_observed_as_a_noncritical_failed_transition(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)

    assert observe_evidence_tool_result(
        "trace-evidence-error",
        tool_name="daemon_recall_l0",
        arguments={},
        result={
            "status": "error",
            "evidence_kind": "conversation_chronology",
            "error_code": "forensic_lookup_failed",
            "response_constraint": "Ne rien inventer.",
        },
    )

    receipt = read_continuity_receipt(
        "trace-evidence-error",
        directory=tmp_path,
    )
    stage = receipt["stages"]["evidence_tool"]
    assert stage["status"] == "failed"
    assert stage["details"]["error_code"] == "forensic_lookup_failed"
    assert stage["details"]["evidence_kind"] == "conversation_chronology"
    assert stage["details"]["response_constraint_present"] is True
    assert stage["details"]["canonical_status"] == "error"
    assert stage["details"]["canonical_evidence_count"] == 0
    assert stage["details"]["canonical_error_code"] == "forensic_lookup_failed"
    assert receipt["status"] == "pending"


def test_canonical_projection_does_not_leak_l0_quote_or_resolution(
    monkeypatch,
    tmp_path,
):
    _enable_receipts(monkeypatch, tmp_path)

    assert observe_evidence_tool_result(
        "trace-evidence-l0",
        tool_name="daemon_recall_l0",
        arguments={"need": "DESTINATION PRIVEE"},
        result={
            "status": "found",
            "evidence_kind": "personal_fact",
            "source_file": "user_private_day.md",
            "evidence": "CITATION L0 PRIVEE",
            "relation_status": "not_established",
            "conclusion": "CONCLUSION PRIVEE",
            "response_constraint": "BORNE PRIVEE",
        },
    )

    receipt = read_continuity_receipt(
        "trace-evidence-l0",
        directory=tmp_path,
    )
    details = receipt["stages"]["evidence_tool"]["details"]
    assert details["canonical_status"] == "complete"
    assert details["canonical_evidence_count"] == 1
    assert details["canonical_resolution_status"] == "not_established"
    assert details["canonical_error_code"] == ""

    raw = (tmp_path / "continuity_receipts.jsonl").read_text(encoding="utf-8")
    for private in (
        "DESTINATION PRIVEE",
        "CITATION L0 PRIVEE",
        "CONCLUSION PRIVEE",
        "BORNE PRIVEE",
        "user_private_day.md",
    ):
        assert private not in raw
