"""Dry-run planning for one sourced open collection declaration.

The MemoryAgent owns the semantic decision. This module only validates the
target and exact L0 provenance, renders frontmatter in memory, and proves that
the staged graph remains healthy. It does not write the vault.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from daemon_core.memory.graph import (
    CollectionState,
    MemoryGraphIndex,
    audit_memory_graph,
    build_memory_graph,
    normalize_souvenir_name,
    normalize_source_ref,
    parse_souvenir_graph,
    source_ref_exists,
)
from daemon_core.memory.metadata import (
    parse_souvenir_frontmatter,
    with_open_collection,
)
from daemon_core.memory.reader import is_system_managed_souvenir


DECLARE_OPEN = "declare_open"


class CollectionPublicationError(ValueError):
    """Raised when a declaration cannot be staged without weakening integrity."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "collection_publication_error",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class CollectionDeclaration:
    action: str
    target: str
    source_ref: str


@dataclass(frozen=True)
class CollectionDeclarationPlan:
    declaration: CollectionDeclaration
    target_path: Path
    current_hash: str
    next_hash: str
    current_content: str
    next_content: str
    collection: CollectionState
    baseline_index: MemoryGraphIndex

    @property
    def changed(self) -> bool:
        return self.current_hash != self.next_hash


def plan_collection_declaration(
    *,
    souvenirs_root: Path,
    memory_root: Path,
    declaration: CollectionDeclaration,
    previous_index: MemoryGraphIndex | None = None,
) -> CollectionDeclarationPlan:
    """Render and validate one declaration entirely in memory."""

    normalized = _validate_declaration(declaration)
    if is_system_managed_souvenir(normalized.target):
        raise CollectionPublicationError(
            "System-managed episodic scenes cannot become graph collections.",
            reason_code="system_managed_capsule",
        )
    target_path = _souvenir_path(souvenirs_root, normalized.target)
    if not target_path.is_file():
        raise CollectionPublicationError(
            f"Unknown collection capsule: {normalized.target}.",
            reason_code="unknown_collection_capsule",
        )
    if not source_ref_exists(memory_root, normalized.source_ref):
        raise CollectionPublicationError(
            f"Exact L0 source does not exist: {normalized.source_ref}.",
            reason_code="exact_l0_source_missing",
        )

    before = build_memory_graph(
        souvenirs_root,
        previous=previous_index,
        memory_root=memory_root,
    )
    current = target_path.read_text(encoding="utf-8")
    current_record = before.index.souvenirs.get(normalized.target)
    if current_record is None:
        raise CollectionPublicationError(
            f"Collection souvenir was not indexed: {normalized.target}.",
            reason_code="collection_capsule_not_indexed",
        )
    try:
        frontmatter, _body = parse_souvenir_frontmatter(current)
    except ValueError as exc:
        raise CollectionPublicationError(
            "Existing collection frontmatter is invalid.",
            reason_code="invalid_existing_collection_state",
        ) from exc
    if any(str(key) == "collection" or str(key).startswith("collection_") for key in frontmatter) and current_record.collection is None:
        raise CollectionPublicationError(
            "Existing collection metadata is invalid and requires maintenance.",
            reason_code="invalid_existing_collection_state",
        )
    if (
        current_record.collection is not None
        and current_record.collection.coverage == "closed"
    ):
        raise CollectionPublicationError(
            "A closed collection cannot be reopened implicitly.",
            reason_code="collection_already_closed",
        )

    source_refs = list(
        current_record.collection.source_refs
        if current_record.collection is not None
        else ()
    )
    if normalized.source_ref not in source_refs:
        source_refs.append(normalized.source_ref)
    try:
        from daemon_core.memory.library_layout import render_vault_links
        rendered = render_vault_links(memory_root, with_open_collection(current, source_refs=source_refs))
    except ValueError as exc:
        raise CollectionPublicationError(
            "Existing collection metadata is invalid.",
            reason_code="invalid_existing_collection_state",
        ) from exc
    staged_record = parse_souvenir_graph(normalized.target, rendered)
    staged_collection = staged_record.collection
    if (
        staged_collection is None
        or staged_collection.coverage != "open"
        or normalized.source_ref not in staged_collection.source_refs
    ):
        raise CollectionPublicationError(
            "Staged collection declaration is incomplete.",
            reason_code="staged_collection_declaration_failed",
        )

    records = dict(before.index.souvenirs)
    records[normalized.target] = staged_record
    staged_index = MemoryGraphIndex(
        library_fingerprint="staged",
        souvenirs=records,
    )
    staged_audit = audit_memory_graph(staged_index, memory_root=memory_root)
    new_issues = _issue_keys(staged_audit.issues) - _issue_keys(before.audit.issues)
    if new_issues:
        raise CollectionPublicationError(
            "Declaration introduces graph integrity issues: "
            + ", ".join(sorted(new_issues)[:4]),
            reason_code="graph_integrity_issue",
        )
    return CollectionDeclarationPlan(
        declaration=normalized,
        target_path=target_path,
        current_hash=_sha256_text(current),
        next_hash=_sha256_text(rendered),
        current_content=current,
        next_content=rendered,
        collection=staged_collection,
        baseline_index=before.index,
    )


def _validate_declaration(
    declaration: CollectionDeclaration,
) -> CollectionDeclaration:
    if declaration.action != DECLARE_OPEN:
        raise CollectionPublicationError(
            f"Unsupported collection action: {declaration.action!r}.",
            reason_code="unsupported_collection_action",
        )
    target = normalize_souvenir_name(declaration.target)
    source_ref = normalize_source_ref(declaration.source_ref)
    if target is None or target != declaration.target:
        raise CollectionPublicationError(
            "Invalid collection souvenir path.",
            reason_code="invalid_collection_capsule_path",
        )
    if source_ref is None:
        raise CollectionPublicationError(
            "Invalid exact L0 source reference.",
            reason_code="invalid_exact_l0_source",
        )
    return CollectionDeclaration(
        action=DECLARE_OPEN,
        target=target,
        source_ref=source_ref,
    )


def _souvenir_path(souvenirs_root: Path, name: str) -> Path:
    from daemon_core.memory.library_layout import vault_root, resolve_note
    root = souvenirs_root.resolve()
    if vault_root(root.parent) is not None:
        return resolve_note(root.parent, name)
    path = (root / f"{name}.md").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CollectionPublicationError(
            "Souvenir path escapes its root.",
            reason_code="capsule_path_escape",
        ) from exc
    return path


def _issue_keys(issues) -> set[str]:
    return {
        ":".join(
            (
                str(issue.souvenir),
                str(issue.code),
                str(issue.line),
                str(issue.target),
                str(issue.detail),
            )
        )
        for issue in issues
    }


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = [
    "CollectionDeclaration",
    "CollectionDeclarationPlan",
    "CollectionPublicationError",
    "DECLARE_OPEN",
    "plan_collection_declaration",
]
