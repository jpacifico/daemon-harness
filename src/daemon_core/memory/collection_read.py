"""Bounded reads of established memory collection memberships."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from daemon_core.memory.graph import (
    MEMBERSHIP_RELATION,
    MemoryGraphIndex,
    normalize_souvenir_name,
    source_ref_exists,
)


MAX_COLLECTION_PAGE_MEMBERS = 8


class CollectionReadError(ValueError):
    """Raised when a collection cannot be read within the explicit contract."""


@dataclass(frozen=True)
class CollectionMember:
    path: str
    role: str
    source_refs: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "role": self.role,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class CollectionPage:
    center: str
    coverage: str
    known_member_count: int
    offset: int
    members: tuple[CollectionMember, ...]
    next_offset: int | None

    @property
    def known_members_complete(self) -> bool:
        return self.next_offset is None

    @property
    def claimed_exhaustive(self) -> bool:
        return self.coverage == "closed" and self.known_members_complete

    def as_dict(self) -> dict:
        return {
            "center": self.center,
            "coverage": self.coverage,
            "known_member_count": self.known_member_count,
            "offset": self.offset,
            "members": [member.as_dict() for member in self.members],
            "next_offset": self.next_offset,
            "known_members_complete": self.known_members_complete,
            "claimed_exhaustive": self.claimed_exhaustive,
        }


def read_collection_page(
    index: MemoryGraphIndex,
    center: str,
    *,
    memory_root: Path,
    offset: int = 0,
    limit: int = MAX_COLLECTION_PAGE_MEMBERS,
) -> CollectionPage:
    """Return only valid sourced `member_of` arcs for one collection center."""

    normalized = normalize_souvenir_name(center)
    if normalized is None or normalized != center:
        raise CollectionReadError("Invalid collection center path.")
    record = index.souvenirs.get(normalized)
    if record is None or record.collection is None:
        raise CollectionReadError(f"Unknown memory collection: {center}.")
    if offset < 0:
        raise CollectionReadError("Collection offset cannot be negative.")
    if limit < 1 or limit > MAX_COLLECTION_PAGE_MEMBERS:
        raise CollectionReadError(
            f"Collection limit must be between 1 and {MAX_COLLECTION_PAGE_MEMBERS}."
        )

    members: list[CollectionMember] = []
    seen: set[str] = set()
    source_existence: dict[str, bool] = {}

    def provenance_exists(source_ref: str) -> bool:
        if source_ref not in source_existence:
            source_existence[source_ref] = source_ref_exists(memory_root, source_ref)
        return source_existence[source_ref]

    for source in sorted(index.souvenirs):
        if source == normalized or source in seen:
            continue
        for relation in index.souvenirs[source].relations:
            if relation.kind != MEMBERSHIP_RELATION or relation.target != normalized:
                continue
            if (
                not relation.description
                or not relation.source_refs
                or not all(
                    provenance_exists(source_ref)
                    for source_ref in relation.source_refs
                )
            ):
                continue
            seen.add(source)
            members.append(
                CollectionMember(
                    path=source,
                    role=relation.description,
                    source_refs=relation.source_refs,
                )
            )
            break

    page_members = tuple(members[offset : offset + limit])
    end = offset + len(page_members)
    next_offset = end if end < len(members) else None
    return CollectionPage(
        center=normalized,
        coverage=record.collection.coverage,
        known_member_count=len(members),
        offset=offset,
        members=page_members,
        next_offset=next_offset,
    )
