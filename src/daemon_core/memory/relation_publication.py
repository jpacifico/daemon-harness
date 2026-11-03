"""Transactional publication of one sourced memory membership relation.

Each relation lives in one authoritative Markdown souvenir and is therefore
published with one atomic file replacement. The graph index is derived and may
be regenerated after a crash; replaying the same mutation is idempotent.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from daemon_core.memory.graph import (
    MEMBERSHIP_RELATION,
    CollectionState,
    MemoryGraphIndex,
    audit_memory_graph,
    build_memory_graph,
    normalize_souvenir_name,
    normalize_source_ref,
    parse_souvenir_graph,
    source_ref_exists,
    write_memory_graph_index,
)
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.obsidian import obsidian_format_enabled
from daemon_core.memory.reader import is_system_managed_souvenir


ALLOWED_MUTATION_ACTIONS = frozenset({"upsert", "remove"})
MAX_RELATION_MUTATION_BATCH = 8


class RelationPublicationError(ValueError):
    """Raised when a mutation cannot be published without weakening integrity."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "relation_publication_error",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class RelationMutation:
    action: str
    source: str
    target: str
    role: str
    source_ref: str


@dataclass(frozen=True)
class RelationMutationPlan:
    mutation: RelationMutation
    source_path: Path
    current_hash: str
    next_hash: str
    current_content: str
    next_content: str
    baseline_issue_keys: tuple[str, ...]
    baseline_index: MemoryGraphIndex
    staged_collection_targets: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.current_hash != self.next_hash


@dataclass(frozen=True)
class RelationPublicationResult:
    status: str
    source: str
    target: str
    index_fingerprint: str


@dataclass(frozen=True)
class RelationMutationBatchSourcePlan:
    source: str
    source_path: Path
    current_hash: str
    next_hash: str
    current_content: str
    next_content: str

    @property
    def changed(self) -> bool:
        return self.current_hash != self.next_hash


@dataclass(frozen=True)
class RelationMutationBatchPlan:
    mutations: tuple[RelationMutation, ...]
    source_plans: tuple[RelationMutationBatchSourcePlan, ...]
    mutation_changed: tuple[bool, ...]
    baseline_issue_keys: tuple[str, ...]
    baseline_index: MemoryGraphIndex
    staged_collection_targets: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return any(source.changed for source in self.source_plans)


@dataclass(frozen=True)
class RelationMutationBatchResult:
    status: str
    published_count: int
    planned_count: int
    unchanged_count: int
    index_fingerprint: str
    items: tuple[RelationPublicationResult, ...]


def _reject_system_managed_relation(mutation: RelationMutation) -> None:
    if is_system_managed_souvenir(
        mutation.source
    ) or is_system_managed_souvenir(mutation.target):
        raise RelationPublicationError(
            "System-managed episodic scenes cannot carry graph mutations.",
            reason_code="system_managed_capsule",
        )


