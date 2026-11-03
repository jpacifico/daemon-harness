"""Sourced, revisable scene model maintained outside the response path.

The scene model is derived working memory, never a source of durable truth. Its
semantic content is judged by an LLM; deterministic code only protects source
provenance, revision ordering, boundedness, and explicit retirement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from daemon_core.held_thought import (
    SHARED_WORLD_SCENE_SCOPE,
    scene_scope_from_origin,
    shared_world_state_enabled,
)
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.metadata import parse_souvenir_frontmatter
from daemon_core.memory.reader import (
    load_active_context,
    load_souvenir,
    load_immediate_focus,
    load_recent_dialogue,
    load_user_agenda,
)
from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.paths import MEMORY_ROOT, PROJECT_ROOT
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider
from daemon_core.providers.telemetry import llm_telemetry_phase
from daemon_core.temporal_frame import format_reference_timestamp

logger = logging.getLogger("daemon_core.scene_model")

SCENE_MODEL_ENV = "DAEMON_CORE_SCENE_MODEL"
SCENE_MODEL_USER_IDS_ENV = "DAEMON_CORE_SCENE_MODEL_USER_IDS"
CURRENT_TOPICS_ENV = "DAEMON_CORE_CURRENT_TOPICS"
CURRENT_TOPICS_USER_IDS_ENV = "DAEMON_CORE_CURRENT_TOPICS_USER_IDS"
SCENE_MODEL_SCHEMA_VERSION = 3
SCENE_MODEL_PROMPT_VERSION = 17
SCENE_MODEL_DIALOGUE_TURNS = 24
SCENE_MODEL_DELTA_SCAN_TURNS = 200
SCENE_MODEL_BOOTSTRAP_SCAN_TURNS = 160
MAX_BOOTSTRAP_SELECTED_TURNS = 16
MAX_BOOTSTRAP_SELECTION_CANDIDATES = 32
MAX_BOOTSTRAP_CHUNK_SELECTED_TURNS = 4
MAX_BOOTSTRAP_CATALOG_CHARS = 70_000
MAX_SCENE_ITEMS = 16
MAX_SCENE_DEPENDENCIES = 8
MAX_HOT_MEMORIES = 6
MAX_CURRENT_TOPICS = 3
MAX_READING_CHARS = 2200
MAX_CONVERSATION_CHARS = 1000
MAX_SOURCE_EXCERPT_CHARS = 4000
MAX_SOURCE_CATALOG_CHARS = 28_000
MIN_BOUNDED_SOURCE_EXCERPT_CHARS = 32
MAX_SCENE_DELTA_TURNS = 80
MAX_SCENE_DELTA_CHARS = 20_000
MAX_SCENE_STAGE_ATTEMPTS = 2
MAX_TEMPORAL_BOUNDARIES = 4
MAX_SCENE_PROPOSITION_CHARS = 700
MAX_SCENE_HORIZON_CHARS = 300
MUTABLE_SCENE_SOURCE_KINDS = frozenset(
    {
        "agenda",
        "active_context",
        "capsule",
        "immediate_focus",
        "conversation_state",
    }
)
AUTHORITATIVE_SCENE_SOURCE_KINDS = frozenset(
    {
        "agenda",
        "capsule",
        "clock",
    }
)
SHADOW_LOG = PROJECT_ROOT / "exports" / "scene_model_shadow.jsonl"
SCENE_INVALIDATION_FILE = ".invalidated.json"

EPISTEMIC_STATES = {"established", "inferred", "uncertain"}
CHANGE_OPERATIONS = {"add", "keep", "revise", "retire"}
CURRENT_TOPIC_OPERATIONS = {"add", "keep", "revise", "release"}
DEPENDENCY_STATES = {"open", "uncertain", "deferred"}
SOURCE_CONDITION_DISPOSITIONS = {"represented", "superseded", "nonoperative"}
SOURCE_DEPENDENCY_DISPOSITIONS = {"represented", "none"}
_TURN_HEADER = re.compile(
    r"^##\s+(.+?)\s+[—-]\s+(user|assistant)\s*$", re.MULTILINE | re.IGNORECASE
)
_PREPARED_TOPIC_MOVEMENT = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bpose(?:r|z)?\b.{0,24}\bquestion",
        r"\bquestionne(?:r|z)?\b",
        r"\brelance(?:r|z)?\b",
        r"\breprends?\b.{0,20}\b(?:sujet|fil)",
        r"\bdemande-lui\b",
        r"\bask\b",
        r"\breopen\b",
    )
)


@dataclass(frozen=True)
class SceneSource:
    source_id: str
    kind: str
    locator: str
    sha256: str
    excerpt: str
    truncated: bool = False


@dataclass(frozen=True)
class SceneTemporalBoundary:
    at: str
    after: str


@dataclass(frozen=True)
class SceneItem:
    item_id: str
    text: str
    epistemic: str
    sources: tuple[str, ...]
    temporal_boundaries: tuple[SceneTemporalBoundary, ...] = ()


@dataclass(frozen=True)
class SceneMemory:
    name: str
    reason: str = ""


@dataclass(frozen=True)
class RetiredSceneItem:
    item_id: str
    reason: str


@dataclass(frozen=True)
class SceneDependency:
    dependency_id: str
    text: str
    status: str
    horizon: str
    sources: tuple[str, ...]
    temporal_boundaries: tuple[SceneTemporalBoundary, ...] = ()


@dataclass(frozen=True)
class SceneCurrentTopic:
    topic_id: str
    text: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class SceneSnapshot:
    scene_scope: str
    source_trace_id: str
    source_turn_started_at: float
    revised_at: float
    revision: int
    provider: dict[str, Any]
    dialogue_watermark_source_id: str = ""
    dialogue_watermark_locator: str = ""
    reading: str = ""
    conversation: str = ""
    items: tuple[SceneItem, ...] = ()
    dependencies: tuple[SceneDependency, ...] = ()
    current_topics: tuple[SceneCurrentTopic, ...] = ()
    sources: tuple[SceneSource, ...] = ()
    source_dispositions: tuple[SceneSourceDisposition, ...] = ()
    hot_memories: tuple[SceneMemory, ...] = ()
    retired_items: tuple[RetiredSceneItem, ...] = ()
    retired_dependencies: tuple[RetiredSceneItem, ...] = ()
    released_memories: tuple[SceneMemory, ...] = ()

    @property
    def hot_souvenirs(self) -> list[str]:
        return [memory.name for memory in self.hot_memories]

    @property
    def body(self) -> str:
        return _render_body(self)


@dataclass(frozen=True)
class SceneChange:
    operation: str
    item_id: str = ""
    text: str = ""
    epistemic: str = ""
    sources: tuple[str, ...] = ()
    temporal_boundaries: tuple[SceneTemporalBoundary, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class SceneCurrentTopicChange:
    operation: str
    topic_id: str = ""
    text: str = ""
    sources: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class ScenePatch:
    reading: str
    conversation: str
    changes: tuple[SceneChange, ...]
    current_topic_changes: tuple[SceneCurrentTopicChange, ...] = ()
    current_topics_valid: bool = True
    current_topics_rejection: str = ""
    hot_memories: tuple[SceneMemory, ...] = ()
    released_memories: tuple[SceneMemory, ...] = ()


@dataclass(frozen=True)
class SceneDependencyChange:
    operation: str
    dependency_id: str = ""
    text: str = ""
    status: str = ""
    horizon: str = ""
    sources: tuple[str, ...] = ()
    temporal_boundaries: tuple[SceneTemporalBoundary, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class SceneSourceDisposition:
    source_id: str
    condition: str
    dependency: str
    evidence_sources: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class SceneDependencyPatch:
    reading: str
    changes: tuple[SceneDependencyChange, ...] = ()
    source_dispositions: tuple[SceneSourceDisposition, ...] = ()


@dataclass(frozen=True)
class SceneBootstrapSelection:
    source_ids: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class SceneRevisionJob:
    user_id: str
    scene_scope: str
    provider: LLMProvider
    source_trace_id: str
    source_turn_started_at: float
    read_souvenirs: tuple[str, ...] = ()


_SCENE_REVISION_LOCK = threading.Lock()
_SCENE_REVISION_PENDING: dict[tuple[str, str], SceneRevisionJob] = {}
_SCENE_REVISION_RUNNING: set[tuple[str, str]] = set()


def scene_model_mode() -> str:
    return os.environ.get(SCENE_MODEL_ENV, "off").strip().lower()


def scene_model_shadow_enabled(user_id: str) -> bool:
    if scene_model_mode() not in {"shadow", "canary"}:
        return False
    allowed = {
        value.strip()
        for value in os.environ.get(SCENE_MODEL_USER_IDS_ENV, "").split(",")
        if value.strip()
    }
    return "*" in allowed or str(user_id).strip() in allowed


def scene_model_voice_enabled(user_id: str) -> bool:
    """Expose a scene to the voice only in explicit canary mode."""

    return scene_model_mode() == "canary" and scene_model_shadow_enabled(user_id)


def current_topics_canary_enabled(user_id: str) -> bool:
    """Expose the experimental topic register only to explicit canary users."""

    if os.environ.get(CURRENT_TOPICS_ENV, "off").strip().casefold() != "canary":
        return False
    allowed = {
        value.strip()
        for value in os.environ.get(CURRENT_TOPICS_USER_IDS_ENV, "").split(",")
        if value.strip()
    }
    return str(user_id).strip() in allowed


def scene_model_path(user_id: str, scene_scope: str) -> Path:
    return (
        MEMORY_ROOT
        / "users"
        / safe_memory_user_id(user_id)
        / "scenes"
        / safe_memory_user_id(scene_scope)
        / "scene_model.md"
    )


def ensure_shared_scene_model(
    user_id: str,
    scene_scope: str,
) -> SceneSnapshot | None:
    """Seed the shared world once from the freshest valid legacy host scene."""

    if (
        not shared_world_state_enabled()
        or scene_scope != SHARED_WORLD_SCENE_SCOPE
    ):
        return None
    target = scene_model_path(user_id, scene_scope)
    marker = scene_invalidation_path(user_id)
    with memory_file_lock(marker):
        invalidated_at = (
            _scene_invalidation_timestamp(marker) if marker.exists() else None
        )
        with memory_file_lock(target):
            current = _read_scene_model_path(target)
            if current is not None:
                return current
            candidates: list[tuple[tuple[float, float, int, str], SceneSnapshot]] = []
            for path in sorted(target.parent.parent.glob("*/scene_model.md")):
                if path == target:
                    continue
                snapshot = _read_scene_model_path(path)
                if snapshot is None:
                    continue
                if marker.exists() and (
                    invalidated_at is None
                    or snapshot.source_turn_started_at <= invalidated_at
                ):
                    continue
                candidates.append(
                    (
                        (
                            snapshot.source_turn_started_at,
                            snapshot.revised_at,
                            snapshot.revision,
                            path.parent.name,
                        ),
                        snapshot,
                    )
                )
            if not candidates:
                return None
            _key, source = max(candidates, key=lambda item: item[0])
            migrated = replace(source, scene_scope=scene_scope)
            atomic_write_text(target, _render_markdown(migrated))
            target.chmod(0o600)
            logger.info(
                "Shared world scene initialized | source_scope=%s | revision=%s",
                source.scene_scope,
                source.revision,
            )
            return migrated


def scene_invalidation_path(user_id: str) -> Path:
    return (
        MEMORY_ROOT
        / "users"
        / safe_memory_user_id(user_id)
        / "scenes"
        / SCENE_INVALIDATION_FILE
    )


def invalidate_scene_models(user_id: str, *, reason: str) -> Path:
    """Move the visibility cutoff beyond every currently deposited scene."""

    marker = scene_invalidation_path(user_id)
    payload = {
        "invalidated_at": time.time(),
        "reason": str(reason).strip()[:160] or "durable_memory_changed",
    }
    with memory_file_lock(marker):
        atomic_write_text(
            marker,
            json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n",
        )
    return marker


def read_scene_model(user_id: str, scene_scope: str) -> SceneSnapshot | None:
    path = scene_model_path(user_id, scene_scope)
    marker = scene_invalidation_path(user_id)
    try:
        with memory_file_lock(marker):
            snapshot = _read_scene_model_path(path)
            if snapshot is None or not marker.exists():
                return snapshot
            invalidated_at = _scene_invalidation_timestamp(marker)
            if (
                invalidated_at is None
                or snapshot.source_turn_started_at <= invalidated_at
            ):
                return None
            return snapshot
    except Exception as exc:
        logger.info(
            "Scene model illisible | scope=%s | error=%s",
            scene_scope,
            exc.__class__.__name__,
        )
        return None


def render_scene_model_for_voice(
    snapshot: SceneSnapshot,
    *,
    now: datetime | None = None,
    include_current_topics: bool = False,
) -> str:
    """Project sourced working state at read time without semantic heuristics."""

    current = _projection_now(now)
    revised = datetime.fromtimestamp(snapshot.revised_at).astimezone()
    sections = [
        f"## Cadre temporel\nScene revised at {revised.isoformat(timespec='seconds')}.\nRead requested at {current.isoformat(timespec='seconds')}."
    ]
    projected_items = [
        _project_scene_item(item, current) for item in snapshot.items
    ]
    projected_dependencies = [
        _project_scene_dependency(dependency, current)
        for dependency in snapshot.dependencies
    ]
    boundary_crossed = any(
        crossed
        for _line, crossed in projected_items + projected_dependencies
    )
    if snapshot.reading and not boundary_crossed:
        sections.append("## Lecture d'ensemble\n" + snapshot.reading)
    elif boundary_crossed:
        sections.append(
            "## Lecture d'ensemble\nThe synthesis written at revision time is suspended: at least one sourced temporal boundary has passed. The projected states below are authoritative for the present."
        )
    if projected_items:
        sections.append(
            "## Conditions présentes\n"
            + "\n".join(line for line, _crossed in projected_items)
        )
    if projected_dependencies:
        sections.append(
            "## Dépendances ouvertes\n"
            + "\n".join(line for line, _crossed in projected_dependencies)
        )
    if snapshot.conversation and not boundary_crossed:
        sections.append("## Texture conversationnelle\n" + snapshot.conversation)
    if include_current_topics and snapshot.current_topics:
        sections.append(
            "## Sujets du moment\n"
            + "\n".join(
                f"- [established] {topic.text}"
                for topic in snapshot.current_topics
            )
        )
    return "\n\n".join(sections).strip()


def render_unrepresented_active_context_for_voice(
    snapshot: SceneSnapshot,
    active_context: str,
) -> str:
    """Return exact current-context units not accounted for by the scene.

    A dependency may cite a source to explain an open consequence without
    representing the condition carried by that source. Only scene items count
    as a representation here. A persisted source disposition may also establish
    that an exact source superseded the unit. ``nonoperative`` alone remains too
    weak to hide active context on the response path. The comparison is
    structural and source-based; it does not attempt a second semantic judgment
    there.
    """

    current_sources = _document_sources(
        kind="active_context",
        locator="contexte_actif.md",
        content=active_context,
    )
    if not current_sources:
        return ""
    represented_source_ids = {
        source_id
        for item in getattr(snapshot, "items", ())
        for source_id in item.sources
    }
    accounted_source_ids = {
        disposition.source_id
        for disposition in getattr(snapshot, "source_dispositions", ())
        if disposition.condition in {"represented", "superseded"}
    }
    return "\n\n".join(
        source.excerpt
        for source_id, source in current_sources.items()
        if source_id not in represented_source_ids
        and source_id not in accounted_source_ids
    ).strip()


def scene_model_dialogue_delta(
    snapshot: SceneSnapshot,
    recent_dialogue: str,
) -> str | None:
    """Return exact turns not yet absorbed by the materialized scene.

    ``None`` means the watermark cannot be proven inside the bounded L0 window;
    callers must use their complete fallback instead of guessing the gap.
    """

    turns = _parse_dialogue_turns(recent_dialogue)
    if not turns:
        return ""
    source_ids = [
        _source(kind="l0_turn", locator=f"{timestamp} — {role}", content=content).source_id
        for timestamp, role, content in turns
    ]
    watermark_ids = []
    if snapshot.dialogue_watermark_source_id:
        watermark_ids.append(snapshot.dialogue_watermark_source_id)
    else:
        watermark_ids.extend(
            source.source_id
            for source in snapshot.sources
            if source.kind == "l0_turn"
        )
    absorbed_indexes = [
        index for index, source_id in enumerate(source_ids) if source_id in watermark_ids
    ]
    if not absorbed_indexes:
        return None
    delta_turns = turns[max(absorbed_indexes) + 1 :]
    if len(delta_turns) > MAX_SCENE_DELTA_TURNS:
        return None
    rendered = _render_dialogue_turns(delta_turns)
    if len(rendered) > MAX_SCENE_DELTA_CHARS:
        return None
    return rendered


def schedule_scene_model_revision(
    user_id: str,
    scene_scope: str,
    provider: LLMProvider,
    *,
    source_trace_id: str,
    source_turn_started_at: float,
    read_souvenirs: list[str] | None = None,
) -> bool:
    """Coalesce rapid turns behind one sequential revision worker per scene."""

    key = (str(user_id), scene_scope)
    job = SceneRevisionJob(
        user_id=str(user_id),
        scene_scope=scene_scope,
        provider=provider,
        source_trace_id=source_trace_id,
        source_turn_started_at=source_turn_started_at,
        read_souvenirs=tuple(_unique_strings(read_souvenirs or [])),
    )
    start_worker = False
    with _SCENE_REVISION_LOCK:
        pending = _SCENE_REVISION_PENDING.get(key)
        if pending is not None:
            if pending.source_turn_started_at > source_turn_started_at:
                return False
            job = replace(
                job,
                read_souvenirs=tuple(
                    _unique_strings(pending.read_souvenirs + job.read_souvenirs)
                ),
            )
        _SCENE_REVISION_PENDING[key] = job
        if key not in _SCENE_REVISION_RUNNING:
            _SCENE_REVISION_RUNNING.add(key)
            start_worker = True
    if start_worker:
        threading.Thread(
            target=_drain_scene_revision_queue,
            args=(key,),
            name=f"daemon-scene-model-{safe_memory_user_id(scene_scope)}",
            daemon=True,
        ).start()
    return True


def _drain_scene_revision_queue(key: tuple[str, str]) -> None:
    while True:
        with _SCENE_REVISION_LOCK:
            job = _SCENE_REVISION_PENDING.pop(key, None)
            if job is None:
                _SCENE_REVISION_RUNNING.discard(key)
                return
        try:
            with llm_telemetry_phase("after_turn.scene"):
                run_scene_model_revision(
                    user_id=job.user_id,
                    scene_scope=job.scene_scope,
                    provider=job.provider,
                    source_trace_id=job.source_trace_id,
                    source_turn_started_at=job.source_turn_started_at,
                    read_souvenirs=list(job.read_souvenirs),
                )
        except Exception as exc:
            logger.info(
                "Scene model worker poursuivi après échec | scope=%s | error=%s",
                job.scene_scope,
                exc.__class__.__name__,
            )


def build_scene_bootstrap_messages(
    sources: dict[str, SceneSource],
    *,
    max_selected_turns: int = MAX_BOOTSTRAP_SELECTED_TURNS,
) -> list[dict]:
    selection_limit = max(
        1,
        min(MAX_BOOTSTRAP_SELECTED_TURNS, int(max_selected_turns)),
    )
    system = """You prepare the forensic bootstrap of conversational working memory.
