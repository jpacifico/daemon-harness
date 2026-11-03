from __future__ import annotations

from collections.abc import Iterable
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import time

from daemon_core.memory.integrity import atomic_write_text
from daemon_core.memory.library_layout import single_note_scan
from daemon_core.memory.metadata import parse_souvenir_frontmatter
from daemon_core.memory.obsidian import collection_properties, parse_note_link, souvenir_link_target


GRAPH_SCHEMA_VERSION = 1
DISCOVERY_RELATION = "related_to"
STRUCTURAL_RELATION = "contains"
MEMBERSHIP_RELATION = "member_of"
SUPPORTED_RELATION_KINDS = {
    DISCOVERY_RELATION,
    STRUCTURAL_RELATION,
    MEMBERSHIP_RELATION,
}
COLLECTION_COVERAGE = {"open", "closed"}
RESERVED_SOUVENIR_NAMES = {"style_de_communication", "theme/communication_style"}
RESERVED_SOUVENIR_PREFIXES = ("templates/",)

_TYPED_RELATION_RE = re.compile(
    r"^(?P<kind>[a-z][a-z0-9_-]*)\s+(?P<link>.+)$"
)
_SOURCE_RE = re.compile(r"^source\s*:\s*(.+)$")
_PATH_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class GraphIssue:
    code: str
    souvenir: str
    line: int = 0
    detail: str = ""
    target: str = ""

    def as_dict(self) -> dict:
        data = {"code": self.code, "capsule": self.souvenir}
        if self.line:
            data["line"] = self.line
        if self.detail:
            data["detail"] = self.detail
        if self.target:
            data["target"] = self.target
        return data

    @classmethod
    def from_dict(cls, raw: object) -> GraphIssue:
        if not isinstance(raw, dict):
            raise ValueError("Invalid graph issue record.")
        return cls(
            code=str(raw.get("code", "")),
            souvenir=str(raw.get("capsule", "")),
            line=int(raw.get("line", 0) or 0),
            detail=str(raw.get("detail", "")),
            target=str(raw.get("target", "")),
        )


@dataclass(frozen=True)
class MemoryRelation:
    source: str
    target: str
    kind: str
    description: str = ""
    source_refs: tuple[str, ...] = ()
    line: int = 0
    syntax: str = "legacy"

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "description": self.description,
            "source_refs": list(self.source_refs),
            "line": self.line,
            "syntax": self.syntax,
        }

    @classmethod
    def from_dict(cls, raw: object) -> MemoryRelation:
        if not isinstance(raw, dict):
            raise ValueError("Invalid memory relation record.")
        refs = raw.get("source_refs", [])
        if not isinstance(refs, list):
            refs = []
        return cls(
            source=str(raw.get("source", "")),
            target=str(raw.get("target", "")),
            kind=str(raw.get("kind", "")),
            description=str(raw.get("description", "")),
            source_refs=tuple(str(item) for item in refs if isinstance(item, str)),
            line=int(raw.get("line", 0) or 0),
            syntax=str(raw.get("syntax", "legacy")),
        )


@dataclass(frozen=True)
class CollectionState:
    coverage: str
    source_refs: tuple[str, ...] = ()
    closure_source_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "coverage": self.coverage,
            "source_refs": list(self.source_refs),
            "closure_source_refs": list(self.closure_source_refs),
        }

    @classmethod
    def from_dict(cls, raw: object) -> CollectionState | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("Invalid collection state.")
        source_refs = raw.get("source_refs", [])
        if not isinstance(source_refs, list):
            source_refs = []
        closure_refs = raw.get("closure_source_refs", [])
        if not isinstance(closure_refs, list):
            closure_refs = []
        return cls(
            coverage=str(raw.get("coverage", "")),
            source_refs=tuple(
                str(item) for item in source_refs if isinstance(item, str)
            ),
            closure_source_refs=tuple(
                str(item) for item in closure_refs if isinstance(item, str)
            ),
        )


