"""Structured, host-neutral interface between the runtime and the voice.

The first implementation is deliberately shadow-only.  It compiles the
canonical :class:`~daemon_core.turn_runtime.TurnPlan` without reading a
source again, and its reference renderer must reproduce every prompt message
exactly.  A later renderer may reorganize those ingredients, but it cannot
silently lose one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, TYPE_CHECKING

from daemon_core.near_term_timeline import (
    NearTermTimeline,
    compile_near_term_timeline,
)


if TYPE_CHECKING:
    from daemon_core.turn_runtime import TurnPlan


VOICE_BRIEF_SCHEMA = "daemon.voice-brief.v5"
TURN_EVIDENCE_SCHEMA = "daemon.turn-evidence.v1"
ATOMICITY_UNVERIFIED = "source_unit_unverified"

_DIALOGUE_HEADER = re.compile(r"(?m)^##\s+(.+?)\s+—\s+(user|assistant)\s*$")


class DialogueCoverageError(ValueError):
    """The canonical prompt cannot prove a continuous dialogue window."""


@dataclass(frozen=True)
class PromptBlockSpec:
    name: str
    markers: tuple[str, ...]
    function: str
    authority: str


LEADING_BLOCK_SPECS = (
    PromptBlockSpec("identity", ("# Identite", '# Identity'), "identity", "persona"),
    PromptBlockSpec(
        "conversation_manner",
        ("# Maniere de converser", '# Conversational manner'),
        "relationship",
        "conversation",
    ),
    PromptBlockSpec(
        "stable_user_context",
        ("# Reperes personnels stables", '# Stable personal reference points'),
        "evidence",
        "user_context",
    ),
    PromptBlockSpec(
        "turn_contract", ('# Turn contract',), "procedure", "runtime_contract"
    ),
    PromptBlockSpec(
        "temporal_frame", ('# Temporal frame',), "situation", "runtime_clock"
    ),
    PromptBlockSpec(
        "threshold_situation",
        ('# Situation at the turn threshold',),
        "situation",
        "scene_model",
    ),
    PromptBlockSpec(
        "current_context",
        ("# Reperes actuels conserves", '# Retained current reference points'),
        "situation",
        "active_context",
    ),
    PromptBlockSpec(
        "authoritative_sources",
        (
            "# Sources durables actuelles faisant autorite",
            '# Current authoritative durable sources',
        ),
        "evidence",
        "connected_source",
    ),
    PromptBlockSpec(
        "open_exact_memories",
        ("# Memoires exactes deja ouvertes", '# Exact memories already open'),
        "evidence",
        "capsules",
    ),
    PromptBlockSpec(
        "memory_catalog",
        ("# Catalogue de la memoire durable", '# Durable memory catalog'),
        "resource",
        "atlas",
    ),
    PromptBlockSpec(
        "memory_access",
        ("# Acces a la memoire exacte", '# Exact memory access'),
        "procedure",
        "memory_tool",
    ),
    PromptBlockSpec(
        "host_tools_contract",
        ('# Host tools', "# Outils de l'hôte"),
        "procedure",
        "host_tools",
    ),
    PromptBlockSpec(
        "yield_contract",
        ("# Limits of this turn (firm rule)", "# Cession au corps"),
        "procedure",
        "host_handoff",
    ),
    PromptBlockSpec(
        "common_ground_contract",
        ('# Common Ground reading threshold',),
        "procedure",
        "common_ground",
    ),
)

NEAR_TURN_BLOCK_SPECS = (
    PromptBlockSpec(
        "turn_threshold", ('# Turn threshold',), "procedure", "runtime_contract"
    ),
    PromptBlockSpec(
        "external_knowledge_boundary",
        (
            "## Frontiere des connaissances externes",
            '## External knowledge boundary',
        ),
        "procedure",
        "host_tools",
    ),
    PromptBlockSpec(
        "connected_source_status",
        (
            "## Etat exact des sources connectees",
            '## Exact state of connected sources',
        ),
        "evidence",
        "connected_source",
    ),
)


@dataclass(frozen=True)
class BriefMessage:
    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class DialogueCoverage:
    status: str
    dialogue: str
    scene_watermark_source_id: str = ""
    expected_turn_ids: tuple[str, ...] = ()
    visible_turn_ids: tuple[str, ...] = ()
    recent_turn_ids: tuple[str, ...] = ()
    scene_delta_turn_ids: tuple[str, ...] = ()
    missing_turn_ids: tuple[str, ...] = ()

    @property
    def exact(self) -> bool:
        return not self.missing_turn_ids and self.status in {
            "recent_only",
            "scene_delta_covered",
        }


@dataclass(frozen=True)
class _DialogueChunk:
    turn_id: str
    timestamp: str
    role: str
    content: str
    raw: str


@dataclass(frozen=True)
class BriefPromptBlock:
    block_id: str
    name: str
    placement: str
    marker: str
    function: str
    authority: str
    raw_span: str
    body: str
    sha256: str


@dataclass(frozen=True)
class BriefSourceRef:
    source_id: str
    kind: str
    locator: str
    sha256: str
    observed_at: str = ""
    excerpt: str = ""
    truncated: bool = False


@dataclass(frozen=True)
class BriefTemporalBoundary:
    at: str
    after: str


@dataclass(frozen=True)
class BriefClaim:
    claim_id: str
    text: str
    epistemic: str
    source_refs: tuple[BriefSourceRef, ...]
    temporal_boundaries: tuple[BriefTemporalBoundary, ...] = ()
    domain: str = "scene"
    atomicity: str = ATOMICITY_UNVERIFIED


@dataclass(frozen=True)
class BriefDependency:
    dependency_id: str
    text: str
    status: str
    horizon: str
    source_refs: tuple[BriefSourceRef, ...]
    temporal_boundaries: tuple[BriefTemporalBoundary, ...] = ()
    atomicity: str = ATOMICITY_UNVERIFIED


@dataclass(frozen=True)
class BriefScene:
    available: bool
    scope: str = ""
    revision: int | None = None
    revised_at: str = ""
    source_trace_id: str = ""
    dialogue_watermark_source_id: str = ""
    dialogue_watermark_locator: str = ""
    reading: str = ""
    conversation: str = ""
    claims: tuple[BriefClaim, ...] = ()
    dependencies: tuple[BriefDependency, ...] = ()
    sources: tuple[BriefSourceRef, ...] = ()
    unresolved_source_ids: tuple[str, ...] = ()
    raw_projection: str = ""


@dataclass(frozen=True)
class BriefIdentity:
    agent_name: str
    persona: str
    raw: str


@dataclass(frozen=True)
class BriefRelationship:
    manner: str
    stable_user_context: str
    shared_language_resources: tuple[str, ...] = ()


@dataclass(frozen=True)
class BriefTemporalFrame:
    raw: str
    now: str = ""
    timezone: str = ""
    weekday: str = ""
    previous_turn_at: str = ""
    elapsed_since_previous_turn: str = ""
    working_state_revised_at: str = ""


@dataclass(frozen=True)
class BriefDocument:
    name: str
    kind: str
    text: str
    sha256: str


@dataclass(frozen=True)
class BriefAtlas:
    text: str
    sha256: str
    souvenir_names: tuple[str, ...] = ()
    common_ground_names: tuple[str, ...] = ()
    expired_souvenir_names: tuple[str, ...] = ()
    rendered_chars: int = 0


@dataclass(frozen=True)
class BriefEvidence:
    current_context_sources: tuple[BriefDocument, ...]
    authoritative_sources: tuple[BriefDocument, ...]
    opened_souvenirs: tuple[BriefDocument, ...]


@dataclass(frozen=True)
class BriefResources:
    atlas: BriefAtlas
    connected_source_status: str
    host_capability_contract: str
    memory_access_contract: str


@dataclass(frozen=True)
class BriefDialogueEvidence:
    turn_id: str
    timestamp: str
    role: str
    content: str
    raw: str
    sha256: str


@dataclass(frozen=True)
class BriefMaterialUnknown:
    unknown_id: str
    kind: str
    text: str
    status: str
    source_refs: tuple[BriefSourceRef, ...] = ()
    temporal_boundaries: tuple[BriefTemporalBoundary, ...] = ()


@dataclass(frozen=True)
class BriefAvailableSource:
    source_id: str
    kind: str
    locator: str
    sha256: str
    state: str


@dataclass(frozen=True)
class BriefCapability:
    capability_id: str
    state: str
    source_block_id: str
    contract: str


@dataclass(frozen=True)
class BriefMaterialFailure:
    failure_id: str
    kind: str
    status: str
    source_ref: str = ""


@dataclass(frozen=True)
class BriefEvidenceCoverage:
    unit_id: str
    source_sha256: str
    source_chars: int
    role: str
    disposition: str
    target: str


@dataclass(frozen=True)
class TurnEvidence:
    schema_version: str
    compiled_at: str
    source_prompt_sha256: str
    temporal_frame: BriefTemporalFrame
    present_claims: tuple[BriefClaim, ...]
    open_dependencies: tuple[BriefDependency, ...]
    material_unknowns: tuple[BriefMaterialUnknown, ...]
    unabsorbed_dialogue: tuple[BriefDialogueEvidence, ...]
    near_term_horizon: NearTermTimeline | None
    opened_evidence: tuple[BriefDocument, ...]
    available_sources: tuple[BriefAvailableSource, ...]
    capability_handles: tuple[BriefCapability, ...]
    material_failures: tuple[BriefMaterialFailure, ...]
    interpretive_reading: str
    conversation_texture: str
    coverage: tuple[BriefEvidenceCoverage, ...]
    source_unit_count: int

    @property
    def coverage_complete(self) -> bool:
        unit_ids = tuple(record.unit_id for record in self.coverage)
        return len(unit_ids) == self.source_unit_count and len(unit_ids) == len(
            set(unit_ids)
        )

    @property
    def renderer_ready(self) -> bool:
        return self.coverage_complete and not self.material_failures


@dataclass(frozen=True)
class VoiceBrief:
    schema_version: str
    compiled_at: str
    source_prompt_sha256: str
    leading_prefix: str
    leading_blocks: tuple[BriefPromptBlock, ...]
    identity: BriefIdentity
    relationship: BriefRelationship
    temporal_frame: BriefTemporalFrame
    near_term_timeline: NearTermTimeline | None
    scene: BriefScene
    evidence: BriefEvidence
    resources: BriefResources
    turn_evidence: TurnEvidence
    dialogue_coverage: DialogueCoverage
    recent_dialogue: tuple[BriefMessage, ...]
    near_turn_prefix: str
    near_turn_blocks: tuple[BriefPromptBlock, ...]
    current_message: BriefMessage

    def block(self, name: str) -> BriefPromptBlock | None:
        for block in (*self.leading_blocks, *self.near_turn_blocks):
            if block.name == name:
                return block
        return None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _messages_sha256(messages: tuple[BriefMessage, ...]) -> str:
    payload = [message.as_dict() for message in messages]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _sha256_text(serialized)


def dialogue_turn_id(timestamp: str, role: str, content: str) -> str:
    """Identity shared by exact L0 coverage and verified source references."""

    return "turn-" + _sha256_text(f"{timestamp}\0{role}\0{content.strip()}")[:16]


def _dialogue_chunks(dialogue: str, *, label: str) -> tuple[_DialogueChunk, ...]:
    matches = list(_DIALOGUE_HEADER.finditer(dialogue or ""))
    chunks: list[_DialogueChunk] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(dialogue)
        content = dialogue[match.end() : end].strip()
        if not content:
            continue
        timestamp = match.group(1).strip()
        role = match.group(2).strip().lower()
        raw = f"## {timestamp} — {role}\n\n{content}"
        chunks.append(
            _DialogueChunk(
                turn_id=dialogue_turn_id(timestamp, role, content),
                timestamp=timestamp,
                role=role,
                content=content,
                raw=raw,
            )
        )
    if dialogue.strip() and not chunks:
        raise DialogueCoverageError(f"{label} dialogue is not structured L0")
    return tuple(chunks)


def _unique_turn_ids(chunks: tuple[_DialogueChunk, ...]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.turn_id not in seen:
            seen.add(chunk.turn_id)
            result.append(chunk.turn_id)
    return tuple(result)


def build_dialogue_coverage(
    *,
    scan_dialogue: str,
    recent_dialogue: str,
    scene_delta: str | None,
    scene_active: bool,
    scene_watermark_source_id: str = "",
) -> DialogueCoverage:
    """Build one ordered L0 window and prove that the scene gap is visible."""

    recent = _dialogue_chunks(recent_dialogue, label="recent")
    recent_ids = _unique_turn_ids(recent)
    if not scene_active:
        return DialogueCoverage(
            status="recent_only",
            dialogue="\n\n".join(chunk.raw for chunk in recent),
            visible_turn_ids=recent_ids,
            recent_turn_ids=recent_ids,
        )
    if scene_delta is None:
        raise DialogueCoverageError("scene dialogue delta is not provable")

    scan = _dialogue_chunks(scan_dialogue, label="scan")
    delta = _dialogue_chunks(scene_delta, label="scene delta")
    delta_ids = _unique_turn_ids(delta)
    required_ids = set((*recent_ids, *delta_ids))
    scan_ids = [chunk.turn_id for chunk in scan]
    absent_from_scan = tuple(
        turn_id for turn_id in required_ids if turn_id not in scan_ids
    )
    if absent_from_scan:
        raise DialogueCoverageError(
            "required dialogue turns are absent from the canonical scan"
        )

    if required_ids:
        first = min(
            index for index, turn_id in enumerate(scan_ids) if turn_id in required_ids
        )
        visible = scan[first:]
    else:
        visible = ()
    visible_ids = _unique_turn_ids(visible)
    missing = tuple(turn_id for turn_id in delta_ids if turn_id not in visible_ids)
    if missing:
        raise DialogueCoverageError("scene dialogue delta is not fully visible")
    return DialogueCoverage(
        status="scene_delta_covered",
        dialogue="\n\n".join(chunk.raw for chunk in visible),
        scene_watermark_source_id=scene_watermark_source_id,
        expected_turn_ids=delta_ids,
        visible_turn_ids=visible_ids,
        recent_turn_ids=recent_ids,
        scene_delta_turn_ids=delta_ids,
        missing_turn_ids=missing,
    )


def _find_marker(text: str, spec: PromptBlockSpec) -> tuple[int, str] | None:
    matches: list[tuple[int, str]] = []
    for marker in spec.markers:
        match = re.search(rf"(?m)^{re.escape(marker)}(?:\n|$)", text)
        if match:
            matches.append((match.start(), marker))
    return min(matches) if matches else None


def _split_prompt_blocks(
    text: str,
    specs: tuple[PromptBlockSpec, ...],
    *,
    placement: str,
) -> tuple[str, tuple[BriefPromptBlock, ...]]:
    found: list[tuple[int, PromptBlockSpec, str]] = []
    for spec in specs:
        located = _find_marker(text, spec)
        if located is not None:
            start, marker = located
            found.append((start, spec, marker))
    found.sort(key=lambda item: item[0])
    if not found:
        return text, ()

    prefix = text[: found[0][0]]
    blocks: list[BriefPromptBlock] = []
    for index, (start, spec, marker) in enumerate(found):
        end = found[index + 1][0] if index + 1 < len(found) else len(text)
        raw_span = text[start:end]
        body = raw_span[len(marker) :].strip()
        blocks.append(
            BriefPromptBlock(
                block_id=f"{placement}:{spec.name}",
                name=spec.name,
                placement=placement,
                marker=marker,
                function=spec.function,
                authority=spec.authority,
                raw_span=raw_span,
                body=body,
                sha256=_sha256_text(raw_span),
            )
        )
    return prefix, tuple(blocks)


def _required_block(
    blocks: tuple[BriefPromptBlock, ...], name: str
) -> BriefPromptBlock:
    for block in blocks:
        if block.name == name:
            return block
    raise ValueError(f"canonical voice brief requires block: {name}")


def _optional_block_body(blocks: tuple[BriefPromptBlock, ...], name: str) -> str:
    for block in blocks:
        if block.name == name:
            return block.body
    return ""


def _compile_identity(raw: str) -> BriefIdentity:
    first, separator, remainder = raw.partition("\n\n")
    match = re.fullmatch('You are (.+)\\.', first.strip())
    if not match:
        return BriefIdentity(agent_name="", persona=raw, raw=raw)
    agent_name = match.group(1).strip()
    for suffix in (
        " (refer to yourself using feminine grammatical agreement)",
        " (refer to yourself using masculine grammatical agreement)",
    ):
        if agent_name.endswith(suffix):
            agent_name = agent_name[: -len(suffix)]
            break
    return BriefIdentity(
        agent_name=agent_name,
        persona=remainder.strip() if separator else "",
        raw=raw,
    )


_CLOCK_RE = re.compile(
    r"^(?P<instant>\S+) \(weekday=(?P<weekday>[^,]+), "
    r"timezone=(?P<timezone>[^)]+)\)$"
)


def _clock_parts(value: str) -> tuple[str, str, str]:
    match = _CLOCK_RE.match(value.strip())
    if not match:
        return value.strip(), "", ""
    return (
        match.group("instant"),
        match.group("weekday"),
        match.group("timezone"),
    )


def _compile_temporal_frame(raw: str) -> BriefTemporalFrame:
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if line.startswith('Now : '):
            fields["now"] = line.removeprefix('Now : ')
        elif line.startswith('Last completed turn') and " : " in line:
            fields["previous"] = line.split(" : ", 1)[1]
        elif line.startswith('Time elapsed since that turn : '):
            fields["elapsed"] = line.removeprefix('Time elapsed since that turn : ')
        elif line.startswith('Working snapshot revised : '):
            fields["revised"] = line.removeprefix('Working snapshot revised : ')
    now, weekday, timezone = _clock_parts(fields.get("now", ""))
    previous, _previous_weekday, _previous_timezone = _clock_parts(
        fields.get("previous", "")
    )
    revised, _revised_weekday, _revised_timezone = _clock_parts(
        fields.get("revised", "")
    )
    return BriefTemporalFrame(
        raw=raw,
        now=now,
        timezone=timezone,
        weekday=weekday,
        previous_turn_at=previous,
        elapsed_since_previous_turn=fields.get("elapsed", ""),
        working_state_revised_at=revised,
    )


def _compile_near_term_timeline(
    context: Any,
    temporal_frame: BriefTemporalFrame,
) -> NearTermTimeline | None:
    personal = str(getattr(context, "personal_agenda", "") or "")
    professional = str(getattr(context, "professional_agenda", "") or "")
    if not personal.strip() and not professional.strip():
        return None
    try:
        now = datetime.fromisoformat(temporal_frame.now)
    except ValueError as exc:
        raise ValueError(
            "canonical near-term timeline requires an ISO turn clock"
        ) from exc
    timeline = compile_near_term_timeline(
        now=now,
        personal_agenda=personal,
        professional_agenda=professional,
    )
    if not timeline.complete:
        raise ValueError("canonical near-term timeline lost an agenda entry")
    return timeline


def _iso_timestamp(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def _compile_scene(scene_model: Any, raw_projection: str) -> BriefScene:
    if scene_model is None:
        return BriefScene(available=False, raw_projection=raw_projection)

    observed_at = _iso_timestamp(getattr(scene_model, "revised_at", None))
    source_refs = tuple(
        BriefSourceRef(
            source_id=str(getattr(source, "source_id", "")),
            kind=str(getattr(source, "kind", "")),
            locator=str(getattr(source, "locator", "")),
            sha256=str(getattr(source, "sha256", "")),
            observed_at=observed_at,
            excerpt=str(getattr(source, "excerpt", "")),
            truncated=bool(getattr(source, "truncated", False)),
        )
        for source in (getattr(scene_model, "sources", ()) or ())
    )
    sources_by_id = {source.source_id: source for source in source_refs}
    unresolved: set[str] = set()

    def resolve(source_ids: Any) -> tuple[BriefSourceRef, ...]:
        resolved = []
        for source_id in source_ids or ():
            source_id = str(source_id)
            source = sources_by_id.get(source_id)
            if source is None:
                unresolved.add(source_id)
                source = BriefSourceRef(
                    source_id=source_id,
                    kind="unresolved",
                    locator="",
                    sha256="",
                    observed_at=observed_at,
                )
            resolved.append(source)
        return tuple(resolved)

    def boundaries(value: Any) -> tuple[BriefTemporalBoundary, ...]:
        return tuple(
            BriefTemporalBoundary(
                at=str(getattr(boundary, "at", "")),
                after=str(getattr(boundary, "after", "")),
            )
            for boundary in (value or ())
        )

    claims = tuple(
        BriefClaim(
            claim_id=str(getattr(item, "item_id", "")),
            text=str(getattr(item, "text", "")),
            epistemic=str(getattr(item, "epistemic", "")),
            source_refs=resolve(getattr(item, "sources", ())),
            temporal_boundaries=boundaries(getattr(item, "temporal_boundaries", ())),
        )
        for item in (getattr(scene_model, "items", ()) or ())
    )
    dependencies = tuple(
        BriefDependency(
            dependency_id=str(getattr(dependency, "dependency_id", "")),
            text=str(getattr(dependency, "text", "")),
            status=str(getattr(dependency, "status", "")),
            horizon=str(getattr(dependency, "horizon", "")),
            source_refs=resolve(getattr(dependency, "sources", ())),
            temporal_boundaries=boundaries(
                getattr(dependency, "temporal_boundaries", ())
            ),
        )
        for dependency in (getattr(scene_model, "dependencies", ()) or ())
    )
    return BriefScene(
        available=True,
        scope=str(getattr(scene_model, "scene_scope", "")),
        revision=getattr(scene_model, "revision", None),
        revised_at=observed_at,
        source_trace_id=str(getattr(scene_model, "source_trace_id", "")),
        dialogue_watermark_source_id=str(
            getattr(scene_model, "dialogue_watermark_source_id", "")
        ),
        dialogue_watermark_locator=str(
            getattr(scene_model, "dialogue_watermark_locator", "")
        ),
        reading=str(getattr(scene_model, "reading", "")),
        conversation=str(getattr(scene_model, "conversation", "")),
        claims=claims,
        dependencies=dependencies,
        sources=source_refs,
        unresolved_source_ids=tuple(sorted(unresolved)),
        raw_projection=raw_projection,
    )


def _compile_opened_souvenirs(
    voice_context: Any,
    fallback_text: str,
) -> tuple[BriefDocument, ...]:
    hot_memory = getattr(voice_context, "hot_memory", None)
    souvenirs = getattr(hot_memory, "souvenirs", None)
    if souvenirs is not None:
        return tuple(
            BriefDocument(
                name=str(name),
                kind="capsule",
                text=str(text),
                sha256=_sha256_text(str(text)),
            )
            for name, text in souvenirs
        )
    if not fallback_text:
        return ()
    return (
        BriefDocument(
            name="combined_open_memory",
            kind="capsule_projection",
            text=fallback_text,
            sha256=_sha256_text(fallback_text),
        ),
    )


def _compile_atlas(voice_context: Any, fallback_text: str) -> BriefAtlas:
    atlas = getattr(voice_context, "atlas", None)
    if atlas is None:
        return BriefAtlas(
            text=fallback_text,
            sha256=_sha256_text(fallback_text),
            rendered_chars=len(fallback_text),
        )
    text = str(getattr(atlas, "text", fallback_text))
    return BriefAtlas(
        text=text,
        sha256=str(getattr(atlas, "sha256", "")) or _sha256_text(text),
        souvenir_names=tuple(getattr(atlas, "souvenir_names", ()) or ()),
        common_ground_names=tuple(getattr(atlas, "common_ground_names", ()) or ()),
        expired_souvenir_names=tuple(getattr(atlas, "expired_souvenir_names", ()) or ()),
        rendered_chars=int(getattr(atlas, "rendered_chars", len(text))),
    )


_PROJECTED_EVIDENCE_TARGETS = {
    "temporal_frame": "temporal_frame",
    "threshold_situation": (
        "present_claims,open_dependencies,material_unknowns,"
        "interpretive_reading,conversation_texture"
    ),
    "current_context": "present_claims,opened_evidence",
    "authoritative_sources": "near_term_horizon,opened_evidence",
    "open_exact_memories": "opened_evidence",
    "memory_catalog": "available_sources",
    "memory_access": "capability_handles",
    "host_tools_contract": "capability_handles",
    "yield_contract": "capability_handles",
    "connected_source_status": "available_sources",
}


def _compile_unabsorbed_dialogue(
    coverage: DialogueCoverage,
) -> tuple[BriefDialogueEvidence, ...]:
    chunks = _dialogue_chunks(coverage.dialogue, label="coverage")
    if coverage.status == "scene_delta_covered":
        target_ids = set(coverage.scene_delta_turn_ids)
    elif coverage.status == "recent_only":
        target_ids = set(coverage.visible_turn_ids)
    else:
        return ()
    result = tuple(
        BriefDialogueEvidence(
            turn_id=chunk.turn_id,
            timestamp=chunk.timestamp,
            role=chunk.role,
            content=chunk.content,
            raw=chunk.raw,
            sha256=_sha256_text(chunk.raw),
        )
        for chunk in chunks
        if chunk.turn_id in target_ids
    )
    if {turn.turn_id for turn in result} != target_ids:
        raise DialogueCoverageError(
            "turn evidence cannot preserve every unabsorbed dialogue turn"
        )
    return result


def _compile_available_sources(
    *,
    resources: BriefResources,
    near_turn_blocks: tuple[BriefPromptBlock, ...],
) -> tuple[BriefAvailableSource, ...]:
    result = [
        BriefAvailableSource(
            source_id="memory_catalog",
            kind="memory_catalog",
            locator="leading_system:memory_catalog",
            sha256=resources.atlas.sha256,
            state="catalogued",
        )
    ]
    connected = next(
        (
            block
            for block in near_turn_blocks
            if block.name == "connected_source_status"
        ),
        None,
    )
    if connected is not None:
        result.append(
            BriefAvailableSource(
                source_id="connected_source_status",
                kind="connected_source_status",
                locator=connected.block_id,
                sha256=connected.sha256,
                state="described",
            )
        )
    return tuple(result)


def _compile_capabilities(
    leading_blocks: tuple[BriefPromptBlock, ...],
) -> tuple[BriefCapability, ...]:
    result = []
    for block_name, capability_id, state in (
        ("memory_access", "exact_memory", "declared"),
        ("host_tools_contract", "host_tools", "declared"),
        ("yield_contract", "host_handoff", "delegated"),
    ):
        block = next(
            (item for item in leading_blocks if item.name == block_name),
            None,
        )
        if block is not None:
            result.append(
                BriefCapability(
                    capability_id=capability_id,
                    state=state,
                    source_block_id=block.block_id,
                    contract=block.body,
                )
            )
    return tuple(result)


def _message_coverage_record(
    *,
    unit_id: str,
    message: BriefMessage,
    disposition: str,
    target: str,
) -> BriefEvidenceCoverage:
    identity = f"{message.role}\0{message.content}"
    return BriefEvidenceCoverage(
        unit_id=unit_id,
        source_sha256=_sha256_text(identity),
        source_chars=len(message.content),
        role=message.role,
        disposition=disposition,
        target=target,
    )


def _compile_evidence_coverage(
    *,
    leading_prefix: str,
    leading_blocks: tuple[BriefPromptBlock, ...],
    dialogue: tuple[BriefMessage, ...],
    near_turn_prefix: str,
    near_turn_blocks: tuple[BriefPromptBlock, ...],
    current: BriefMessage,
    unabsorbed_dialogue: tuple[BriefDialogueEvidence, ...],
) -> tuple[BriefEvidenceCoverage, ...]:
    records: list[BriefEvidenceCoverage] = []
    if leading_prefix:
        records.append(
            BriefEvidenceCoverage(
                unit_id="leading_system:prefix",
                source_sha256=_sha256_text(leading_prefix),
                source_chars=len(leading_prefix),
                role="system",
                disposition="retained",
                target="voice_brief.leading_prefix",
            )
        )
    for block in leading_blocks:
        target = _PROJECTED_EVIDENCE_TARGETS.get(block.name)
        records.append(
            BriefEvidenceCoverage(
                unit_id=block.block_id,
                source_sha256=block.sha256,
                source_chars=len(block.raw_span),
                role="system",
                disposition="projected" if target else "retained",
                target=target or f"voice_brief.{block.name}",
            )
        )
    records.extend(
        _message_coverage_record(
            unit_id=f"dialogue:{index}",
            message=message,
            disposition="retained",
            target="voice_brief.recent_dialogue",
        )
        for index, message in enumerate(dialogue)
    )
    if near_turn_prefix:
        records.append(
            BriefEvidenceCoverage(
                unit_id="near_turn_system:prefix",
                source_sha256=_sha256_text(near_turn_prefix),
                source_chars=len(near_turn_prefix),
                role="system",
                disposition="retained",
                target="voice_brief.near_turn_prefix",
            )
        )
    for block in near_turn_blocks:
        target = _PROJECTED_EVIDENCE_TARGETS.get(block.name)
        records.append(
            BriefEvidenceCoverage(
                unit_id=block.block_id,
                source_sha256=block.sha256,
                source_chars=len(block.raw_span),
                role="system",
                disposition="projected" if target else "retained",
                target=target or f"voice_brief.{block.name}",
            )
        )
    records.append(
        _message_coverage_record(
            unit_id="current_message",
            message=current,
            disposition="retained",
            target="voice_brief.current_message",
        )
    )
    records.extend(
        BriefEvidenceCoverage(
            unit_id=f"dialogue_coverage:{turn.turn_id}",
            source_sha256=turn.sha256,
            source_chars=len(turn.raw),
            role=turn.role,
            disposition="projected",
            target="unabsorbed_dialogue",
        )
        for turn in unabsorbed_dialogue
    )
    return tuple(records)


def _expected_evidence_unit_ids(
    *,
    leading_prefix: str,
    leading_blocks: tuple[BriefPromptBlock, ...],
    dialogue: tuple[BriefMessage, ...],
    near_turn_prefix: str,
    near_turn_blocks: tuple[BriefPromptBlock, ...],
    unabsorbed_dialogue: tuple[BriefDialogueEvidence, ...],
) -> tuple[str, ...]:
    result = []
    if leading_prefix:
        result.append("leading_system:prefix")
    result.extend(block.block_id for block in leading_blocks)
    result.extend(f"dialogue:{index}" for index in range(len(dialogue)))
    if near_turn_prefix:
        result.append("near_turn_system:prefix")
    result.extend(block.block_id for block in near_turn_blocks)
    result.append("current_message")
    result.extend(
        f"dialogue_coverage:{turn.turn_id}" for turn in unabsorbed_dialogue
    )
    return tuple(result)


def _latest_crossed_boundary(
    boundaries: tuple[BriefTemporalBoundary, ...],
    now: datetime,
) -> tuple[BriefTemporalBoundary | None, bool]:
    parsed: list[tuple[datetime, BriefTemporalBoundary]] = []
    for boundary in boundaries:
        try:
            instant = datetime.fromisoformat(boundary.at)
        except ValueError:
            return None, False
        if instant.tzinfo is None or instant.utcoffset() is None or not boundary.after:
            return None, False
        parsed.append((instant, boundary))
    crossed = [pair for pair in parsed if pair[0] <= now]
    return (max(crossed, key=lambda pair: pair[0])[1] if crossed else None), True


def _project_scene_for_turn(
    scene: BriefScene,
    temporal_frame: BriefTemporalFrame,
) -> tuple[
    tuple[BriefClaim, ...],
    tuple[BriefDependency, ...],
    tuple[BriefMaterialUnknown, ...],
    bool,
    tuple[BriefMaterialFailure, ...],
]:
    try:
        now = datetime.fromisoformat(temporal_frame.now)
    except ValueError:
        now = None
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        now = None

    present: list[BriefClaim] = []
    dependencies: list[BriefDependency] = []
    unknowns: list[BriefMaterialUnknown] = []
    failures: list[BriefMaterialFailure] = []
    boundary_crossed = False

    for claim in scene.claims:
        crossed = None
        valid = True
        if claim.temporal_boundaries:
            if now is None:
                valid = False
            else:
                crossed, valid = _latest_crossed_boundary(
                    claim.temporal_boundaries,
                    now,
                )
        if not valid:
            failures.append(
                BriefMaterialFailure(
                    failure_id=f"claim_boundary_unparsed:{claim.claim_id}",
                    kind="temporal_boundary",
                    status="unparsed",
                    source_ref=claim.claim_id,
                )
            )
        if crossed is not None:
            boundary_crossed = True
            unknowns.append(
                BriefMaterialUnknown(
                    unknown_id=claim.claim_id,
                    kind="scene_claim_after_boundary",
                    text=crossed.after,
                    status="uncertain",
                    source_refs=claim.source_refs,
                    temporal_boundaries=claim.temporal_boundaries,
                )
            )
        elif claim.epistemic == "established":
            present.append(claim)
        else:
            unknowns.append(
                BriefMaterialUnknown(
                    unknown_id=claim.claim_id,
                    kind="scene_claim",
                    text=claim.text,
                    status=claim.epistemic,
                    source_refs=claim.source_refs,
                    temporal_boundaries=claim.temporal_boundaries,
                )
            )

    for dependency in scene.dependencies:
        crossed = None
        valid = True
        if dependency.temporal_boundaries:
            if now is None:
                valid = False
            else:
                crossed, valid = _latest_crossed_boundary(
                    dependency.temporal_boundaries,
                    now,
                )
        if not valid:
            failures.append(
                BriefMaterialFailure(
                    failure_id=(
                        "dependency_boundary_unparsed:"
                        f"{dependency.dependency_id}"
                    ),
                    kind="temporal_boundary",
                    status="unparsed",
                    source_ref=dependency.dependency_id,
                )
            )
        if crossed is not None:
            boundary_crossed = True
            dependencies.append(
                replace(
                    dependency,
                    text=crossed.after,
                    status="uncertain",
                )
            )
        else:
            dependencies.append(dependency)
    return (
        tuple(present),
        tuple(dependencies),
        tuple(unknowns),
        boundary_crossed,
        tuple(failures),
    )


def _compile_turn_evidence(
    *,
    compiled_at: str,
    source_prompt_sha256: str,
    leading_prefix: str,
    leading_blocks: tuple[BriefPromptBlock, ...],
    temporal_frame: BriefTemporalFrame,
    near_term_timeline: NearTermTimeline | None,
    scene: BriefScene,
    evidence: BriefEvidence,
    resources: BriefResources,
    dialogue_coverage: DialogueCoverage,
    dialogue: tuple[BriefMessage, ...],
    near_turn_prefix: str,
    near_turn_blocks: tuple[BriefPromptBlock, ...],
    current: BriefMessage,
) -> TurnEvidence:
    (
        present_claims,
        open_dependencies,
        material_unknowns,
        boundary_crossed,
        temporal_failures,
    ) = _project_scene_for_turn(
        scene,
        temporal_frame,
    )
    unabsorbed = _compile_unabsorbed_dialogue(dialogue_coverage)
    failures = list(temporal_failures)
    failures.extend(
        BriefMaterialFailure(
            failure_id=f"unresolved_scene_source:{source_id}",
            kind="scene_source",
            status="unresolved",
            source_ref=source_id,
        )
        for source_id in scene.unresolved_source_ids
    )
    if dialogue_coverage.status == "unverified":
        failures.append(
            BriefMaterialFailure(
                failure_id="dialogue_coverage_unverified",
                kind="dialogue_coverage",
                status="unverified",
            )
        )
    if not temporal_frame.now:
        failures.append(
            BriefMaterialFailure(
                failure_id="turn_clock_unparsed",
                kind="temporal_frame",
                status="unparsed",
            )
        )
    coverage = _compile_evidence_coverage(
        leading_prefix=leading_prefix,
        leading_blocks=leading_blocks,
        dialogue=dialogue,
        near_turn_prefix=near_turn_prefix,
        near_turn_blocks=near_turn_blocks,
        current=current,
        unabsorbed_dialogue=unabsorbed,
    )
    expected_unit_ids = _expected_evidence_unit_ids(
        leading_prefix=leading_prefix,
        leading_blocks=leading_blocks,
        dialogue=dialogue,
        near_turn_prefix=near_turn_prefix,
        near_turn_blocks=near_turn_blocks,
        unabsorbed_dialogue=unabsorbed,
    )
    covered_unit_ids = tuple(record.unit_id for record in coverage)
    if (
        len(covered_unit_ids) != len(set(covered_unit_ids))
        or set(covered_unit_ids) != set(expected_unit_ids)
    ):
        raise ValueError("turn evidence source coverage is incomplete")
    turn_evidence = TurnEvidence(
        schema_version=TURN_EVIDENCE_SCHEMA,
        compiled_at=compiled_at,
        source_prompt_sha256=source_prompt_sha256,
        temporal_frame=temporal_frame,
        present_claims=present_claims,
        open_dependencies=open_dependencies,
        material_unknowns=material_unknowns,
        unabsorbed_dialogue=unabsorbed,
        near_term_horizon=near_term_timeline,
        opened_evidence=(
            *evidence.current_context_sources,
            *evidence.authoritative_sources,
            *evidence.opened_souvenirs,
        ),
        available_sources=_compile_available_sources(
            resources=resources,
            near_turn_blocks=near_turn_blocks,
        ),
        capability_handles=_compile_capabilities(leading_blocks),
        material_failures=tuple(failures),
        interpretive_reading="" if boundary_crossed else scene.reading,
        conversation_texture="" if boundary_crossed else scene.conversation,
        coverage=coverage,
        source_unit_count=len(expected_unit_ids),
    )
    if not turn_evidence.coverage_complete:
        raise ValueError("turn evidence source coverage is incomplete")
    return turn_evidence


def _canonical_shape(
    plan: TurnPlan,
) -> tuple[
    BriefMessage,
    tuple[BriefMessage, ...],
    BriefMessage,
    BriefMessage,
]:
    messages = tuple(
        BriefMessage(
            role=str(message.get("role") or ""),
            content=str(message.get("content") or ""),
        )
        for message in plan.messages
    )
    if len(messages) < 3 or messages[0].role != "system":
        raise ValueError("canonical voice brief requires a leading system message")
    if messages[-1].role != "user":
        raise ValueError("canonical voice brief requires a final user message")
    if messages[-2].role != "system":
        raise ValueError("canonical voice brief requires a near-turn system message")
    dialogue = messages[1:-2]
    if any(message.role not in {"user", "assistant"} for message in dialogue):
        raise ValueError(
            "canonical voice brief dialogue accepts user/assistant roles only"
        )
    return messages[0], dialogue, messages[-2], messages[-1]


def compile_voice_brief(plan: TurnPlan) -> VoiceBrief:
    """Compile one canonical turn plan without rereading or judging a source."""

    leading, dialogue, near_turn, current = _canonical_shape(plan)
    leading_prefix, leading_blocks = _split_prompt_blocks(
        leading.content,
        LEADING_BLOCK_SPECS,
        placement="leading_system",
    )
    near_turn_prefix, near_turn_blocks = _split_prompt_blocks(
        near_turn.content,
        NEAR_TURN_BLOCK_SPECS,
        placement="near_turn_system",
    )
    for required in (
        "identity",
        "turn_contract",
        "temporal_frame",
        "threshold_situation",
        "open_exact_memories",
        "memory_catalog",
    ):
        _required_block(leading_blocks, required)
    if not any(
        block.name in {"host_tools_contract", "yield_contract"}
        for block in leading_blocks
    ):
        raise ValueError(
            "canonical voice brief requires a host capability or handoff block"
        )
    for required in ("turn_threshold", "external_knowledge_boundary"):
        _required_block(near_turn_blocks, required)

    identity = _compile_identity(_required_block(leading_blocks, "identity").body)
    temporal = _compile_temporal_frame(
        _required_block(leading_blocks, "temporal_frame").body
    )
    scene_projection = _required_block(leading_blocks, "threshold_situation").body
    current_context_text = _optional_block_body(leading_blocks, "current_context")
    authoritative_text = _optional_block_body(leading_blocks, "authoritative_sources")
    open_memory_text = _required_block(leading_blocks, "open_exact_memories").body
    atlas_text = _required_block(leading_blocks, "memory_catalog").body
    context = plan.voice_context
    dialogue_coverage = getattr(context, "dialogue_coverage", None)
    if not isinstance(dialogue_coverage, DialogueCoverage):
        dialogue_coverage = DialogueCoverage(
            status="unverified",
            dialogue="",
        )
    if dialogue_coverage.missing_turn_ids:
        raise DialogueCoverageError(
            "voice brief received an incomplete dialogue coverage record"
        )
    atlas = _compile_atlas(context, atlas_text)
    all_messages = (leading, *dialogue, near_turn, current)
    compiled_at = ""
    if plan.request.message_received_at is not None:
        compiled_at = _iso_timestamp(plan.request.message_received_at)
    source_prompt_sha256 = _messages_sha256(all_messages)
    relationship = BriefRelationship(
        manner=_optional_block_body(leading_blocks, "conversation_manner"),
        stable_user_context=_optional_block_body(leading_blocks, "stable_user_context"),
        shared_language_resources=atlas.common_ground_names,
    )
    near_term_timeline = _compile_near_term_timeline(context, temporal)
    scene = _compile_scene(
        getattr(context, "scene_model", None),
        scene_projection,
    )
    evidence = BriefEvidence(
        current_context_sources=(
            (
                BriefDocument(
                    name="active_context",
                    kind="active_context_projection",
                    text=current_context_text,
                    sha256=_sha256_text(current_context_text),
                ),
            )
            if current_context_text
            else ()
        ),
        authoritative_sources=(
            (
                BriefDocument(
                    name="authoritative_context",
                    kind="connected_source",
                    text=authoritative_text,
                    sha256=_sha256_text(authoritative_text),
                ),
            )
            if authoritative_text
            else ()
        ),
        opened_souvenirs=_compile_opened_souvenirs(
            context,
            open_memory_text,
        ),
    )
    resources = BriefResources(
        atlas=atlas,
        connected_source_status=_optional_block_body(
            near_turn_blocks, "connected_source_status"
        ),
        host_capability_contract=(
            _optional_block_body(leading_blocks, "host_tools_contract")
            or _optional_block_body(leading_blocks, "yield_contract")
        ),
        memory_access_contract=_optional_block_body(leading_blocks, "memory_access"),
    )
    turn_evidence = _compile_turn_evidence(
        compiled_at=compiled_at,
        source_prompt_sha256=source_prompt_sha256,
        leading_prefix=leading_prefix,
        leading_blocks=leading_blocks,
        temporal_frame=temporal,
        near_term_timeline=near_term_timeline,
        scene=scene,
        evidence=evidence,
        resources=resources,
        dialogue_coverage=dialogue_coverage,
        dialogue=dialogue,
        near_turn_prefix=near_turn_prefix,
        near_turn_blocks=near_turn_blocks,
        current=current,
    )

    return VoiceBrief(
        schema_version=VOICE_BRIEF_SCHEMA,
        compiled_at=compiled_at,
        source_prompt_sha256=source_prompt_sha256,
        leading_prefix=leading_prefix,
        leading_blocks=leading_blocks,
        identity=identity,
        relationship=relationship,
        temporal_frame=temporal,
        near_term_timeline=near_term_timeline,
        scene=scene,
        evidence=evidence,
        resources=resources,
        turn_evidence=turn_evidence,
        dialogue_coverage=dialogue_coverage,
        recent_dialogue=dialogue,
        near_turn_prefix=near_turn_prefix,
        near_turn_blocks=near_turn_blocks,
        current_message=current,
    )


def render_reference_messages(brief: VoiceBrief) -> tuple[dict[str, str], ...]:
    """Rebuild the source prompt exactly; this is the compiler's loss barrier."""

    leading = brief.leading_prefix + "".join(
        block.raw_span for block in brief.leading_blocks
    )
    near_turn = brief.near_turn_prefix + "".join(
        block.raw_span for block in brief.near_turn_blocks
    )
    messages = (
        BriefMessage("system", leading),
        *brief.recent_dialogue,
        BriefMessage("system", near_turn),
        brief.current_message,
    )
    if _messages_sha256(messages) != brief.source_prompt_sha256:
        raise ValueError("voice brief reference rendering changed the source prompt")
    return tuple(message.as_dict() for message in messages)


__all__ = [
    "ATOMICITY_UNVERIFIED",
    "BriefAtlas",
    "BriefAvailableSource",
    "BriefCapability",
    "BriefClaim",
    "BriefDependency",
    "BriefDialogueEvidence",
    "DialogueCoverage",
    "DialogueCoverageError",
    "BriefDocument",
    "BriefEvidence",
    "BriefEvidenceCoverage",
    "BriefIdentity",
    "BriefMaterialFailure",
    "BriefMaterialUnknown",
    "BriefMessage",
    "BriefPromptBlock",
    "BriefRelationship",
    "BriefResources",
    "BriefScene",
    "BriefSourceRef",
    "BriefTemporalBoundary",
    "BriefTemporalFrame",
    "LEADING_BLOCK_SPECS",
    "NEAR_TURN_BLOCK_SPECS",
    "TURN_EVIDENCE_SCHEMA",
    "VOICE_BRIEF_SCHEMA",
    "TurnEvidence",
    "VoiceBrief",
    "build_dialogue_coverage",
    "compile_voice_brief",
    "render_reference_messages",
]