You do not build the scene or respond to the person: you only select the exact
L0 turns a judge must reread to avoid losing a still-current situation within
a broader recent window.

Select the turns needed to recover present conditions, nearby transitions,
questions, commitments or potentially unresolved dependencies, as well as
confirmations that may instead establish their resolution. An earlier change
of subject is not a resolution. An assistant statement may show that a question
or action was raised, but never proves that the person followed it. Discard
closed anecdotes, repetition and durable facts with no effect on the present.
Semantic content is entirely a matter for your judgment; no domain category
or vocabulary is predefined.

Do not select simply by recency. For each potentially current thread, go back
to the turn establishing the original condition or transformation, then also
select any later statement that actually establishes its resolution, deferral
or persistence. A recent request for a recap and an assistant response
summarizing it do not replace those original sources. A nearby transition
may make a much older condition in the window decisive again.
"""
    system += (
        f'\nChoose at most {selection_limit} supplied identifiers. Respond in strict JSON, without Markdown: {{"selected":["source-id"],"reason":"brief reason"}}'
    )
    catalog = _render_bootstrap_catalog(sources)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": 'Available L0 turns:\n\n' + catalog},
    ]


def parse_scene_bootstrap_selection(
    raw: str,
    *,
    allowed_sources: set[str],
    max_selected_turns: int = MAX_BOOTSTRAP_SELECTED_TURNS,
) -> SceneBootstrapSelection | None:
    try:
        data = json.loads(_strip_json_fence(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("selected"), list):
        return None
    selected = _unique_strings(data["selected"])
    if (
        len(selected) > max(0, int(max_selected_turns))
        or any(source_id not in allowed_sources for source_id in selected)
    ):
        return None
    return SceneBootstrapSelection(
        source_ids=tuple(selected),
        reason=_clean_text(data.get("reason"), 500),
    )


def _judge_scene_bootstrap_candidates(
    provider: LLMProvider,
    candidates: dict[str, SceneSource],
    *,
    max_selected_turns: int = MAX_BOOTSTRAP_SELECTED_TURNS,
) -> SceneBootstrapSelection:
    messages = build_scene_bootstrap_messages(
        candidates,
        max_selected_turns=max_selected_turns,
    )
    log_llm_prompt(logger, "SCENE MODEL BOOTSTRAP", messages)
    raw = provider.complete(messages=messages, temperature=0.0)
    selection = parse_scene_bootstrap_selection(
        raw,
        allowed_sources=set(candidates),
        max_selected_turns=max_selected_turns,
    )
    if selection is None:
        raise ValueError("invalid scene bootstrap selection")
    return selection


def select_scene_bootstrap_sources(
    provider: LLMProvider,
    recent_dialogue: str,
) -> tuple[dict[str, SceneSource], SceneBootstrapSelection]:
    """Select a global forensic shortlist without one oversized LLM call.

    The semantic contract remains unchanged: the model first keeps a generous
    shortlist inside chronological, bounded catalogs, then arbitrates that
    shortlist globally. With the 200-turn recovery window, no call sees more
    than 32 candidates and the final catalog also remains within that bound.
    """

    candidates = _dialogue_sources(recent_dialogue, max_chars=600)
    if not candidates:
        return {}, SceneBootstrapSelection()
    candidate_limit = max(1, int(MAX_BOOTSTRAP_SELECTION_CANDIDATES))
    if len(candidates) <= candidate_limit:
        selection = _judge_scene_bootstrap_candidates(provider, candidates)
    else:
        candidate_items = list(candidates.items())
        shortlisted: dict[str, SceneSource] = {}
        for offset in range(0, len(candidate_items), candidate_limit):
            chunk = dict(candidate_items[offset : offset + candidate_limit])
            chunk_selection = _judge_scene_bootstrap_candidates(
                provider,
                chunk,
                max_selected_turns=MAX_BOOTSTRAP_CHUNK_SELECTED_TURNS,
            )
            for source_id in chunk_selection.source_ids:
                shortlisted[source_id] = candidates[source_id]
        if not shortlisted:
            selection = SceneBootstrapSelection()
        else:
            if len(shortlisted) > candidate_limit:
                raise ValueError(
                    "scene bootstrap shortlist exceeds its explicit bound "
                    f"({len(shortlisted)} > {candidate_limit} candidates)"
                )
            selection = _judge_scene_bootstrap_candidates(
                provider,
                shortlisted,
            )
        logger.info(
            "Scene bootstrap hiérarchique | candidates=%s | shortlisted=%s",
            len(candidates),
            len(shortlisted),
        )
    exact_candidates = _dialogue_sources(recent_dialogue, max_chars=2000)
    return (
        {
            source_id: exact_candidates[source_id]
            for source_id in selection.source_ids
            if source_id in exact_candidates
        },
        selection,
    )


def capture_scene_model_context(
    user_id: str,
    scene_scope: str,
    read_souvenirs: list[str] | None,
    provider: LLMProvider | None = None,
    dialogue_turn_limit: int | None = None,
) -> dict[str, Any]:
    previous = read_scene_model(user_id, scene_scope)
    if previous is None and scene_invalidation_path(user_id).exists():
        try:
            previous = _read_scene_model_path(
                scene_model_path(user_id, scene_scope)
            )
        except Exception as exc:
            logger.info(
                "Socle de scène invalidé illisible | scope=%s | error=%s",
                scene_scope,
                exc.__class__.__name__,
            )
    bootstrap_sources: dict[str, SceneSource] = {}
    if previous is None:
        recent_dialogue = load_recent_dialogue(
            user_id, max_turns=SCENE_MODEL_DIALOGUE_TURNS
        )
        if provider is not None:
            bootstrap_dialogue = load_recent_dialogue(
                user_id,
                max_turns=SCENE_MODEL_BOOTSTRAP_SCAN_TURNS,
            )
            bootstrap_sources, _selection = select_scene_bootstrap_sources(
                provider,
                bootstrap_dialogue,
            )
    else:
        dialogue_window = load_recent_dialogue(
            user_id, max_turns=SCENE_MODEL_DELTA_SCAN_TURNS
        )
        recent_dialogue = scene_model_dialogue_delta(previous, dialogue_window)
        if recent_dialogue is None:
            if provider is None:
                raise ValueError(
                    "scene dialogue watermark is outside the bounded L0 window"
                )
            recent_dialogue = load_recent_dialogue(
                user_id,
                max_turns=SCENE_MODEL_DIALOGUE_TURNS,
            )
            bootstrap_sources, _selection = select_scene_bootstrap_sources(
                provider,
                dialogue_window,
            )
            logger.info(
                "Scene model rebased from bounded L0 | scope=%s | "
                "selected=%s",
                scene_scope,
                len(bootstrap_sources),
            )
    dialogue_turns = _parse_dialogue_turns(recent_dialogue)
    dialogue_total_turns = len(dialogue_turns)
    effective_limit = max(0, int(dialogue_turn_limit or 0))
    dialogue_truncated = bool(
        previous is not None
        and effective_limit
        and dialogue_total_turns > effective_limit
    )
    if dialogue_truncated:
        unprocessed_source_ids = {
            _source(
                kind="l0_turn",
                locator=f"{timestamp} — {role}",
                content=content,
            ).source_id
            for timestamp, role, content in dialogue_turns[effective_limit:]
        }
        bootstrap_sources = {
            source_id: source
            for source_id, source in bootstrap_sources.items()
            if source_id not in unprocessed_source_ids
        }
        recent_dialogue = _render_dialogue_turns(
            dialogue_turns[:effective_limit]
        )
    sources = build_scene_sources(
        recent_dialogue=recent_dialogue,
        agenda=load_user_agenda(user_id),
        active_context=load_active_context(user_id),
        immediate_focus=load_immediate_focus(user_id),
        # The exact L0 delta and the Scene Model's own `conversation` field
        # already carry the current texture. Feeding a separate LLM summary
        # into this judgment created a projection of a projection.
        conversation_state="",
        previous=previous,
    )
    for source_id, source in bootstrap_sources.items():
        sources.setdefault(source_id, source)
    source_accounting_ids = _source_accounting_ids(
        previous=previous,
        sources=sources,
        bootstrap_source_ids=set(bootstrap_sources),
    )
    current_reads = _unique_strings(read_souvenirs or [])
    previous_souvenirs = previous.hot_souvenirs if previous else []
    allowed_souvenirs = _unique_strings(current_reads + previous_souvenirs)
    souvenir_contents: dict[str, str] = {}
    for name in allowed_souvenirs:
        content = load_souvenir(name).strip()
        if content:
            souvenir_contents[name] = content[:MAX_SOURCE_EXCERPT_CHARS]
            source = _source(
                kind="capsule",
                locator=name,
                content=content,
                max_chars=MAX_SOURCE_EXCERPT_CHARS,
            )
            sources[source.source_id] = source
    return {
        "previous": previous,
        "sources": sources,
        "allowed_souvenirs": allowed_souvenirs,
        "souvenir_contents": souvenir_contents,
        "source_accounting_ids": source_accounting_ids,
        "dialogue_turn_count": len(_parse_dialogue_turns(recent_dialogue)),
        "dialogue_total_turns": dialogue_total_turns,
        "dialogue_truncated": dialogue_truncated,
    }


def build_scene_sources(
    *,
    recent_dialogue: str,
    agenda: str = "",
    active_context: str = "",
    immediate_focus: str = "",
    conversation_state: str = "",
    previous: SceneSnapshot | None = None,
    now: datetime | None = None,
) -> dict[str, SceneSource]:
    sources = _dialogue_sources(recent_dialogue)
    for kind, locator, content in (
        ("agenda", "agenda.md", agenda),
        ("active_context", "contexte_actif.md", active_context),
        ("immediate_focus", "focus_immediat.md", immediate_focus),
        ("conversation_state", "conversation_state.md", conversation_state),
    ):
        if content.strip():
            sources.update(
                _document_sources(
                    kind=kind,
                    locator=locator,
                    content=content,
                )
            )
    clock = format_reference_timestamp(now)
    source = _source(kind="clock", locator="current_time", content=clock)
    sources[source.source_id] = source
    if previous:
        retained_previous_source_ids = _previous_source_ids_required_for_revision(
            previous,
            current_source_ids=set(sources),
        )
        for previous_source in previous.sources:
            if previous_source.kind in MUTABLE_SCENE_SOURCE_KINDS:
                continue
            if previous_source.source_id not in retained_previous_source_ids:
                continue
            sources.setdefault(previous_source.source_id, previous_source)
    return sources


def _previous_source_ids_required_for_revision(
    previous: SceneSnapshot,
    *,
    current_source_ids: set[str],
) -> set[str]:
    """Keep only immutable provenance still used by the revisable state.

    L0 source dispositions normally account for one dialogue delta, after
    which the watermark makes them obsolete. The exception is a supersession
    justified by mutable evidence: its exact L0 source must remain available
    so the decision can be revisited when that evidence changes.
    """

    required = {
        source_id
        for item in previous.items
        for source_id in item.sources
    }
    required.update(
        source_id
        for dependency in previous.dependencies
        for source_id in dependency.sources
    )
    required.update(
        source_id
        for topic in previous.current_topics
        for source_id in topic.sources
    )
    previous_sources = {
        source.source_id: source for source in previous.sources
    }
    for disposition in previous.source_dispositions:
        source = previous_sources.get(disposition.source_id)
        if source is None:
            continue
        if source.kind == "l0_turn":
            if not _l0_supersession_has_mutable_evidence(
                disposition,
                previous_sources,
            ):
                continue
            required.add(disposition.source_id)
            required.update(disposition.evidence_sources)
            continue
        if (
            source.kind in MUTABLE_SCENE_SOURCE_KINDS
            and source.source_id not in current_source_ids
        ):
            continue
        required.add(disposition.source_id)
        required.update(disposition.evidence_sources)
    return required


def _l0_supersession_has_mutable_evidence(
    disposition: SceneSourceDisposition,
    sources: dict[str, SceneSource],
) -> bool:
    source = sources.get(disposition.source_id)
    return bool(
        source is not None
        and source.kind == "l0_turn"
        and disposition.condition == "superseded"
        and set(disposition.evidence_sources).issubset(sources)
        and any(
            evidence_source is not None
            and evidence_source.kind in MUTABLE_SCENE_SOURCE_KINDS
            for evidence_source in (
                sources.get(source_id)
                for source_id in disposition.evidence_sources
            )
        )
    )


def run_scene_model_revision(
    user_id: str,
    scene_scope: str,
    provider: LLMProvider,
    *,
    source_trace_id: str,
    source_turn_started_at: float,
    read_souvenirs: list[str] | None = None,
    context: dict[str, Any] | None = None,
    dialogue_turn_limit: int | None = None,
    allow_same_source_revision: bool = False,
    strict: bool = False,
) -> SceneSnapshot | None:
    started_at = time.time()
    event: dict[str, Any] = {
        "ts": time.time(),
        "event": "scene_model_revision",
        "scene_scope": scene_scope,
        "source_trace_id": source_trace_id,
        "provider": _provider_snapshot(provider),
    }
    grounding_rejections: list[str] = []
    dependency_rejections: list[str] = []
    current_topics_enabled = current_topics_canary_enabled(user_id)
    event["current_topics_canary"] = current_topics_enabled
    try:
        context = context or capture_scene_model_context(
            user_id,
            scene_scope,
            read_souvenirs,
            provider=provider,
            dialogue_turn_limit=dialogue_turn_limit,
        )
        messages = build_scene_model_messages(
            previous=context["previous"],
            sources=context["sources"],
            allowed_souvenirs=context["allowed_souvenirs"],
            souvenir_contents=context["souvenir_contents"],
            source_accounting_ids=context.get("source_accounting_ids"),
            current_topics_enabled=current_topics_enabled,
        )
        from daemon_core.first_meeting import add_scene_guidance, record_completion
        meeting_active = add_scene_guidance(messages, MEMORY_ROOT, user_id)
        projection_duration = 0.0
        projection_attempts = 0
        projection_rejections: list[str] = []
        patch = None
        dependency_patch = None
        rejected_raw = ""
        rejection_reason = ""
        sources = context.get("sources", {})
        temporal_source_ids = {
            source_id
            for source_id, source in sources.items()
            if source.kind == "clock"
        }
        for attempt in range(MAX_SCENE_STAGE_ATTEMPTS):
            projection_attempts += 1
            attempt_messages = (
                messages
                if attempt == 0
                else _scene_stage_retry_messages(
                    messages,
                    "single projection",
                    rejected_raw=rejected_raw,
                    rejection_reason=rejection_reason,
                )
            )
            log_llm_prompt(logger, "SCENE MODEL SINGLE PASS", attempt_messages)
            projection_started_at = time.perf_counter()
            raw = provider.complete(messages=attempt_messages, temperature=0.0)
            projection_duration += time.perf_counter() - projection_started_at
            patch, dependency_patch, rejection_reasons = parse_scene_revision(
                raw,
                previous=context.get("previous"),
                allowed_sources=set(sources),
                allowed_souvenirs=context.get("allowed_souvenirs", []),
                authoritative_sources=authoritative_scene_source_ids(sources),
                temporal_source_ids=temporal_source_ids,
                current_topics_enabled=current_topics_enabled,
                user_turn_sources={
                    source_id
                    for source_id, source in sources.items()
                    if source.kind == "l0_turn"
                    and source.locator.rsplit(" — ", 1)[-1].casefold() == "user"
                },
                required_source_accounting=set(
                    context.get("source_accounting_ids", [])
                ),
            )
            if patch is not None and dependency_patch is not None:
                break
            rejected_raw = raw
            rejection_reason = ";".join(rejection_reasons) or "unknown"
            projection_rejections.extend(rejection_reasons or ["unknown"])
            grounding_rejections.extend(
                reason.removeprefix("scene:")
                for reason in rejection_reasons
                if reason.startswith("scene:")
            )
            dependency_rejections.extend(
                reason.removeprefix("dependencies:")
                for reason in rejection_reasons
                if reason.startswith("dependencies:")
            )
            logger.info(
                "Projection de scène rejetée | scope=%s | attempt=%s | reason=%s",
                scene_scope,
                attempt + 1,
                rejection_reason,
            )
        if patch is None or dependency_patch is None:
            raise ValueError("invalid single-pass scene projection")
        if current_topics_enabled and not patch.current_topics_valid:
            logger.info(
                "Sujets du moment ignorés sans bloquer la scène | "
                "scope=%s | reason=%s",
                scene_scope,
                patch.current_topics_rejection,
            )
        snapshot = _deposit_scene_model(
            user_id=user_id,
            scene_scope=scene_scope,
            source_trace_id=source_trace_id,
            source_turn_started_at=source_turn_started_at,
            provider=provider,
            previous=context.get("previous"),
            sources=context.get("sources", {}),
            patch=patch,
            dependency_patch=dependency_patch,
            allow_same_source_revision=allow_same_source_revision,
        )
        if meeting_active and snapshot is not None:
            record_completion(MEMORY_ROOT, user_id, raw, sources,
                              trace_id=source_trace_id)
        event.update(
            duration_s=round(time.time() - started_at, 3),
            deposited=snapshot is not None,
            revision=snapshot.revision if snapshot else None,
            active_items=len(snapshot.items) if snapshot else None,
            hot_memories=snapshot.hot_souvenirs if snapshot else [],
            current_topics=(
                [topic.text for topic in snapshot.current_topics]
                if snapshot is not None
                else []
            ),
            current_topics_valid=patch.current_topics_valid,
            current_topics_rejection=patch.current_topics_rejection,
            projection_mode="single_pass",
            projection_duration_s=round(projection_duration, 3),
            projection_attempts=projection_attempts,
            projection_rejections=projection_rejections,
            grounding_duration_s=round(projection_duration, 3),
            grounding_attempts=projection_attempts,
            grounding_rejections=grounding_rejections,
            dependency_duration_s=0.0,
            dependency_attempts=0,
            dependency_rejections=dependency_rejections,
            dependencies_valid=True,
            challenge_duration_s=0.0,
            challenge_attempts=0,
            challenge_valid=False,
            challenge_additions=0,
            challenge_retirements=0,
            active_dependencies=len(snapshot.dependencies) if snapshot else None,
        )
        _append_shadow_event(event)
        return snapshot
    except Exception as exc:
        logger.info(
            "Scene model non révisé | scope=%s | error=%s | detail=%s",
            scene_scope,
            exc.__class__.__name__,
            str(exc)[:500],
        )
        event.update(
            duration_s=round(time.time() - started_at, 3),
            deposited=False,
            error=exc.__class__.__name__,
            error_detail=str(exc)[:500] or None,
            grounding_rejections=grounding_rejections,
            dependency_rejections=dependency_rejections,
        )
        _append_shadow_event(event)
        if strict:
            raise
        return None


def build_scene_model_messages(
    *,
    previous: SceneSnapshot | None,
    sources: dict[str, SceneSource],
    allowed_souvenirs: list[str],
    souvenir_contents: dict[str, str],
    source_accounting_ids: list[str] | None = None,
    current_topics_enabled: bool = False,
) -> list[dict]:
    current_source_ids = set(sources)
    stale_item_ids = [
        item.item_id
        for item in (previous.items if previous is not None else ())
        if any(source_id not in current_source_ids for source_id in item.sources)
    ]
    stale_item_recovery = _render_stale_item_recovery(
        previous,
        sources,
        stale_item_ids,
    )
    stale_dependency_ids = [
        dependency.dependency_id
        for dependency in (previous.dependencies if previous is not None else ())
        if any(
            source_id not in current_source_ids
            for source_id in dependency.sources
        )
    ]
    system = """You maintain the Daemon Harness current scene model between turns.