@dataclass(frozen=True)
class SouvenirGraphRecord:
    source_hash: str
    relations: tuple[MemoryRelation, ...] = ()
    issues: tuple[GraphIssue, ...] = ()
    collection: CollectionState | None = None

    def as_dict(self) -> dict:
        return {
            "source_hash": self.source_hash,
            "relations": [relation.as_dict() for relation in self.relations],
            "issues": [issue.as_dict() for issue in self.issues],
            "collection": self.collection.as_dict() if self.collection else None,
        }

    @classmethod
    def from_dict(cls, raw: object) -> SouvenirGraphRecord:
        if not isinstance(raw, dict):
            raise ValueError("Invalid souvenir graph record.")
        relations = raw.get("relations", [])
        issues = raw.get("issues", [])
        if not isinstance(relations, list) or not isinstance(issues, list):
            raise ValueError("Invalid souvenir graph relation lists.")
        return cls(
            source_hash=str(raw.get("source_hash", "")),
            relations=tuple(MemoryRelation.from_dict(item) for item in relations),
            issues=tuple(GraphIssue.from_dict(item) for item in issues),
            collection=CollectionState.from_dict(raw.get("collection")),
        )


@dataclass(frozen=True)
class MemoryGraphIndex:
    library_fingerprint: str
    souvenirs: dict[str, SouvenirGraphRecord] = field(default_factory=dict)
    schema_version: int = GRAPH_SCHEMA_VERSION

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "library_fingerprint": self.library_fingerprint,
            "capsule_count": len(self.souvenirs),
            "capsules": {
                name: self.souvenirs[name].as_dict() for name in sorted(self.souvenirs)
            },
            "backlinks": _derived_backlinks(self),
            "collections": _derived_collections(self),
        }

    @classmethod
    def from_dict(cls, raw: object) -> MemoryGraphIndex:
        if not isinstance(raw, dict):
            raise ValueError("Invalid memory graph index.")
        schema_version = int(raw.get("schema_version", 0) or 0)
        if schema_version != GRAPH_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported memory graph schema version: {schema_version}."
            )
        souvenirs = raw.get("capsules", {})
        if not isinstance(souvenirs, dict):
            raise ValueError("Invalid memory graph souvenir map.")
        return cls(
            schema_version=schema_version,
            library_fingerprint=str(raw.get("library_fingerprint", "")),
            souvenirs={
                str(name): SouvenirGraphRecord.from_dict(record)
                for name, record in souvenirs.items()
            },
        )


@dataclass(frozen=True)
class GraphBuildStats:
    discovered_souvenirs: int
    parsed_souvenirs: int
    reused_souvenirs: int
    removed_souvenirs: int
    duration_seconds: float

    def as_dict(self) -> dict:
        return {
            "discovered_capsules": self.discovered_souvenirs,
            "parsed_capsules": self.parsed_souvenirs,
            "reused_capsules": self.reused_souvenirs,
            "removed_capsules": self.removed_souvenirs,
            "duration_seconds": round(self.duration_seconds, 6),
        }


