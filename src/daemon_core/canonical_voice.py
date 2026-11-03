"""Canonical host-neutral composition for the daemon's sovereign voice.

This module owns the promoted runtime path: one thin voice, a current scene or
bounded seed, exact hot memories, and a consultable structural Atlas. Historical
feature flags remain at compatibility boundaries; they do not select the
composition used by :func:`prepare_turn`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from daemon_core import held_atlas_sovereign as memory_library
from daemon_core.common_ground_library import COMMON_GROUND_CONSUMPTION_CONTRACT


logger = logging.getLogger(__name__)
CANONICAL_HISTORY_TURNS = 24
CANONICAL_PRIOR_DIALOGUE_CHARS = 6000
CANONICAL_PRIOR_DIALOGUE_FILES = 2
SEMANTIC_ATLAS_DIALOGUE_TURNS = 4


@dataclass(frozen=True)
class CanonicalVoiceContext:
    atlas: memory_library.ExactMemoryAtlas
    hot_memory: memory_library.ExactSouvenirRead
    scene_scope: str
    personal_agenda: str = ""
    professional_agenda: str = ""
    active_context: str = ""
    prior_dialogue: str = ""
    scene_model: object | None = None
    scene_delta: str = ""
    episodic_scene_path: str = ""
    dialogue_coverage: object | None = None
    prompt_variant: str = "voice_brief_reference"
    situation_source: str = "seed"

    @property
    def held_thought(self):
        """Compatibility surface for the retired thin Held Thought field."""

        return None


def compose_canonical_voice_messages(
    user_id: str,
    message: str,
    *,
    platform: str = "telegram",
    for_delegation: bool = True,
    message_received_at: float | None = None,
) -> tuple[list[dict], CanonicalVoiceContext]:
    """Compose one canonical turn without routing or pre-response judgment."""

    personal_agenda = memory_library.load_user_agenda(user_id)
    agenda = personal_agenda

    recent_dialogue = memory_library.load_recent_dialogue(
        user_id,
        max_turns=CANONICAL_HISTORY_TURNS,
    )
    from daemon_core.first_meeting import user_turns
    from daemon_core.memory.reader import without_trailing_user_message

    prior_user_turns = user_turns(without_trailing_user_message(recent_dialogue, message))
    from daemon_core.memory.reader import load_prior_dialogue

    prior_dialogue = load_prior_dialogue(
        user_id,
        max_chars=CANONICAL_PRIOR_DIALOGUE_CHARS,
        max_files=CANONICAL_PRIOR_DIALOGUE_FILES,
    )
    scene_scope = memory_library.scene_scope_from_origin(
        {"platform": platform, "chat_id": user_id}
    )
    from daemon_core.voice_brief import build_dialogue_coverage
    from daemon_core.after_turn_spool import AfterTurnSpool, after_turn_spool_enabled

    # Snapshot pending reads before the scene: publication precedes queue removal.
    recent_souvenir_sources = ()
    if after_turn_spool_enabled():
        recent_coverage = build_dialogue_coverage(
            scan_dialogue="",
            recent_dialogue=without_trailing_user_message(recent_dialogue, message),
            scene_delta="", scene_active=False,
        )
        recent_souvenir_sources = AfterTurnSpool().read_recent_souvenir_sources(
            user_id, scene_scope, memory_root=memory_library.MEMORY_ROOT,
            recent_turn_ids=recent_coverage.recent_turn_ids,
            now=time.time() if message_received_at is None else message_received_at,
        )
    scene_model = None
    scene_delta = ""
    scene_status = "absent"
    try:
        from daemon_core.held_thought import shared_world_state_enabled
        from daemon_core.scene_model import (
            SCENE_MODEL_DELTA_SCAN_TURNS,
            ensure_shared_scene_model,
        )

        if shared_world_state_enabled():
            ensure_shared_scene_model(user_id, scene_scope)

        scene_dialogue = memory_library.load_recent_dialogue(
            user_id,
            max_turns=SCENE_MODEL_DELTA_SCAN_TURNS,
        )
        scene_view = memory_library.inspect_current_scene_model(
            user_id,
            scene_scope,
            scene_dialogue,
            current_message=message,
            message_received_at=message_received_at,
        )
        scene_status = scene_view.status
        if scene_view.usable:
            scene_model = scene_view.snapshot
            scene_delta = scene_view.delta or ""
        elif scene_view.status != "absent":
            logger.warning(
                "Scene model ignored; bounded canonical seed used | "
                "user_id=%s | platform=%s | status=%s",
                user_id,
                platform,
                scene_view.status,
            )
    except Exception as exc:
        logger.info(
            "Scene model voix indisponible | error=%s",
            exc.__class__.__name__,
        )
        from daemon_core.voice_brief import DialogueCoverageError

        if isinstance(exc, DialogueCoverageError):
            raise
        raise DialogueCoverageError("scene model inspection failed") from exc

    dialogue_coverage = build_dialogue_coverage(
        scan_dialogue=without_trailing_user_message(scene_dialogue, message),
        recent_dialogue=without_trailing_user_message(recent_dialogue, message),
        scene_delta=scene_delta if scene_model is not None else "",
        scene_active=scene_model is not None,
        scene_watermark_source_id=(
            str(getattr(scene_model, "dialogue_watermark_source_id", ""))
            if scene_model is not None
            else ""
        ),
    )
    recent_dialogue = dialogue_coverage.dialogue

    user_context = memory_library.load_user_context(user_id)
    active_context = memory_library.load_active_context(user_id)
    authoritative_context = ""
    preserved_current_context = ""
    if scene_model is None:
        conversation_state = memory_library.load_conversation_state(user_id)
        if not conversation_state.strip():
            conversation_state = memory_library.load_immediate_focus(user_id)

        from daemon_core.thin_sovereign import build_compact_scene_context

        hot_souvenirs: list[str] = []
        scene_model_text = build_compact_scene_context(
            user_agenda=agenda,
            active_context=active_context,
            conversation_state=conversation_state,
        )
    else:
        from daemon_core.scene_model import (
            current_topics_canary_enabled,
            render_scene_model_for_voice,
            render_unrepresented_active_context_for_voice,
        )

        hot_souvenirs = scene_model.hot_souvenirs
        projection_time = (
            datetime.fromtimestamp(message_received_at).astimezone()
            if message_received_at is not None
            else None
        )
        scene_model_text = render_scene_model_for_voice(
            scene_model,
            now=projection_time,
            include_current_topics=current_topics_canary_enabled(user_id),
        )
        preserved_current_context = render_unrepresented_active_context_for_voice(
            scene_model,
            active_context,
        )
        authoritative_sections = []
        if agenda.strip():
            authoritative_sections.append('## Current agenda\n' + agenda.strip())
        authoritative_context = "\n\n".join(authoritative_sections)

    combined_hot_souvenirs = list(hot_souvenirs)
    for source_id, paths in recent_souvenir_sources:
        if scene_model is not None and source_id not in dialogue_coverage.scene_delta_turn_ids:
            continue
        for path in paths:
            if path not in combined_hot_souvenirs and len(combined_hot_souvenirs) < memory_library.SOUVENIR_READ_MAX_COUNT:
                combined_hot_souvenirs.append(path)

    def read_hot(paths):
        return memory_library.read_exact_active_souvenirs(
            paths, memory_library.MEMORY_ROOT,
            max_chars=memory_library.DEFAULT_MAX_HOT_MEMORY_CHARS,
            user_id=user_id, platform=platform,
        )

    if combined_hot_souvenirs == list(hot_souvenirs):
        hot_memory = read_hot(hot_souvenirs)
    else:
        try:
            hot_memory = read_hot(combined_hot_souvenirs)
        except (OSError, ValueError):
            logger.warning("Recent souvenir supplement rejected; canonical hot memory retained")
            hot_memory = read_hot(hot_souvenirs)
    selected_souvenir_names = None
    episodic_scene_path = ""
    episodic_scene_text = ""
    catalog_notice = ""
    from daemon_core.personal_context import personal_context_canary_enabled

    personal_context_active = personal_context_canary_enabled(user_id, platform)
    semantic_atlas_active = False
    try:
        from daemon_core.semantic_atlas import (
            TOP12_CATALOG_NOTICE,
            select_semantic_atlas_paths,
            semantic_atlas_canary_enabled,
        )

        if semantic_atlas_canary_enabled(user_id, platform):
            semantic_dialogue = memory_library.load_recent_dialogue(
                user_id,
                max_turns=SEMANTIC_ATLAS_DIALOGUE_TURNS,
            )
            semantic_need = message.strip()
            if semantic_dialogue.strip() and not personal_context_active:
                semantic_need = (
                    'Immediate dialogue establishing the references:\n'
                    + semantic_dialogue.strip()
                    + '\n\nLatest message:\n'
                    + semantic_need
                )
            selection = select_semantic_atlas_paths(
                semantic_need,
                semantic_dialogue,
                memory_library.MEMORY_ROOT,
            )
            selected_souvenir_names = selection.paths
            catalog_notice = TOP12_CATALOG_NOTICE
            semantic_atlas_active = True
            from daemon_core.episodic_rem import (
                episodic_scene_voice_enabled,
                resolve_selected_episode,
            )

            if episodic_scene_voice_enabled(user_id, platform):
                channel_heads = dict(
                    getattr(selection, "channel_heads", ())
                )
                leading_scene = channel_heads.get("need_body", "")
                agreed_scene = (
                    leading_scene
                    if leading_scene
                    == channel_heads.get("need_routing", "")
                    else ""
                )
                episodic_scene_path, episodic_scene_text = (
                    resolve_selected_episode(
                        (agreed_scene,) if agreed_scene else (),
                        memory_root=memory_library.MEMORY_ROOT,
                    )
                )
            logger.info(
                "Atlas top12 canary | user_id=%s | paths=%d | elapsed=%.3fs | "
                "cache_misses=%d | corpus=%s",
                user_id,
                len(selection.paths),
                selection.duration_s,
                selection.cache_misses,
                selection.corpus_fingerprint[:16],
            )
    except Exception as exc:
        logger.warning(
            "Atlas top12 canary unavailable; full Atlas preserved | "
            "user_id=%s | error=%s | reason=%s",
            user_id,
            exc.__class__.__name__,
            str(exc)[:160],
        )
    atlas = memory_library.build_exact_memory_atlas(
        memory_library.MEMORY_ROOT,
        user_id=user_id,
        platform=platform,
        selected_souvenir_names=selected_souvenir_names,
        catalog_notice=catalog_notice,
    )
    from daemon_core.daemon_identity import load_daemon_identity

    identity = load_daemon_identity(memory_library.MEMORY_ROOT)
    from daemon_core.onboarding_user import render_onboarding_user

    initial_preferences = render_onboarding_user(identity.user, user_id=user_id, platform=platform)
    if initial_preferences:
        user_context = initial_preferences + ("\n\n" + user_context if user_context.strip() else "")
    from daemon_core.first_meeting import render as render_first_meeting

    meeting = render_first_meeting(memory_library.MEMORY_ROOT, user_id, prior_user_turns)
    if meeting:
        user_context = meeting + ("\n\n" + user_context if user_context.strip() else "")
    daemon_persona = memory_library.load_daemon_persona(user_id)
    conversation_manner = memory_library.load_conversation_manner(user_id)
    working_state_revised_at = (
        scene_model.revised_at if scene_model is not None else None
    )

    from daemon_core.thin_sovereign import build_thin_held_atlas_messages

    messages = build_thin_held_atlas_messages(
        message=message,
        atlas=atlas,
        hot_memory=hot_memory,
        scene_model_text=scene_model_text,
        recent_dialogue=recent_dialogue,
        prior_dialogue=prior_dialogue,
        daemon_persona=daemon_persona,
        conversation_manner=conversation_manner,
        user_context=user_context,
        current_context=preserved_current_context,
        authoritative_context=authoritative_context,
        episodic_scene_text=episodic_scene_text,
        connected_source_status="",
        agent_name=identity.name,
        agent_gender=identity.gender,
        host_tools_available=for_delegation,
        message_received_at=message_received_at,
        working_state_revised_at=working_state_revised_at,
        recognize_shared_context=personal_context_active,
    )
    if atlas.common_ground_names:
        messages[0]["content"] += (
            '\n\n# Common Ground reading threshold\n\n'
            + COMMON_GROUND_CONSUMPTION_CONTRACT
        )

    return messages, CanonicalVoiceContext(
        atlas=atlas,
        hot_memory=hot_memory,
        scene_scope=scene_scope,
        personal_agenda=personal_agenda,
        professional_agenda="",
        active_context=active_context,
        prior_dialogue=prior_dialogue,
        scene_model=scene_model,
        scene_delta=scene_delta,
        episodic_scene_path=episodic_scene_path,
        dialogue_coverage=dialogue_coverage,
        prompt_variant=(
            "voice_brief_atlas_top12"
            if semantic_atlas_active
            else "voice_brief_reference"
        ),
        situation_source=(
            "scene"
            if scene_model is not None
            else (
                "seed"
                if scene_status == "absent"
                else f"seed_recovery:{scene_status}"
            )
        ),
    )


__all__ = [
    "CANONICAL_HISTORY_TURNS",
    "CANONICAL_PRIOR_DIALOGUE_CHARS",
    "CANONICAL_PRIOR_DIALOGUE_FILES",
    "CanonicalVoiceContext",
    "compose_canonical_voice_messages",
]