This model is descriptive working memory. It is not a response to the person,
a conduct plan, a task list or a prediction of their next message. Produce the
smallest coherent reading that makes the present state understandable as a
whole, without restricting yourself to the latest named topic or mechanically
importing everything important in their life.

Semantic content is entirely a matter for your judgment. No situation category
is predefined. Ground current conditions, transformations and unknowns in the
sources, then judge in the same pass the material dependencies they leave
open. Emptiness is preferable to decorative analysis.

Each active item carries an epistemic status:
- `established`: explicitly established by an exact source;
- `inferred`: only to retain or revise an inference already present in the
  previous state; do not add any new inference;
- `uncertain`: an unresolved possibility or condition whose uncertainty matters.

Each item must cite at least one supplied source identifier. Exact excerpts
and bodies are authoritative; the previous reading is revisable and is not
evidence. An assistant statement does not on its own establish a fact about
the person. Preserve distinctions of attribution and uncertainty.

The same object may pass through several actions or stages with different
horizons. Preserve each temporal relationship separately: a time or period
attached to one action never qualifies another action without a source
establishing that link. If one stage has only a date and another a time,
retain that difference instead of manufacturing a shared horizon.

`active_context`, `immediate_focus` and `conversation_state` are derived
projections that guide revision: they can never on their own support a new
`established` item. The same applies to an unconfirmed assistant L0 turn.
For `established`, also cite at least one authoritative exact source: a user
L0 turn, an actually loaded souvenir body, the agenda, a connected source or
the clock, according to the nature of the fact. Otherwise retain the material
as `uncertain` instead of laundering the authority of its projection.

