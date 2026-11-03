"""Renderers for the canonical voice brief and its bounded canaries."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass

from daemon_core.near_term_timeline import render_near_term_timeline
from daemon_core.voice_brief import (
    BriefMessage,
    BriefPromptBlock,
    VoiceBrief,
)

NARRATIVE_RENDERER_SCHEMA = "daemon.voice-brief-render.v1"
AUTHORITY_HIERARCHY_RENDERER_SCHEMA = (
    "daemon.voice-brief-authority-hierarchy-render.v3"
)
AUTHORITY_HIERARCHY_PROMPT_VARIANT = "voice_brief_authority_hierarchy_v3"
AUTHORITY_HIERARCHY_CANARY_ENV = (
    "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY"
)
AUTHORITY_HIERARCHY_CANARY_USER_IDS_ENV = (
    "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_USER_IDS"
)
AUTHORITY_HIERARCHY_CANARY_PLATFORMS_ENV = (
    "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS"
)
TEMPORAL_RENDERER_SCHEMA = "daemon.voice-brief-temporal-render.v1"
TEMPORAL_PROMPT_VARIANT = "voice_brief_temporal_v2"
TEMPORAL_CANARY_ENV = "DAEMON_CORE_TEMPORAL_VOICE_BRIEF"
TEMPORAL_CANARY_USER_IDS_ENV = (
    "DAEMON_CORE_TEMPORAL_VOICE_BRIEF_USER_IDS"
)
TEMPORAL_CANARY_PLATFORMS_ENV = (
    "DAEMON_CORE_TEMPORAL_VOICE_BRIEF_PLATFORMS"
)

_AUTHORITY_BACKGROUND_BLOCKS = frozenset(
    {
        "stable_user_context",
        "authoritative_sources",
    }
)

logger = logging.getLogger(__name__)


def authority_hierarchy_canary_enabled(
    user_id: str,
    platform: str,
) -> bool:
    """Require an explicit flag, user allowlist and platform allowlist."""

    if os.getenv(AUTHORITY_HIERARCHY_CANARY_ENV, "").strip().casefold() not in {
        "1",
        "on",
        "true",
    }:
        return False
    users = {
        value.strip()
        for value in os.getenv(
            AUTHORITY_HIERARCHY_CANARY_USER_IDS_ENV,
            "",
        ).split(",")
        if value.strip()
    }
    platforms = {
        value.strip().casefold()
        for value in os.getenv(
            AUTHORITY_HIERARCHY_CANARY_PLATFORMS_ENV,
            "",
        ).split(",")
        if value.strip()
    }
    return bool(
        user_id
        and user_id in users
        and platform.strip().casefold() in platforms
    )


def temporal_voice_brief_canary_enabled(
    user_id: str,
    platform: str,
) -> bool:
    """Require an explicit flag, user allowlist and platform allowlist."""

    if os.getenv(TEMPORAL_CANARY_ENV, "").strip().casefold() not in {
        "1",
        "on",
        "true",
    }:
        return False
    users = {
        value.strip()
        for value in os.getenv(TEMPORAL_CANARY_USER_IDS_ENV, "").split(",")
        if value.strip()
    }
    platforms = {
        value.strip().casefold()
        for value in os.getenv(TEMPORAL_CANARY_PLATFORMS_ENV, "").split(",")
        if value.strip()
    }
    return bool(
        user_id
        and user_id in users
        and platform.strip().casefold() in platforms
    )


@dataclass(frozen=True)
class BriefBlockCoverage:
    block_id: str
    source_sha256: str
    source_chars: int
    disposition: str
    target_section: str


@dataclass(frozen=True)
class NarrativeBriefRender:
    schema_version: str
    messages: tuple[dict[str, str], ...]
    coverage: tuple[BriefBlockCoverage, ...]
    uncovered_block_ids: tuple[str, ...]
    source_prompt_sha256: str
    rendered_prompt_sha256: str
    source_text_chars: int
    rendered_text_chars: int

    @property
    def complete(self) -> bool:
        return not self.uncovered_block_ids

    @property
    def char_delta(self) -> int:
        return self.rendered_text_chars - self.source_text_chars

    @property
    def char_delta_ratio(self) -> float:
        if not self.source_text_chars:
            return 0.0
        return self.char_delta / self.source_text_chars


@dataclass(frozen=True)
class AuthorityHierarchyRender:
    schema_version: str
    messages: tuple[dict[str, str], ...]
    coverage: tuple[BriefBlockCoverage, ...]
    source_prompt_sha256: str
    rendered_prompt_sha256: str
    source_text_chars: int
    rendered_text_chars: int
    primary_system_chars: int
    developer_background_chars: int

    @property
    def char_delta(self) -> int:
        return self.rendered_text_chars - self.source_text_chars


@dataclass(frozen=True)
class TemporalBriefRender:
    schema_version: str
    messages: tuple[dict[str, str], ...]
    coverage: tuple[BriefBlockCoverage, ...]
    source_prompt_sha256: str
    rendered_prompt_sha256: str
    source_text_chars: int
    rendered_text_chars: int
    timeline_entry_count: int
    authoritative_agenda_representations: int

    @property
    def char_delta(self) -> int:
        return self.rendered_text_chars - self.source_text_chars

    @property
    def char_delta_ratio(self) -> float:
        if not self.source_text_chars:
            return 0.0
        return self.char_delta / self.source_text_chars


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _messages_sha256(messages: tuple[dict[str, str], ...]) -> str:
    return _sha256_text(
        json.dumps(
            messages,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _text_chars(messages: tuple[dict[str, str], ...]) -> int:
    return sum(len(str(message.get("content") or "")) for message in messages)


class _NarrativeBuilder:
    def __init__(self, brief: VoiceBrief):
        self.brief = brief
        self.blocks = {
            block.block_id: block
            for block in (*brief.leading_blocks, *brief.near_turn_blocks)
        }
        self.coverage: list[BriefBlockCoverage] = []
        self.consumed: set[str] = set()

    def consume(
        self,
        placement: str,
        name: str,
        *,
        target: str,
        disposition: str = "transformed",
    ) -> BriefPromptBlock | None:
        block_id = f"{placement}:{name}"
        block = self.blocks.get(block_id)
        if block is None:
            return None
        if block_id in self.consumed:
            raise ValueError(f"voice brief block consumed twice: {block_id}")
        self.consumed.add(block_id)
        self.coverage.append(
            BriefBlockCoverage(
                block_id=block_id,
                source_sha256=block.sha256,
                source_chars=len(block.raw_span),
                disposition=disposition,
                target_section=target,
            )
        )
        return block

    def remaining(self, placement: str) -> tuple[BriefPromptBlock, ...]:
        return tuple(
            block
            for block in self.blocks.values()
            if block.placement == placement and block.block_id not in self.consumed
        )

    def mark_preserved(self, block: BriefPromptBlock, *, target: str) -> None:
        consumed = self.consume(
            block.placement,
            block.name,
            target=target,
            disposition="preserved",
        )
        if consumed is None:
            raise ValueError(f"voice brief block disappeared: {block.block_id}")


def _part(title: str, block: BriefPromptBlock | None) -> str:
    if block is None:
        return ""
    return f"## {title}\n\n{block.body}".strip()


def _section(title: str, parts: list[str]) -> str:
    content = "\n\n".join(part for part in parts if part.strip())
    return f"# {title}\n\n{content}".strip()


def _narrative_leading_system(
    brief: VoiceBrief,
    builder: _NarrativeBuilder,
) -> str:
    sections: list[str] = []
    if brief.leading_prefix:
        sections.append(brief.leading_prefix.rstrip())

    identity = builder.consume(
        "leading_system", "identity", target='I am'
    )
    sections.append(_section('I am', [identity.body if identity else ""]))

    stable = builder.consume(
        "leading_system",
        "stable_user_context",
        target='You and us / Established personal bearings',
    )
    manner = builder.consume(
        "leading_system",
        "conversation_manner",
        target='You and us / Way of being together',
    )
    common_ground = builder.consume(
        "leading_system",
        "common_ground_contract",
        target='You and us / Consultable shared language',
        disposition="moved",
    )
    relationship_parts = [
        _part('Established personal bearings', stable),
        _part('Way of being together', manner),
        _part('Consultable shared language', common_ground),
    ]
    if any(relationship_parts):
        sections.append(_section('You and us', relationship_parts))

    turn_contract = builder.consume(
        "leading_system",
        "turn_contract",
        target='Rules of truth and conduct',
    )
    sections.append(
        _section(
            'Rules of truth and conduct',
            [turn_contract.body if turn_contract else ""],
        )
    )

    temporal = builder.consume(
        "leading_system", "temporal_frame", target='We are'
    )
    sections.append(_section('We are', [temporal.body if temporal else ""]))

    situation = builder.consume(
        "leading_system",
        "threshold_situation",
        target='Here is the situation',
    )
    current_context = builder.consume(
        "leading_system",
        "current_context",
        target='Here is the situation / Retained current bearings',
    )
    sections.append(
        _section(
            'Here is the situation',
            [
                situation.body if situation else "",
                _part('Retained current bearings', current_context),
            ],
        )
    )

    authoritative = builder.consume(
        "leading_system",
        "authoritative_sources",
        target='The coming days and current sources',
    )
    if authoritative:
        sections.append(
            _section(
                'The coming days and current sources',
                [authoritative.body],
            )
        )

    opened = builder.consume(
        "leading_system",
        "open_exact_memories",
        target='Exact evidence already open',
        disposition="moved",
    )
    sections.append(
        _section(
            'Exact evidence already open',
            [opened.body if opened else ""],
        )
    )

    catalog = builder.consume(
        "leading_system",
        "memory_catalog",
        target='What I can consult / Durable memory',
        disposition="moved",
    )
    memory_access = builder.consume(
        "leading_system",
        "memory_access",
        target='What I can consult / Reading exact memory',
        disposition="moved",
    )
    host_tools = builder.consume(
        "leading_system",
        "host_tools_contract",
        target='What I can consult / Host capabilities',
        disposition="moved",
    )
    handoff = builder.consume(
        "leading_system",
        "yield_contract",
        target='What I can consult / Limits of this turn',
        disposition="moved",
    )
    resource_parts = [
        _part('Durable memory', catalog),
        _part('Reading exact memory', memory_access),
        _part('Host capabilities', host_tools),
        _part('Limits of this turn', handoff),
    ]
    sections.append(_section('What I can consult', resource_parts))

    for block in builder.remaining("leading_system"):
        builder.mark_preserved(block, target='Unclassified inherited elements')
        sections.append(block.raw_span.strip())
    return "\n\n".join(section for section in sections if section.strip())


def _narrative_near_turn_system(
    brief: VoiceBrief,
    builder: _NarrativeBuilder,
) -> str:
    parts: list[str] = []
    if brief.near_turn_prefix:
        parts.append(brief.near_turn_prefix.rstrip())
    threshold = builder.consume(
        "near_turn_system",
        "turn_threshold",
        target='At the threshold of this message / Situation to recompose',
    )
    external = builder.consume(
        "near_turn_system",
        "external_knowledge_boundary",
        target='At the threshold of this message / External knowledge boundary',
    )
    connected = builder.consume(
        "near_turn_system",
        "connected_source_status",
        target='At the threshold of this message / Connected source freshness',
    )
    parts.extend(
        [
            _part('Situation to recompose', threshold),
            _part('External knowledge boundary', external),
            _part('Connected source freshness', connected),
        ]
    )
    for block in builder.remaining("near_turn_system"):
        builder.mark_preserved(block, target='At the threshold of this message / Inherited')
        parts.append(block.raw_span.strip())
    return _section('At the threshold of this message', parts)


def render_narrative_voice_brief(brief: VoiceBrief) -> NarrativeBriefRender:
    """Render one replacement prompt and prove where every source block went."""

    builder = _NarrativeBuilder(brief)
    leading = _narrative_leading_system(brief, builder)
    near_turn = _narrative_near_turn_system(brief, builder)
    rendered_messages = (
        BriefMessage("system", leading).as_dict(),
        *(message.as_dict() for message in brief.recent_dialogue),
        BriefMessage("system", near_turn).as_dict(),
        brief.current_message.as_dict(),
    )
    uncovered = tuple(
        sorted(set(builder.blocks) - builder.consumed)
    )
    source_messages = (
        BriefMessage(
            "system",
            brief.leading_prefix
            + "".join(block.raw_span for block in brief.leading_blocks),
        ).as_dict(),
        *(message.as_dict() for message in brief.recent_dialogue),
        BriefMessage(
            "system",
            brief.near_turn_prefix
            + "".join(block.raw_span for block in brief.near_turn_blocks),
        ).as_dict(),
        brief.current_message.as_dict(),
    )
    return NarrativeBriefRender(
        schema_version=NARRATIVE_RENDERER_SCHEMA,
        messages=rendered_messages,
        coverage=tuple(builder.coverage),
        uncovered_block_ids=uncovered,
        source_prompt_sha256=brief.source_prompt_sha256,
        rendered_prompt_sha256=_messages_sha256(rendered_messages),
        source_text_chars=_text_chars(source_messages),
        rendered_text_chars=_text_chars(rendered_messages),
    )


def _source_messages(brief: VoiceBrief) -> tuple[dict[str, str], ...]:
    return (
        BriefMessage(
            "system",
            brief.leading_prefix
            + "".join(block.raw_span for block in brief.leading_blocks),
        ).as_dict(),
        *(message.as_dict() for message in brief.recent_dialogue),
        BriefMessage(
            "system",
            brief.near_turn_prefix
            + "".join(block.raw_span for block in brief.near_turn_blocks),
        ).as_dict(),
        brief.current_message.as_dict(),
    )


def render_authority_hierarchy_voice_brief(
    brief: VoiceBrief,
) -> AuthorityHierarchyRender:
    """Keep the current world primary and expose governed durable background.

    Stable context moves without semantic rewriting.  The raw personal agenda
    is the one exception: it is replaced by the already compiled operational
    timeline, so legacy memory and old closed horizons cannot acquire present
    authority merely by living in ``agenda.md``.
    """

    source_messages = _source_messages(brief)
    timeline = brief.near_term_timeline
    if timeline is not None and not timeline.complete:
        raise ValueError("authority renderer refuses an incomplete agenda projection")
    timeline_section = ""
    if timeline is not None and timeline.sources:
        if timeline.withheld_entries:
            logger.info(
                "Agenda rows withheld from authority | count=%d | refs=%s",
                len(timeline.withheld_entries),
                ",".join(item.sha256[:12] for item in timeline.withheld_entries),
            )
        timeline_section = (
            '## Operational agenda\n'
            + render_near_term_timeline(timeline, include_now=False)
        )
    primary_parts = [brief.leading_prefix]
    background_parts: list[str] = []
    coverage: list[BriefBlockCoverage] = []
    timeline_inserted = False
    for block in brief.leading_blocks:
        if block.name == "authoritative_sources" and timeline_section:
            preamble = _authority_preamble(block)
            authority_body = "\n\n".join(
                part for part in (preamble, timeline_section) if part.strip()
            )
            background_parts.append(_replace_block(block, authority_body))
            timeline_inserted = True
            disposition = "transformed_to_developer_background"
            target = "developer_background:operational_agenda"
        elif block.name in _AUTHORITY_BACKGROUND_BLOCKS:
            background_parts.append(block.raw_span)
            disposition = "moved_to_developer_background"
            target = "developer_background"
        elif (
            block.name == "threshold_situation"
            and timeline_section
            and not brief.scene.available
        ):
            primary_parts.append(
                _replace_block(block, _without_seed_agenda(block.body))
            )
            disposition = "transformed_in_primary_system"
            target = "primary_system:without_duplicate_agenda"
        else:
            primary_parts.append(block.raw_span)
            disposition = "preserved_in_primary_system"
            target = "primary_system"
        coverage.append(
            BriefBlockCoverage(
                block_id=block.block_id,
                source_sha256=block.sha256,
                source_chars=len(block.raw_span),
                disposition=disposition,
                target_section=target,
            )
        )
    if timeline_section and not timeline_inserted:
        # A cold scene seed does not necessarily carry an
        # ``authoritative_sources`` block.  The agenda is nevertheless an
        # independently compiled source and must remain visible while the
        # scene is absent or rebuilding.
        background_parts.append(
            '# Current authoritative durable sources\n\n'
            + timeline_section
            + "\n\n"
        )
    near_turn = brief.near_turn_prefix + "".join(
        block.raw_span for block in brief.near_turn_blocks
    )
    coverage.extend(
        BriefBlockCoverage(
            block_id=block.block_id,
            source_sha256=block.sha256,
            source_chars=len(block.raw_span),
            disposition="preserved_in_near_turn_system",
            target_section="near_turn_system",
        )
        for block in brief.near_turn_blocks
    )
    primary = "".join(primary_parts).rstrip()
    background = "".join(background_parts).rstrip()
    developer_messages = (
        (BriefMessage("developer", background).as_dict(),)
        if background
        else ()
    )
    rendered_messages = (
        BriefMessage("system", primary).as_dict(),
        *developer_messages,
        *(message.as_dict() for message in brief.recent_dialogue),
        BriefMessage("system", near_turn).as_dict(),
        brief.current_message.as_dict(),
    )
    return AuthorityHierarchyRender(
        schema_version=AUTHORITY_HIERARCHY_RENDERER_SCHEMA,
        messages=rendered_messages,
        coverage=tuple(coverage),
        source_prompt_sha256=brief.source_prompt_sha256,
        rendered_prompt_sha256=_messages_sha256(rendered_messages),
        source_text_chars=_text_chars(source_messages),
        rendered_text_chars=_text_chars(rendered_messages),
        primary_system_chars=len(primary),
        developer_background_chars=len(background),
    )


def _replace_block(block: BriefPromptBlock, body: str) -> str:
    suffix = "\n\n" if block.raw_span.endswith("\n\n") else "\n"
    return f"{block.marker}\n\n{body.strip()}{suffix}"


def _without_seed_agenda(value: str) -> str:
    marker = '## Agenda and horizons'
    start = value.find(marker)
    if start < 0:
        return value
    next_section = value.find("\n## ", start + len(marker))
    if next_section < 0:
        return value[:start].rstrip()
    return (value[:start].rstrip() + "\n\n" + value[next_section + 1 :]).strip()


def _authority_preamble(block: BriefPromptBlock | None) -> str:
    if block is None:
        return ""
    agenda_marker = '\n\n## Current agenda'
    if agenda_marker not in block.body:
        return ""
    return block.body.split(agenda_marker, 1)[0].strip()


def render_temporal_voice_brief(brief: VoiceBrief) -> TemporalBriefRender:
    """Replace only the scattered agenda projection with one typed timeline."""

    timeline = brief.near_term_timeline
    source_messages = _source_messages(brief)
    if timeline is None or not timeline.sources:
        coverage = tuple(
            BriefBlockCoverage(
                block_id=block.block_id,
                source_sha256=block.sha256,
                source_chars=len(block.raw_span),
                disposition="preserved",
                target_section=block.marker,
            )
            for block in (*brief.leading_blocks, *brief.near_turn_blocks)
        )
        return TemporalBriefRender(
            schema_version=TEMPORAL_RENDERER_SCHEMA,
            messages=source_messages,
            coverage=coverage,
            source_prompt_sha256=brief.source_prompt_sha256,
            rendered_prompt_sha256=_messages_sha256(source_messages),
            source_text_chars=_text_chars(source_messages),
            rendered_text_chars=_text_chars(source_messages),
            timeline_entry_count=0,
            authoritative_agenda_representations=0,
        )
    if not timeline.complete:
        raise ValueError("temporal renderer refuses an incomplete agenda projection")
    if timeline.withheld_entries:
        logger.info(
            "Agenda rows withheld from temporal authority | count=%d | refs=%s",
            len(timeline.withheld_entries),
            ",".join(item.sha256[:12] for item in timeline.withheld_entries),
        )

    authoritative = brief.block("authoritative_sources")
    if brief.scene.available and authoritative is None:
        raise ValueError(
            "temporal renderer requires the exact agenda authority beside a scene"
        )

    timeline_body = render_near_term_timeline(timeline, include_now=False)
    timeline_section = "## Canonical agenda timeline\n" + timeline_body
    preamble = _authority_preamble(authoritative)
    authority_body = "\n\n".join(
        part for part in (preamble, timeline_section) if part.strip()
    )
    cold_temporal_body = "\n\n".join(
        (brief.temporal_frame.raw, timeline_section)
    )

    leading_parts = [brief.leading_prefix]
    coverage: list[BriefBlockCoverage] = []
    for block in brief.leading_blocks:
        if block.name == "temporal_frame":
            if authoritative is None:
                leading_parts.append(_replace_block(block, cold_temporal_body))
                disposition = "transformed"
                target = "temporal_frame+canonical_agenda_timeline"
            else:
                leading_parts.append(block.raw_span)
                disposition = "preserved"
                target = block.marker
        elif block.name == "authoritative_sources":
            leading_parts.append(_replace_block(block, authority_body))
            disposition = "transformed"
            target = "authoritative_sources+canonical_agenda_timeline"
        elif block.name == "threshold_situation" and not brief.scene.available:
            seed = _without_seed_agenda(block.body)
            leading_parts.append(_replace_block(block, seed))
            disposition = "transformed"
            target = "threshold_situation_without_duplicate_agenda"
        else:
            leading_parts.append(block.raw_span)
            disposition = "preserved"
            target = block.marker
        coverage.append(
            BriefBlockCoverage(
                block_id=block.block_id,
                source_sha256=block.sha256,
                source_chars=len(block.raw_span),
                disposition=disposition,
                target_section=target,
            )
        )

    near_turn = brief.near_turn_prefix + "".join(
        block.raw_span for block in brief.near_turn_blocks
    )
    coverage.extend(
        BriefBlockCoverage(
            block_id=block.block_id,
            source_sha256=block.sha256,
            source_chars=len(block.raw_span),
            disposition="preserved",
            target_section=block.marker,
        )
        for block in brief.near_turn_blocks
    )
    messages = (
        BriefMessage("system", "".join(leading_parts).rstrip()).as_dict(),
        *(message.as_dict() for message in brief.recent_dialogue),
        BriefMessage("system", near_turn).as_dict(),
        brief.current_message.as_dict(),
    )
    return TemporalBriefRender(
        schema_version=TEMPORAL_RENDERER_SCHEMA,
        messages=messages,
        coverage=tuple(coverage),
        source_prompt_sha256=brief.source_prompt_sha256,
        rendered_prompt_sha256=_messages_sha256(messages),
        source_text_chars=_text_chars(source_messages),
        rendered_text_chars=_text_chars(messages),
        timeline_entry_count=len(timeline.entries),
        authoritative_agenda_representations=1,
    )


__all__ = [
    "AUTHORITY_HIERARCHY_CANARY_ENV",
    "AUTHORITY_HIERARCHY_CANARY_PLATFORMS_ENV",
    "AUTHORITY_HIERARCHY_CANARY_USER_IDS_ENV",
    "AUTHORITY_HIERARCHY_PROMPT_VARIANT",
    "AUTHORITY_HIERARCHY_RENDERER_SCHEMA",
    "AuthorityHierarchyRender",
    "BriefBlockCoverage",
    "NARRATIVE_RENDERER_SCHEMA",
    "NarrativeBriefRender",
    "TEMPORAL_CANARY_ENV",
    "TEMPORAL_CANARY_PLATFORMS_ENV",
    "TEMPORAL_CANARY_USER_IDS_ENV",
    "TEMPORAL_PROMPT_VARIANT",
    "TEMPORAL_RENDERER_SCHEMA",
    "TemporalBriefRender",
    "authority_hierarchy_canary_enabled",
    "render_authority_hierarchy_voice_brief",
    "render_narrative_voice_brief",
    "render_temporal_voice_brief",
    "temporal_voice_brief_canary_enabled",
]