def plan_relation_mutation(
    *,
    souvenirs_root: Path,
    memory_root: Path,
    mutation: RelationMutation,
    previous_index: MemoryGraphIndex | None = None,
    staged_collections: Mapping[str, CollectionState] | None = None,
) -> RelationMutationPlan:
    """Prepare and validate one mutation entirely in memory."""

    normalized = _validate_mutation(mutation)
    _reject_system_managed_relation(normalized)
    source_path = _souvenir_path(souvenirs_root, normalized.source)
    target_path = _souvenir_path(souvenirs_root, normalized.target)
    if not source_path.is_file():
        raise RelationPublicationError(
            f"Unknown source capsule: {normalized.source}.",
            reason_code="unknown_source_capsule",
        )
    if not target_path.is_file():
        raise RelationPublicationError(
            f"Unknown target capsule: {normalized.target}.",
            reason_code="unknown_target_capsule",
        )
    if not source_ref_exists(memory_root, normalized.source_ref):
        raise RelationPublicationError(
            f"Exact L0 source does not exist: {normalized.source_ref}.",
            reason_code="exact_l0_source_missing",
        )

    before = build_memory_graph(
        souvenirs_root,
        previous=previous_index,
        memory_root=memory_root,
    )
    effective_index = _with_staged_collections(
        before.index,
        staged_collections or {},
        memory_root=memory_root,
    )
    target_record = effective_index.souvenirs.get(normalized.target)
    if target_record is None or target_record.collection is None:
        raise RelationPublicationError(
            f"Target is not a declared collection: {normalized.target}.",
            reason_code="target_not_declared_collection",
        )
    current = source_path.read_text(encoding="utf-8")
    normalized = _preserve_existing_relation_role(current, normalized)
    from daemon_core.memory.library_layout import render_vault_links
    rendered = render_vault_links(memory_root, render_relation_mutation(current, normalized))
    records = dict(effective_index.souvenirs)
    records[normalized.source] = parse_souvenir_graph(
        normalized.source,
        rendered,
    )
    staged_index = MemoryGraphIndex(
        library_fingerprint="staged",
        souvenirs=records,
    )
    staged_audit = audit_memory_graph(staged_index, memory_root=memory_root)
    new_issues = _new_issue_keys(before.audit.issues, staged_audit.issues)
    if new_issues:
        detail = ", ".join(sorted(new_issues)[:4])
        raise RelationPublicationError(
            f"Mutation introduces graph integrity issues: {detail}.",
            reason_code="graph_integrity_issue",
        )
    _verify_staged_relation(staged_index, normalized)
    return RelationMutationPlan(
        mutation=normalized,
        source_path=source_path,
        current_hash=_sha256_text(current),
        next_hash=_sha256_text(rendered),
        current_content=current,
        next_content=rendered,
        baseline_issue_keys=tuple(
            sorted(_issue_key(issue) for issue in before.audit.issues)
        ),
        baseline_index=before.index,
        staged_collection_targets=tuple(sorted(staged_collections or {})),
    )


