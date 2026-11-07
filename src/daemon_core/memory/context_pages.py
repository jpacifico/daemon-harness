"""Opt-in context pages and rebuildable navigation for an isolated prototype.

The model declares kind and view membership. This module validates syntax and
renders links; it does not infer subject matter, resolution or relationships.
"""
from __future__ import annotations

from collections import Counter
import json
import logging
import os
from pathlib import Path
import re
import yaml

from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.library_layout import garden_rule_block

logger = logging.getLogger(__name__)
MARKER = ".memory-structure-prototype.json"
GENERATED = "<!-- daemon-memory-structure:v1; generated navigation -->"
VIEWS = ("Projects", "Areas", "Resources", "Garden", "Archives")
KINDS = frozenset(("project", "area", "situation", "person", "group", "organization",
                   "place", "object", "resource", "knowledge", "episode"))
PROPERTIES = frozenset(("kind", "memory_views", "memory_areas"))
SECTIONS = frozenset(("Scope", "Last confirmed state", "History", "Decisions",
                      "Still unknown", "Links", "Sources"))

PAGE_RULES = """Context pages — isolated memory-structure V1:
- Keep one canonical note per coherent identity or history. Reuse its existing
  path, preserve its facts and exact sources; never migrate unrelated notes.
- New paths use English, under theme/<domain>/<subject> or
  theme/<domain>/<subdomain>/<subject>, maximum three levels under theme.
- Use flat YAML properties: kind, memory_views, memory_areas, subjects, facets; aliases only
  for genuine alternate names of the same note, preserving personal aliases.
- kind is one of: project, area, situation, person, group, organization, place,
  object, resource, knowledge, episode. Do not turn every mention into a note.
- memory_views lists explicit relevant entries from Projects, Areas, Resources,
  Garden, Archives. Projects follow intended outcomes; Areas ongoing domains,
  roles, relationships and situations; Resources references to use; Garden
  as defined below. Archives offers past or inactive histories with their
  exact state. Views can overlap.
  A closed project keeps its identity; silence never establishes closure.
- memory_areas is an optional list of meaningful durable domain labels in
  English (lowercase slugs, e.g. health, family, work). Reuse existing labels
  when appropriate; no mandatory taxonomy. These are navigation, not facts.
- Preserve subjects/facets as open discovery labels. View membership is not a
  factual relationship, evidence of completion, an access control or a deadline.
- Use a readable title and English section names. For project/situation use
  Scope, Last confirmed state, History; include Decisions, Still unknown,
  Links, Sources only when meaningful. Other kinds use relevant sections.
  Body content keeps the conversation's language. Never fill empty headings.
- In Last confirmed state distinguish the date of evidence from file revision.
  Keep important earlier events, failed attempts, reasons, corrections and
  provenance in History. Keep existing factual lines verbatim when updating;
  moving them between sections is allowed. Never replace history by a summary.
- Link only supplied exact notes or exact L0 sources, using native vault-root
  wikilinks. Explain meaningful links briefly. Do not invent paths or links to
  fill a graph. Do not copy linked facts into navigation or Area pages.
- People retain their own preferences and dated roles; group membership does
  not prove presence today. A resource reference does not mean it was read.
  An assistant suggestion is not an adopted idea. Agenda remains read-only.
- Existing REM episodes remain owned by REM. Do not rewrite them or create a
  Retrospective section. This prototype works without new REM summaries.
- Return the existing response schema, complete note and compact index_line.
""" + garden_rule_block(growth=True)


def structure_enabled(memory_root: Path | None = None) -> bool:
    if os.getenv("DAEMON_CORE_MEMORY_STRUCTURE") != "v1":
        return False
    if memory_root is None:
        from daemon_core.paths import MEMORY_ROOT
        memory_root = MEMORY_ROOT
    root = Path(memory_root).absolute()
    marker = root / MARKER
    try:
        if root.is_symlink() or marker.is_symlink() or root.resolve() != root:
            return False
        value = json.loads(marker.read_text(encoding="utf-8"))
        return value == {"schema": 1, "memory_root": str(root)}
    except (OSError, ValueError):
        return False


