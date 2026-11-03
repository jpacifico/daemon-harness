"""Native note syntax for the isolated Obsidian-format prototype.

This module translates explicit references only. It never chooses a relation,
turns a label into an entity, or searches for a similarly named file.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import posixpath
import re
from urllib.parse import unquote, urlsplit


COLLECTION_FIELDS = ("coverage", "source_refs", "closure_source_refs")
MANAGED_PROPERTIES = {"subjects", "facets", "collection"} | {
    "collection_" + key for key in COLLECTION_FIELDS
}
_WIKI = re.compile(r"^\[\[([^\]\r\n]+)\]\](?:\s*:\s*(.*))?$")
_MARKDOWN = re.compile(
    r"^\[([^\]\r\n]*)\]\((?:<([^>\r\n]+)>|([^\s()\r\n]+))\)"
    r"(?:\s*:\s*(.*))?$"
)


def obsidian_format_enabled() -> bool:
    return os.getenv("DAEMON_CORE_OBSIDIAN_FORMAT", "") == "1"


@dataclass(frozen=True)
class NoteLink:
    path: str
    anchor: str
    label: str
    description: str
    syntax: str


def parse_note_link(payload: str) -> NoteLink | None:
    """Read a whole relation bullet, not arbitrary prose or embedded code."""
    match = _WIKI.fullmatch(payload)
    if match:
        reference, _, label = match[1].partition("|")
        path, _, anchor = reference.partition("#")
        return NoteLink(path.strip(), anchor, label, (match[2] or "").strip(), "wikilink")
    match = _MARKDOWN.fullmatch(payload)
    if not match:
        return None
    raw = match[2] or match[3]
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.query:
        return None
    return NoteLink(unquote(parsed.path), unquote(parsed.fragment), match[1],
                    (match[4] or "").strip(), "markdown")


def souvenir_link_target(link: NoteLink, source: str, *, memory_root=None) -> str | None:
    """Resolve explicit vault/relative paths; caller validates the note name."""
    from daemon_core.memory.library_layout import name_from_vault_link, FOLDERS
    from daemon_core.paths import MEMORY_ROOT
    path = link.path.replace("\\", "/")
    try:
        physical = name_from_vault_link(memory_root or MEMORY_ROOT, path, source=source)
        if physical is not None:
            return physical
        if path.split("/", 1)[0] in FOLDERS:
            return None
    except (OSError, ValueError):
        return None
    if any(c in path for c in "\x00\r\n#|:"):
        return None
    if not path:
        return source if link.anchor else None
    if path.startswith("memory/capsules/"):
        # Historical daemon spelling, still readable without guessing a name.
        path = path[len("memory/"):]
    elif path.startswith("theme/"):
        path = "capsules/" + path
    elif path.startswith("./") or path.startswith("../") or (
        link.syntax == "markdown" and not path.startswith(("/", "capsules/"))
    ):
        path = posixpath.join("capsules", posixpath.dirname(source), path)
    elif not path.startswith(("/", "capsules/")):
        # A qualified historic name may also be outside theme/.
        path = "capsules/" + path
    path = posixpath.normpath(path.lstrip("/"))
    if not path.startswith("capsules/"):
        return None
    return path[len("capsules/"):]


def collection_properties(frontmatter: dict) -> dict | None:
    """Read either schema; refuse conflicting dual declarations."""
    legacy = frontmatter.get("collection")
    flat = {key: frontmatter["collection_" + key]
            for key in COLLECTION_FIELDS if "collection_" + key in frontmatter}
    if legacy is not None and not isinstance(legacy, dict):
        raise ValueError("collection frontmatter must be a mapping")
    if legacy is not None and flat:
        raise ValueError("conflicting collection representations")
    return dict(legacy) if legacy is not None else flat or None


def native_frontmatter(frontmatter: dict) -> dict:
    """Flatten only the known collection schema, without losing extra fields."""
    result = dict(frontmatter)
    if "collection" in result:
        collection = collection_properties(result)
        if collection is None or set(collection) - set(COLLECTION_FIELDS):
            raise ValueError("unsupported collection properties; original must be preserved")
        result.pop("collection")
        for key, value in collection.items():
            result["collection_" + key] = value
    for key, value in result.items():
        if not isinstance(key, str) or isinstance(value, dict) or (
            isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value)
        ):
            raise ValueError("nested or unnamed native property; original must be preserved")
    return result


def prepare_souvenir_format(content: str, name: str) -> str:
    """Render existing structural declarations as links in the opt-in writer."""
    if not obsidian_format_enabled():
        return content
    from daemon_core.memory.graph import parse_souvenir_graph
    from daemon_core.memory.metadata import parse_souvenir_frontmatter, _render_frontmatter

    record = parse_souvenir_graph(name, content)
    lines = content.splitlines(keepends=True)
    for relation in record.relations:
        if relation.syntax != "legacy":
            continue
        line = lines[relation.line - 1]
        prefix = line[:len(line) - len(line.lstrip())]
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        description = ": " + relation.description if relation.description else ""
        lines[relation.line - 1] = (
            f"{prefix}- [[capsules/{relation.target}]]{description}{ending}"
        )
    rendered = "".join(lines)
    from daemon_core.memory.library_layout import render_vault_links
    from daemon_core.paths import MEMORY_ROOT
    rendered = render_vault_links(MEMORY_ROOT, rendered)
    frontmatter, body = parse_souvenir_frontmatter(rendered)
    return _render_frontmatter(frontmatter, body) if frontmatter else rendered