def plan_relation_mutation_batch(
    *,
    souvenirs_root: Path,
    memory_root: Path,
    mutations: Sequence[RelationMutation],
    previous_index: MemoryGraphIndex | None = None,
    staged_collections: Mapping[str, CollectionState] | None = None,
) -> RelationMutationBatchPlan:
    """Prepare one bounded relation batch against a shared staged graph."""

    if not 1 <= len(mutations) <= MAX_RELATION_MUTATION_BATCH:
        raise RelationPublicationError(
            "A relation batch must contain between 1 and "
            f"{MAX_RELATION_MUTATION_BATCH} mutations.",
            reason_code="relation_batch_out_of_bounds",
        )
    normalized = tuple(_validate_mutation(item) for item in mutations)
    for mutation in normalized:
        _reject_system_managed_relation(mutation)
    _validate_batch_pairs(normalized)

    before = build_memory_graph(
        souvenirs_root,
        previous=previous_index,
        memory_root=memory_root,
    )
    effective_index = _with_staged_collections(
        before.index,
        staged_collections or {},
        memory_root=memory_root,
    )
    staged_contents: dict[str, str] = {}
    original_contents: dict[str, str] = {}
    source_paths: dict[str, Path] = {}
    effective_mutations = []
    mutation_changed = []

    for mutation in normalized:
        source_path = _souvenir_path(souvenirs_root, mutation.source)
        target_path = _souvenir_path(souvenirs_root, mutation.target)
        if not source_path.is_file():
            raise RelationPublicationError(
                f"Unknown source capsule: {mutation.source}.",
                reason_code="unknown_source_capsule",
            )
        if not target_path.is_file():
            raise RelationPublicationError(
                f"Unknown target capsule: {mutation.target}.",
                reason_code="unknown_target_capsule",
            )
        if not source_ref_exists(memory_root, mutation.source_ref):
            raise RelationPublicationError(
                f"Exact L0 source does not exist: {mutation.source_ref}.",
                reason_code="exact_l0_source_missing",
            )
        target_record = effective_index.souvenirs.get(mutation.target)
        if target_record is None or target_record.collection is None:
            raise RelationPublicationError(
                f"Target is not a declared collection: {mutation.target}.",
                reason_code="target_not_declared_collection",
            )

        current = staged_contents.get(mutation.source)
        if current is None:
            current = source_path.read_text(encoding="utf-8")
            original_contents[mutation.source] = current
            staged_contents[mutation.source] = current
            source_paths[mutation.source] = source_path
        effective = _preserve_existing_relation_role(current, mutation)
        from daemon_core.memory.library_layout import render_vault_links
        rendered = render_vault_links(memory_root, render_relation_mutation(current, effective))
        effective_mutations.append(effective)
        mutation_changed.append(_sha256_text(current) != _sha256_text(rendered))
        staged_contents[mutation.source] = rendered

    records = dict(effective_index.souvenirs)
    for source, content in staged_contents.items():
        records[source] = parse_souvenir_graph(source, content)
    staged_index = MemoryGraphIndex(
        library_fingerprint="staged-relation-batch",
        souvenirs=records,
    )
    staged_audit = audit_memory_graph(staged_index, memory_root=memory_root)
    new_issues = _new_issue_keys(before.audit.issues, staged_audit.issues)
    if new_issues:
        detail = ", ".join(sorted(new_issues)[:4])
        raise RelationPublicationError(
            f"Relation batch introduces graph integrity issues: {detail}.",
            reason_code="graph_integrity_issue",
        )
    for mutation in effective_mutations:
        _verify_staged_relation(staged_index, mutation)

    source_plans = tuple(
        RelationMutationBatchSourcePlan(
            source=source,
            source_path=source_paths[source],
            current_hash=_sha256_text(original_contents[source]),
            next_hash=_sha256_text(staged_contents[source]),
            current_content=original_contents[source],
            next_content=staged_contents[source],
        )
        for source in sorted(staged_contents)
    )
    return RelationMutationBatchPlan(
        mutations=tuple(effective_mutations),
        source_plans=source_plans,
        mutation_changed=tuple(mutation_changed),
        baseline_issue_keys=tuple(
            sorted(_issue_key(issue) for issue in before.audit.issues)
        ),
        baseline_index=before.index,
        staged_collection_targets=tuple(sorted(staged_collections or {})),
    )


def apply_relation_mutation(
    plan: RelationMutationPlan,
    *,
    souvenirs_root: Path,
    memory_root: Path,
    index_path: Path,
    dry_run: bool = False,
) -> RelationPublicationResult:
    """Publish one planned source file and regenerate its derived index."""

    if dry_run:
        return RelationPublicationResult(
            status="planned" if plan.changed else "unchanged",
            source=plan.mutation.source,
            target=plan.mutation.target,
            index_fingerprint=plan.baseline_index.library_fingerprint,
        )
    if plan.staged_collection_targets:
        raise RelationPublicationError(
            "Staged collections must be published before an active relation write.",
            reason_code="staged_collection_not_published",
        )

    with ExitStack() as locks:
        locks.enter_context(memory_file_lock(_publication_lock_path(memory_root)))
        locks.enter_context(memory_file_lock(plan.source_path))
        latest = plan.source_path.read_text(encoding="utf-8")
        if _sha256_text(latest) != plan.current_hash:
            raise RelationPublicationError(
                "Source souvenir changed after the relation plan was prepared.",
                reason_code="source_changed_after_plan",
            )
        if plan.changed:
            atomic_write_text(plan.source_path, plan.next_content)
        try:
            build = build_memory_graph(
                souvenirs_root,
                previous=plan.baseline_index,
                memory_root=memory_root,
            )
            new_issues = {
                _issue_key(issue) for issue in build.audit.issues
            } - set(plan.baseline_issue_keys)
            touched_issues = {
                issue
                for issue in new_issues
                if issue.startswith(plan.mutation.source + ":")
                or issue.startswith(plan.mutation.target + ":")
            }
            if touched_issues:
                raise RelationPublicationError(
                    "Published relation failed graph validation: "
                    + ", ".join(sorted(touched_issues)[:4]),
                    reason_code="published_graph_validation_failed",
                )
            _verify_staged_relation(build.index, plan.mutation)
        except Exception:
            if plan.changed:
                atomic_write_text(plan.source_path, plan.current_content)
            raise
        # The Markdown is authoritative. If this derived write fails, the
        # outbox replay leaves the idempotent relation in place and rebuilds it.
        write_memory_graph_index(index_path, build.index)
    return RelationPublicationResult(
        status="published" if plan.changed else "unchanged",
        source=plan.mutation.source,
        target=plan.mutation.target,
        index_fingerprint=build.index.library_fingerprint,
    )