@dataclass(frozen=True)
class MemoryGraphAudit:
    souvenir_count: int
    relation_count: int
    valid_relation_count: int
    relations_by_kind: dict[str, int]
    valid_relations_by_kind: dict[str, int]
    collection_count: int
    collection_member_count: int
    reciprocal_discovery_pairs: int
    component_count: int
    largest_component: int
    isolate_count: int
    component_sizes: tuple[int, ...]
    issues: tuple[GraphIssue, ...]

    @property
    def unhealthy(self) -> bool:
        return bool(self.issues)

    def as_dict(self) -> dict:
        return {
            "capsule_count": self.souvenir_count,
            "relation_count": self.relation_count,
            "valid_relation_count": self.valid_relation_count,
            "relations_by_kind": dict(sorted(self.relations_by_kind.items())),
            "valid_relations_by_kind": dict(
                sorted(self.valid_relations_by_kind.items())
            ),
            "collection_count": self.collection_count,
            "collection_member_count": self.collection_member_count,
            "reciprocal_discovery_pairs": self.reciprocal_discovery_pairs,
            "component_count": self.component_count,
            "largest_component": self.largest_component,
            "isolate_count": self.isolate_count,
            "component_sizes": list(self.component_sizes),
            "issue_count": len(self.issues),
            "issues_by_code": dict(
                sorted(Counter(issue.code for issue in self.issues).items())
            ),
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class MemoryGraphBuild:
    index: MemoryGraphIndex
    audit: MemoryGraphAudit
    stats: GraphBuildStats


@dataclass
class _RelationDraft:
    source: str
    target: str
    kind: str
    description: str
    line: int
    syntax: str
    source_refs: list[str] = field(default_factory=list)

    def freeze(self) -> MemoryRelation:
        return MemoryRelation(
            source=self.source,
            target=self.target,
            kind=self.kind,
            description=self.description,
            source_refs=tuple(self.source_refs),
            line=self.line,
            syntax=self.syntax,
        )


def parse_souvenir_graph(
    souvenir_name: str,
    content: str,
    *,
    source_hash: str | None = None,
    memory_root: Path | None = None,
) -> SouvenirGraphRecord:
    """Parse graph declarations without changing the souvenir content."""

    digest = source_hash or hashlib.sha256(content.encode("utf-8")).hexdigest()
    issues: list[GraphIssue] = []
    try:
        frontmatter, body = parse_souvenir_frontmatter(content)
    except ValueError as exc:
        return SouvenirGraphRecord(
            source_hash=digest,
            issues=(
                GraphIssue(
                    code="invalid_frontmatter",
                    souvenir=souvenir_name,
                    line=1,
                    detail=str(exc),
                ),
            ),
        )

    collection = _parse_collection_state(souvenir_name, frontmatter, issues)
    body_line_offset = _body_line_offset(content)
    drafts: list[_RelationDraft] = []
    active_draft: _RelationDraft | None = None
    section = ""
    fence = ""

    for relative_line, raw_line in enumerate(body.splitlines(), start=1):
        line_number = body_line_offset + relative_line
        stripped = raw_line.strip()
        fence_match = re.match(r"^(\x60{3,}|~{3,})", stripped)
        if fence_match:
            marker = fence_match[1]
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
            continue
        if fence:
            continue
        if stripped.startswith("## "):
            section = stripped.casefold()
            active_draft = None
            continue
        if section not in {
            "## liens proches",
            "## links",
            "## sous-capsules",
            "## relations",
        }:
            continue
        if not stripped.startswith("- "):
            continue

        indent = len(raw_line) - len(raw_line.lstrip())
        payload = stripped[2:].strip()
        if section == "## relations" and indent > 0:
            source_match = _SOURCE_RE.fullmatch(payload)
            if source_match is None:
                issues.append(
                    GraphIssue(
                        code="malformed_relation_detail",
                        souvenir=souvenir_name,
                        line=line_number,
                        detail=payload,
                    )
                )
                continue
            if active_draft is None:
                issues.append(
                    GraphIssue(
                        code="unattached_relation_source",
                        souvenir=souvenir_name,
                        line=line_number,
                        detail=payload,
                    )
                )
                continue
            source_ref = _normalize_source_ref(source_match.group(1), memory_root)
            if source_ref is None:
                issues.append(
                    GraphIssue(
                        code="invalid_relation_source",
                        souvenir=souvenir_name,
                        line=line_number,
                        detail=source_match.group(1),
                    )
                )
                continue
            if source_ref not in active_draft.source_refs:
                active_draft.source_refs.append(source_ref)
            continue

        if section == "## relations":
            active_draft = _parse_typed_relation(
                souvenir_name,
                payload,
                line_number,
                issues,
                memory_root=memory_root,
            )
        else:
            active_draft = _parse_discovery_relation(
                souvenir_name,
                payload,
                line_number,
                STRUCTURAL_RELATION
                if section == "## sous-capsules"
                else DISCOVERY_RELATION,
                issues,
                memory_root=memory_root,
            )
        if active_draft is not None:
            drafts.append(active_draft)

    relations = tuple(draft.freeze() for draft in drafts)
    seen: set[tuple[str, str]] = set()
    for relation in relations:
        key = (relation.kind, relation.target)
        if key in seen:
            issues.append(
                GraphIssue(
                    code="duplicate_relation",
                    souvenir=souvenir_name,
                    line=relation.line,
                    target=relation.target,
                    detail=relation.kind,
                )
            )
        seen.add(key)
        if not relation.description:
            issues.append(
                GraphIssue(
                    code="missing_relation_description",
                    souvenir=souvenir_name,
                    line=relation.line,
                    target=relation.target,
                    detail=relation.kind,
                )
            )
        if relation.kind == MEMBERSHIP_RELATION and not relation.source_refs:
            issues.append(
                GraphIssue(
                    code="missing_relation_provenance",
                    souvenir=souvenir_name,
                    line=relation.line,
                    target=relation.target,
                )
            )

    return SouvenirGraphRecord(
        source_hash=digest,
        relations=relations,
        issues=tuple(issues),
        collection=collection,
    )


def build_memory_graph(
    souvenirs_root: Path,
    *,
    previous: MemoryGraphIndex | None = None,
    memory_root: Path | None = None,
) -> MemoryGraphBuild:
    """Build a fully regenerable graph, reusing unchanged parsed records."""

    started = time.perf_counter()
    root = souvenirs_root.resolve()
    files = _souvenir_files(root)
    from daemon_core.memory.library_layout import vault_root
    previous_records = previous.souvenirs if previous and vault_root(memory_root or root.parent) is None else {}
    records: dict[str, SouvenirGraphRecord] = {}
    parsed = 0
    reused = 0

    with single_note_scan():
        for name, path in files:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            prior = previous_records.get(name)
            if prior is not None and prior.source_hash == digest:
                records[name] = prior
                reused += 1
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                records[name] = SouvenirGraphRecord(
                    source_hash=digest,
                    issues=(
                        GraphIssue(
                            code="invalid_utf8",
                            souvenir=name,
                            detail=str(exc),
                        ),
                    ),
                )
            else:
                records[name] = parse_souvenir_graph(
                    name,
                    content,
                    source_hash=digest,
                    memory_root=memory_root or root.parent,
                )
            parsed += 1

        fingerprint = _library_fingerprint(records)
    index = MemoryGraphIndex(
        library_fingerprint=fingerprint,
        souvenirs=records,
    )
    resolved_memory_root = (
        memory_root.resolve() if memory_root is not None else root.parent.resolve()
    )
    audit = audit_memory_graph(index, memory_root=resolved_memory_root)
    stats = GraphBuildStats(
        discovered_souvenirs=len(records),
        parsed_souvenirs=parsed,
        reused_souvenirs=reused,
        removed_souvenirs=len(set(previous_records) - set(records)),
        duration_seconds=time.perf_counter() - started,
    )
    return MemoryGraphBuild(index=index, audit=audit, stats=stats)


def build_memory_graph_from_entries(
    entries: Iterable[tuple[str, str]],
    *,
    previous: MemoryGraphIndex | None = None,
    memory_root: Path | None = None,
) -> MemoryGraphBuild:
    """Build the graph from souvenir bodies already loaded by the runtime.

    The canonical Atlas has already paid the I/O cost of reading every active
    souvenir. Reusing those exact bodies keeps collection discovery incremental
    without introducing a second scan or making a persisted index authoritative.
    """

    started = time.perf_counter()
    previous_records = previous.souvenirs if previous else {}
    records: dict[str, SouvenirGraphRecord] = {}
    parsed = 0
    reused = 0

    with single_note_scan():
        for name, content in sorted(entries):
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            prior = previous_records.get(name)
            if prior is not None and prior.source_hash == digest:
                records[name] = prior
                reused += 1
                continue
            records[name] = parse_souvenir_graph(
                name,
                content,
                source_hash=digest,
            )
            parsed += 1

    index = MemoryGraphIndex(
        library_fingerprint=_library_fingerprint(records),
        souvenirs=records,
    )
    audit = audit_memory_graph(index, memory_root=memory_root)
    return MemoryGraphBuild(
        index=index,
        audit=audit,
        stats=GraphBuildStats(
            discovered_souvenirs=len(records),
            parsed_souvenirs=parsed,
            reused_souvenirs=reused,
            removed_souvenirs=len(set(previous_records) - set(records)),
            duration_seconds=time.perf_counter() - started,
        ),
    )


def audit_memory_graph(
    index: MemoryGraphIndex,
    *,
    memory_root: Path | None = None,
) -> MemoryGraphAudit:
    names = set(index.souvenirs)
    issues = [
        issue
        for name in sorted(index.souvenirs)
        for issue in index.souvenirs[name].issues
    ]
    relations = [
        relation
        for name in sorted(index.souvenirs)
        for relation in index.souvenirs[name].relations
    ]
    valid_relations: list[MemoryRelation] = []
    relation_counts = Counter(relation.kind for relation in relations)
    valid_counts: Counter[str] = Counter()
    source_existence: dict[str, bool] = {}
    seen_relations: set[tuple[str, str, str]] = set()

    def source_exists(source_ref: str) -> bool:
        if memory_root is None:
            return True
        if source_ref not in source_existence:
            source_existence[source_ref] = _source_ref_exists(
                memory_root,
                source_ref,
            )
        return source_existence[source_ref]

    for relation in relations:
        valid = True
        relation_key = (relation.source, relation.kind, relation.target)
        if relation_key in seen_relations:
            valid = False
        else:
            seen_relations.add(relation_key)
        if relation.target not in names:
            issues.append(
                GraphIssue(
                    code="missing_relation_target",
                    souvenir=relation.source,
                    line=relation.line,
                    target=relation.target,
                    detail=relation.kind,
                )
            )
            valid = False
        if relation.target == relation.source:
            issues.append(
                GraphIssue(
                    code="self_relation",
                    souvenir=relation.source,
                    line=relation.line,
                    target=relation.target,
                    detail=relation.kind,
                )
            )
            valid = False
        if relation.kind == MEMBERSHIP_RELATION:
            target_record = index.souvenirs.get(relation.target)
            if target_record is not None and target_record.collection is None:
                issues.append(
                    GraphIssue(
                        code="undeclared_collection_target",
                        souvenir=relation.source,
                        line=relation.line,
                        target=relation.target,
                    )
                )
                valid = False
            for source_ref in relation.source_refs:
                if not source_exists(source_ref):
                    issues.append(
                        GraphIssue(
                            code="missing_relation_source",
                            souvenir=relation.source,
                            line=relation.line,
                            target=source_ref,
                        )
                    )
                    valid = False
        if valid:
            valid_relations.append(relation)
            valid_counts[relation.kind] += 1

    for name, record in sorted(index.souvenirs.items()):
        collection = record.collection
        if collection is None:
            continue
        for source_ref in collection.source_refs:
            if not source_exists(source_ref):
                issues.append(
                    GraphIssue(
                        code="missing_collection_source",
                        souvenir=name,
                        target=source_ref,
                    )
                )
        for source_ref in collection.closure_source_refs:
            if not source_exists(source_ref):
                issues.append(
                    GraphIssue(
                        code="missing_collection_closure_source",
                        souvenir=name,
                        target=source_ref,
                    )
                )

    component_sizes = _component_sizes(names, valid_relations)
    discovery_edges = {
        (relation.source, relation.target)
        for relation in valid_relations
        if relation.kind == DISCOVERY_RELATION
    }
    reciprocal_pairs = sum(
        1
        for source, target in discovery_edges
        if source < target and (target, source) in discovery_edges
    )
    collection_members = [
        relation
        for relation in valid_relations
        if relation.kind == MEMBERSHIP_RELATION
    ]
    issues.sort(
        key=lambda issue: (
            issue.souvenir,
            issue.line,
            issue.code,
            issue.target,
        )
    )
    return MemoryGraphAudit(
        souvenir_count=len(names),
        relation_count=len(relations),
        valid_relation_count=len(valid_relations),
        relations_by_kind=dict(relation_counts),
        valid_relations_by_kind=dict(valid_counts),
        collection_count=sum(
            1 for record in index.souvenirs.values() if record.collection is not None
        ),
        collection_member_count=len(collection_members),
        reciprocal_discovery_pairs=reciprocal_pairs,
        component_count=len(component_sizes),
        largest_component=component_sizes[0] if component_sizes else 0,
        isolate_count=sum(size == 1 for size in component_sizes),
        component_sizes=component_sizes,
        issues=tuple(issues),
    )


def load_memory_graph_index(path: Path) -> MemoryGraphIndex:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return MemoryGraphIndex.from_dict(raw)


def write_memory_graph_index(path: Path, index: MemoryGraphIndex) -> None:
    rendered = json.dumps(
        index.as_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    atomic_write_text(path, rendered + "\n")


def render_memory_graph_audit(
    audit: MemoryGraphAudit,
    *,
    stats: GraphBuildStats | None = None,
) -> str:
    lines = [
        "Memory graph audit",
        f"souvenirs: {audit.souvenir_count}",
        (
            "relations: "
            f"{audit.valid_relation_count}/{audit.relation_count} valid"
        ),
    ]
    for kind in sorted(audit.relations_by_kind):
        lines.append(
            f"  {kind}: {audit.valid_relations_by_kind.get(kind, 0)}/"
            f"{audit.relations_by_kind[kind]} valid"
        )
    lines.extend(
        [
            f"collections: {audit.collection_count} "
            f"({audit.collection_member_count} members)",
            f"reciprocal discovery pairs: {audit.reciprocal_discovery_pairs}",
            (
                "components: "
                f"{audit.component_count} "
                f"(largest {audit.largest_component}, isolates {audit.isolate_count})"
            ),
            f"issues: {len(audit.issues)}",
        ]
    )
    if stats is not None:
        lines.append(
            "build: "
            f"{stats.parsed_souvenirs} parsed, "
            f"{stats.reused_souvenirs} reused, "
            f"{stats.removed_souvenirs} removed, "
            f"{stats.duration_seconds:.3f}s"
        )
    counts = Counter(issue.code for issue in audit.issues)
    for code, count in sorted(counts.items()):
        lines.append(f"  {code}: {count}")
    for issue in audit.issues:
        location = issue.souvenir
        if issue.line:
            location += f":{issue.line}"
        suffix = f" -> {issue.target}" if issue.target else ""
        detail = f" ({issue.detail})" if issue.detail else ""
        lines.append(f"- {issue.code}: {location}{suffix}{detail}")
    return "\n".join(lines)


def _parse_discovery_relation(
    souvenir_name: str,
    payload: str,
    line_number: int,
    kind: str,
    issues: list[GraphIssue],
    *, memory_root: Path | None = None,
) -> _RelationDraft | None:
    link = parse_note_link(payload)
    if link is not None:
        raw_target = souvenir_link_target(link, souvenir_name, memory_root=memory_root) or ""
        description = link.description
        syntax = link.syntax
    else:
        raw_target, separator, description = payload.partition(":")
        if not separator:
            description = ""
        raw_target = raw_target.strip().strip("`")
        description = description.strip()
        syntax = "legacy"
    target = normalize_souvenir_name(raw_target)
    if target is None:
        issues.append(
            GraphIssue(
                code="malformed_relation",
                souvenir=souvenir_name,
                line=line_number,
                detail=payload,
            )
        )
        return None
    return _RelationDraft(
        source=souvenir_name,
        target=target,
        kind=kind,
        description=description,
        line=line_number,
        syntax=syntax,
    )


def _parse_typed_relation(
    souvenir_name: str,
    payload: str,
    line_number: int,
    issues: list[GraphIssue],
    *, memory_root: Path | None = None,
) -> _RelationDraft | None:
    match = _TYPED_RELATION_RE.fullmatch(payload)
    if match is None:
        issues.append(
            GraphIssue(
                code="malformed_typed_relation",
                souvenir=souvenir_name,
                line=line_number,
                detail=payload,
            )
        )
        return None
    kind = match.group("kind")
    if kind not in SUPPORTED_RELATION_KINDS:
        issues.append(
            GraphIssue(
                code="unsupported_relation_kind",
                souvenir=souvenir_name,
                line=line_number,
                detail=kind,
            )
        )
        return None
    link = parse_note_link(match.group("link"))
    raw_target = souvenir_link_target(link, souvenir_name, memory_root=memory_root) if link else None
    target = normalize_souvenir_name(raw_target or "")
    if target is None:
        issues.append(
            GraphIssue(
                code="malformed_relation_target",
                souvenir=souvenir_name,
                line=line_number,
                detail=match.group("link"),
            )
        )
        return None
    return _RelationDraft(
        source=souvenir_name,
        target=target,
        kind=kind,
        description=link.description,
        line=line_number,
        syntax=link.syntax,
    )


def _parse_collection_state(
    souvenir_name: str,
    frontmatter: dict,
    issues: list[GraphIssue],
) -> CollectionState | None:
    try:
        raw = collection_properties(frontmatter)
    except ValueError as exc:
        issues.append(GraphIssue(
            code="invalid_collection_state", souvenir=souvenir_name, detail=str(exc),
        ))
        return None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        issues.append(
            GraphIssue(
                code="invalid_collection_state",
                souvenir=souvenir_name,
                detail="collection must be a mapping",
            )
        )
        return None
    coverage = str(raw.get("coverage", "")).strip().casefold()
    if coverage not in COLLECTION_COVERAGE:
        issues.append(
            GraphIssue(
                code="invalid_collection_coverage",
                souvenir=souvenir_name,
                detail=coverage,
            )
        )
        return None
    raw_source_refs = raw.get("source_refs", [])
    if not isinstance(raw_source_refs, list):
        issues.append(
            GraphIssue(
                code="invalid_collection_sources",
                souvenir=souvenir_name,
            )
        )
        raw_source_refs = []
    source_refs: list[str] = []
    for raw_ref in raw_source_refs:
        source_ref = _normalize_source_ref(str(raw_ref))
        if source_ref is None:
            issues.append(
                GraphIssue(
                    code="invalid_collection_source",
                    souvenir=souvenir_name,
                    detail=str(raw_ref),
                )
            )
            continue
        if source_ref not in source_refs:
            source_refs.append(source_ref)

    raw_closure_refs = raw.get("closure_source_refs", [])
    if not isinstance(raw_closure_refs, list):
        issues.append(
            GraphIssue(
                code="invalid_collection_closure_sources",
                souvenir=souvenir_name,
            )
        )
        raw_closure_refs = []
    closure_refs: list[str] = []
    for raw_ref in raw_closure_refs:
        source_ref = _normalize_source_ref(str(raw_ref))
        if source_ref is None:
            issues.append(
                GraphIssue(
                    code="invalid_collection_closure_source",
                    souvenir=souvenir_name,
                    detail=str(raw_ref),
                )
            )
            continue
        if source_ref not in closure_refs:
            closure_refs.append(source_ref)
    if coverage == "closed" and not closure_refs:
        issues.append(
            GraphIssue(
                code="closed_collection_without_provenance",
                souvenir=souvenir_name,
            )
        )
    return CollectionState(
        coverage=coverage,
        source_refs=tuple(source_refs),
        closure_source_refs=tuple(closure_refs),
    )


def normalize_souvenir_name(raw: str) -> str | None:
    value = raw.strip().strip("`").replace("\\", "/")
    if not value or "|" in value or "#" in value:
        return None
    while value.startswith("./"):
        value = value[2:]
    value = value.lstrip("/")
    for prefix in ("memory/capsules/", "capsules/"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    if value.endswith(".md"):
        value = value[:-3]
    path = PurePosixPath(value)
    if not path.parts or any(
        part in {"", ".", ".."} or _PATH_PART_RE.fullmatch(part) is None
        for part in path.parts
    ):
        return None
    name = path.as_posix()
    if name == "index" or name.endswith("/index"):
        return None
    return name


def normalize_source_ref(raw: str) -> str | None:
    """Normalize an exact L0 wikilink reference used as graph provenance."""

    return _normalize_source_ref(raw)


def source_ref_exists(memory_root: Path, raw: str) -> bool:
    """Return whether a normalized L0 reference resolves to its exact heading."""

    source_ref = _normalize_source_ref(raw, memory_root)
    return bool(
        source_ref is not None
        and _source_ref_exists(memory_root.resolve(), source_ref)
    )


def _normalize_source_ref(raw: str, memory_root: Path | None = None) -> str | None:
    from daemon_core.memory.library_layout import internal_source_reference
    from daemon_core.paths import MEMORY_ROOT
    value = internal_source_reference(memory_root or MEMORY_ROOT, raw) or raw.strip()
    link = parse_note_link(value)
    if link is not None:
        value = link.path + "#" + link.anchor
    if not value or "|" in value or "#" not in value:
        return None
    path_part, anchor = value.split("#", 1)
    path_part = path_part.strip().replace("\\", "/").lstrip("/")
    anchor = anchor.strip()
    if path_part.endswith(".md"):
        path_part = path_part[:-3]
    path = PurePosixPath(path_part)
    if (
        not anchor
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or not path.as_posix().startswith("levels/L0_ground_zero/")
    ):
        return None
    return f"{path.as_posix()}#{anchor}"


def _source_ref_exists(memory_root: Path, source_ref: str) -> bool:
    path_part, anchor = source_ref.split("#", 1)
    candidate = (memory_root / f"{path_part}.md").resolve()
    try:
        candidate.relative_to(memory_root.resolve())
    except ValueError:
        return False
    if not candidate.is_file():
        return False
    try:
        content = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return any(
        line.lstrip("#").strip() == anchor
        for line in content.splitlines()
        if line.startswith("#")
    )


def _body_line_offset(content: str) -> int:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return 0
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            return index + 1
    return 0


def _souvenir_files(root: Path) -> list[tuple[str, Path]]:
    from daemon_core.memory.library_layout import unique_note_locations, vault_root
    files: list[tuple[str, Path]] = []
    candidates = ((item.name, item.path) for item in unique_note_locations(root.parent)) if vault_root(root.parent) is not None else (
        (path.relative_to(root).with_suffix("").as_posix(), path) for path in root.rglob("*.md"))
    for name, path in candidates:
        relative = Path(name + ".md")
        if path.stem == "index":
            continue
        name = relative.with_suffix("").as_posix()
        if name in RESERVED_SOUVENIR_NAMES:
            continue
        if name.startswith(RESERVED_SOUVENIR_PREFIXES):
            continue
        files.append((name, path))
    return sorted(files)


def _library_fingerprint(records: dict[str, SouvenirGraphRecord]) -> str:
    digest = hashlib.sha256()
    for name in sorted(records):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(records[name].source_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _derived_backlinks(index: MemoryGraphIndex) -> dict[str, list[dict]]:
    backlinks: dict[str, list[dict]] = {}
    names = set(index.souvenirs)
    seen: set[tuple[str, str, str]] = set()
    for source in sorted(index.souvenirs):
        for relation in index.souvenirs[source].relations:
            key = (source, relation.kind, relation.target)
            if key in seen:
                continue
            seen.add(key)
            if relation.target not in names or relation.target == source:
                continue
            backlinks.setdefault(relation.target, []).append(
                {
                    "source": source,
                    "kind": relation.kind,
                }
            )
    return backlinks


def _derived_collections(index: MemoryGraphIndex) -> dict[str, dict]:
    collections = {
        name: {
            "coverage": record.collection.coverage,
            "source_refs": list(record.collection.source_refs),
            "closure_source_refs": list(record.collection.closure_source_refs),
            "members": [],
        }
        for name, record in sorted(index.souvenirs.items())
        if record.collection is not None
    }
    seen: set[tuple[str, str]] = set()
    for source in sorted(index.souvenirs):
        for relation in index.souvenirs[source].relations:
            if relation.kind != MEMBERSHIP_RELATION:
                continue
            key = (source, relation.target)
            if key in seen:
                continue
            seen.add(key)
            collection = collections.get(relation.target)
            if collection is None:
                continue
            collection["members"].append(
                {
                    "path": source,
                    "role": relation.description,
                }
            )
    return collections


def _component_sizes(
    names: set[str],
    relations: list[MemoryRelation],
) -> tuple[int, ...]:
    neighbors = {name: set() for name in names}
    for relation in relations:
        neighbors[relation.source].add(relation.target)
        neighbors[relation.target].add(relation.source)
    unseen = set(names)
    sizes = []
    while unseen:
        root = unseen.pop()
        stack = [root]
        size = 0
        while stack:
            current = stack.pop()
            size += 1
            for neighbor in neighbors[current]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    stack.append(neighbor)
        sizes.append(size)
    return tuple(sorted(sizes, reverse=True))
