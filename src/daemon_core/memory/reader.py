import logging
from datetime import datetime
from pathlib import Path

import yaml

from daemon_core.paths import MEMORY_ROOT
from daemon_core.memory.library_layout import resolve_note, unique_note_locations, vault_root, LibraryLayoutError
from daemon_core.memory.metadata import SouvenirMetadata, parse_souvenir_metadata
from daemon_core.memory.writer import GroundZeroReference, active_context_path, souvenir_recalls_path, conversation_state_path, ground_zero_conversation_path, immediate_focus_path, legacy_carnet_path, legacy_current_discussion_path, legacy_moment_memory_path, read_ground_zero_record, safe_memory_user_id, stance_register_path, trajectory_path, user_agenda_path, user_context_path, user_preferences_path

RESERVED_SOUVENIR_NAMES = {"style_de_communication", "theme/communication_style"}
RESERVED_SOUVENIR_PREFIXES = ("templates/",)
SYSTEM_MANAGED_SOUVENIR_PREFIXES = ("theme/scenes_episodiques/",)
MAX_ZOOM_LINKED_SOUVENIRS = 12

logger = logging.getLogger("daemon_core.memory.reader")


def is_system_managed_souvenir(name: str) -> bool:
    """True for souvenirs owned by a dedicated producer, not MemoryAgent/Dreaming."""

    normalized = name.strip().replace("\\", "/").strip("/")
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in SYSTEM_MANAGED_SOUVENIR_PREFIXES
    )