def apply_relation_mutation_batch(
    plan: RelationMutationBatchPlan,
    *,
    souvenirs_root: Path,
    memory_root: Path,
    index_path: Path,
    dry_run: bool = False,
) -> RelationMutationBatchResult:
    """Publish a prevalidated batch and roll every source back on failure."""

    if dry_run:
        return _batch_result(
            plan,
            changed_status="planned",
            index_fingerprint=plan.baseline_index.library_fingerprint,
        )
    if plan.staged_collection_targets:
        raise RelationPublicationError(
            "Staged collections must be published before an active relation batch.",
            reason_code="staged_collection_not_published",
        )

    ordered_sources = tuple(
        sorted(plan.source_plans, key=lambda item: str(item.source_path.resolve()))
    )
    with ExitStack() as locks:
        locks.enter_context(memory_file_lock(_publication_lock_path(memory_root)))
        for source in ordered_sources:
            locks.enter_context(memory_file_lock(source.source_path))

        for source in ordered_sources:
            latest = source.source_path.read_text(encoding="utf-8")
            if _sha256_text(latest) != source.current_hash:
                raise RelationPublicationError(
                    "A source souvenir changed after the relation batch was prepared.",
                    reason_code="source_changed_after_batch_plan",
                )

        try:
            for source in ordered_sources:
                if source.changed:
                    atomic_write_text(source.source_path, source.next_content)
            build = build_memory_graph(
                souvenirs_root,
                previous=plan.baseline_index,
                memory_root=memory_root,
            )
            new_issues = {
                _issue_key(issue) for issue in build.audit.issues
            } - set(plan.baseline_issue_keys)
            touched = {
                name
                for mutation in plan.mutations
                for name in (mutation.source, mutation.target)
            }
            touched_issues = {
                issue
                for issue in new_issues
                if any(issue.startswith(name + ":") for name in touched)
            }
            if touched_issues:
                raise RelationPublicationError(
                    "Published relation batch failed graph validation: "
                    + ", ".join(sorted(touched_issues)[:4]),
                    reason_code="published_graph_validation_failed",
                )
            for mutation in plan.mutations:
                _verify_staged_relation(build.index, mutation)
        except BaseException as exc:
            rollback_errors = _rollback_batch_sources(ordered_sources)
            if rollback_errors:
                raise RelationPublicationError(
                    "Relation batch rollback could not restore every source: "
                    + ", ".join(rollback_errors[:4]),
                    reason_code="relation_batch_rollback_failed",
                ) from exc
            raise

        # Markdown remains authoritative. A failed derived-index write leaves
        # the complete idempotent batch in place for the durable replay.
        write_memory_graph_index(index_path, build.index)

    return _batch_result(
        plan,
        changed_status="published",
        index_fingerprint=build.index.library_fingerprint,
    )


