import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.turn_runtime import TurnRequest, prepare_turn
from daemon_core.voice_brief import DialogueCoverage
from daemon_core.canonical_voice import CanonicalVoiceContext
from daemon_core.continuity import read_continuity_receipt


def _voice_context(*, canonical_ready=False):
    context = SimpleNamespace(
        prompt_variant="thin",
        scene_scope="telegram__u",
        situation_source="scene",
        hot_memory=SimpleNamespace(souvenir_names=("theme/a",)),
    )
    if canonical_ready:
        context.atlas = SimpleNamespace(
            text="- theme/a",
            souvenir_names=("theme/a",),
            rendered_chars=9,
            sha256="atlas",
            common_ground_names=(),
            expired_souvenir_names=(),
        )
        context.hot_memory = SimpleNamespace(
            souvenir_names=(),
            souvenirs=(),
            rendered_chars=0,
        )
        context.dialogue_coverage = DialogueCoverage(
            status="recent_only",
            dialogue="",
        )
    return context


def _canonical_messages():
    return [
        {
            "role": "system",
            "content": (
                "# Identity\n\nTu es Lyra.\n\nPersona.\n\n# Stable personal reference points\n\nRepères.\n\n# Turn contract\n\nContrat.\n\n# Temporal frame\n\nHeure actuelle : 2026-08-03T12:00:00+02:00.\n\n# Situation at the turn threshold\n\nSituation.\n\n# Current authoritative durable sources\n\nAgenda.\n\n# Exact memories already open\n\nAucune.\n\n# Durable memory catalog\n\n- theme/a\n\n# Outils de l'hôte\n\nOutils."
            ),
        },
        {
            "role": "system",
            "content": (
                '# Turn threshold\n\nSeuil.\n\n## External knowledge boundary\n\nFrontière.'
            ),
        },
        {"role": "user", "content": "m"},
    ]