Structured documents are supplied as independent evidence units: citing or
retaining one unit never counts as examining the others. `active_context` and
`agenda` describe potentially present material that disappears only with a
source establishing resolution or supersession. `immediate_focus` and
`conversation_state` describe recent attention and texture; even when they
use exhaustive wording such as "the only point", they cannot resolve or
negate a condition carried by another exact source.

A rule whose sources establish only its scope of application does not prove
the opposite outcome outside that scope: the new regime remains unknown.
Likewise, the person's answer to an alternative, hypothesis or question
formulated by the assistant does not allow attributing a stronger negation
than their own words. Retain the positive assertion actually expressed and
leave the rest open.

Current time is an exact source, just like timestamped turns. Any proposition
whose present meaning changes at a known instant must use an absolute time
reference and carry `temporal_boundaries`. Each boundary provides `at`, an ISO
date with timezone supported by the sources, and `after`, the smallest safe
wording once that boundary has passed. This projection never invents the
outcome: if the boundary alone proves neither waking, arrival nor action,
`after` says that the scheduled time has passed and the outcome remains
unknown. Cite the `clock` source among the item's sources. Leave no relative
expression such as "in three hours", "tomorrow" or "tonight" in `text`,
`reading` or `conversation` when it could be reread later. If no known
boundary changes the proposition, return an empty list.

Revise the state instead of regenerating it. Each previous active item must
appear exactly once in `changes` with one of these operations: `keep`,
`revise` or `retire`. `keep` preserves the exact previous text, status, sources
and boundaries: return only `op` and `id`; any other fields will be ignored.
Any modification requires `revise`. `retire` requires a concrete reason.
Use `add` for a new item, with an empty `id`. If a previous source no longer
appears in the current catalog, `keep` is invalid: revise the item from current
sources or retire it if they establish that it is no longer active.
Do not retire anything merely to save space, because the topic briefly changed
or because you have already summarized the item. A detour is not a resolution.

The optional `Dépendances ouvertes` section remains separate from the items.
Revise it in `dependency_changes` instead of copying it into `changes`.

The state is strictly bounded to sixteen active items and is not an archive.
Before any addition, judge what has been explicitly resolved, replaced,
absorbed by a broader item or made redundant. You may then revise an item to
merge its material and retire the others with that concrete reason. Never
retire a still-open dependency merely to meet the bound. A durable preference
or remark that does not change the present situation belongs in durable
memory, not in a new scene item.

Each `text` and each `after` must fit within 700 characters. A longer unit must
be split into atomic propositions; the runtime will never truncate it.

`reading` is a short, natural understanding of the whole situation. It respects
epistemic statuses without reciting sources and keeps material dependencies
still open before the next concrete horizon visible, even if the latest turns
concern something else. `conversation` describes only the exchange's current
texture: material still alive, exhausted or declined, and the perceptible
degree of openness. It prescribes no question, follow-up or response and does
not copy the persona.

Authorized souvenirs are only those actually read in this turn or already held
in the previous turn. Choose at most six `hot_memories`. They remain attention
references: their exact body, not the scene, will provide evidence for the
voice. A hot memory is neither an archive nor a topic's permanent companion:
retain it only if its exact body can still materially change the immediate
continuation of the exchange. A topic may remain summarized in the scene
without automatically retaining its supporting souvenir. When the conversation
has genuinely moved to other material and no longer depends on that body's
details, release the souvenir; it remains discoverable through search. Every
previous hot memory absent from `hot_memories` must appear in
`released_memories` with a concrete reason.

Respond in strict JSON, without Markdown:
{"reading":"short reading","conversation":"short texture","changes":[{"op":"add|keep|revise|retire","id":"previous identifier or empty for add","text":"absolute active proposition, empty for retire","epistemic":"established|inferred|uncertain","sources":["source-id"],"temporal_boundaries":[{"at":"ISO date with timezone","after":"safe state after this boundary"}],"reason":"reason required for retire, otherwise optional"}],"hot_memories":[{"name":"path/souvenir","reason":"current reason"}],"released_memories":[{"name":"path/souvenir","reason":"concrete reason"}]}"""
    if current_topics_enabled:
        old_schema = (
            '{"reading":"short reading","conversation":"short texture","changes":[{"op":"add|keep|revise|retire","id":"previous identifier or empty for add","text":"absolute active proposition, empty for retire","epistemic":"established|inferred|uncertain","sources":["source-id"],"temporal_boundaries":[{"at":"ISO date with timezone","after":"safe state after this boundary"}],"reason":"reason required for retire, otherwise optional"}],"hot_memories":[{"name":"path/souvenir","reason":"current reason"}],"released_memories":[{"name":"path/souvenir","reason":"concrete reason"}]}'
        )
        topic_contract = """

You also maintain a tightly bounded projection of `sujets du moment`. A current
topic is still-live material that the person's exact words make salient now:
they return to it spontaneously, name it as important themselves, or mention
a nearby personal event in their words. It is neither the set of active facts,
a task list, a question list nor a prediction of the next turn.

Selection already expresses salience. The `text` therefore describes only the
material and its concrete state; it never asserts that it occupies the person's
attention, mind or mental space, weighs on them or obsesses them, unless their
own exact words establish it. It contains no question, follow-up instruction,
prepared response or advice for the voice.

Current-topic rules:
- retain at most three topics; emptiness is valid;
- unresolved material is not automatically a current topic;
- a brief practical detour does not displace already-live material;
- a recent user statement resolving, declining or deprioritizing something
  takes immediate precedence;
- an assistant suggestion never enters without explicit adoption;
- each addition or revision cites at least one exact user L0 turn; a souvenir
  or agenda may supplement that evidence, never replace it;
- retaining a topic never suffices to keep its souvenir hot: retain it only if
  recent conversation still explores, requests or uses details of its exact
  body; otherwise release it while retaining the useful summary.

Each previous topic appears exactly once in `current_topic_changes`: `keep`
preserves its exact text and sources; `revise` updates it; `release` removes
it from the foreground with a concrete reason without claiming to erase the
fact; `add` creates a topic with an empty `id`.
"""
        new_schema = (
            old_schema[:-1]
            + ',"current_topic_changes":[{"op":"add|keep|revise|release","id":"previous identifier or empty for add","text":"descriptive material, empty for release","sources":["source-id"],"reason":"reason required for release, otherwise optional"}]}'
        )
        system = system.replace(
            '\n\nRespond in strict JSON, without Markdown:\n' + old_schema,
            topic_contract + '\nRespond in strict JSON, without Markdown:\n' + new_schema,
        )
    system += """

In this same judgment, maintain open dependencies. A dependency is a condition,
question, commitment or material capability whose resolution can genuinely
change what remains possible or necessary before an already-present horizon.
It is neither a conversation topic, advice nor an invented task. An assistant
statement may flag a question; it never proves that the person followed a
suggestion or resolved the state.

Always describe a still-open state or capability, never the action to perform
to resolve it. Distinguish material effects with different horizons: a
preparation time does not become the time of the next stage without exact
evidence. Reaching a horizon does not on its own resolve an unknown. A
transformation may separately leave a residual state and an ordinary
capability unavailable; retain both only if they genuinely change the scene.
Structured evidence units remain independent: using one unit, summarizing it
or not mentioning something never proves another unit's resolution.

Each previous dependency appears exactly once in `dependency_changes` with
`keep`, `revise` or `retire`. `keep` preserves its exact content and returns
only `op` and `id`. `retire` requires current evidence of resolution,
contradiction or replacement and a concrete reason. `add` creates a dependency
with an empty identifier. Each addition or revision cites authorized sources,
an `open`, `uncertain` or `deferred` status, a horizon and a
`temporal_boundaries` list. At most eight active dependencies.

Before emitting, challenge your own result once: separately check whether a
condition leaves an unresolved state, casts doubt on an ordinary capability
needed until the horizon, or whether new evidence closes an old dependency.
Correct `dependency_changes` directly; produce no separate contradiction report.

Also return exactly one `source_dispositions` entry for each requested
identifier. `condition` is `represented`, `superseded` or `nonoperative`;
`dependency` is `represented` or `none`. A `superseded` disposition cites exact
resolution evidence. A condition or dependency declared `represented` must
actually be cited by the corresponding unit.

The same JSON object specified above must therefore also contain these two
required fields:
"dependency_changes":[{"op":"add|keep|revise|retire","id":"previous identifier or empty for add","text":"descriptive dependency","status":"open|uncertain|deferred","horizon":"natural or absolute horizon","sources":["source-id"],"temporal_boundaries":[{"at":"ISO date with timezone","after":"safe state after this boundary"}],"reason":"reason required for retire, otherwise optional"}],
"source_dispositions":[{"source":"requested source-id","condition":"represented|superseded|nonoperative","dependency":"represented|none","evidence_sources":["resolution source-id"],"reason":"reason required for superseded, nonoperative, or a represented condition without a dependency"}].
Emit only one root JSON object."""
    source_text = _render_source_catalog(sources)
    previous_text = (
        previous.body if previous is not None else 'No previous state.'
    )
    souvenir_text = "\n\n".join(
        f"### [[{name}]]\n{content}"
        for name, content in souvenir_contents.items()
    ) or 'No authorized souvenir body.'
    user = f"""Previous state to revise:
{previous_text}

Available source catalog:
{source_text}

Souvenirs authorized as hot attention:
{json.dumps(allowed_souvenirs, ensure_ascii=False)}

Corresponding exact bodies:
{souvenir_text}

Units this judgment must explicitly account for:
{json.dumps(source_accounting_ids or [], ensure_ascii=False)}

Previous units with at least one source missing from the current catalog
(`keep` forbidden; `revise` or `retire` required):
{json.dumps(stale_item_ids, ensure_ascii=False)}

Sources still available to recover these units (the disappearance of some
of their sources never establishes their resolution):
{stale_item_recovery}

Scene budget: {len(previous.items) if previous else 0} active item(s)
before this revision, at most 16 after revision.

Previous dependencies to explicitly retain, revise or retire:
{_render_previous_dependencies(previous)}

Previous dependencies with at least one source missing from the current
catalog (`keep` forbidden; `revise` or `retire` required):
{json.dumps(stale_dependency_ids, ensure_ascii=False)}