def context_page_prompt(system: str, *, merge: bool = False) -> str:
    """Replace the legacy format contract only inside a marked prototype."""
    if not structure_enabled():
        return system
    if not merge:
        start = system.find("- New thematic souvenir names must follow")
        end = system.find("- Perishability:", start)
        if start >= 0 and end > start:
            system = system[:start] + system[end:]
        system = system.replace(
            "- If you modify an old souvenir that does not follow this format, rewrite it in this format without information loss.",
            "- Adopt the context-page format on notes you legitimately update, preserving all information.",
        )
    from daemon_core.memory.library_layout import vault_root
    from daemon_core.paths import MEMORY_ROOT
    rules = PAGE_RULES
    if vault_root(MEMORY_ROOT) is not None:
        rules += ROOT_FOLDER_RULES
    return system + "\n\n" + rules


def validate_context_page(content: str) -> None:
    from daemon_core.memory.metadata import parse_souvenir_frontmatter
    metadata, body = parse_souvenir_frontmatter(content)
    if not (PROPERTIES & metadata.keys()):
        return  # Legacy notes remain readable without an invented classification.
    if metadata.get("kind") not in KINDS:
        raise ValueError("unknown context-page kind")
    views = metadata.get("memory_views")
    if not isinstance(views, list) or any(not isinstance(v, str) or v not in VIEWS for v in views):
        raise ValueError("invalid context-page views")
    if len(views) != len(set(views)):
        raise ValueError("duplicate context-page views")
    areas = metadata.get("memory_areas", [])
    if (not isinstance(areas, list) or len(areas) > 8 or any(
            not isinstance(a, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", a)
            for a in areas)):
        raise ValueError("invalid context-page area labels")
    if metadata["kind"] in {"project", "situation"}:
        headings = {line[3:].strip() for line in body.splitlines() if line.startswith("## ")}
        # A newly entrusted project may have no prior history. Requiring an
        # empty History contradicts the curator's no-empty-headings contract.
        # Existing factual history is protected by the canonical merge guard.
        if not {"Scope", "Last confirmed state"} <= headings:
            raise ValueError("context page lacks its scope or confirmed state")


def exact_body_lines(content: str) -> Counter:
    """Content-preservation guard for offline formatting (not semantic scoring)."""
    from daemon_core.memory.metadata import parse_souvenir_frontmatter
    _, body = parse_souvenir_frontmatter(content)
    return Counter(line.strip() for line in body.splitlines()
                   if line.strip() and not re.match(r"^#{1,6} ", line.strip()))


def _link(relative: Path) -> str:
    # Explicit relative path, never shortest-name resolution.
    return "[[" + relative.with_suffix("").as_posix() + "]]"


def rebuild_navigation(memory_root: Path) -> dict:
    """Regenerate only our own pages; never overwrite an unowned user file."""
    from daemon_core.memory.metadata import parse_souvenir_frontmatter
    root = memory_root.resolve()
    if not structure_enabled(root):
        return {"enabled": False}
    from daemon_core.memory.library_layout import vault_root
    vault = vault_root(root)
    if vault is not None:
        # The real folder tree is the user navigation; no derived page to maintain.
        return {"enabled": False, "reason": "physical-folders"}
    buckets = {name: [] for name in (*VIEWS, "People", "Situations", "All notes")}
    areas = {}
    invalid = []
    capsules = root / "capsules"
    if capsules.is_symlink():
        raise ValueError("symlink capsule root")
    for path in sorted(capsules.rglob("*.md")):
        if path.name == "index.md" or "templates" in path.relative_to(capsules).parts:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("symlink or escaped note")
        relative = path.relative_to(root)
        if any(char in relative.as_posix() for char in "[]|#\r\n"):
            invalid.append(relative.as_posix())
            continue
        entry = "- " + _link(relative)
        buckets["All notes"].append(entry)
        try:
            content = path.read_text(encoding="utf-8")
            validate_context_page(content)
            metadata, _ = parse_souvenir_frontmatter(content)
            for view in metadata.get("memory_views", []):
                buckets[view].append(entry)
            for area in metadata.get("memory_areas", []):
                areas.setdefault(area, []).append(entry)
            if metadata.get("kind") in {"person", "group", "organization"}:
                buckets["People"].append(entry)
            if metadata.get("kind") == "situation":
                buckets["Situations"].append(entry)
        except (ValueError, TypeError, yaml.YAMLError):
            invalid.append(relative.as_posix())
    pages = {}
    for name, entries in buckets.items():
        slug = name.lower().replace(" ", "_")
        pages[Path("views") / (slug + ".md")] = (
            GENERATED + f"\n# {name}\n\n"
            + ("\n".join(entries) if entries else "No classified notes yet.") + "\n"
        )
    if areas:
        pages[Path("views/areas.md")] = GENERATED + "\n# Areas\n\n" + "\n".join(
            f"- [[views/areas/{area}|{area.replace('_', ' ').replace('-', ' ').title()}]]"
            for area in sorted(areas)) + "\n\n## Other area notes\n\n" + "\n".join(
                entry for entry in buckets["Areas"]
                if not any(entry in entries for entries in areas.values())) + "\n"
    # Empty previously generated domains as well, so reclassification leaves
    # no stale links. User-owned pages are still protected below.
    for path in (root / "views/areas").glob("*.md"):
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", path.stem):
            areas.setdefault(path.stem, [])
    for area, entries in areas.items():
        pages[Path("views/areas") / (area + ".md")] = (
            GENERATED + "\n# " + area.replace('_', ' ').replace('-', ' ').title()
            + "\n\n" + ("\n".join(entries) or "No classified notes yet.") + "\n")
    pages[Path("home.md")] = (GENERATED + "\n# Memory\n\n"
        "Prototype · personal memories remain in their canonical notes.\n\n"
        + "\n".join(f"- [[views/{name.lower().replace(' ', '_')}|{name}]]"
                    for name in buckets) + "\n")
    pages[Path("views/home.md")] = pages[Path("home.md")]
    changed, protected = [], []
    for relative, content in pages.items():
        path = root / relative
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("escaped navigation path")
        with memory_file_lock(path):
            existing = path.read_text(encoding="utf-8") if path.exists() else None
            if existing is not None and not existing.startswith(GENERATED + "\n"):
                protected.append(relative.as_posix())
                continue
            if existing != content:
                atomic_write_text(path, content)
                changed.append(relative.as_posix())
    return {"enabled": True, "counts": {k: len(v) for k, v in buckets.items()},
            "changed": changed, "protected": protected, "invalid": invalid}


def refresh_navigation_safely(memory_root: Path) -> None:
    if structure_enabled(memory_root):
        try:
            rebuild_navigation(memory_root)
        except Exception:
            logger.exception("Prototype navigation refresh failed; exact notes remain authoritative")


ROOT_FOLDER_RULES = """
Physical vault folders (override the earlier navigation-only placement rules):
- Each note has ONE real location under Projects, Areas, Resources, Garden or
  Archives. These folders are the user-visible library, not additional views.
- `name` remains its stable technical identity. Never change `memory_id`.
  A user rename or move wins over old memory_views: update the existing note
  at its current location. Do not recreate it under its former filename.
- For each NEW capsule_updates item include `storage_path`: a full vault-relative
  Markdown path such as "Projects/Work/Atlas.md". Choose the relevant folder
  and optional English subfolders yourself from the exact entrusted content.
  Do not create root People, Situations or All notes categories. People and
  situations are kinds: locate them in their relevant domain, usually Areas.
- Projects: a desired outcome with an end. Areas: ongoing responsibilities,
  relationships and situations. Resources: useful references. Garden: as
  defined above. Archives: explicitly finished or inactive histories.
  Preserve provenance and uncertainty; mere age never proves completion.
- Existing notes require no storage_path. Their physical folder is authoritative.
  memory_views and memory_areas remain optional discovery labels, not commands
  to move a file. Prefer supplied physical links for the native Obsidian vault.
"""