def test_prepare_turn_preserves_promoted_composer_output_exactly():
    captured = {}
    messages = [
        {"role": "system", "content": "IDENTITE"},
        {"role": "assistant", "content": "tour précédent"},
        {"role": "system", "content": "SITUATION"},
        {"role": "user", "content": "message"},
        {"role": "tool", "content": "hors plan initial"},
    ]
    context = _voice_context()

    def composer(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return messages, context

    request = TurnRequest(
        user_id="u",
        message="message",
        platform="telegram",
        session_id="session-1",
        for_delegation=True,
        message_received_at=123.0,
        trace_id="trace-1",
    )
    plan = prepare_turn(request, composer=composer)

    assert list(plan.messages) == messages
    assert plan.voice_context is context
    assert captured == {
        "args": ("u", "message"),
        "kwargs": {
            "platform": "telegram",
            "for_delegation": True,
            "message_received_at": 123.0,
        },
    }
    assert plan.leading_system_prompt == "IDENTITE"
    assert plan.near_turn_system_prompt == ""
    assert plan.turns == messages[1:4]
    assert plan.collapsed_system_prompt == "IDENTITE\n\nSITUATION"
    assert plan.prompt_variant == "thin"
    assert plan.scene_scope == "telegram__u"
    assert plan.hot_souvenirs == ("theme/a",)
    assert plan.provenance == {
        "voice_path": "injected",
        "situation_source": "scene",
        "memory_source": "unspecified",
        "composer": f"{__name__}.composer",
    }


def test_prepare_turn_resolves_canonical_composer_at_call_time(monkeypatch):
    import daemon_core.canonical_voice as canonical_voice

    seen = {}
    context = _voice_context(canonical_ready=True)

    def composer(*args, **kwargs):
        seen["called"] = (args, kwargs)
        return _canonical_messages(), context

    monkeypatch.setattr(
        canonical_voice,
        "compose_canonical_voice_messages",
        composer,
    )

    plan = prepare_turn(
        TurnRequest(user_id="u", message="m", platform="openclaw")
    )

    assert plan.leading_system_prompt.startswith('# Identity')
    assert plan.voice_path == "canonical"
    assert seen["called"][0] == ("u", "m")


def test_prepare_turn_exposes_native_authority_hierarchy(monkeypatch):
    import daemon_core.canonical_voice as canonical_voice

    fixture = _voice_context(canonical_ready=True)
    context = CanonicalVoiceContext(
        atlas=fixture.atlas,
        hot_memory=fixture.hot_memory,
        scene_scope=fixture.scene_scope,
        dialogue_coverage=fixture.dialogue_coverage,
        situation_source="scene",
    )
    monkeypatch.setattr(
        canonical_voice,
        "compose_canonical_voice_messages",
        lambda *_args, **_kwargs: (_canonical_messages(), context),
    )
    monkeypatch.setenv("DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY", "on")
    monkeypatch.setenv(
        "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_USER_IDS",
        "u",
    )
    monkeypatch.setenv(
        "DAEMON_CORE_NARRATIVE_AUTHORITY_HIERARCHY_PLATFORMS",
        "telegram,openclaw",
    )

    plan = prepare_turn(
        TurnRequest(user_id="u", message="m", platform="openclaw")
    )

    assert plan.prompt_variant == "voice_brief_authority_hierarchy_v3"
    assert plan.messages[0]["role"] == "system"
    assert plan.messages[1]["role"] == "developer"
    assert plan.developer_instruction_addition == plan.messages[1]["content"]
    assert 'Stable personal reference points' not in plan.leading_system_prompt
    assert 'Stable personal reference points' in plan.developer_instruction_addition
    assert 'Durable memory catalog' in plan.leading_system_prompt
    assert 'Durable memory catalog' not in (
        plan.developer_instruction_addition
    )
    assert 'Turn threshold' in plan.collapsed_system_prompt
    assert plan.near_turn_system_prompt.startswith('# Turn threshold')
    assert "Seuil." in plan.near_turn_system_prompt
    assert 'Stable personal reference points' not in plan.collapsed_system_prompt


def test_prepare_turn_observes_the_canonical_evidence_topology(
    monkeypatch,
    tmp_path,
):
    import daemon_core.canonical_voice as canonical_voice

    context = _voice_context(canonical_ready=True)
    monkeypatch.setattr(
        canonical_voice,
        "compose_canonical_voice_messages",
        lambda *_args, **_kwargs: (_canonical_messages(), context),
    )
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPTS", "shadow")
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPT_DIR", str(tmp_path))

    prepare_turn(
        TurnRequest(
            user_id="u",
            message="m",
            platform="openclaw",
            trace_id="trace-turn-evidence",
        )
    )

    receipt = read_continuity_receipt(
        "trace-turn-evidence",
        directory=tmp_path,
    )
    evidence = receipt["stages"]["evidence_preparation"]
    assert evidence["status"] == "completed"
    assert evidence["details"]["atlas_capsule_count"] == 1
    assert evidence["details"]["dialogue_missing_turns"] == 0
    assert evidence["details"]["prompt_variant"] == "thin"


@pytest.mark.parametrize("platform", ("telegram", "openclaw"))
def test_prepare_turn_records_only_a_technical_episodic_scene_reference(
    monkeypatch,
    tmp_path,
    platform,
):
    import daemon_core.canonical_voice as canonical_voice
    from daemon_core.continuity import technical_ref

    scene_path = "theme/scenes_episodiques/2026/09/episode-prive"
    context = _voice_context(canonical_ready=True)
    context.episodic_scene_path = scene_path
    messages = _canonical_messages()
    monkeypatch.setattr(
        canonical_voice,
        "compose_canonical_voice_messages",
        lambda *_args, **_kwargs: (messages, context),
    )
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPTS", "shadow")
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPT_DIR", str(tmp_path))
    trace_id = f"trace-episodic-{platform}"

    plan = prepare_turn(
        TurnRequest(
            user_id="u",
            message="m",
            platform=platform,
            trace_id=trace_id,
        )
    )

    assert list(plan.messages) == messages
    receipt = read_continuity_receipt(trace_id, directory=tmp_path)
    details = receipt["stages"]["preparation"]["details"]
    assert details["episodic_scene_selected"] is True
    assert details["episodic_scene_ref"] == technical_ref(scene_path)
    assert scene_path not in json.dumps(receipt, ensure_ascii=False)


def test_prepare_turn_records_no_episodic_reference_when_none_was_selected(
    monkeypatch,
    tmp_path,
):
    import daemon_core.canonical_voice as canonical_voice

    context = _voice_context(canonical_ready=True)
    context.episodic_scene_path = ""
    monkeypatch.setattr(
        canonical_voice,
        "compose_canonical_voice_messages",
        lambda *_args, **_kwargs: (_canonical_messages(), context),
    )
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPTS", "shadow")
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPT_DIR", str(tmp_path))

    prepare_turn(
        TurnRequest(
            user_id="u",
            message="m",
            platform="telegram",
            trace_id="trace-no-episodic-scene",
        )
    )

    receipt = read_continuity_receipt(
        "trace-no-episodic-scene",
        directory=tmp_path,
    )
    details = receipt["stages"]["preparation"]["details"]
    assert details["episodic_scene_selected"] is False
    assert "episodic_scene_ref" not in details


def test_prepare_turn_rejects_an_incomplete_canonical_plan(monkeypatch):
    import daemon_core.canonical_voice as canonical_voice

    monkeypatch.setattr(
        canonical_voice,
        "compose_canonical_voice_messages",
        lambda *_args, **_kwargs: ([], _voice_context(canonical_ready=True)),
    )

    with pytest.raises(ValueError, match="leading system"):
        prepare_turn(TurnRequest(user_id="u", message="m", platform="telegram"))


def test_prepare_turn_uses_temporal_renderer_only_for_allowed_telegram_user(
    monkeypatch,
):
    import daemon_core.canonical_voice as canonical_voice

    messages = _canonical_messages()
    messages[0]["content"] = messages[0]["content"].replace(
        "Heure actuelle : 2026-08-03T12:00:00+02:00.",
        'Now : 2026-08-03T12:00:00+02:00 (weekday=Monday, timezone=Europe/Paris)',
    ).replace(
        '# Exact memories already open',
        '# Current authoritative durable sources\n\nLa source exacte prévaut.\n\n## Current agenda\n- 2026-08-04 | Alex | rendez-vous\n\n# Exact memories already open',
    )
    base = _voice_context(canonical_ready=True)
    context = CanonicalVoiceContext(
        atlas=base.atlas,
        hot_memory=base.hot_memory,
        scene_scope=base.scene_scope,
        personal_agenda="- 2026-08-04 | Alex | rendez-vous",
        dialogue_coverage=base.dialogue_coverage,
    )
    monkeypatch.setattr(
        canonical_voice,
        "compose_canonical_voice_messages",
        lambda *_args, **_kwargs: (messages, context),
    )
    monkeypatch.setenv("DAEMON_CORE_TEMPORAL_VOICE_BRIEF", "on")
    monkeypatch.setenv(
        "DAEMON_CORE_TEMPORAL_VOICE_BRIEF_USER_IDS",
        "u",
    )
    monkeypatch.setenv(
        "DAEMON_CORE_TEMPORAL_VOICE_BRIEF_PLATFORMS",
        "telegram",
    )

    telegram = prepare_turn(
        TurnRequest(user_id="u", message="m", platform="telegram")
    )
    openclaw = prepare_turn(
        TurnRequest(user_id="u", message="m", platform="openclaw")
    )

    assert telegram.prompt_variant == "voice_brief_temporal_v2"
    assert "## Canonical agenda timeline" in telegram.leading_system_prompt
    assert openclaw.prompt_variant == "voice_brief_reference"
    assert "## Canonical agenda timeline" not in openclaw.leading_system_prompt


def test_turn_plan_handles_missing_optional_context_fields():
    plan = prepare_turn(
        TurnRequest(user_id="u", message="m", platform="test"),
        composer=lambda *args, **kwargs: (
            [{"role": "user", "content": "m"}],
            object(),
        ),
    )

    assert plan.leading_system_prompt == ""
    assert plan.collapsed_system_prompt == ""
    assert plan.prompt_variant == "current"
    assert plan.scene_scope == ""
    assert plan.hot_souvenirs == ()
    assert plan.voice_path == "injected"
    assert plan.situation_source == "unspecified"
    assert plan.memory_source == "unspecified"


def test_prepare_turn_matches_promoted_composer_on_thin_seed(monkeypatch):
    from daemon_core.canonical_voice import compose_canonical_voice_messages
    import daemon_core.held_atlas_sovereign as held_atlas
    from daemon_core.held_atlas_sovereign import (
        ExactSouvenirRead,
        ExactMemoryAtlas,
    )

    for name, value in {
        "DAEMON_CORE_SOVEREIGN_VOICE": "on",
        "DAEMON_CORE_SOVEREIGN_MODE": "delegate",
        "DAEMON_CORE_SOVEREIGN_PLATFORMS": "telegram",
        "DAEMON_CORE_SINGLE_PASS": "on",
        "DAEMON_CORE_SINGLE_PASS_USER_IDS": "u",
        "DAEMON_CORE_SOVEREIGN_LIBRARY": "on",
        "DAEMON_CORE_SOVEREIGN_LIBRARY_USER_IDS": "u",
        "DAEMON_CORE_THIN_SOVEREIGN_VOICE": "on",
        "DAEMON_CORE_THIN_SOVEREIGN_USER_IDS": "u",
        "DAEMON_CORE_SCENE_MODEL": "canary",
        "DAEMON_CORE_SCENE_MODEL_USER_IDS": "u",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "daemon_core.scene_model.read_scene_model",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        held_atlas,
        "load_recent_dialogue",
        lambda *_args, **_kwargs: (
            "## 2026-07-25T08:00:00+02:00 — user\n\nBonjour"
        ),
    )
    monkeypatch.setattr(
        held_atlas,
        "load_user_agenda",
        lambda *_args: "- 2026-07-26 | retour prévu",
    )
    monkeypatch.setattr(
        held_atlas,
        "load_active_context",
        lambda *_args: "Contexte actif daté.",
    )
    monkeypatch.setattr(
        held_atlas,
        "load_conversation_state",
        lambda *_args: "Échange calme.",
    )
    monkeypatch.setattr(
        held_atlas,
        "load_user_context",
        lambda *_args: "Fait stable.",
    )
    monkeypatch.setattr(
        held_atlas,
        "load_daemon_persona",
        lambda *_args: "Persona.",
    )
    monkeypatch.setattr(
        held_atlas,
        "load_conversation_manner",
        lambda *_args: "Manière.",
    )
    monkeypatch.setattr(
        held_atlas,
        "build_exact_memory_atlas",
        lambda *_args, **_kwargs: ExactMemoryAtlas(
            "- theme/a",
            ("theme/a",),
            9,
            "atlas",
        ),
    )
    monkeypatch.setattr(
        held_atlas,
        "read_exact_active_souvenirs",
        lambda *_args, **_kwargs: ExactSouvenirRead((), (), 0),
    )
    request = TurnRequest(
        user_id="u",
        message="Et maintenant ?",
        platform="telegram",
        message_received_at=123.0,
    )

    canonical_messages, canonical_context = (
        compose_canonical_voice_messages(
            request.user_id,
            request.message,
            platform=request.platform,
            for_delegation=request.for_delegation,
            message_received_at=request.message_received_at,
        )
    )
    plan = prepare_turn(request)

    assert list(plan.messages) == canonical_messages
    assert plan.voice_context == canonical_context
    assert plan.prompt_variant == "voice_brief_reference"
