"""Host-neutral projection of exact-source tool results.

The first consumer is shadow observability.  Historical tool payloads remain
unchanged until a separately evaluated renderer proves that the common
envelope is safe for the voice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EVIDENCE_RESULT_SCHEMA = "daemon.evidence-result.v1"
EVIDENCE_RESULT_STATUSES = frozenset(
    {"complete", "partial", "not_found", "error"}
)

_SOUVENIR_TOOL = "daemon_read_souvenirs"
_CONNECTED_SOURCE_TOOL = "daemon_read_source"
_L0_RECALL_TOOL = "daemon_recall_l0"


@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    source_ref: str
    content: str
    authority: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "source_ref": self.source_ref,
            "content": self.content,
            "authority": self.authority,
        }


@dataclass(frozen=True)
class EvidenceResolution:
    relation_status: str = ""
    conclusion: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "relation_status": self.relation_status,
            "conclusion": self.conclusion,
        }


@dataclass(frozen=True)
class EvidenceResult:
    status: str
    evidence: tuple[EvidenceItem, ...] = ()
    source_refs: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    response_constraint: str = ""
    resolution: EvidenceResolution = field(default_factory=EvidenceResolution)
    error_code: str = ""
    schema_version: str = EVIDENCE_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.status not in EVIDENCE_RESULT_STATUSES:
            raise ValueError(f"Unsupported evidence status: {self.status}")
        if self.status in {"not_found", "error"} and self.evidence:
            raise ValueError(f"{self.status} cannot carry evidence")
        item_refs = _unique_strings(item.source_ref for item in self.evidence)
        if item_refs != self.source_refs:
            raise ValueError("source_refs must exactly cover evidence items")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "evidence": [item.as_dict() for item in self.evidence],
            "source_refs": list(self.source_refs),
            "unresolved": list(self.unresolved),
            "response_constraint": self.response_constraint,
            "resolution": self.resolution.as_dict(),
            "error_code": self.error_code,
        }


def project_evidence_tool_result(
    tool_name: str,
    *,
    arguments: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> EvidenceResult:
    """Project one historical tool payload without reading another source."""

    name = str(tool_name).strip()
    args = arguments if isinstance(arguments, dict) else {}
    payload = result if isinstance(result, dict) else {}
    if name == _SOUVENIR_TOOL:
        return _project_souvenirs(args, payload)
    if name == _CONNECTED_SOURCE_TOOL:
        return _project_connected_source(args, payload)
    if name == _L0_RECALL_TOOL:
        if str(payload.get("status") or "").strip() == "capsule_sources":
            return _project_souvenirs(args, payload)
        return _project_l0(args, payload)
    return _error_result(
        unresolved=(name or "unknown_tool",),
        error_code="unknown_evidence_tool",
    )


def _project_souvenirs(
    arguments: dict[str, Any],
    payload: dict[str, Any],
) -> EvidenceResult:
    requested = _string_tuple(arguments.get("paths"))
    raw_souvenirs = payload.get("capsules")
    items: list[EvidenceItem] = []
    if isinstance(raw_souvenirs, list):
        for raw in raw_souvenirs:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("path") or "").strip()
            content = raw.get("content")
            if not path or not isinstance(content, str) or not content.strip():
                continue
            items.append(
                EvidenceItem(
                    kind="personal_memory",
                    source_ref=path,
                    content=content,
                    authority="exact_capsule",
                )
            )
    evidence = tuple(items)
    read_refs = _unique_strings(item.source_ref for item in evidence)
    rejected = _string_tuple(payload.get("rejected_paths"))
    missing_requested = tuple(
        path for path in requested if path not in read_refs and path not in rejected
    )
    unresolved = _unique_strings((*rejected, *missing_requested))
    legacy_status = str(payload.get("status") or "error").strip().lower()

    if legacy_status in {"error", "unavailable"}:
        return _error_result(
            unresolved=unresolved or requested,
            response_constraint=_response_constraint(payload),
            error_code=_error_code(payload, legacy_status),
        )
    if not evidence:
        if legacy_status in {"not_found", "partial", "ok", "capsule_sources"}:
            return EvidenceResult(
                status="not_found",
                unresolved=unresolved or requested,
                response_constraint=_response_constraint(payload),
            )
        return _error_result(
            unresolved=unresolved or requested,
            response_constraint=_response_constraint(payload),
            error_code="invalid_tool_result",
        )

    partial = legacy_status == "partial" or bool(unresolved)
    if legacy_status not in {"ok", "partial", "capsule_sources"}:
        return _error_result(
            unresolved=unresolved or requested,
            response_constraint=_response_constraint(payload),
            error_code="invalid_tool_result",
        )
    constraint = _response_constraint(payload)
    return EvidenceResult(
        status="partial" if partial else "complete",
        evidence=evidence,
        source_refs=read_refs,
        unresolved=unresolved,
        response_constraint=constraint,
    )


def _project_connected_source(
    arguments: dict[str, Any],
    payload: dict[str, Any],
) -> EvidenceResult:
    requested_source = str(arguments.get("source") or "").strip()
    legacy_status = str(payload.get("status") or "error").strip().lower()
    if legacy_status in {"error", "unavailable"}:
        return _error_result(
            unresolved=(requested_source,) if requested_source else (),
            response_constraint=_response_constraint(payload),
            error_code=_error_code(payload, legacy_status),
        )
    if legacy_status == "not_found":
        return EvidenceResult(
            status="not_found",
            unresolved=(requested_source,) if requested_source else (),
            response_constraint=_response_constraint(payload),
        )

    source = str(payload.get("source") or requested_source).strip()
    locator = str(payload.get("locator") or source).strip()
    content = payload.get("content")
    if (
        legacy_status not in {"ok", "partial"}
        or not source
        or not locator
        or not isinstance(content, str)
        or not content.strip()
    ):
        return _error_result(
            unresolved=(requested_source,) if requested_source else (),
            response_constraint=_response_constraint(payload),
            error_code="invalid_tool_result",
        )
    item = EvidenceItem(
        kind="connected_source_snapshot",
        source_ref=locator,
        content=content,
        authority="external_snapshot",
    )
    truncated = legacy_status == "partial" or payload.get("truncated") is True
    unresolved = (f"{locator}#truncated-tail",) if truncated else ()
    return EvidenceResult(
        status="partial" if truncated else "complete",
        evidence=(item,),
        source_refs=(locator,),
        unresolved=unresolved,
        response_constraint=_response_constraint(payload),
    )


def _project_l0(
    arguments: dict[str, Any],
    payload: dict[str, Any],
) -> EvidenceResult:
    need = str(arguments.get("need") or "").strip()
    legacy_status = str(payload.get("status") or "error").strip().lower()
    response_constraint = _response_constraint(payload)
    resolution = EvidenceResolution(
        relation_status=str(payload.get("relation_status") or "").strip(),
        conclusion=str(payload.get("conclusion") or "").strip(),
    )
    if legacy_status in {"error", "unavailable"}:
        return _error_result(
            unresolved=(need,) if need else (),
            response_constraint=response_constraint,
            error_code=_error_code(payload, legacy_status),
            resolution=resolution,
        )
    if legacy_status in {
        "not_found",
        "invalid_evidence",
        "abstained",
        "not_attempted",
    }:
        return EvidenceResult(
            status="not_found",
            unresolved=(need,) if need else (),
            response_constraint=response_constraint,
            resolution=resolution,
        )
    if legacy_status not in {"found", "partial"}:
        return _error_result(
            unresolved=(need,) if need else (),
            response_constraint=response_constraint,
            error_code="invalid_tool_result",
            resolution=resolution,
        )

    source_ref = str(payload.get("source_file") or "").strip()
    content = payload.get("evidence")
    kind = str(payload.get("evidence_kind") or "personal_fact").strip()
    if not source_ref or not isinstance(content, str) or not content.strip():
        return _error_result(
            unresolved=(need,) if need else (),
            response_constraint=response_constraint,
            error_code="invalid_tool_result",
            resolution=resolution,
        )
    item = EvidenceItem(
        kind=kind,
        source_ref=source_ref,
        content=content,
        authority="exact_user_l0",
    )
    return EvidenceResult(
        status="partial" if legacy_status == "partial" else "complete",
        evidence=(item,),
        source_refs=(source_ref,),
        unresolved=(need,) if legacy_status == "partial" and need else (),
        response_constraint=response_constraint,
        resolution=resolution,
    )


def _error_result(
    *,
    unresolved: tuple[str, ...] = (),
    response_constraint: str = "",
    error_code: str,
    resolution: EvidenceResolution | None = None,
) -> EvidenceResult:
    return EvidenceResult(
        status="error",
        unresolved=_unique_strings(unresolved),
        response_constraint=response_constraint,
        resolution=resolution or EvidenceResolution(),
        error_code=error_code,
    )


def _response_constraint(payload: dict[str, Any]) -> str:
    for key in (
        "response_constraint",
        "coverage_checkpoint",
        "boundary",
        "reason",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _error_code(payload: dict[str, Any], legacy_status: str) -> str:
    value = str(payload.get("error_code") or "").strip()
    if value:
        return value
    return "tool_unavailable" if legacy_status == "unavailable" else "tool_error"


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return _unique_strings(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )


def _unique_strings(values) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in result:
            result.append(item)
    return tuple(result)


__all__ = [
    "EVIDENCE_RESULT_SCHEMA",
    "EVIDENCE_RESULT_STATUSES",
    "EvidenceItem",
    "EvidenceResolution",
    "EvidenceResult",
    "project_evidence_tool_result",
]