def read_markdown(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

def read_markdown_with_legacy(path: Path, legacy_path: Path) -> str:
    content = read_markdown(path)
    return content if content.strip() else read_markdown(legacy_path)

def core_context_path() -> Path:
    return MEMORY_ROOT / "core" / "contexte_actif_minimal.md"

def souvenir_path(name: str) -> Path:
    return resolve_note(MEMORY_ROOT, name)

def load_core_context() -> str:
    return read_markdown(core_context_path())

def load_souvenir(name: str) -> str:
    try:
        return read_markdown(souvenir_path(name))
    except (FileNotFoundError, LibraryLayoutError):
        logger.warning("Note missing or ambiguous; exact read unavailable | name=%s", name)
        return ""

def load_souvenir_metadata(name: str) -> SouvenirMetadata:
    try:
        return parse_souvenir_metadata(load_souvenir(name))
    except (ValueError, yaml.YAMLError) as exc:
        logger.warning(
            "Métadonnées souvenir ignorées : frontmatter invalide | "
            "name=%s | error=%s",
            name,
            exc.__class__.__name__,
        )
        return SouvenirMetadata()

def load_souvenir_metadata_records() -> dict[str, SouvenirMetadata]:
    records = {}
    for item in unique_note_locations(MEMORY_ROOT):
        name = item.name
        if item.path.stem == "index" or name in RESERVED_SOUVENIR_NAMES or name.startswith(RESERVED_SOUVENIR_PREFIXES):
            continue
        try:
            metadata = parse_souvenir_metadata(read_markdown(item.path))
        except (ValueError, yaml.YAMLError):
            continue
        if not metadata.is_empty:
            records[name] = metadata
    return records

def souvenir_subject_projection() -> dict[str, list[str]]:
    return _metadata_projection("subjects")

def souvenir_facet_projection() -> dict[str, list[str]]:
    return _metadata_projection("facets")

def list_souvenirs_for_metadata(
    subjects: list[str] | None = None,
    facets: list[str] | None = None,
) -> list[str]:
    """Exact lookup over canonical metadata labels; semantic choice stays with the LLM."""
    wanted_subjects = {item.casefold() for item in (subjects or []) if item.strip()}
    wanted_facets = {item.casefold() for item in (facets or []) if item.strip()}
    selected = []
    for name, metadata in load_souvenir_metadata_records().items():
        subject_match = wanted_subjects.intersection(
            item.casefold() for item in metadata.subjects
        )
        facet_match = wanted_facets.intersection(
            item.casefold() for item in metadata.facets
        )
        axes_match = (
            (not wanted_subjects or bool(subject_match))
            and (not wanted_facets or bool(facet_match))
        )
        if axes_match and (wanted_subjects or wanted_facets):
            selected.append(name)
    return selected

def load_metadata_souvenir_index(
    subjects: list[str] | None = None,
    facets: list[str] | None = None,
) -> str:
    indexed = _parse_souvenir_index(read_markdown(souvenir_index_path()))
    return "\n".join(
        indexed.get(name) or _souvenir_catalog_entry(name)
        for name in list_souvenirs_for_metadata(subjects=subjects, facets=facets)
    )

def load_named_souvenir_index(names: list[str]) -> str:
    indexed = _parse_souvenir_index(read_markdown(souvenir_index_path()))
    available = set(list_available_souvenirs())
    selected = []
    for name in names:
        if name in available and name not in selected:
            selected.append(name)
    return "\n".join(
        indexed.get(name) or _souvenir_catalog_entry(name) for name in selected
    )

def load_souvenir_projection_map(max_souvenirs_per_entry: int = 20) -> str:
    """Human-readable diagnostic projection, regenerated from souvenir frontmatter."""
    sections = []
    for title, projection in (
        ("Sujets", souvenir_subject_projection()),
        ("Facettes", souvenir_facet_projection()),
    ):
        lines = [f"## {title}"]
        for label, names in projection.items():
            visible = names[:max_souvenirs_per_entry]
            suffix = f" (+{len(names) - len(visible)})" if len(names) > len(visible) else ""
            lines.append(f"- {label}: {', '.join(visible)}{suffix}")
        if len(lines) > 1:
            sections.append("\n".join(lines))
    return "\n\n".join(sections)

def load_souvenir_metadata_label_catalog() -> str:
    """Compact labels-only map for an LLM to choose projection axes."""
    sections = []
    for title, projection in (
        ("Sujets disponibles", souvenir_subject_projection()),
        ("Facettes disponibles", souvenir_facet_projection()),
    ):
        if not projection:
            continue
        lines = [f"## {title}"]
        lines.extend(f"- {label} ({len(names)})" for label, names in projection.items())
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def load_souvenir_facet_label_catalog() -> str:
    """Return reusable non-identifying discovery facets for the memory writer."""

    projection = souvenir_facet_projection()
    if not projection:
        return ""
    lines = ["## Facettes disponibles"]
    lines.extend(f"- {label} ({len(names)})" for label, names in projection.items())
    return "\n".join(lines)


def load_souvenir_structural_catalog(*, include_subjects: bool = True) -> str:
    """Render discovery structure without exposing descriptive index anchors.

    The MemoryAgent needs to know which souvenirs already exist, but the
    human-authored index descriptions can contain enough detail to be mistaken
    for souvenir evidence. Keep that material behind an exact souvenir load.
    """

    lines = []
    locations = {item.name: item.path for item in unique_note_locations(MEMORY_ROOT)}
    vault = vault_root(MEMORY_ROOT)
    for name in list_available_souvenirs():
        try:
            metadata = parse_souvenir_metadata(read_markdown(locations[name]))
        except (ValueError, yaml.YAMLError):
            logger.warning("Métadonnées souvenir ignorées : frontmatter invalide | name=%s", name)
            metadata = SouvenirMetadata()
        qualifiers = []
        if vault is not None:
            qualifiers.append("location: " + locations[name].relative_to(vault).as_posix())
        if include_subjects and metadata.subjects:
            qualifiers.append("subjects: " + ", ".join(metadata.subjects))
        if metadata.facets:
            qualifiers.append("facets: " + ", ".join(metadata.facets))
        suffix = " | " + " | ".join(qualifiers) if qualifiers else ""
        lines.append(f"- {name}{suffix}")
    return "\n".join(lines)

def _metadata_projection(field_name: str) -> dict[str, list[str]]:
    projection: dict[str, list[str]] = {}
    canonical_labels: dict[str, str] = {}
    for name, metadata in load_souvenir_metadata_records().items():
        for label in getattr(metadata, field_name):
            key = label.casefold()
            canonical = canonical_labels.setdefault(key, label)
            projection.setdefault(canonical, []).append(name)
    return dict(sorted(projection.items(), key=lambda item: item[0].casefold()))

def list_available_souvenirs() -> list[str]:
    return sorted(
        name
        for item in unique_note_locations(MEMORY_ROOT)
        if item.path.stem != "index"
        for name in [item.name]
        if name not in RESERVED_SOUVENIR_NAMES
        if not name.startswith(RESERVED_SOUVENIR_PREFIXES)
    )

def souvenir_index_path() -> Path:
    return MEMORY_ROOT / "capsules" / "index.md"

def load_souvenir_index() -> str:
    names = list_available_souvenirs()
    indexed = parse_souvenir_index(read_markdown(souvenir_index_path()))
    return "\n".join(indexed.get(name) or _souvenir_catalog_entry(name) for name in names)


def parse_souvenir_index(content: str) -> dict[str, str]:
    """Return exact catalog lines keyed by their canonical souvenir path."""
    return _parse_souvenir_index(content)

def souvenir_domain_names() -> set[str]:
    """Return path prefixes that group other memories through descendant paths."""
    domains = set()
    for name in list_available_souvenirs():
        parts = name.split("/")
        for depth in range(1, len(parts)):
            domains.add("/".join(parts[:depth]))
    return domains

def load_root_souvenir_index() -> str:
    """Build a root map with one line per entry at depth two or less.

    Entries with deeper descendants include a domain-size marker.
    Their details are available through load_domain_souvenir_index."""
    names = list_available_souvenirs()
    indexed = _parse_souvenir_index(read_markdown(souvenir_index_path()))
    available = set(names)
    roots: list[str] = []
    descendants: dict[str, list[str]] = {}
    for name in names:
        parts = name.split("/")
        root = name if len(parts) <= 2 else "/".join(parts[:2])
        if root not in descendants:
            descendants[root] = []
            roots.append(root)
        if name != root:
            descendants[root].append(name)
    lines = []
    for root in roots:
        line = indexed.get(root)
        if not line:
            line = _souvenir_catalog_entry(root) if root in available else _synthesized_domain_entry(root, descendants[root])
        if descendants[root]:
            line = f"{line} [domain: {len(descendants[root])} souvenirs]"
        lines.append(line)
    return "\n".join(lines)

def _synthesized_domain_entry(root: str, children: list[str]) -> str:
    stems = sorted({child[len(root) + 1 :].split("/")[0] for child in children})
    return f"- {root}: Domain. — {', '.join(stems)}"

def list_domain_souvenirs(selected: list[str]) -> list[str]:
    """Return selected memory names together with their complete subtrees."""
    return [
        name
        for name in list_available_souvenirs()
        if any(name == s or name.startswith(s + "/") for s in selected)
    ]


def list_zoom_souvenirs(selected: list[str]) -> list[str]:
    """Expand selected subtrees with their explicit Markdown links.

    Follow one bounded hop, never a recursive traversal of the graph.
    The LLM remains responsible for selecting the notes finally loaded."""
    covered = list_domain_souvenirs(selected)
    available = set(list_available_souvenirs())
    linked: list[str] = []
    # Selected maps declare outgoing links for their subtree. Reading every
    # descendant's links too would turn expansion into
    # an unbounded graph traversal that brings too many topics into competition.
    for source in selected:
        if source not in available:
            continue
        for name in _souvenir_links(load_souvenir(source), source=source):
            if name in available and name not in covered and name not in linked:
                linked.append(name)
                if len(linked) >= MAX_ZOOM_LINKED_SOUVENIRS:
                    return covered + linked
    return covered + linked


def load_domain_souvenir_index(selected: list[str]) -> str:
    """Build an expanded view of selected subtrees and explicitly linked neighbors."""
    indexed = _parse_souvenir_index(read_markdown(souvenir_index_path()))
    return "\n".join(indexed.get(name) or _souvenir_catalog_entry(name) for name in list_zoom_souvenirs(selected))


def _souvenir_links(content: str, *, source: str = "") -> list[str]:
    if vault_root(MEMORY_ROOT) is not None:
        from daemon_core.memory.graph import parse_souvenir_graph
        return list(dict.fromkeys(relation.target for relation in
            parse_souvenir_graph(source, content, memory_root=MEMORY_ROOT).relations))
    links: list[str] = []
    in_links = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_links = stripped.casefold() == "## liens proches"
            continue
        if not in_links or not stripped.startswith("- "):
            continue
        name = stripped[2:].partition(":")[0].strip().strip("`")
        if name and name not in links:
            links.append(name)
    return links

def _parse_souvenir_index(content: str) -> dict[str, str]:
    entries = {}
    for line in content.splitlines():
        name = _index_entry_name(line)
        if name and name not in RESERVED_SOUVENIR_NAMES and not name.startswith(RESERVED_SOUVENIR_PREFIXES):
            entries[name] = line
    return entries

def _index_entry_name(line: str) -> str:
    stripped = line.strip()
    if not stripped.startswith("- "):
        return ""
    body = stripped[2:].strip()
    name, _, _description = body.partition(":")
    return name.strip()

def _souvenir_catalog_entry(name: str) -> str:
    content = load_souvenir(name).strip()
    title = _first_markdown_heading(content) or name
    return f"- {name}: {title}"

def _first_markdown_heading(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""

def load_souvenir_recalls() -> str:
    return read_markdown(souvenir_recalls_path())

def load_trajectory(user_id: str) -> str:
    return read_markdown(trajectory_path(user_id))

def load_stance_register(user_id: str) -> str:
    return read_markdown_with_legacy(stance_register_path(user_id), legacy_carnet_path(user_id))

def load_carnet(user_id: str) -> str:
    return load_stance_register(user_id)

def load_user_context(user_id: str) -> str:
    return read_markdown(user_context_path(user_id))

def load_user_preferences(user_id: str) -> str:
    return read_markdown(user_preferences_path(user_id))

def load_user_agenda(user_id: str) -> str:
    return read_markdown(user_agenda_path(user_id))

def load_user_agenda_pro(user_id: str) -> str:
    """Read the legacy synchronized professional-calendar file, if present.

    The file is machine-managed; neither MemoryAgent nor Dreaming writes it."""
    return read_markdown(user_agenda_path(user_id).with_name("agenda_pro.md"))

def load_active_context(user_id: str) -> str:
    return read_markdown_with_legacy(active_context_path(user_id), legacy_moment_memory_path(user_id))

def load_immediate_focus(user_id: str) -> str:
    return read_markdown_with_legacy(immediate_focus_path(user_id), legacy_current_discussion_path(user_id))


def load_conversation_state(user_id: str) -> str:
    return read_markdown(conversation_state_path(user_id))

def load_current_discussion(user_id: str) -> str:
    return load_immediate_focus(user_id)

def load_moment_memory(user_id: str) -> str:
    return load_active_context(user_id)

def load_recent_ground_zero(user_id: str, max_chars: int = 5000) -> str:
    content = _recent_ground_zero_content(user_id)
    return content[-max_chars:] if len(content) > max_chars else content

def load_recent_dialogue(user_id: str, max_turns: int = 8) -> str:
    content = _recent_ground_zero_content(user_id)
    return _dialogue_tail(content, max_turns=max_turns)


def load_dialogue_since(
    user_id: str,
    since: datetime | str,
    *,
    max_turns: int = 32,
) -> str:
    """Return a bounded exact view of dialogue recorded since ``since``.

    Deferred judgments need to see what changed after their source decision.
    Keeping only the latest messages can discard an immediate answer before a
    busy conversation reaches the deferred judgment. When the interval is
    larger than the budget, retain both its beginning and its current edge.
    """

    try:
        boundary = datetime.fromisoformat(since) if isinstance(since, str) else since
    except ValueError:
        return ""
    root = ground_zero_conversation_path(user_id).parent
    if not root.exists():
        return ""
    safe_user = safe_memory_user_id(user_id)
    selected: list[str] = []
    for path in sorted(root.glob(f"{safe_user}_????-??-??.md")):
        for chunk in _dialogue_chunks(read_markdown(path)):
            timestamp = _dialogue_chunk_datetime(chunk)
            if timestamp is None:
                continue
            comparable_boundary = boundary
            if timestamp.tzinfo is None and comparable_boundary.tzinfo is not None:
                timestamp = timestamp.replace(tzinfo=comparable_boundary.tzinfo)
            elif timestamp.tzinfo is not None and comparable_boundary.tzinfo is None:
                comparable_boundary = comparable_boundary.replace(tzinfo=timestamp.tzinfo)
            if timestamp >= comparable_boundary:
                selected.append(chunk)

    budget = max(1, int(max_turns))
    if len(selected) <= budget:
        return "\n\n".join(selected)
    beginning = (budget + 1) // 2
    end = budget - beginning
    omitted = len(selected) - budget
    chunks = selected[:beginning]
    chunks.append(f"[... {omitted} intermediate exact message(s) omitted ...]")
    if end:
        chunks.extend(selected[-end:])
    return "\n\n".join(chunks)


def load_prior_dialogue(
    user_id: str,
    *,
    max_chars: int = 6000,
    max_files: int = 2,
) -> str:
    """Return bounded exact excerpts from the days before today's L0 journal.

    The canonical recent window can be filled entirely by a busy current day.
    Keep a small tail from each preceding journal so references such as "hier"
    and "ces derniers jours" remain directly consumable.  The immediately
    preceding day receives most of the budget, while an older day keeps a
    smaller foothold.  The projection preserves complete L0 turns and never
    rewrites their content.
    """

    budget = max(0, int(max_chars))
    file_limit = max(0, int(max_files))
    if budget == 0 or file_limit == 0:
        return ""
    current = ground_zero_conversation_path(user_id)
    root = current.parent
    if not root.exists():
        return ""
    safe_user = safe_memory_user_id(user_id)
    paths = [
        path
        for path in sorted(root.glob(f"{safe_user}_????-??-??.md"))
        if path.name < current.name
    ][-file_limit:]
    if not paths:
        return ""

    if len(paths) == 1:
        file_budgets = [budget]
    else:
        latest_budget = (budget * 3) // 4
        older_budget = budget - latest_budget
        older_count = len(paths) - 1
        file_budgets = [older_budget // older_count] * older_count
        file_budgets[-1] += older_budget % older_count
        file_budgets.append(latest_budget)
    excerpts = [
        _dialogue_tail_by_chars(read_markdown(path), max_chars=file_budget)
        for path, file_budget in zip(paths, file_budgets, strict=True)
    ]
    return "\n\n".join(excerpt for excerpt in excerpts if excerpt.strip())


def load_recent_dialogue_through(
    user_id: str,
    reference: GroundZeroReference | dict,
    max_turns: int = 8,
    max_files: int = 3,
) -> str:
    """Read a dialogue window whose upper bound is one verified L0 entry.

    Durable after-turn jobs can run well after their source turn. Their context
    must stop at that turn instead of drifting to the current journal head.
    """

    ref = (
        reference
        if isinstance(reference, GroundZeroReference)
        else GroundZeroReference.from_dict(reference)
    )
    read_ground_zero_record(ref)
    target = (MEMORY_ROOT / ref.path).resolve()
    conversations = (
        MEMORY_ROOT / "levels" / "L0_ground_zero" / "conversations"
    ).resolve()
    try:
        target.relative_to(conversations)
    except ValueError as exc:
        raise ValueError("ground zero reference is outside conversations") from exc

    safe_user = safe_memory_user_id(user_id)
    if not target.name.startswith(f"{safe_user}_"):
        raise ValueError("ground zero reference belongs to another user")
    paths = [
        path
        for path in sorted(conversations.glob(f"{safe_user}_????-??-??.md"))
        if path.name <= target.name
    ][-max(1, int(max_files)):]
    chunks = []
    for path in paths:
        if path.resolve() == target:
            with path.open("rb") as handle:
                encoded = handle.read(ref.offset + ref.length)
            chunks.append(encoded.decode("utf-8"))
        else:
            chunks.append(read_markdown(path))
    return _dialogue_tail("\n".join(chunks), max_turns=max_turns)


def limit_recent_dialogue(recent_dialogue: str, max_turns: int) -> str:
    """Apply the same turn bound to an already materialized L0 window."""

    return _dialogue_tail(recent_dialogue, max_turns=max_turns)


def _dialogue_tail(content: str, max_turns: int) -> str:
    chunks = _dialogue_chunks(content)
    return "\n\n".join(chunks[-max(1, int(max_turns)):])


def _dialogue_chunk_datetime(chunk: str) -> datetime | None:
    header = chunk.partition("\n")[0]
    if not header.startswith("## ") or " — " not in header:
        return None
    value = header[3:].rsplit(" — ", 1)[0].strip()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _dialogue_tail_by_chars(content: str, *, max_chars: int) -> str:
    budget = max(0, int(max_chars))
    if budget == 0:
        return ""
    selected: list[str] = []
    rendered_chars = 0
    for chunk in reversed(_dialogue_chunks(content)):
        separator_chars = 2 if selected else 0
        required = len(chunk) + separator_chars
        if required > budget - rendered_chars:
            break
        selected.append(chunk)
        rendered_chars += required
    return "\n\n".join(reversed(selected))


def _dialogue_chunks(content: str) -> list[str]:
    if not content.strip():
        return []
    chunks: list[str] = []
    current: list[str] = []
    for line in content.splitlines():
        if line.startswith("## ") and " — " in line:
            if current:
                chunks.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    # Historical transcript recovery appends exact records without rewriting
    # journals or invalidating byte-offset receipts. The read projection must
    # follow their source dates, not the order in which repairs were appended.
    dates = [_dialogue_chunk_datetime(chunk) for chunk in chunks]
    if all(date is not None for date in dates):
        chunks = [
            chunk for _, chunk in sorted(
                zip(dates, chunks), key=lambda item: item[0].timestamp()
            )
        ]
    return chunks


def without_trailing_user_message(recent_dialogue: str, message: str) -> str:
    """Remove the current user turn when L0 was durably written before inference."""
    chunks = []
    current = []
    for line in recent_dialogue.splitlines():
        if line.startswith("## ") and " — " in line:
            if current:
                chunks.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    if not chunks:
        return recent_dialogue
    header, _, body = chunks[-1].partition("\n")
    role = header.rsplit(" — ", 1)[-1].strip().lower()
    if role == "user" and body.strip() == message.strip():
        chunks.pop()
    return "\n\n".join(chunks)


def _recent_ground_zero_content(user_id: str, max_files: int = 3) -> str:
    """Read the latest raw journals across a day boundary.

    L0 remains split by date; this is only a read projection for conversational
    continuity around midnight.
    """
    current = ground_zero_conversation_path(user_id)
    root = current.parent
    if not root.exists():
        return ""
    safe_user = safe_memory_user_id(user_id)
    paths = sorted(root.glob(f"{safe_user}_????-??-??.md"))[-max_files:]
    return "\n".join(read_markdown(path) for path in paths)