def render_relation_mutation(content: str, mutation: RelationMutation) -> str:
    """Render one idempotent relation change while preserving souvenir prose."""

    normalized = _preserve_existing_relation_role(
        content,
        _validate_mutation(mutation),
    )
    record = parse_souvenir_graph(normalized.source, content)
    matches = [
        relation
        for relation in record.relations
        if relation.kind == MEMBERSHIP_RELATION
        and relation.target == normalized.target
    ]
    if len(matches) > 1:
        raise RelationPublicationError(
            "Duplicate existing membership relation.",
            reason_code="duplicate_membership_relation",
        )
    if normalized.action == "remove":
        if not matches:
            return _normalized_document(content)
        return _remove_relation_at_line(content, matches[0].line)

    if matches:
        relation = matches[0]
        if normalized.source_ref in relation.source_refs:
            return _normalized_document(content)
        return _insert_source_ref(
            content,
            relation_line=relation.line,
            source_ref=normalized.source_ref,
        )
    return _append_relation(content, normalized)


def _preserve_existing_relation_role(
    content: str,
    mutation: RelationMutation,
) -> RelationMutation:
    """Keep role changes outside ordinary monotone upserts."""

    if mutation.action != "upsert":
        return mutation
    record = parse_souvenir_graph(mutation.source, content)
    matches = [
        relation
        for relation in record.relations
        if relation.kind == MEMBERSHIP_RELATION
        and relation.target == mutation.target
    ]
    if len(matches) > 1:
        raise RelationPublicationError(
            "Duplicate existing membership relation.",
            reason_code="duplicate_membership_relation",
        )
    if not matches or matches[0].description == mutation.role:
        return mutation
    return replace(mutation, role=matches[0].description)


def mutation_from_audit_item(
    item: dict[str, Any],
    decision: dict[str, Any],
    *,
    source_ref: str,
) -> RelationMutation | None:
    """Bind a validated audit action back to its immutable input identity."""

    disposition = str(decision.get("disposition", ""))
    action = str(decision.get("action", ""))
    item_id = str(item.get("id", ""))
    decision_id = str(decision.get("item_id", ""))
    if not item_id or decision_id != item_id:
        raise RelationPublicationError(
            "Audit decision identity mismatch.",
            reason_code="audit_identity_mismatch",
        )
    if disposition not in {"confirm", "repair"} or action == "none":
        return None
    if action not in ALLOWED_MUTATION_ACTIONS:
        raise RelationPublicationError(
            f"Unsupported audit action: {action}.",
            reason_code="unsupported_audit_action",
        )
    if disposition == "confirm" and action != str(item.get("proposed_action", "")):
        raise RelationPublicationError(
            "Confirmed action differs from its proposal.",
            reason_code="audit_action_mismatch",
        )
    if disposition == "repair" and str(item.get("mode", "")) != "maintenance":
        raise RelationPublicationError(
            "Repair decision is outside maintenance mode.",
            reason_code="repair_outside_maintenance",
        )
    return RelationMutation(
        action=action,
        source=str(item.get("source", "")),
        target=str(item.get("target", "")),
        role=str(item.get("role", "")),
        source_ref=source_ref,
    )


def _batch_result(
    plan: RelationMutationBatchPlan,
    *,
    changed_status: str,
    index_fingerprint: str,
) -> RelationMutationBatchResult:
    items = tuple(
        RelationPublicationResult(
            status=changed_status if changed else "unchanged",
            source=mutation.source,
            target=mutation.target,
            index_fingerprint=index_fingerprint,
        )
        for mutation, changed in zip(
            plan.mutations,
            plan.mutation_changed,
            strict=True,
        )
    )
    published_count = sum(item.status == "published" for item in items)
    planned_count = sum(item.status == "planned" for item in items)
    unchanged_count = sum(item.status == "unchanged" for item in items)
    return RelationMutationBatchResult(
        status=(
            changed_status
            if published_count or planned_count
            else "unchanged"
        ),
        published_count=published_count,
        planned_count=planned_count,
        unchanged_count=unchanged_count,
        index_fingerprint=index_fingerprint,
        items=items,
    )


