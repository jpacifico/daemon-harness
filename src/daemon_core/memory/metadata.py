from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import yaml
from daemon_core.memory.obsidian import (
    COLLECTION_FIELDS, MANAGED_PROPERTIES, collection_properties,
    native_frontmatter, obsidian_format_enabled,
)


MAX_METADATA_SUBJECTS = 8
MAX_METADATA_FACETS = 12
MAX_METADATA_LABEL_CHARS = 120


class _ObsidianDumper(yaml.SafeDumper):
    pass


_ObsidianDumper.add_representer(
    datetime,
    lambda dumper, value: dumper.represent_scalar(
        "tag:yaml.org,2002:timestamp", value.isoformat(),
    ),
)


@dataclass
class SouvenirMetadata:
    """Discovery metadata only; the Markdown body remains the memory truth."""

    subjects: list[str] = field(default_factory=list)
    facets: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: object) -> SouvenirMetadata:
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            return cls()
        return cls(
            subjects=_clean_labels(raw.get("subjects"), MAX_METADATA_SUBJECTS),
            facets=_clean_labels(raw.get("facets"), MAX_METADATA_FACETS),
        )

    @property
    def is_empty(self) -> bool:
        return not self.subjects and not self.facets


def parse_souvenir_frontmatter(content: str) -> tuple[dict, str]:
    """Return YAML frontmatter and the exact body following its closing fence."""
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, content

    closing = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip("\r\n") == "---"
        ),
        None,
    )
    if closing is None:
        raise ValueError("Unclosed YAML frontmatter.")

    raw = "".join(lines[1:closing])
    loaded = yaml.safe_load(raw) if raw.strip() else {}
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("YAML frontmatter must be an object.")
    return loaded, "".join(lines[closing + 1 :])


def parse_souvenir_metadata(content: str) -> SouvenirMetadata:
    frontmatter, _body = parse_souvenir_frontmatter(content)
    return SouvenirMetadata.from_raw(frontmatter)


def with_souvenir_metadata(content: str, metadata: SouvenirMetadata) -> str:
    """Merge discovery fields while preserving the Markdown body byte-for-byte."""
    frontmatter, body = parse_souvenir_frontmatter(content)
    cleaned = SouvenirMetadata.from_raw(
        {"subjects": metadata.subjects, "facets": metadata.facets}
    )
    if cleaned.subjects:
        frontmatter["subjects"] = cleaned.subjects
    else:
        frontmatter.pop("subjects", None)
    if cleaned.facets:
        frontmatter["facets"] = cleaned.facets
    else:
        frontmatter.pop("facets", None)

    return _render_frontmatter(frontmatter, body)


def preserve_existing_frontmatter(current: str, proposed: str) -> str:
    """Carry every existing frontmatter key into a proposed full rewrite."""
    current_frontmatter, _current_body = parse_souvenir_frontmatter(current)
    if not current_frontmatter:
        return proposed
    proposed_frontmatter, proposed_body = parse_souvenir_frontmatter(proposed)
    if obsidian_format_enabled():
        current_frontmatter = native_frontmatter(current_frontmatter)
        proposed_frontmatter = native_frontmatter(proposed_frontmatter)
    merged = dict(current_frontmatter)
    merged.update(proposed_frontmatter)
    if obsidian_format_enabled():
        from daemon_core.memory.context_pages import PROPERTIES, structure_enabled
        managed = MANAGED_PROPERTIES | (PROPERTIES if structure_enabled() else set())
        for key, value in current_frontmatter.items():
            if key not in managed:
                merged[key] = value
    return _render_frontmatter(merged, proposed_body)


def with_open_collection(
    content: str,
    *,
    source_refs: list[str] | tuple[str, ...],
) -> str:
    """Declare an open collection while preserving its Markdown body exactly."""

    frontmatter, body = parse_souvenir_frontmatter(content)
    current = collection_properties(frontmatter)
    if current is None:
        collection = {}
    elif isinstance(current, dict):
        collection = dict(current)
    else:
        raise ValueError("collection frontmatter must be a mapping")
    collection["coverage"] = "open"
    collection["source_refs"] = [f"[[{source_ref}]]" for source_ref in source_refs]
    for key in COLLECTION_FIELDS:
        frontmatter.pop("collection_" + key, None)
    frontmatter["collection"] = collection
    return _render_frontmatter(frontmatter, body)


def _render_frontmatter(frontmatter: dict, body: str) -> str:
    if obsidian_format_enabled():
        frontmatter = native_frontmatter(frontmatter)
    rendered = yaml.dump(
        frontmatter,
        Dumper=_ObsidianDumper if obsidian_format_enabled() else yaml.SafeDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).rstrip()
    return f"---\n{rendered}\n---\n{body}"


def _clean_labels(value: object, max_items: int) -> list[str]:
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        label = " ".join(item.split())[:MAX_METADATA_LABEL_CHARS].strip()
        key = label.casefold()
        if not label or key in seen:
            continue
        labels.append(label)
        seen.add(key)
        if len(labels) >= max_items:
            break
    return labels
