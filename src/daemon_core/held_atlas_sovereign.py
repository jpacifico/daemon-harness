"""Exact Atlas, bounded reads and scene inspection for canonical composition.

The historical response composer is retired. Canonical voice also consumes
the explicitly re-exported memory/persona loaders below. Historical Held Thought
readers and candidate flags are retired; active scene inspection stays here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from daemon_core.common_ground_library import (
    common_ground_library_enabled,
    resolve_common_ground_library,
)
from daemon_core.held_thought import (
    scene_scope_from_origin as scene_scope_from_origin,
)
from daemon_core.memory.reader import (
    load_active_context as load_active_context,
    load_conversation_state as load_conversation_state,
    load_immediate_focus as load_immediate_focus,
    load_recent_dialogue as load_recent_dialogue,
    load_user_agenda as load_user_agenda,
    load_user_context as load_user_context,
    without_trailing_user_message,
)
from daemon_core.memory.collection_read import (
    CollectionPage,
    read_collection_page,
)
from daemon_core.memory.graph import (
    MemoryGraphIndex,
    build_memory_graph_from_entries,
)
from daemon_core.paths import MEMORY_ROOT
from daemon_core.persona import (
    load_conversation_manner as load_conversation_manner,
    load_daemon_persona as load_daemon_persona,
)
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider
from daemon_core.single_pass_sovereign import (
    load_active_memory_entries,
    load_searchable_memory_entries,
)
from daemon_core.sovereign_library import frontmatter_routing_metadata
DEFAULT_MAX_ATLAS_CHARS = 120_000
DEFAULT_MAX_HOT_MEMORY_CHARS = 50_000
SOUVENIR_READ_MAX_COUNT = 8
SOUVENIR_READ_MAX_CHARS = 50_000
SCENE_COVERAGE_MAX_ITEMS = 8

logger = logging.getLogger(__name__)

_runtime_graph_lock = threading.Lock()
_runtime_graph_cache: dict[Path, MemoryGraphIndex] = {}


@dataclass(frozen=True)
class ExactMemoryAtlas:
    text: str
    souvenir_names: tuple[str, ...]
    rendered_chars: int
    sha256: str
    expired_souvenir_names: tuple[str, ...] = ()
    common_ground_names: tuple[str, ...] = ()
    collection_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExactSouvenirRead:
    souvenirs: tuple[tuple[str, str], ...]
    rejected_paths: tuple[str, ...]
    rendered_chars: int
    collection_page: CollectionPage | None = None

    @property
    def souvenir_names(self) -> tuple[str, ...]:
        return tuple(name for name, _content in self.souvenirs)

    @property
    def text(self) -> str:
        return "\n\n".join(
            f"## [[{name}]]\n\n{content}" for name, content in self.souvenirs
        )


@dataclass(frozen=True)
class SouvenirLibraryPreflight:
    paths: tuple[str, ...]
    reason: str
    confidence: float


@dataclass(frozen=True)
class SceneCoverage:
    established: tuple[str, ...]
    uncertain: tuple[str, ...]
    need: str
    handling_mode: str
    handling_plan: str

    def as_dict(self) -> dict[str, object]:
        return {
            "established": list(self.established),
            "uncertain": list(self.uncertain),
            "need": self.need,
            "handling": {
                "mode": self.handling_mode,
                "plan": self.handling_plan,
            },
        }


@dataclass(frozen=True)
class CurrentSceneModelView:
    """Read result that keeps absence distinct from an unprovable gap."""

    snapshot: object | None
    delta: str | None
    status: str

    @property
    def usable(self) -> bool:
        return self.status == "ready" and self.snapshot is not None


def build_exact_memory_atlas(
    memory_root: Path = MEMORY_ROOT,
    *,
    max_chars: int = DEFAULT_MAX_ATLAS_CHARS,
    today: date | None = None,
    user_id: str = "",
    platform: str | None = None,
    selected_souvenir_names: tuple[str, ...] | None = None,
    catalog_notice: str = "",
) -> ExactMemoryAtlas:
    """Render exact paths plus non-factual frontmatter routing metadata."""

    entries, expired, common_ground_names = _load_exact_library_entries(
        memory_root,
        today=today,
        user_id=user_id,
        platform=platform,
    )
    graph = _active_runtime_graph(entries, memory_root)
    if selected_souvenir_names is not None:
        available = {name: content for name, content in entries}
        requested = tuple(dict.fromkeys(selected_souvenir_names))
        if any(name not in available for name in requested):
            searchable, _search_expired = load_searchable_memory_entries(
                memory_root,
                today=today,
            )
            available.update(
                (name, content)
                for name, content in searchable
                if name in requested
            )
        missing = [name for name in requested if name not in available]
        if missing:
            raise ValueError(
                "Selected Atlas souvenirs are unavailable: " + ", ".join(missing)
            )
        ordered_names = list(requested)
        ordered_names.extend(
            name for name in common_ground_names if name not in set(ordered_names)
        )
        entries = [(name, available[name]) for name in ordered_names]
    lines = []
    collection_names = []
    for name, content in entries:
        subjects, facets = frontmatter_routing_metadata(content)
        qualifiers = []
        if subjects:
            qualifiers.append("subjects: " + ", ".join(subjects))
        if facets:
            qualifiers.append("facets: " + ", ".join(facets))
        record = graph.souvenirs.get(name)
        if record is not None and record.collection is not None:
            try:
                page = read_collection_page(
                    graph,
                    name,
                    memory_root=memory_root,
                )
            except ValueError:
                pass
            else:
                qualifiers.append(
                    "collection: "
                    f"{page.coverage} ({page.known_member_count} known members)"
                )
                collection_names.append(name)
        suffix = " | " + " | ".join(qualifiers) if qualifiers else ""
        lines.append(f"- {name}{suffix}")
    rendered = "\n".join(lines)
    if catalog_notice.strip():
        rendered = catalog_notice.strip() + ("\n" + rendered if rendered else "")
    if len(rendered) > max_chars:
        raise ValueError(
            "Exact memory Atlas exceeds its explicit bound "
            f"({len(rendered)} > {max_chars} chars); refusing silent truncation."
        )
    return ExactMemoryAtlas(
        text=rendered,
        souvenir_names=tuple(name for name, _content in entries),
        rendered_chars=len(rendered),
        sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        expired_souvenir_names=tuple(expired),
        common_ground_names=common_ground_names,
        collection_names=tuple(collection_names),
    )


def read_exact_active_souvenirs(
    paths: list[str],
    memory_root: Path = MEMORY_ROOT,
    *,
    max_souvenirs: int = SOUVENIR_READ_MAX_COUNT,
    max_chars: int = SOUVENIR_READ_MAX_CHARS,
    today: date | None = None,
    user_id: str = "",
    platform: str | None = None,
    collection_path: str = "",
    collection_offset: int = 0,
) -> ExactSouvenirRead:
    """Read exact bodies for a bounded list of canonical Atlas paths."""

    requested = _unique_paths(paths)
    if len(requested) > max_souvenirs:
        raise ValueError(
            f"Too many souvenir paths ({len(requested)} > {max_souvenirs})."
        )
    active_entries, _expired, _common_ground_names = (
        _load_exact_library_entries(
            memory_root,
            today=today,
            user_id=user_id,
            platform=platform,
        )
    )
    active = dict(active_entries)
    if any(name not in active for name in requested):
        searchable, _search_expired = load_searchable_memory_entries(
            memory_root,
            today=today,
        )
        active.update(
            (name, content)
            for name, content in searchable
            if name in requested
        )
    if not isinstance(collection_path, str):
        raise ValueError("Collection path must be a string.")
    if not isinstance(collection_offset, int) or isinstance(collection_offset, bool):
        raise ValueError("Collection offset must be an integer.")
    collection_page = None
    expanded = list(requested)
    if collection_path.strip():
        center = collection_path.strip()
        if center not in requested:
            raise ValueError(
                "Collection path must also be present in the exact paths list."
            )
        remaining = max_souvenirs - len(requested)
        if remaining < 1:
            raise ValueError(
                "No bounded souvenir slot remains for collection members."
            )
        graph = _active_runtime_graph(active_entries, memory_root)
        collection_page = read_collection_page(
            graph,
            center,
            memory_root=memory_root,
            offset=collection_offset,
            limit=remaining,
        )
        for member in collection_page.members:
            if member.path not in expanded:
                expanded.append(member.path)
    souvenirs = tuple((name, active[name]) for name in expanded if name in active)
    rejected = tuple(name for name in requested if name not in active)
    rendered_chars = len(
        "\n\n".join(f"## [[{name}]]\n\n{content}" for name, content in souvenirs)
    )
    if rendered_chars > max_chars:
        raise ValueError(
            "Exact souvenir read exceeds its explicit bound "
            f"({rendered_chars} > {max_chars} chars); refusing silent truncation."
        )
    return ExactSouvenirRead(
        souvenirs=souvenirs,
        rejected_paths=rejected,
        rendered_chars=rendered_chars,
        collection_page=collection_page,
    )


def collection_coverage_checkpoint(page: CollectionPage | None) -> str:
    """Render the deterministic epistemic boundary of one collection page."""

    if page is None:
        return ""
    if page.next_offset is not None:
        return (
            f'This page does not yet cover the {page.known_member_count} known members. To continue before any coverage conclusion, reread the collection with collection_offset={page.next_offset}.'
        )
    if page.coverage == "open":
        return (
            'All currently established members of this open collection have been returned. Use them, but never present this list as exhaustive or the absence of another member as a fact.'
        )
    return (
        'All established members of this closed collection have been returned; its coverage may be presented as exhaustive.'
    )


def _active_runtime_graph(
    entries: list[tuple[str, str]],
    memory_root: Path,
) -> MemoryGraphIndex:
    """Maintain one incremental, regenerable graph view per memory root."""

    root = Path(memory_root).resolve()
    with _runtime_graph_lock:
        previous = _runtime_graph_cache.get(root)
        build = build_memory_graph_from_entries(
            entries,
            previous=previous,
            memory_root=root,
        )
        _runtime_graph_cache[root] = build.index
        return build.index


def _load_exact_library_entries(
    memory_root: Path,
    *,
    today: date | None,
    user_id: str,
    platform: str | None,
) -> tuple[list[tuple[str, str]], list[str], tuple[str, ...]]:
    entries, expired = load_active_memory_entries(memory_root, today=today)
    if not common_ground_library_enabled(user_id, platform):
        return entries, expired, ()
    try:
        resolution = resolve_common_ground_library(user_id=user_id)
    except Exception as exc:
        logger.warning(
            "Common Ground library unavailable | user_id=%s | error=%s",
            hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:20],
            exc.__class__.__name__,
        )
        return entries, expired, ()
    if resolution.rejections:
        logger.warning(
            "Common Ground library entries rejected | user_id=%s | reasons=%s",
            hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:20],
            [item.reason_code for item in resolution.rejections],
        )
    common_ground_entries = list(resolution.library_entries)
    known_names = {name for name, _content in entries}
    accepted = [
        (name, content)
        for name, content in common_ground_entries
        if name not in known_names
    ]
    entries.extend(accepted)
    return (
        entries,
        expired,
        tuple(name for name, _content in accepted),
    )


def parse_scene_coverage(arguments: object) -> SceneCoverage:
    """Validate the voice's own scene judgment without judging it in code."""

    if not isinstance(arguments, dict):
        raise ValueError("A scene assessment is required.")
    raw = arguments.get("scene")
    if not isinstance(raw, dict):
        raise ValueError("A structured scene assessment is required.")
    established = _scene_items(raw.get("established"), "established")
    uncertain = _scene_items(raw.get("uncertain"), "uncertain")
    need = raw.get("need")
    if not isinstance(need, str) or not need.strip():
        raise ValueError("The evidence need this read must resolve is required.")
    handling = raw.get("handling")
    if not isinstance(handling, dict):
        raise ValueError("Structured response handling is required.")
    mode = str(handling.get("mode", "")).strip()
    if mode not in {"direct", "clarify", "condition", "offer"}:
        raise ValueError(
            "Response handling must be direct, clarify, condition or offer."
        )
    plan = handling.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        raise ValueError("The uncertainty-handling plan is required.")
    return SceneCoverage(
        established,
        uncertain,
        need.strip()[:1000],
        mode,
        plan.strip()[:1000],
    )