def _validate_batch_pairs(
    mutations: tuple[RelationMutation, ...],
) -> None:
    pairs: dict[tuple[str, str], RelationMutation] = {}
    for mutation in mutations:
        key = (mutation.source, mutation.target)
        previous = pairs.get(key)
        if previous is None:
            pairs[key] = mutation
            continue
        if previous.action != mutation.action or (
            mutation.action == "upsert" and previous.role != mutation.role
        ):
            raise RelationPublicationError(
                "A relation batch contains contradictory mutations for one pair.",
                reason_code="contradictory_relation_batch_pair",
            )


def _rollback_batch_sources(
    sources: tuple[RelationMutationBatchSourcePlan, ...],
) -> list[str]:
    errors = []
    for source in reversed(sources):
        try:
            latest = source.source_path.read_text(encoding="utf-8")
            latest_hash = _sha256_text(latest)
            if latest_hash == source.current_hash:
                continue
            if latest_hash != source.next_hash:
                errors.append(source.source)
                continue
            atomic_write_text(source.source_path, source.current_content)
            restored = source.source_path.read_text(encoding="utf-8")
            if _sha256_text(restored) != source.current_hash:
                errors.append(source.source)
        except Exception:
            errors.append(source.source)
    return errors


def _publication_lock_path(memory_root: Path) -> Path:
    return memory_root / "levels" / ".relation-publication"


def _validate_mutation(mutation: RelationMutation) -> RelationMutation:
    if mutation.action not in ALLOWED_MUTATION_ACTIONS:
        raise RelationPublicationError(
            f"Invalid action: {mutation.action!r}.",
            reason_code="invalid_action",
        )
    source = normalize_souvenir_name(mutation.source)
    target = normalize_souvenir_name(mutation.target)
    source_ref = normalize_source_ref(mutation.source_ref)
    role = " ".join(str(mutation.role).split())
    if source is None or source != mutation.source:
        raise RelationPublicationError(
            "Invalid source souvenir path.",
            reason_code="invalid_source_capsule_path",
        )
    if target is None or target != mutation.target or target == source:
        raise RelationPublicationError(
            "Invalid target souvenir path.",
            reason_code="invalid_target_capsule_path",
        )
    if source_ref is None:
        raise RelationPublicationError(
            "Invalid exact L0 source reference.",
            reason_code="invalid_exact_l0_source",
        )
    if mutation.action == "upsert" and (not role or len(role) > 400):
        raise RelationPublicationError(
            "An upsert requires a bounded membership role.",
            reason_code="invalid_membership_role",
        )
    if mutation.action == "remove" and len(role) > 400:
        raise RelationPublicationError(
            "Removal role is too long.",
            reason_code="removal_role_too_long",
        )
    return RelationMutation(
        action=mutation.action,
        source=source,
        target=target,
        role=role,
        source_ref=source_ref,
    )


def _with_staged_collections(
    index: MemoryGraphIndex,
    staged_collections: Mapping[str, CollectionState],
    *,
    memory_root: Path,
) -> MemoryGraphIndex:
    if not staged_collections:
        return index
    records = dict(index.souvenirs)
    for raw_name, collection in staged_collections.items():
        name = normalize_souvenir_name(str(raw_name))
        if name is None or name != raw_name or name not in records:
            raise RelationPublicationError(
                "Invalid staged collection target.",
                reason_code="invalid_staged_collection_target",
            )
        if not isinstance(collection, CollectionState) or collection.coverage != "open":
            raise RelationPublicationError(
                "Invalid staged collection state.",
                reason_code="invalid_staged_collection_state",
            )
        current = records[name].collection
        if current is not None and current.coverage == "closed":
            raise RelationPublicationError(
                "A closed collection cannot be staged as open.",
                reason_code="collection_already_closed",
            )
        for source_ref in collection.source_refs:
            if not source_ref_exists(memory_root, source_ref):
                raise RelationPublicationError(
                    "Staged collection has missing exact L0 provenance.",
                    reason_code="staged_collection_source_missing",
                )
        records[name] = replace(records[name], collection=collection)
    return MemoryGraphIndex(
        library_fingerprint="staged-collections",
        souvenirs=records,
    )