Dependency budget: {len(previous.dependencies) if previous else 0}
active dependency/dependencies before this revision, at most 8 after revision."""
    if current_topics_enabled:
        previous_topics = [
            {
                "id": topic.topic_id,
                "text": topic.text,
                "sources": list(topic.sources),
            }
            for topic in (previous.current_topics if previous else ())
        ]
        user += (
            '\n\nPrevious current topics to explicitly retain, revise or release:\n'
            + json.dumps(previous_topics, ensure_ascii=False, indent=2)
            + '\n\nCurrent-topic budget: at most three.'
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _render_stale_item_recovery(
    previous: SceneSnapshot | None,
    sources: dict[str, SceneSource],
    stale_item_ids: list[str],
) -> str:
    if previous is None or not stale_item_ids:
        return 'No unit to recover.'
    stale = set(stale_item_ids)
    recovery = []
    for item in previous.items:
        if item.item_id not in stale:
            continue
        available = [source_id for source_id in item.sources if source_id in sources]
        recovery.append(
            {
                "id": item.item_id,
                "sources_still_available": available,
            }
        )
    return json.dumps(recovery, ensure_ascii=False, indent=2)


def parse_scene_revision(
    raw: str,
    *,
    previous: SceneSnapshot | None,
    allowed_sources: set[str],
    allowed_souvenirs: list[str],
    authoritative_sources: set[str],
    temporal_source_ids: set[str],
    current_topics_enabled: bool,
    user_turn_sources: set[str],
    required_source_accounting: set[str],
) -> tuple[ScenePatch | None, SceneDependencyPatch | None, list[str]]:
    """Validate both halves of the single Scene Model judgment."""

    scene_rejections: list[str] = []
    patch = parse_scene_patch(
        raw,
        previous=previous,
        allowed_sources=allowed_sources,
        allowed_souvenirs=allowed_souvenirs,
        authoritative_sources=authoritative_sources,
        temporal_source_ids=temporal_source_ids,
        current_topics_enabled=current_topics_enabled,
        user_turn_sources=user_turn_sources,
        rejection_reasons=scene_rejections,
    )
    if patch is None:
        return None, None, [f"scene:{reason}" for reason in scene_rejections]

    try:
        data = json.loads(_strip_json_fence(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None, ["scene:invalid_json"]
    dependency_rejections: list[str] = []
    dependency_raw = json.dumps(
        {
            "reading": data.get("reading"),
            "changes": data.get("dependency_changes"),
            "source_dispositions": data.get("source_dispositions"),
        },
        ensure_ascii=False,
    )
    dependency_patch = parse_scene_dependency_patch(
        dependency_raw,
        previous=previous,
        allowed_sources=allowed_sources,
        temporal_source_ids=temporal_source_ids,
        required_source_accounting=required_source_accounting,
        grounded_condition_sources={
            source_id
            for change in patch.changes
            if change.operation != "retire"
            for source_id in change.sources
        },
        rejection_reasons=dependency_rejections,
    )
    if dependency_patch is None:
        return (
            patch,
            None,
            [f"dependencies:{reason}" for reason in dependency_rejections],
        )
    return patch, dependency_patch, []


def parse_scene_dependency_patch(
    raw: str,
    *,
    previous: SceneSnapshot | None,
    allowed_sources: set[str],
    temporal_source_ids: set[str] | None = None,
    required_source_accounting: set[str] | None = None,
    grounded_condition_sources: set[str] | None = None,
    rejection_reasons: list[str] | None = None,
) -> SceneDependencyPatch | None:
    repairable_rejections: list[str] = []

    def reject(reason: str = "") -> None:
        reasons = _unique_strings([*repairable_rejections, reason])
        return _reject(rejection_reasons, ";".join(reasons))

    try:
        data = json.loads(_strip_json_fence(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return reject("invalid_json")
    if not isinstance(data, dict) or not isinstance(data.get("changes"), list):
        return reject("root_or_changes_invalid")
    reading = _clean_text(data.get("reading"), MAX_READING_CHARS)
    changes: list[SceneDependencyChange] = []
    previous_by_id = {
        dependency.dependency_id: dependency
        for dependency in previous.dependencies
    } if previous else {}
    previous_ids = set(previous_by_id)
    seen_previous: set[str] = set()
    for value in data["changes"]:
        if not isinstance(value, dict):
            return reject("change_not_object")
        operation = str(value.get("op", "")).strip().lower()
        dependency_id = str(value.get("id", "")).strip()
        if operation not in CHANGE_OPERATIONS:
            return reject("invalid_operation")
        if operation == "add":
            if dependency_id:
                return reject("add_has_id")
        else:
            if dependency_id not in previous_ids or dependency_id in seen_previous:
                return reject("previous_id_invalid_or_duplicate")
            seen_previous.add(dependency_id)
        reason = _clean_text(value.get("reason"), 500)
        if operation == "retire":
            if not reason:
                return reject("retire_reason_missing")
            changes.append(
                SceneDependencyChange(
                    operation=operation,
                    dependency_id=dependency_id,
                    reason=reason,
                )
            )
            continue
        if operation == "keep":
            kept = previous_by_id[dependency_id]
            if any(ref not in allowed_sources for ref in kept.sources):
                return reject("keep_source_unavailable")
            changes.append(
                SceneDependencyChange(
                    operation=operation,
                    dependency_id=dependency_id,
                    text=kept.text,
                    status=kept.status,
                    horizon=kept.horizon,
                    sources=kept.sources,
                    temporal_boundaries=kept.temporal_boundaries,
                    reason=reason,
                )
            )
            continue
        text = _strict_clean_text(
            value.get("text"), MAX_SCENE_PROPOSITION_CHARS
        )
        status = str(value.get("status", "")).strip().lower()
        horizon = _strict_clean_text(
            value.get("horizon"), MAX_SCENE_HORIZON_CHARS
        )
        source_refs = _unique_strings(value.get("sources", []))
        if "temporal_boundaries" not in value:
            return reject("temporal_boundaries_missing")
        temporal_boundaries = _parse_temporal_boundaries(
            value.get("temporal_boundaries")
        )
        if not text:
            return reject("text_invalid")
        if status not in DEPENDENCY_STATES:
            return reject("status_invalid")
        if not horizon:
            return reject("horizon_invalid")
        if not source_refs:
            return reject("sources_missing")
        if any(ref not in allowed_sources for ref in source_refs):
            return reject("source_unavailable")
        if temporal_boundaries is None:
            return reject("temporal_boundaries_invalid")
        if (
            temporal_boundaries
            and temporal_source_ids is not None
            and not set(source_refs).intersection(temporal_source_ids)
        ):
            repairable_rejections.append("temporal_clock_source_missing")
        changes.append(
            SceneDependencyChange(
                operation=operation,
                dependency_id=dependency_id,
                text=text,
                status=status,
                horizon=horizon,
                sources=tuple(source_refs),
                temporal_boundaries=temporal_boundaries,
                reason=reason,
            )
        )
    if seen_previous != previous_ids:
        repairable_rejections.append("previous_dependencies_unaccounted")
    if sum(change.operation != "retire" for change in changes) > MAX_SCENE_DEPENDENCIES:
        repairable_rejections.append("too_many_dependencies")
    grounded_dependency_sources = {
        source_id
        for change in changes
        if change.operation != "retire"
        for source_id in change.sources
    }
    required = required_source_accounting or set()
    dispositions: list[SceneSourceDisposition] = []
    if required:
        raw_dispositions = data.get("source_dispositions")
        if not isinstance(raw_dispositions, list):
            return reject("source_dispositions_not_list")
        seen_sources: set[str] = set()
        for value in raw_dispositions:
            if not isinstance(value, dict):
                return reject("source_disposition_not_object")
            source_id = str(value.get("source", "")).strip()
            condition = str(value.get("condition", "")).strip().lower()
            dependency = str(value.get("dependency", "")).strip().lower()
            evidence_sources = _unique_strings(value.get("evidence_sources", []))
            reason = _clean_text(value.get("reason"), 500)
            if source_id not in required or source_id in seen_sources:
                return reject("source_disposition_id_invalid_or_duplicate")
            if condition not in SOURCE_CONDITION_DISPOSITIONS:
                return reject("source_condition_invalid")
            if dependency not in SOURCE_DEPENDENCY_DISPOSITIONS:
                return reject("source_dependency_invalid")
            if any(ref not in allowed_sources for ref in evidence_sources):
                return reject("disposition_evidence_unavailable")
            if condition == "superseded" and (not reason or not evidence_sources):
                return reject("superseded_evidence_missing")
            if condition == "nonoperative" and not reason:
                return reject("nonoperative_reason_missing")
            if dependency == "none" and condition == "represented" and not reason:
                return reject("represented_none_reason_missing")
            if condition != "represented" and dependency != "none":
                return reject("source_disposition_inconsistent")
            if (
                condition == "represented"
                and grounded_condition_sources is not None
                and source_id not in grounded_condition_sources
            ):
                repairable_rejections.append(
                    "represented_condition_not_grounded"
                )
            if (
                dependency == "represented"
                and source_id not in grounded_dependency_sources
            ):
                repairable_rejections.append(
                    "represented_dependency_not_grounded"
                )
            seen_sources.add(source_id)
            dispositions.append(
                SceneSourceDisposition(
                    source_id=source_id,
                    condition=condition,
                    dependency=dependency,
                    evidence_sources=tuple(evidence_sources),
                    reason=reason,
                )
            )
        if seen_sources != required:
            repairable_rejections.append("source_dispositions_unaccounted")
    if repairable_rejections:
        return reject()
    return SceneDependencyPatch(
        reading=reading,
        changes=tuple(changes),
        source_dispositions=tuple(dispositions),
    )


def parse_scene_patch(
    raw: str,
    *,
    previous: SceneSnapshot | None,
    allowed_sources: set[str],
    allowed_souvenirs: list[str],
    authoritative_sources: set[str],
    temporal_source_ids: set[str] | None = None,
    current_topics_enabled: bool = False,
    user_turn_sources: set[str] | None = None,
    rejection_reasons: list[str] | None = None,
) -> ScenePatch | None:
    try:
        data = json.loads(_strip_json_fence(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return _reject(rejection_reasons, "invalid_json")
    if not isinstance(data, dict):
        return _reject(rejection_reasons, "root_not_object")
    reading = _clean_text(data.get("reading"), MAX_READING_CHARS)
    conversation = _clean_text(
        data.get("conversation"), MAX_CONVERSATION_CHARS
    )
    raw_changes = data.get("changes")
    if not isinstance(raw_changes, list):
        return _reject(rejection_reasons, "changes_not_list")
    changes: list[SceneChange] = []
    seen_previous: set[str] = set()
    previous_by_id = {
        item.item_id: item for item in previous.items
    } if previous else {}
    previous_ids = set(previous_by_id)
    for value in raw_changes:
        if not isinstance(value, dict):
            return _reject(rejection_reasons, "change_not_object")
        operation = str(value.get("op", "")).strip().lower()
        item_id = str(value.get("id", "")).strip()
        if operation not in CHANGE_OPERATIONS:
            return _reject(rejection_reasons, "invalid_operation")
        if operation == "add":
            if item_id:
                return _reject(rejection_reasons, "add_has_id")
        else:
            if item_id not in previous_ids or item_id in seen_previous:
                return _reject(rejection_reasons, "previous_id_invalid_or_duplicate")
            seen_previous.add(item_id)
        reason = _clean_text(value.get("reason"), 500)
        if operation == "retire":
            if not reason:
                return _reject(rejection_reasons, "retire_reason_missing")
            changes.append(
                SceneChange(operation=operation, item_id=item_id, reason=reason)
            )
            continue
        if operation == "keep":
            kept = previous_by_id[item_id]
            if any(ref not in allowed_sources for ref in kept.sources):
                return _reject(rejection_reasons, "keep_source_unavailable")
            changes.append(
                SceneChange(
                    operation=operation,
                    item_id=item_id,
                    text=kept.text,
                    epistemic=kept.epistemic,
                    sources=kept.sources,
                    temporal_boundaries=kept.temporal_boundaries,
                    reason=reason,
                )
            )
            continue
        text = _strict_clean_text(
            value.get("text"), MAX_SCENE_PROPOSITION_CHARS
        )
        epistemic = str(value.get("epistemic", "")).strip().lower()
        source_refs = _unique_strings(value.get("sources", []))
        if "temporal_boundaries" not in value:
            return _reject(rejection_reasons, "temporal_boundaries_missing")
        temporal_boundaries = _parse_temporal_boundaries(
            value.get("temporal_boundaries")
        )
        if not text:
            return _reject(rejection_reasons, "text_invalid")
        if epistemic not in EPISTEMIC_STATES:
            return _reject(rejection_reasons, "epistemic_invalid")
        if not source_refs:
            return _reject(rejection_reasons, "sources_missing")
        if any(ref not in allowed_sources for ref in source_refs):
            return _reject(rejection_reasons, "source_unavailable")
        if temporal_boundaries is None:
            return _reject(rejection_reasons, "temporal_boundaries_invalid")
        if (
            temporal_boundaries
            and temporal_source_ids is not None
            and not set(source_refs).intersection(temporal_source_ids)
        ):
            return _reject(rejection_reasons, "temporal_clock_source_missing")
        if (
            epistemic == "established"
            and not set(source_refs).intersection(authoritative_sources)
        ):
            return _reject(rejection_reasons, "established_authority_missing")
        if operation == "add" and epistemic == "inferred":
            return _reject(rejection_reasons, "add_inferred_forbidden")
        changes.append(
            SceneChange(
                operation=operation,
                item_id=item_id,
                text=text,
                epistemic=epistemic,
                sources=tuple(source_refs),
                temporal_boundaries=temporal_boundaries,
                reason=reason,
            )
        )
    if seen_previous != previous_ids:
        return _reject(rejection_reasons, "previous_items_unaccounted")

    allowed = set(allowed_souvenirs)
    hot = _parse_memories(data.get("hot_memories"), allowed)
    released = _parse_memories(data.get("released_memories"), allowed)
    if hot is None or released is None:
        return _reject(rejection_reasons, "memories_invalid")
    if len(hot) > MAX_HOT_MEMORIES:
        return _reject(rejection_reasons, "too_many_hot_memories")
    hot_names = {memory.name for memory in hot}
    released_names = {memory.name for memory in released}
    if hot_names & released_names:
        return _reject(rejection_reasons, "hot_released_overlap")
    previous_hot = set(previous.hot_souvenirs if previous else [])
    if not previous_hot.issubset(hot_names | released_names):
        return _reject(rejection_reasons, "previous_hot_unaccounted")
    if any(not memory.reason for memory in released):
        return _reject(rejection_reasons, "released_reason_missing")

    current_topic_changes: tuple[SceneCurrentTopicChange, ...] = ()
    current_topics_valid = True
    current_topics_rejection = ""
    if current_topics_enabled:
        parsed_topics, current_topics_rejection = _parse_current_topic_changes(
            data.get("current_topic_changes"),
            previous=previous,
            allowed_sources=allowed_sources,
            user_turn_sources=user_turn_sources or set(),
        )
        if parsed_topics is None:
            current_topics_valid = False
        else:
            current_topic_changes = parsed_topics

    active_count = sum(change.operation != "retire" for change in changes)
    if active_count > MAX_SCENE_ITEMS:
        return _reject(rejection_reasons, "item_budget_exceeded")
    return ScenePatch(
        reading=reading,
        conversation=conversation,
        changes=tuple(changes),
        current_topic_changes=current_topic_changes,
        current_topics_valid=current_topics_valid,
        current_topics_rejection=current_topics_rejection,
        hot_memories=tuple(hot),
        released_memories=tuple(released),
    )


def _parse_current_topic_changes(
    raw_changes: object,
    *,
    previous: SceneSnapshot | None,
    allowed_sources: set[str],
    user_turn_sources: set[str],
) -> tuple[tuple[SceneCurrentTopicChange, ...] | None, str]:
    if not isinstance(raw_changes, list):
        return None, "current_topic_changes_not_list"
    previous_by_id = {
        topic.topic_id: topic for topic in (previous.current_topics if previous else ())
    }
    previous_ids = set(previous_by_id)
    seen_previous: set[str] = set()
    active_count = 0
    changes: list[SceneCurrentTopicChange] = []
    for value in raw_changes:
        if not isinstance(value, dict):
            return None, "current_topic_change_not_object"
        operation = str(value.get("op", "")).strip().casefold()
        topic_id = str(value.get("id", "")).strip()
        if operation not in CURRENT_TOPIC_OPERATIONS:
            return None, "current_topic_operation_invalid"
        if operation == "add":
            if topic_id:
                return None, "current_topic_add_has_id"
        else:
            if topic_id not in previous_ids or topic_id in seen_previous:
                return None, "current_topic_previous_id_invalid_or_duplicate"
            seen_previous.add(topic_id)
        reason = _clean_text(value.get("reason"), 500)
        if operation == "release":
            if not reason:
                return None, "current_topic_release_reason_missing"
            changes.append(
                SceneCurrentTopicChange(
                    operation=operation,
                    topic_id=topic_id,
                    reason=reason,
                )
            )
            continue
        active_count += 1
        if operation == "keep":
            kept = previous_by_id[topic_id]
            if any(source_id not in allowed_sources for source_id in kept.sources):
                return None, "current_topic_keep_source_unavailable"
            changes.append(
                SceneCurrentTopicChange(
                    operation=operation,
                    topic_id=topic_id,
                    text=kept.text,
                    sources=kept.sources,
                )
            )
            continue
        text = _strict_clean_text(value.get("text"), 500)
        source_refs = tuple(_unique_strings(value.get("sources", [])))
        if not text:
            return None, "current_topic_text_invalid"
        if "?" in text or any(pattern.search(text) for pattern in _PREPARED_TOPIC_MOVEMENT):
            return None, "current_topic_prepared_movement_forbidden"
        if not source_refs:
            return None, "current_topic_sources_missing"
        if any(source_id not in allowed_sources for source_id in source_refs):
            return None, "current_topic_source_unavailable"
        if not set(source_refs).intersection(user_turn_sources):
            return None, "current_topic_user_authority_missing"
        changes.append(
            SceneCurrentTopicChange(
                operation=operation,
                topic_id=topic_id,
                text=text,
                sources=source_refs,
            )
        )
    if seen_previous != previous_ids:
        return None, "current_topics_previous_unaccounted"
    if active_count > MAX_CURRENT_TOPICS:
        return None, "current_topics_budget_exceeded"
    return tuple(changes), ""


def _reject(
    rejection_reasons: list[str] | None,
    reason: str,
) -> None:
    if rejection_reasons is not None:
        rejection_reasons.append(reason)
    return None


def _deposit_scene_model(
    *,
    user_id: str,
    scene_scope: str,
    source_trace_id: str,
    source_turn_started_at: float,
    provider: LLMProvider,
    previous: SceneSnapshot | None,
    sources: dict[str, SceneSource],
    patch: ScenePatch,
    dependency_patch: SceneDependencyPatch,
    allow_same_source_revision: bool = False,
) -> SceneSnapshot | None:
    path = scene_model_path(user_id, scene_scope)
    marker = scene_invalidation_path(user_id)
    with memory_file_lock(marker):
        invalidated = marker.exists()
        invalidated_at = (
            _scene_invalidation_timestamp(marker) if invalidated else None
        )
        if invalidated and (
            invalidated_at is None
            or source_turn_started_at <= invalidated_at
        ):
            logger.info(
                "Scene model antérieur à l'invalidation non déposé | "
                "scope=%s | source=%s",
                scene_scope,
                source_trace_id,
            )
            return None
        with memory_file_lock(path):
            current = _read_scene_model_path(path) if path.exists() else None
            same_source_continuation = bool(
                allow_same_source_revision
                and current
                and current.source_trace_id == source_trace_id
                and current.source_turn_started_at == source_turn_started_at
            )
            if (
                current
                and current.source_turn_started_at >= source_turn_started_at
                and not same_source_continuation
            ):
                logger.info(
                    "Scene model obsolète non déposé | scope=%s | source=%s",
                    scene_scope,
                    source_trace_id,
                )
                return None
            if current != previous:
                logger.info(
                    "Scene model révisé depuis un socle périmé | scope=%s | source=%s",
                    scene_scope,
                    source_trace_id,
                )
                return None
            revision = (current.revision + 1) if current else 1
            items: list[SceneItem] = []
            retired: list[RetiredSceneItem] = []
            add_index = 0
            for change in patch.changes:
                if change.operation == "retire":
                    retired.append(
                        RetiredSceneItem(change.item_id, change.reason)
                    )
                    continue
                if change.operation == "add":
                    add_index += 1
                    item_id = _new_item_id(revision, add_index, change.text)
                else:
                    item_id = change.item_id
                items.append(
                    SceneItem(
                        item_id=item_id,
                        text=change.text,
                        epistemic=change.epistemic,
                        sources=change.sources,
                        temporal_boundaries=change.temporal_boundaries,
                    )
                )
            if len(items) > MAX_SCENE_ITEMS:
                return None
            dependencies: list[SceneDependency] = []
            retired_dependencies: list[RetiredSceneItem] = []
            dependency_add_index = 0
            for change in dependency_patch.changes:
                if change.operation == "retire":
                    retired_dependencies.append(
                        RetiredSceneItem(change.dependency_id, change.reason)
                    )
                    continue
                if change.operation == "add":
                    dependency_add_index += 1
                    dependency_id = _new_dependency_id(
                        revision,
                        dependency_add_index,
                        change.text,
                    )
                else:
                    dependency_id = change.dependency_id
                dependencies.append(
                    SceneDependency(
                        dependency_id=dependency_id,
                        text=change.text,
                        status=change.status,
                        horizon=change.horizon,
                        sources=change.sources,
                        temporal_boundaries=change.temporal_boundaries,
                    )
                )
            if len(dependencies) > MAX_SCENE_DEPENDENCIES:
                return None
            current_topics: list[SceneCurrentTopic] = []
            current_topic_add_index = 0
            for change in patch.current_topic_changes:
                if change.operation == "release":
                    continue
                if change.operation == "add":
                    current_topic_add_index += 1
                    topic_id = _new_current_topic_id(
                        revision,
                        current_topic_add_index,
                        change.text,
                    )
                else:
                    topic_id = change.topic_id
                current_topics.append(
                    SceneCurrentTopic(
                        topic_id=topic_id,
                        text=change.text,
                        sources=change.sources,
                    )
                )
            if len(current_topics) > MAX_CURRENT_TOPICS:
                return None
            used_source_ids = {
                source_id for item in items for source_id in item.sources
            }
            used_source_ids.update(
                source_id
                for dependency in dependencies
                for source_id in dependency.sources
            )
            used_source_ids.update(
                source_id
                for topic in current_topics
                for source_id in topic.sources
            )
            item_source_ids = {
                source_id for item in items for source_id in item.sources
            }
            dependency_source_ids = {
                source_id
                for dependency in dependencies
                for source_id in dependency.sources
            }
            dispositions_by_source = {
                disposition.source_id: disposition
                for disposition in (
                    current.source_dispositions if current is not None else ()
                )
                if disposition.source_id in sources
                and set(disposition.evidence_sources).issubset(sources)
                and (
                    sources[disposition.source_id].kind != "l0_turn"
                    or _l0_supersession_has_mutable_evidence(
                        disposition,
                        sources,
                    )
                )
                and (
                    disposition.condition != "represented"
                    or disposition.source_id in item_source_ids
                )
                and (
                    disposition.dependency != "represented"
                    or disposition.source_id in dependency_source_ids
                )
                and (
                    disposition.condition == "represented"
                    or disposition.source_id not in item_source_ids
                )
            }
            dispositions_by_source.update(
                {
                    disposition.source_id: disposition
                    for disposition in dependency_patch.source_dispositions
                    if disposition.source_id in sources
                    and (
                        sources[disposition.source_id].kind != "l0_turn"
                        or _l0_supersession_has_mutable_evidence(
                            disposition,
                            sources,
                        )
                    )
                }
            )
            source_dispositions = tuple(
                dispositions_by_source[source_id]
                for source_id in sorted(dispositions_by_source)
            )
            used_source_ids.update(
                disposition.source_id for disposition in source_dispositions
            )
            used_source_ids.update(
                source_id
                for disposition in source_dispositions
                for source_id in disposition.evidence_sources
            )
            retained_sources = tuple(
                sources[source_id]
                for source_id in sorted(used_source_ids)
                if source_id in sources
            )
            if len(retained_sources) != len(used_source_ids):
                missing = sorted(used_source_ids.difference(sources))
                raise ValueError(
                    "scene deposit references unavailable sources: "
                    + ", ".join(missing)
                )
            l0_sources = [
                source for source in sources.values() if source.kind == "l0_turn"
            ]
            watermark = max(
                l0_sources,
                key=lambda source: source.locator.rsplit(" — ", 1)[0],
                default=None,
            )
            watermark_source_id = (
                watermark.source_id if watermark is not None else ""
            )
            watermark_locator = watermark.locator if watermark is not None else ""
            if current is not None and (
                not watermark_locator
                or current.dialogue_watermark_locator.rsplit(" — ", 1)[0]
                >= watermark_locator.rsplit(" — ", 1)[0]
            ):
                watermark_source_id = current.dialogue_watermark_source_id
                watermark_locator = current.dialogue_watermark_locator
            snapshot = SceneSnapshot(
                scene_scope=scene_scope,
                source_trace_id=source_trace_id,
                source_turn_started_at=source_turn_started_at,
                revised_at=time.time(),
                revision=revision,
                provider=_provider_snapshot(provider),
                dialogue_watermark_source_id=watermark_source_id,
                dialogue_watermark_locator=watermark_locator,
                reading=patch.reading,
                conversation=patch.conversation,
                items=tuple(items),
                dependencies=tuple(dependencies),
                current_topics=tuple(current_topics),
                sources=retained_sources,
                source_dispositions=source_dispositions,
                hot_memories=patch.hot_memories,
                retired_items=tuple(retired),
                retired_dependencies=tuple(retired_dependencies),
                released_memories=patch.released_memories,
            )
            atomic_write_text(path, _render_markdown(snapshot))
            return snapshot


def _scene_invalidation_timestamp(marker: Path) -> float | None:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        value = float(payload.get("invalidated_at"))
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if value > 0 else None


def _read_scene_model_path(path: Path) -> SceneSnapshot | None:
    if not path.exists():
        return None
    frontmatter, body = parse_souvenir_frontmatter(path.read_text(encoding="utf-8"))
    if int(frontmatter.get("schema_version", 0)) != SCENE_MODEL_SCHEMA_VERSION:
        return None
    raw_items = frontmatter.get("items", [])
    raw_dependencies = frontmatter.get("dependencies", [])
    raw_current_topics = frontmatter.get("current_topics", [])
    if (
        not isinstance(raw_items, list)
        or not isinstance(raw_dependencies, list)
        or not isinstance(raw_current_topics, list)
    ):
        return None
    if any(
        not isinstance(value, dict)
        or "temporal_boundaries" not in value
        or _parse_temporal_boundaries(value.get("temporal_boundaries")) is None
        for value in raw_items + raw_dependencies
    ):
        return None
    items = tuple(
        SceneItem(
            item_id=str(value.get("id", "")).strip(),
            text=str(value.get("text", "")).strip(),
            epistemic=str(value.get("epistemic", "")).strip(),
            sources=tuple(_unique_strings(value.get("sources", []))),
            temporal_boundaries=(
                _parse_temporal_boundaries(value.get("temporal_boundaries")) or ()
            ),
        )
        for value in raw_items
        if isinstance(value, dict) and str(value.get("id", "")).strip()
    )
    dependencies = tuple(
        SceneDependency(
            dependency_id=str(value.get("id", "")).strip(),
            text=str(value.get("text", "")).strip(),
            status=str(value.get("status", "")).strip(),
            horizon=str(value.get("horizon", "")).strip(),
            sources=tuple(_unique_strings(value.get("sources", []))),
            temporal_boundaries=(
                _parse_temporal_boundaries(value.get("temporal_boundaries")) or ()
            ),
        )
        for value in raw_dependencies
        if isinstance(value, dict) and str(value.get("id", "")).strip()
    )
    current_topics = tuple(
        SceneCurrentTopic(
            topic_id=str(value.get("id", "")).strip(),
            text=str(value.get("text", "")).strip(),
            sources=tuple(_unique_strings(value.get("sources", []))),
        )
        for value in raw_current_topics
        if isinstance(value, dict) and str(value.get("id", "")).strip()
    )
    sources = tuple(
        SceneSource(
            source_id=str(value.get("id", "")).strip(),
            kind=str(value.get("kind", "")).strip(),
            locator=str(value.get("locator", "")).strip(),
            sha256=str(value.get("sha256", "")).strip(),
            excerpt=str(value.get("excerpt", "")),
            truncated=bool(value.get("truncated", False)),
        )
        for value in frontmatter.get("sources", [])
        if isinstance(value, dict) and str(value.get("id", "")).strip()
    )
    raw_source_dispositions = frontmatter.get("source_dispositions", [])
    if not isinstance(raw_source_dispositions, list):
        return None
    source_dispositions = tuple(
        SceneSourceDisposition(
            source_id=str(value.get("source", "")).strip(),
            condition=str(value.get("condition", "")).strip(),
            dependency=str(value.get("dependency", "")).strip(),
            evidence_sources=tuple(
                _unique_strings(value.get("evidence_sources", []))
            ),
            reason=str(value.get("reason", "")).strip(),
        )
        for value in raw_source_dispositions
        if isinstance(value, dict) and str(value.get("source", "")).strip()
    )
    hot = _read_memories(frontmatter.get("hot_memories", []))
    retired = tuple(
        RetiredSceneItem(
            item_id=str(value.get("id", "")).strip(),
            reason=str(value.get("reason", "")).strip(),
        )
        for value in frontmatter.get("retired_items", [])
        if isinstance(value, dict) and str(value.get("id", "")).strip()
    )
    retired_dependencies = tuple(
        RetiredSceneItem(
            item_id=str(value.get("id", "")).strip(),
            reason=str(value.get("reason", "")).strip(),
        )
        for value in frontmatter.get("retired_dependencies", [])
        if isinstance(value, dict) and str(value.get("id", "")).strip()
    )
    released = _read_memories(frontmatter.get("released_memories", []))
    sections = _parse_body_sections(body)
    snapshot = SceneSnapshot(
        scene_scope=str(frontmatter.get("scene_scope", "")).strip(),
        source_trace_id=str(frontmatter.get("source_trace_id", "")).strip(),
        source_turn_started_at=float(frontmatter.get("source_turn_started_at", 0.0)),
        revised_at=float(frontmatter.get("revised_at", 0.0)),
        revision=int(frontmatter.get("revision", 0)),
        provider=frontmatter.get("provider", {})
        if isinstance(frontmatter.get("provider"), dict)
        else {},
        dialogue_watermark_source_id=str(
            frontmatter.get("dialogue_watermark_source_id", "")
        ).strip(),
        dialogue_watermark_locator=str(
            frontmatter.get("dialogue_watermark_locator", "")
        ).strip(),
        reading=sections.get("Lecture de la scène", ""),
        conversation=sections.get("Texture conversationnelle", ""),
        items=items,
        dependencies=dependencies,
        current_topics=current_topics,
        sources=sources,
        source_dispositions=source_dispositions,
        hot_memories=hot,
        retired_items=retired,
        retired_dependencies=retired_dependencies,
        released_memories=released,
    )
    if any(item.epistemic not in EPISTEMIC_STATES for item in snapshot.items):
        return None
    if any(
        dependency.status not in DEPENDENCY_STATES
        or not dependency.horizon
        for dependency in snapshot.dependencies
    ):
        return None
    if (
        len(snapshot.current_topics) > MAX_CURRENT_TOPICS
        or len({topic.topic_id for topic in snapshot.current_topics})
        != len(snapshot.current_topics)
        or any(not topic.text for topic in snapshot.current_topics)
    ):
        return None
    source_ids = {source.source_id for source in snapshot.sources}
    clock_source_ids = {
        source.source_id for source in snapshot.sources if source.kind == "clock"
    }
    if any(not set(item.sources).issubset(source_ids) for item in snapshot.items):
        return None
    if any(
        not set(topic.sources).issubset(source_ids) or not topic.sources
        for topic in snapshot.current_topics
    ):
        return None
    if any(
        item.temporal_boundaries
        and not set(item.sources).intersection(clock_source_ids)
        for item in snapshot.items
    ):
        return None
    if any(
        not set(dependency.sources).issubset(source_ids)
        for dependency in snapshot.dependencies
    ):
        return None
    if any(
        dependency.temporal_boundaries
        and not set(dependency.sources).intersection(clock_source_ids)
        for dependency in snapshot.dependencies
    ):
        return None
    disposition_source_ids = [
        disposition.source_id for disposition in snapshot.source_dispositions
    ]
    item_source_ids = {
        source_id for item in snapshot.items for source_id in item.sources
    }
    dependency_source_ids = {
        source_id
        for dependency in snapshot.dependencies
        for source_id in dependency.sources
    }
    if (
        len(disposition_source_ids) != len(set(disposition_source_ids))
        or any(
            disposition.source_id not in source_ids
            or disposition.condition not in SOURCE_CONDITION_DISPOSITIONS
            or disposition.dependency not in SOURCE_DEPENDENCY_DISPOSITIONS
            or not set(disposition.evidence_sources).issubset(source_ids)
            or (
                disposition.condition == "represented"
                and disposition.source_id not in item_source_ids
            )
            or (
                disposition.dependency == "represented"
                and disposition.source_id not in dependency_source_ids
            )
            or (
                disposition.condition == "superseded"
                and (
                    not disposition.reason
                    or not disposition.evidence_sources
                )
            )
            or (
                disposition.condition == "nonoperative"
                and not disposition.reason
            )
            or (
                disposition.condition == "represented"
                and disposition.dependency == "none"
                and not disposition.reason
            )
            or (
                disposition.condition != "represented"
                and disposition.dependency != "none"
            )
            for disposition in snapshot.source_dispositions
        )
    ):
        return None
    return snapshot


def _render_markdown(snapshot: SceneSnapshot) -> str:
    frontmatter = {
        "schema_version": SCENE_MODEL_SCHEMA_VERSION,
        "prompt_version": SCENE_MODEL_PROMPT_VERSION,
        "scene_scope": snapshot.scene_scope,
        "source_trace_id": snapshot.source_trace_id,
        "source_turn_started_at": snapshot.source_turn_started_at,
        "revised_at": snapshot.revised_at,
        "revision": snapshot.revision,
        "provider": snapshot.provider,
        "dialogue_watermark_source_id": snapshot.dialogue_watermark_source_id,
        "dialogue_watermark_locator": snapshot.dialogue_watermark_locator,
        "items": [
            {
                "id": item.item_id,
                "text": item.text,
                "epistemic": item.epistemic,
                "sources": list(item.sources),
                "temporal_boundaries": [
                    asdict(boundary) for boundary in item.temporal_boundaries
                ],
            }
            for item in snapshot.items
        ],
        "dependencies": [
            {
                "id": dependency.dependency_id,
                "text": dependency.text,
                "status": dependency.status,
                "horizon": dependency.horizon,
                "sources": list(dependency.sources),
                "temporal_boundaries": [
                    asdict(boundary)
                    for boundary in dependency.temporal_boundaries
                ],
            }
            for dependency in snapshot.dependencies
        ],
        "current_topics": [
            {
                "id": topic.topic_id,
                "text": topic.text,
                "sources": list(topic.sources),
            }
            for topic in snapshot.current_topics
        ],
        "sources": [
            {
                "id": source.source_id,
                "kind": source.kind,
                "locator": source.locator,
                "sha256": source.sha256,
                "excerpt": source.excerpt,
                "truncated": source.truncated,
            }
            for source in snapshot.sources
        ],
        "source_dispositions": [
            {
                "source": disposition.source_id,
                "condition": disposition.condition,
                "dependency": disposition.dependency,
                "evidence_sources": list(disposition.evidence_sources),
                "reason": disposition.reason,
            }
            for disposition in snapshot.source_dispositions
        ],
        "hot_memories": [asdict(memory) for memory in snapshot.hot_memories],
        "retired_items": [
            {"id": item.item_id, "reason": item.reason}
            for item in snapshot.retired_items
        ],
        "retired_dependencies": [
            {"id": item.item_id, "reason": item.reason}
            for item in snapshot.retired_dependencies
        ],
        "released_memories": [
            asdict(memory) for memory in snapshot.released_memories
        ],
    }
    rendered = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{rendered}\n---\n\n{snapshot.body.strip()}\n"


def _render_body(snapshot: SceneSnapshot) -> str:
    sections = ["# Scene model"]
    if snapshot.reading:
        sections.append(f"## Lecture de la scène\n{snapshot.reading}")
    if snapshot.conversation:
        sections.append(
            f"## Texture conversationnelle\n{snapshot.conversation}"
        )
    if snapshot.items:
        lines = []
        for item in snapshot.items:
            line = f"- `{item.item_id}` [{item.epistemic}] {item.text}"
            if item.temporal_boundaries:
                boundaries = "; ".join(
                    f"{boundary.at} -> {boundary.after}"
                    for boundary in item.temporal_boundaries
                )
                line += f" (seuils: {boundaries})"
            lines.append(line)
        sections.append("## Éléments actifs\n" + "\n".join(lines))
    if snapshot.dependencies:
        lines = []
        for dependency in snapshot.dependencies:
            line = (
                f"- `{dependency.dependency_id}` [{dependency.status}] "
                f"({dependency.horizon}) {dependency.text}"
            )
            if dependency.temporal_boundaries:
                boundaries = "; ".join(
                    f"{boundary.at} -> {boundary.after}"
                    for boundary in dependency.temporal_boundaries
                )
                line += f" (seuils: {boundaries})"
            lines.append(line)
        sections.append("## Dépendances ouvertes\n" + "\n".join(lines))
    if snapshot.current_topics:
        sections.append(
            "## Sujets du moment\n"
            + "\n".join(
                f"- `{topic.topic_id}` {topic.text}"
                for topic in snapshot.current_topics
            )
        )
    if snapshot.hot_memories:
        lines = [
            f"- [[{memory.name}]] — {memory.reason or 'still present'}"
            for memory in snapshot.hot_memories
        ]
        sections.append("## Mémoires en attention\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _render_previous_dependencies(previous: SceneSnapshot | None) -> str:
    if previous is None or not previous.dependencies:
        return 'No previous dependency.'
    return json.dumps(
        [
            {
                "id": dependency.dependency_id,
                "text": dependency.text,
                "status": dependency.status,
                "horizon": dependency.horizon,
                "sources": list(dependency.sources),
                "temporal_boundaries": [
                    asdict(boundary)
                    for boundary in dependency.temporal_boundaries
                ],
            }
            for dependency in previous.dependencies
        ],
        ensure_ascii=False,
        indent=2,
    )


def _dialogue_sources(
    dialogue: str,
    *,
    max_chars: int = MAX_SOURCE_EXCERPT_CHARS,
) -> dict[str, SceneSource]:
    sources: dict[str, SceneSource] = {}
    for timestamp, role, content in _parse_dialogue_turns(dialogue):
        source = _source(
            kind="l0_turn",
            locator=f"{timestamp} — {role}",
            content=content,
            max_chars=max_chars,
        )
        sources[source.source_id] = source
    return sources


def _source_accounting_ids(
    *,
    previous: SceneSnapshot | None,
    sources: dict[str, SceneSource],
    bootstrap_source_ids: set[str],
) -> list[str]:
    previous_source_ids = {
        source.source_id for source in previous.sources
    } if previous else set()
    previous_disposition_ids = {
        disposition.source_id
        for disposition in previous.source_dispositions
        if set(disposition.evidence_sources).issubset(sources)
    } if previous else set()
    stale_l0_disposition_ids = {
        disposition.source_id
        for disposition in previous.source_dispositions
        if disposition.condition == "superseded"
        and disposition.source_id in sources
        and not set(disposition.evidence_sources).issubset(sources)
    } if previous else set()
    result: list[str] = []
    for source_id, source in sources.items():
        if (
            source.kind == "l0_turn"
            and source_id in previous_source_ids
            and source_id not in stale_l0_disposition_ids
        ):
            continue
        if (
            source_id in previous_source_ids
            and source_id in previous_disposition_ids
        ):
            continue
        role = source.locator.rsplit(" — ", 1)[-1].casefold()
        if (
            source.kind in {
                "active_context",
                "agenda",
            }
            or source_id in bootstrap_source_ids
            or source_id in stale_l0_disposition_ids
            or (source.kind == "l0_turn" and role == "user")
        ):
            result.append(source_id)
    return result


def authoritative_scene_source_ids(
    sources: dict[str, SceneSource],
) -> set[str]:
    """Return sources allowed to establish, without making a semantic choice."""

    authoritative = set()
    for source_id, source in sources.items():
        if source.kind == "l0_turn":
            role = source.locator.rsplit(" — ", 1)[-1].casefold()
            if role == "user":
                authoritative.add(source_id)
            continue
        if source.kind in AUTHORITATIVE_SCENE_SOURCE_KINDS:
            authoritative.add(source_id)
    return authoritative


def _document_sources(
    *,
    kind: str,
    locator: str,
    content: str,
) -> dict[str, SceneSource]:
    """Expose each Markdown statement as an independently traceable source."""

    text = content.strip()
    if not text:
        return {}
    fragments = _markdown_source_fragments(text)
    if len(fragments) <= 1:
        source = _source(kind=kind, locator=locator, content=text)
        return {source.source_id: source}
    sources: dict[str, SceneSource] = {}
    for headings, body in fragments:
        excerpt = "\n".join((*headings, body)).strip()
        section = headings[-1].lstrip("# ").strip() if headings else "document"
        digest = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()[:10]
        source = _source(
            kind=kind,
            locator=f"{locator}#{section}/{digest}",
            content=excerpt,
        )
        sources[source.source_id] = source
    return sources


def _markdown_source_fragments(
    text: str,
) -> list[tuple[tuple[str, ...], str]]:
    headings: list[str] = []
    fragments: list[tuple[tuple[str, ...], str]] = []
    current: list[str] = []
    current_headings: tuple[str, ...] = ()

    def flush() -> None:
        nonlocal current
        body = "\n".join(current).strip()
        if body:
            fragments.append((current_headings, body))
        current = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and stripped.lstrip("#").startswith(" "):
            flush()
            level = len(stripped) - len(stripped.lstrip("#"))
            headings[:] = headings[: max(0, level - 1)]
            headings.append(stripped)
            continue
        if stripped.startswith("- "):
            flush()
            current_headings = tuple(headings)
            current = [stripped]
            continue
        if not stripped:
            flush()
            continue
        if not current:
            current_headings = tuple(headings)
        current.append(line.rstrip())
    flush()
    return fragments


def _render_bootstrap_catalog(sources: dict[str, SceneSource]) -> str:
    blocks = [
        f"### {source.source_id}\nlocator: {source.locator}\n\n{source.excerpt}"
        for source in sources.values()
    ]
    rendered = "\n\n".join(blocks)
    if len(rendered) > MAX_BOOTSTRAP_CATALOG_CHARS:
        raise ValueError(
            "scene bootstrap catalog exceeds its explicit bound "
            f"({len(rendered)} > {MAX_BOOTSTRAP_CATALOG_CHARS} chars)"
        )
    return rendered or 'No L0 turn.'


def _render_source_catalog(sources: dict[str, SceneSource]) -> str:
    values = list(sources.values())
    blocks = [
        _render_source_catalog_block(source, source.excerpt)
        for source in values
    ]
    rendered = "\n\n".join(blocks)
    if len(rendered) <= MAX_SOURCE_CATALOG_CHARS:
        return rendered or 'No source.'

    empty_blocks = [
        _render_source_catalog_block(source, "")
        for source in values
    ]
    fixed_chars = sum(len(block) for block in empty_blocks)
    fixed_chars += max(0, len(empty_blocks) - 1) * 2
    excerpt_budget = MAX_SOURCE_CATALOG_CHARS - fixed_chars
    if excerpt_budget < len(values) * MIN_BOUNDED_SOURCE_EXCERPT_CHARS:
        raise ValueError(
            "scene source catalog metadata exceeds its explicit bound "
            f"({fixed_chars} fixed chars for {len(values)} sources)"
        )

    limits = _allocate_source_excerpt_limits(
        [len(source.excerpt) for source in values],
        excerpt_budget,
    )
    bounded_blocks = [
        _render_source_catalog_block(
            source,
            _bounded_source_excerpt(source.excerpt, limit),
        )
        for source, limit in zip(values, limits, strict=True)
    ]
    bounded = "\n\n".join(bounded_blocks)
    if len(bounded) > MAX_SOURCE_CATALOG_CHARS:
        raise ValueError(
            "bounded scene source catalog exceeds its explicit bound "
            f"({len(bounded)} > {MAX_SOURCE_CATALOG_CHARS} chars)"
        )
    logger.info(
        "Scene source catalog borné | sources=%s | chars=%s->%s",
        len(values),
        len(rendered),
        len(bounded),
    )
    return bounded or 'No source.'


def _render_source_catalog_block(source: SceneSource, excerpt: str) -> str:
    return (
        f"### {source.source_id}\n"
        f"kind: {source.kind}\nlocator: {source.locator}\n\n{excerpt}"
    )


def _allocate_source_excerpt_limits(
    lengths: list[int],
    budget: int,
) -> list[int]:
    """Share a fixed prompt budget while preserving every source identifier."""

    limits = [0] * len(lengths)
    active = list(range(len(lengths)))
    remaining = max(0, budget)
    while active:
        share = remaining // len(active)
        completed = [index for index in active if lengths[index] <= share]
        if not completed:
            for index in active:
                limits[index] = share
            leftover = remaining - share * len(active)
            for index in active[:leftover]:
                limits[index] += 1
            break
        for index in completed:
            limits[index] = lengths[index]
            remaining -= lengths[index]
        active = [index for index in active if index not in completed]
    return limits


def _bounded_source_excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = '\n[... bounded source excerpt ...]\n'
    if limit <= len(marker) + 2:
        return text[:limit]
    available = limit - len(marker)
    head = (available + 1) // 2
    tail = available - head
    return text[:head] + marker + text[-tail:]


def _source(
    *,
    kind: str,
    locator: str,
    content: str,
    max_chars: int = MAX_SOURCE_EXCERPT_CHARS,
) -> SceneSource:
    exact = content.strip()
    digest = hashlib.sha256(exact.encode("utf-8")).hexdigest()
    identity = f"{kind}\0{locator}\0{exact}"
    identity_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    source_id = f"src-{kind}-{identity_digest[:12]}"
    excerpt = exact[:max_chars]
    return SceneSource(
        source_id=source_id,
        kind=kind,
        locator=locator,
        sha256=digest,
        excerpt=excerpt,
        truncated=len(exact) > len(excerpt),
    )


def _parse_dialogue_turns(dialogue: str) -> list[tuple[str, str, str]]:
    matches = list(_TURN_HEADER.finditer(dialogue or ""))
    turns: list[tuple[str, str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(dialogue)
        content = dialogue[start:end].strip()
        if content:
            turns.append((match.group(1).strip(), match.group(2).lower(), content))
    return turns


def _render_dialogue_turns(turns: list[tuple[str, str, str]]) -> str:
    return "\n\n".join(
        f"## {timestamp} — {role}\n\n{content}"
        for timestamp, role, content in turns
    )


def _parse_memories(
    value: object, allowed: set[str]
) -> list[SceneMemory] | None:
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    result: list[SceneMemory] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name", "")).strip()
        reason = _clean_text(item.get("reason"), 500)
        if not name or name not in allowed or name in seen:
            return None
        seen.add(name)
        result.append(SceneMemory(name=name, reason=reason))
    return result


def _read_memories(value: object) -> tuple[SceneMemory, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        SceneMemory(
            name=str(item.get("name", "")).strip(),
            reason=str(item.get("reason", "")).strip(),
        )
        for item in value
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    )


def _parse_body_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = ""
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(lines).strip()
            current = line[3:].strip()
            lines = []
        elif current:
            lines.append(line)
    if current:
        sections[current] = "\n".join(lines).strip()
    return sections


def _projection_now(now: datetime | None) -> datetime:
    if now is not None:
        current = now
    else:
        forced = os.getenv("DAEMON_CORE_EVAL_NOW", "").strip()
        try:
            current = datetime.fromisoformat(forced) if forced else None
        except ValueError:
            current = None
        current = current or datetime.now().astimezone()
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return current


def _boundary_datetime(boundary: SceneTemporalBoundary) -> datetime:
    return datetime.fromisoformat(boundary.at)


def _crossed_boundary(
    boundaries: tuple[SceneTemporalBoundary, ...],
    now: datetime,
) -> SceneTemporalBoundary | None:
    crossed = [
        boundary
        for boundary in boundaries
        if now >= _boundary_datetime(boundary)
    ]
    return crossed[-1] if crossed else None


def _next_boundary(
    boundaries: tuple[SceneTemporalBoundary, ...],
    now: datetime,
) -> SceneTemporalBoundary | None:
    return next(
        (
            boundary
            for boundary in boundaries
            if now < _boundary_datetime(boundary)
        ),
        None,
    )


def _project_scene_item(
    item: SceneItem,
    now: datetime,
) -> tuple[str, bool]:
    crossed = _crossed_boundary(item.temporal_boundaries, now)
    if crossed is not None:
        return (
            f"- [uncertain; seuil franchi: {crossed.at}] {crossed.after}",
            True,
        )
    upcoming = _next_boundary(item.temporal_boundaries, now)
    suffix = f"; prochain seuil: {upcoming.at}" if upcoming else ""
    return f"- [{item.epistemic}{suffix}] {item.text}", False


def _project_scene_dependency(
    dependency: SceneDependency,
    now: datetime,
) -> tuple[str, bool]:
    crossed = _crossed_boundary(dependency.temporal_boundaries, now)
    if crossed is not None:
        return (
            f"- [uncertain; seuil franchi: {crossed.at}] {crossed.after}",
            True,
        )
    upcoming = _next_boundary(dependency.temporal_boundaries, now)
    suffix = f"; prochain seuil: {upcoming.at}" if upcoming else ""
    return (
        f"- [{dependency.status}; horizon: {dependency.horizon}{suffix}] "
        f"{dependency.text}",
        False,
    )


def _parse_temporal_boundaries(
    value: object,
) -> tuple[SceneTemporalBoundary, ...] | None:
    if not isinstance(value, list) or len(value) > MAX_TEMPORAL_BOUNDARIES:
        return None
    result: list[tuple[datetime, SceneTemporalBoundary]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            return None
        after = _strict_clean_text(
            raw.get("after"), MAX_SCENE_PROPOSITION_CHARS
        )
        try:
            parsed = datetime.fromisoformat(str(raw.get("at", "")).strip())
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None or not after:
            return None
        at = parsed.isoformat(timespec="seconds")
        if at in seen:
            return None
        seen.add(at)
        result.append((parsed, SceneTemporalBoundary(at=at, after=after)))
    result.sort(key=lambda pair: pair[0])
    return tuple(boundary for _parsed, boundary in result)


def _clean_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit].rstrip()


def _strict_clean_text(value: object, limit: int) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    if len(text) > limit:
        return None
    return text


def _unique_strings(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _new_item_id(revision: int, index: int, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"scene-{revision}-{index}-{digest}"


def _new_dependency_id(revision: int, index: int, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"dependency-{revision}-{index}-{digest}"


def _new_current_topic_id(revision: int, index: int, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"topic-{revision}-{index}-{digest}"


def _provider_snapshot(provider: LLMProvider) -> dict[str, Any]:
    return {
        "type": provider.__class__.__name__,
        "model": getattr(provider, "model", None),
        "reasoning_effort": getattr(provider, "reasoning_effort", None),
        "scene_model_prompt_version": SCENE_MODEL_PROMPT_VERSION,
    }


def _scene_stage_retry_messages(
    messages: list[dict],
    stage: str,
    *,
    rejected_raw: str = "",
    rejection_reason: str = "",
) -> list[dict]:
    prefix = (
        f'The previous output of judgment "{stage}" was rejected by the structural contract. Redo the judgment from the supplied sources and emit one strict JSON object matching exactly the schema, authorized identifiers and all bounds. '
    )
    suffix = (
        'A `keep` retains the previous unit without rewriting it. Any previous source absent from the current catalog forbids `keep` and requires `revise` or `retire`. Any unit carrying `temporal_boundaries` must cite an authorized `clock` source in `sources`.'
    )
    reason = rejection_reason.strip()
    if not rejected_raw.strip() and not reason:
        return [*messages, {"role": "user", "content": prefix + suffix}]
    retry = list(messages)
    if rejected_raw.strip():
        retry.append({"role": "assistant", "content": rejected_raw.strip()})
    detail = _scene_stage_retry_guidance(stage, reason)
    audit = _scene_stage_retry_audit(stage)
    rejection_label = 'Rejection codes' if ";" in reason else 'Rejection code'
    retry.append(
        {
            "role": "user",
            "content": (
                prefix
                + (
                    f"{rejection_label} : `{reason}`. {detail} "
                    if reason
                    else ""
                )
                + f'Before emitting, audit the entire JSON: {audit} '
                + suffix
            ),
        },
    )
    return retry


def _scene_stage_retry_audit(stage: str) -> str:
    if stage == "dépendances":
        return (
            'each previous dependency appears exactly once; each temporal boundary cites a `clock` source; each source is authorized; each unit to account for has exactly one disposition consistent with the conditions and dependencies citing it.'
        )
    return (
        'each previous item appears exactly once; each `established` change cites at least one authoritative exact source; each temporal boundary cites a `clock` source; each source and souvenir is authorized; each previous hot memory is retained or explicitly released.'
    )


def _scene_stage_retry_guidance(stage: str, reason: str) -> str:
    reasons = _unique_strings(reason.split(";"))
    if len(reasons) > 1:
        return " ".join(
            _scene_stage_retry_guidance(stage, value)
            for value in reasons
        )
    if reason == "established_authority_missing":
        return (
            'At least one `established` change cites only a derived projection. Add an authorized exact source that actually establishes this change, or explicitly retain its uncertainty.'
        )
    if reason == "temporal_clock_source_missing":
        return (
            'At least one change carries a temporal boundary without citing the available `clock` source. Add it to the affected change without inventing its outcome.'
        )
    if stage == "dépendances" and reason == "represented_condition_not_grounded":
        return (
            'A disposition claims that its source represents a condition, but no grounded item cites it. Correct the disposition without fabricating a condition.'
        )
    if stage == "dépendances" and reason == "represented_dependency_not_grounded":
        return (
            'A disposition claims that its source represents a dependency, but no active dependency cites it. Correct the disposition or dependency from the supplied evidence.'
        )
    if stage == "dépendances" and reason == "source_dispositions_unaccounted":
        return (
            'At least one unit to account for is missing from `source_dispositions`. Return exactly one disposition for each, with no addition or duplicate.'
        )
    return 'Correct this violation precisely without relaxing the other rules.'


def _strip_json_fence(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _append_shadow_event(event: dict[str, Any]) -> None:
    try:
        SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SHADOW_LOG.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
    except OSError:
        return


__all__ = [
    "SceneChange",
    "SceneCurrentTopic",
    "SceneCurrentTopicChange",
    "SceneDependency",
    "SceneDependencyChange",
    "SceneDependencyPatch",
    "SceneBootstrapSelection",
    "SceneItem",
    "SceneMemory",
    "ScenePatch",
    "SceneSnapshot",
    "SceneSource",
    "SceneSourceDisposition",
    "SceneTemporalBoundary",
    "build_scene_bootstrap_messages",
    "build_scene_model_messages",
    "build_scene_sources",
    "capture_scene_model_context",
    "current_topics_canary_enabled",
    "ensure_shared_scene_model",
    "invalidate_scene_models",
    "parse_scene_patch",
    "parse_scene_bootstrap_selection",
    "parse_scene_dependency_patch",
    "read_scene_model",
    "render_scene_model_for_voice",
    "render_unrepresented_active_context_for_voice",
    "run_scene_model_revision",
    "schedule_scene_model_revision",
    "select_scene_bootstrap_sources",
    "scene_model_dialogue_delta",
    "scene_invalidation_path",
    "scene_model_mode",
    "scene_model_path",
    "scene_model_shadow_enabled",
    "scene_model_voice_enabled",
    "scene_scope_from_origin",
]