def scene_coverage_checkpoint(coverage: SceneCoverage) -> str:
    """Keep the model-authored scene assessment adjacent to exact evidence."""

    if coverage.uncertain and coverage.handling_mode == "clarify":
        commitment = (
            f'If the unknown remains after this read, neither choose nor commit to any branch: then ask this single natural question you had considered: "{coverage.handling_plan}", then wait without composing those choices. If the exact bodies resolve it, respond directly from them.'
        )
    elif coverage.uncertain and coverage.handling_mode == "condition":
        commitment = (
            f'If the unknown remains but a useful response can proceed, begin with this condition you had considered: "{coverage.handling_plan}". If the exact bodies resolve the unknown, respond directly without artificially retaining the condition.'
        )
    elif coverage.uncertain:
        commitment = (
            f'The conduct `{coverage.handling_mode}` proposed before reading does not resolve the declared unknowns. If the exact bodies resolve them, respond directly; otherwise now choose clarify or condition according to their actual effect on the response.'
        )
    elif coverage.handling_mode == "offer":
        commitment = (
            f'Now respond with exactly this proposal you chose yourself: "{coverage.handling_plan}". Add no elaboration before the user responds.'
        )
    elif coverage.handling_mode in {"clarify", "condition"}:
        commitment = (
            f'No material unknown was declared before reading: the proposed precaution `{coverage.handling_mode}` was provisional. Ask a question or state a condition only if the exact bodies still reveal an unknown that genuinely changes the response; otherwise respond directly.'
        )
    else:
        commitment = 'No material unknown was declared: respond directly.'
    return (
        'Now reassess the scene from the exact bodies: pre-reading conduct was provisional. '
        + commitment
        + ' Retain separately each established fact and each still-unknown detail: the unknown neither downgrades nor erases the established fact. '
        + ' A plausible suggestion never suffices to resolve a material unknown. With no material unknown, remain direct and do not request confirmation as a precaution.'
    )