def _verify_staged_relation(
    index: MemoryGraphIndex,
    mutation: RelationMutation,
) -> None:
    record = index.souvenirs[mutation.source]
    matches = [
        relation
        for relation in record.relations
        if relation.kind == MEMBERSHIP_RELATION
        and relation.target == mutation.target
    ]
    if mutation.action == "remove":
        if matches:
            raise RelationPublicationError(
                "Staged removal left the relation present.",
                reason_code="staged_removal_failed",
            )
        return
    if len(matches) != 1:
        raise RelationPublicationError(
            "Staged upsert did not create one relation.",
            reason_code="staged_upsert_failed",
        )
    relation = matches[0]
    if relation.description != mutation.role:
        raise RelationPublicationError(
            "Staged relation role mismatch.",
            reason_code="staged_role_mismatch",
        )
    if mutation.source_ref not in relation.source_refs:
        raise RelationPublicationError(
            "Staged relation provenance mismatch.",
            reason_code="staged_provenance_mismatch",
        )


def _append_relation(content: str, mutation: RelationMutation) -> str:
    lines = content.rstrip("\n").splitlines()
    heading = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().casefold() == "## relations"
        ),
        None,
    )
    target = ("capsules/" if obsidian_format_enabled() else "") + mutation.target
    relation_lines = [
        f"- member_of [[{target}]] : {mutation.role}",
        f"  - source: [[{mutation.source_ref}]]",
    ]
    if heading is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(["", "## Relations", *relation_lines])
        return "\n".join(lines).rstrip() + "\n"

    end = next(
        (
            index
            for index in range(heading + 1, len(lines))
            if lines[index].strip().startswith("## ")
        ),
        len(lines),
    )
    insertion = end
    while insertion > heading + 1 and not lines[insertion - 1].strip():
        insertion -= 1
    prefix = [] if insertion == heading + 1 else [""]
    lines[insertion:insertion] = [*prefix, *relation_lines]
    return "\n".join(lines).rstrip() + "\n"


def _insert_source_ref(
    content: str,
    *,
    relation_line: int,
    source_ref: str,
) -> str:
    lines = content.rstrip("\n").splitlines()
    insertion = relation_line
    while insertion < len(lines):
        stripped = lines[insertion].strip()
        indent = len(lines[insertion]) - len(lines[insertion].lstrip())
        if indent <= 0 or not stripped.startswith("- source:"):
            break
        insertion += 1
    lines.insert(insertion, f"  - source: [[{source_ref}]]")
    return "\n".join(lines).rstrip() + "\n"


def _remove_relation_at_line(content: str, relation_line: int) -> str:
    lines = content.rstrip("\n").splitlines()
    start = relation_line - 1
    end = start + 1
    while end < len(lines):
        stripped = lines[end].strip()
        indent = len(lines[end]) - len(lines[end].lstrip())
        if indent <= 0 or not stripped.startswith("- source:"):
            break
        end += 1
    del lines[start:end]
    return "\n".join(lines).rstrip() + "\n"


def _souvenir_path(souvenirs_root: Path, name: str) -> Path:
    from daemon_core.memory.library_layout import vault_root, resolve_note
    root = souvenirs_root.resolve()
    if vault_root(root.parent) is not None:
        return resolve_note(root.parent, name)
    path = (root / f"{name}.md").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RelationPublicationError(
            "Souvenir path escapes its root.",
            reason_code="capsule_path_escape",
        ) from exc
    return path


def _new_issue_keys(before, after) -> set[str]:
    before_keys = {_issue_key(issue) for issue in before}
    return {_issue_key(issue) for issue in after} - before_keys


def _issue_key(issue) -> str:
    return ":".join(
        (
            str(issue.souvenir),
            str(issue.code),
            str(issue.line),
            str(issue.target),
            str(issue.detail),
        )
    )


def _normalized_document(content: str) -> str:
    return content.rstrip("\n") + "\n"


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