def judge_unread_souvenirs_before_l0(
    need: str,
    atlas: ExactMemoryAtlas,
    read_paths: list[str],
    provider: LLMProvider,
    *,
    recent_dialogue: str = "",
    max_souvenirs: int = SOUVENIR_READ_MAX_COUNT,
) -> SouvenirLibraryPreflight:
    """Let a focused LLM exhaust plausible durable memory before L0."""

    already_read = set(_unique_paths(read_paths))
    unread = set(atlas.souvenir_names) - already_read
    if not need.strip() or not unread:
        return SouvenirLibraryPreflight((), 'No unread durable source.', 1.0)
    unread_catalog = "\n".join(
        line
        for line in atlas.text.splitlines()
        if line.startswith("- ") and line[2:].split(" |", 1)[0] in unread
    )
    messages = [
        {
            "role": "system",
            "content": (
                'You are the last-resort librarian before a forensic L0 search. Judge whether NOT YET READ durable souvenirs are plausible sources for the requested personal item. The catalog contains only paths and structural metadata: they guide a read but prove no fact. Select all plausible complementary sources required for complete coverage, without stopping at the first resemblance. When the request seeks additional information, discard souvenirs that would merely reestablish the already-known item. Select nothing out of curiosity. At the slightest plausible path, prefer its exact read to L0. Respond in strict JSON: {"paths":["theme/domaine/souvenir"],"reason":"short reason","confidence":0.0}.'
            ),
        },
        {
            "role": "user",
            "content": (
                f"Sought personal item:\n{need.strip()}\n\nSouvenirs already read in this turn:\n{json.dumps(sorted(already_read), ensure_ascii=False)}\n\nRecent dialogue:\n{recent_dialogue.strip()[-6000:] or 'None.'}\n\nUnread structural catalog:\n{unread_catalog or 'None.'}"
            ),
        },
    ]
    log_llm_prompt(logger, "PRÉ-VOL BIBLIOTHÈQUE AVANT L0", messages)
    try:
        raw = provider.complete(messages=messages, temperature=0.0).strip()
        if raw.startswith("```"):
            lines = raw.splitlines()[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines).strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("invalid preflight result")
        raw_paths = data.get("paths", [])
        if not isinstance(raw_paths, list):
            raw_paths = []
        paths = tuple(
            path
            for path in _unique_paths(raw_paths)
            if path in unread
        )[:max_souvenirs]
        reason = str(data.get("reason", "")).strip()[:500]
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        return SouvenirLibraryPreflight(paths, reason, confidence)
    except Exception as exc:
        logger.info(
            "Pré-vol bibliothèque indisponible | error=%s",
            exc.__class__.__name__,
        )
        return SouvenirLibraryPreflight((), 'Judgment unavailable.', 0.0)


def verify_l0_evidence_coverage(
    need: str,
    evidence: str,
    provider: LLMProvider,
    evidence_context: str = "",
) -> tuple[bool, str]:
    """Judge the exact quote with its validated dated speaker context, if available."""
    messages = [
        {
            "role": "system",
            "content": (
                'You verify the coverage of a personal-memory quotation. The exact user quotation and its supplied exact surrounding context must explicitly establish the entire requested item, with correct attribution. Context retains conversation dates and speaker labels so you can resolve references from user statements. Assistant statements may clarify a question, but never establish a personal fact or supply a missing value. A conversation date dates the statement; it does not prove an unstated duration. An inference, the first judge\'s rationale or coverage of just one part is insufficient. With no surrounding context, judge only the quotation. Respond in strict JSON: {"complete":false,"reason":"short reason"}.'
            ),
        },
        {
            "role": "user",
            "content": (f'Requested item:\n{need}\n\nExact quotation:\n{evidence}'
                        f'\n\nExact surrounding context (dated speaker turns):\n{evidence_context or "None."}'),
        },
    ]
    raw = provider.complete(messages=messages, temperature=0.0).strip()
    if raw.startswith("```"):
        lines = raw.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        return False, "Verdict de couverture invalide."
    reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
    return data.get("complete") is True, reason.strip()[:500]


def inspect_current_scene_model(
    user_id: str,
    scene_scope: str,
    recent_dialogue: str,
    *,
    current_message: str = "",
    message_received_at: float | None = None,
) -> CurrentSceneModelView:
    """Describe why a scene can or cannot safely cover the current turn."""

    from daemon_core.scene_model import (
        read_scene_model,
        scene_model_dialogue_delta,
    )

    snapshot = read_scene_model(user_id, scene_scope)
    if snapshot is None:
        return CurrentSceneModelView(None, None, "absent")
    if snapshot.scene_scope != scene_scope:
        return CurrentSceneModelView(snapshot, None, "scope_mismatch")
    received_at = message_received_at if message_received_at is not None else time.time()
    if snapshot.revised_at > received_at:
        return CurrentSceneModelView(snapshot, None, "future_revision")
    dialogue_before_message = without_trailing_user_message(
        recent_dialogue,
        current_message,
    )
    delta = scene_model_dialogue_delta(snapshot, dialogue_before_message)
    if delta is None:
        return CurrentSceneModelView(snapshot, None, "unprovable_delta")
    return CurrentSceneModelView(snapshot, delta, "ready")


def _unique_paths(paths: list[str]) -> list[str]:
    result = []
    seen = set()
    for path in paths:
        value = str(path).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _scene_items(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Scene field {field} must be a list.")
    items = []
    seen = set()
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"Scene field {field} contains an invalid value.")
        item = raw.strip()[:500]
        if item not in seen:
            seen.add(item)
            items.append(item)
    if len(items) > SCENE_COVERAGE_MAX_ITEMS:
        raise ValueError(
            f"Scene field {field} exceeds {SCENE_COVERAGE_MAX_ITEMS} items."
        )
    return tuple(items)
