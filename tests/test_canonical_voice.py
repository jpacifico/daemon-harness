import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.canonical_voice import compose_canonical_voice_messages  # noqa: E402
from daemon_core.held_atlas_sovereign import (  # noqa: E402
    CurrentSceneModelView,
    ExactSouvenirRead,
    ExactMemoryAtlas,
)


def _freeze_inputs(monkeypatch, *, scene):
    import daemon_core.held_atlas_sovereign as memory_library
    import daemon_core.scene_model as scene_model
    import daemon_core.thin_sovereign as thin

    monkeypatch.setattr(
        memory_library,
        "load_recent_dialogue",
        lambda *_args, **_kwargs: (
            "## 2026-07-25T08:00:00+02:00 — user\n\nBonjour\n\n"
            "## 2026-07-25T08:01:00+02:00 — assistant\n\nSalut."
        ),
    )
    monkeypatch.setattr(memory_library, "load_user_agenda", lambda *_args: "Agenda.")
    monkeypatch.setattr(
        memory_library,
        "load_active_context",
        lambda *_args: "Contexte actif.",
    )
    monkeypatch.setattr(
        memory_library,
        "load_conversation_state",
        lambda *_args: "État de conversation.",
    )
    monkeypatch.setattr(memory_library, "load_immediate_focus", lambda *_args: "")
    monkeypatch.setattr(
        memory_library,
        "load_user_context",
        lambda *_args: "Repère stable.",
    )
    monkeypatch.setattr(
        memory_library,
        "load_daemon_persona",
        lambda *_args: "Persona.",
    )
    monkeypatch.setattr(
        memory_library,
        "load_conversation_manner",
        lambda *_args: "Manière.",
    )
    monkeypatch.setattr(
        memory_library,
        "build_exact_memory_atlas",
        lambda *_args, **_kwargs: ExactMemoryAtlas(
            text="- theme/a | subjects: sujet",
            souvenir_names=("theme/a",),
            rendered_chars=27,
            sha256="atlas",
        ),
    )
    monkeypatch.setattr(
        memory_library,
        "read_exact_active_souvenirs",
        lambda paths, *_args, **_kwargs: ExactSouvenirRead(
            tuple((path, "Corps exact.") for path in paths),
            (),
            12 * len(paths),
        ),
    )
    delta = (
        "## 2026-07-25T08:01:00+02:00 — assistant\n\nSalut."
        if scene
        else None
    )
    monkeypatch.setattr(
        memory_library,
        "inspect_current_scene_model",
        lambda *_args, **_kwargs: CurrentSceneModelView(
            scene,
            delta,
            "ready" if scene else "absent",
        ),
    )
    monkeypatch.setattr(
        scene_model,
        "render_scene_model_for_voice",
        lambda *_args, **_kwargs: "Scène rendue.",
    )
    monkeypatch.setattr(
        thin,
        "build_temporal_frame",
        lambda *_args, **_kwargs: SimpleNamespace(render=lambda: "Temps figé."),
    )


def _fingerprint(messages):
    payload = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_canonical_seed_is_thin_without_historical_feature_flags(monkeypatch):
    for name in (
        "DAEMON_CORE_SINGLE_PASS",
        "DAEMON_CORE_SOVEREIGN_LIBRARY",
        "DAEMON_CORE_THIN_SOVEREIGN_VOICE",
        "DAEMON_CORE_SCENE_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    # The V0 reference fingerprints were taken when the fallback name was Lyra;
    # naming it keeps the comparison with the frozen composition exact.
    monkeypatch.setenv("DAEMON_CORE_AGENT_NAME", "Lyra")
    _freeze_inputs(monkeypatch, scene=None)

    messages, context = compose_canonical_voice_messages(
        "u",
        "Et maintenant ?",
        platform="telegram",
        message_received_at=123.0,
    )

    assert context.prompt_variant == "voice_brief_reference"
    assert context.situation_source == "seed"
    assert context.hot_memory.souvenir_names == ()
    assert _fingerprint(messages) == (
        '97d5c51d4d674223fc19665e2ae6cce9f6398e1f2bd579183ba4237e43cb686f'
    )


def test_canonical_personal_hosts_canary_use_semantic_top12(monkeypatch):
    import daemon_core.held_atlas_sovereign as memory_library
    import daemon_core.semantic_atlas as semantic_atlas

    _freeze_inputs(monkeypatch, scene=None)
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12", "canary")
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS", "u")
    selection = SimpleNamespace(
        paths=("theme/a", "theme/b"),
        duration_s=0.12,
        cache_misses=0,
        corpus_fingerprint="f" * 64,
    )
    selection_input = {}

    def select_paths(need, dialogue, *_args, **_kwargs):
        selection_input["need"] = need
        selection_input["dialogue"] = dialogue
        return selection

    monkeypatch.setattr(semantic_atlas, "select_semantic_atlas_paths", select_paths)
    observed = {}

    def build_atlas(*_args, **kwargs):
        observed.update(kwargs)
        return ExactMemoryAtlas(
            text=semantic_atlas.TOP12_CATALOG_NOTICE + "\n- theme/a\n- theme/b",
            souvenir_names=("theme/a", "theme/b"),
            rendered_chars=180,
            sha256="atlas-top12",
        )

    monkeypatch.setattr(memory_library, "build_exact_memory_atlas", build_atlas)

    for platform in ("telegram", "openclaw"):
        _messages, context = compose_canonical_voice_messages(
            "u",
            "Et maintenant ?",
            platform=platform,
            message_received_at=123.0,
        )

        assert observed["selected_souvenir_names"] == ("theme/a", "theme/b")
        assert observed["catalog_notice"] == semantic_atlas.TOP12_CATALOG_NOTICE
        assert selection_input["need"].endswith(
            'Latest message:\nEt maintenant ?'
        )
        assert "Bonjour" in selection_input["need"]
        assert selection_input["dialogue"].startswith(
            "## 2026-07-25T08:00:00+02:00 — user"
        )
        assert context.prompt_variant == "voice_brief_atlas_top12"
        assert context.atlas.souvenir_names == ("theme/a", "theme/b")


def test_canonical_hosts_receive_the_same_leading_episodic_scene(monkeypatch):
    import daemon_core.episodic_rem as episodic_rem
    import daemon_core.held_atlas_sovereign as memory_library
    import daemon_core.semantic_atlas as semantic_atlas

    _freeze_inputs(monkeypatch, scene=None)
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12", "canary")
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS", "u")
    monkeypatch.setenv("DAEMON_CORE_REM_EPISODIC_SCENES", "canary")
    monkeypatch.setenv("DAEMON_CORE_REM_EPISODIC_SCENE_USER_IDS", "u")
    selected = (
        "theme/scenes_episodiques/2026/08/scene",
        "theme/a",
    )
    monkeypatch.setattr(
        semantic_atlas,
        "select_semantic_atlas_paths",
        lambda *_args, **_kwargs: SimpleNamespace(
            paths=selected,
            channel_heads=(
                ("need_body", selected[0]),
                ("need_routing", selected[0]),
            ),
            duration_s=0.1,
            cache_misses=0,
            corpus_fingerprint="f" * 64,
        ),
    )
    monkeypatch.setattr(
        episodic_rem,
        "resolve_selected_episode",
        lambda paths, **_kwargs: (
            paths[0],
            "# Une scène passée\n\n## Établi\n\n- Un fait sourcé.",
        ),
    )
    monkeypatch.setattr(
        memory_library,
        "build_exact_memory_atlas",
        lambda *_args, **_kwargs: ExactMemoryAtlas(
            text="\n".join(f"- {name}" for name in selected),
            souvenir_names=selected,
            rendered_chars=70,
            sha256="episodic-atlas",
        ),
    )

    prompts = {}
    for platform in ("telegram", "openclaw"):
        messages, context = compose_canonical_voice_messages(
            "u",
            "Cela me rappelle quelque chose.",
            platform=platform,
            message_received_at=123.0,
        )
        prompts[platform] = messages[0]["content"]
        assert context.episodic_scene_path == selected[0]
        assert '# Potentially relevant past scene' in prompts[platform]
        assert "# Une scène passée" in prompts[platform]

    assert prompts["telegram"] == prompts["openclaw"]


def test_canonical_hosts_consume_the_same_shared_world_scope(monkeypatch):
    import daemon_core.scene_model as scene_model

    _freeze_inputs(monkeypatch, scene=None)
    monkeypatch.setenv("DAEMON_CORE_SHARED_WORLD_STATE", "canary")
    initialized = []
    monkeypatch.setattr(
        scene_model,
        "ensure_shared_scene_model",
        lambda user_id, scene_scope: initialized.append(
            (user_id, scene_scope)
        ),
    )

    contexts = {
        platform: compose_canonical_voice_messages(
            "u",
            "Et maintenant ?",
            platform=platform,
            message_received_at=123.0,
        )[1]
        for platform in ("telegram", "openclaw")
    }

    assert contexts["telegram"].scene_scope == "world"
    assert contexts["openclaw"].scene_scope == "world"
    assert initialized == [("u", "world"), ("u", "world")]


def test_canonical_voice_ignores_scene_without_semantic_view_agreement(
    monkeypatch,
):
    import daemon_core.episodic_rem as episodic_rem
    import daemon_core.semantic_atlas as semantic_atlas

    _freeze_inputs(monkeypatch, scene=None)
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12", "canary")
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS", "u")
    monkeypatch.setenv("DAEMON_CORE_REM_EPISODIC_SCENES", "canary")
    monkeypatch.setenv("DAEMON_CORE_REM_EPISODIC_SCENE_USER_IDS", "u")
    scene = "theme/scenes_episodiques/2026/08/scene"
    monkeypatch.setattr(
        semantic_atlas,
        "select_semantic_atlas_paths",
        lambda *_args, **_kwargs: SimpleNamespace(
            paths=(scene, "theme/a"),
            channel_heads=(
                ("need_body", scene),
                ("need_routing", "theme/a"),
            ),
            duration_s=0.1,
            cache_misses=0,
            corpus_fingerprint="f" * 64,
        ),
    )
    monkeypatch.setattr(
        episodic_rem,
        "resolve_selected_episode",
        lambda paths, **_kwargs: (paths[0], "unexpected") if paths else ("", ""),
    )

    messages, context = compose_canonical_voice_messages(
        "u",
        "Salut toi",
        platform="telegram",
        message_received_at=123.0,
    )

    assert context.episodic_scene_path == ""
    assert '# Potentially relevant past scene' not in messages[0]["content"]


def test_canonical_semantic_failure_preserves_full_atlas(monkeypatch):
    import daemon_core.held_atlas_sovereign as memory_library
    import daemon_core.semantic_atlas as semantic_atlas

    _freeze_inputs(monkeypatch, scene=None)
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12", "canary")
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS", "u")
    monkeypatch.setattr(
        semantic_atlas,
        "select_semantic_atlas_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            semantic_atlas.SemanticAtlasUnavailable("cache_cold")
        ),
    )
    observed = {}

    def build_atlas(*_args, **kwargs):
        observed.update(kwargs)
        return ExactMemoryAtlas(
            text="- theme/a",
            souvenir_names=("theme/a",),
            rendered_chars=9,
            sha256="atlas-full",
        )

    monkeypatch.setattr(memory_library, "build_exact_memory_atlas", build_atlas)

    _messages, context = compose_canonical_voice_messages(
        "u",
        "Et maintenant ?",
        platform="telegram",
        message_received_at=123.0,
    )

    assert observed["selected_souvenir_names"] is None
    assert observed["catalog_notice"] == ""
    assert context.prompt_variant == "voice_brief_reference"


def test_canonical_scene_is_thin_without_historical_feature_flags(monkeypatch):
    for name in (
        "DAEMON_CORE_SINGLE_PASS",
        "DAEMON_CORE_SOVEREIGN_LIBRARY",
        "DAEMON_CORE_THIN_SOVEREIGN_VOICE",
        "DAEMON_CORE_SCENE_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    # The V0 reference fingerprints were taken when the fallback name was Lyra;
    # naming it keeps the comparison with the frozen composition exact.
    monkeypatch.setenv("DAEMON_CORE_AGENT_NAME", "Lyra")
    scene = SimpleNamespace(
        revised_at=100.0,
        hot_souvenirs=("theme/a",),
        revision=4,
    )
    _freeze_inputs(monkeypatch, scene=scene)

    messages, context = compose_canonical_voice_messages(
        "u",
        "Et maintenant ?",
        platform="openclaw",
        message_received_at=123.0,
    )

    assert context.prompt_variant == "voice_brief_reference"
    assert context.situation_source == "scene"
    assert context.scene_model is scene
    assert "Salut." in context.scene_delta
    assert context.dialogue_coverage.exact is True
    assert context.dialogue_coverage.missing_turn_ids == ()
    assert context.hot_memory.souvenir_names == ("theme/a",)
    assert '# Retained current reference points' in messages[0]["content"]
    assert "Contexte actif." in messages[0]["content"]
    assert '# Current authoritative durable sources' in messages[0]["content"]
    assert '## Current agenda\nAgenda.' in messages[0]["content"]
    assert _fingerprint(messages) == (
        'd24a1e8a967c39cd9e84e756002dd9ef4f511257773fd158e50063ad296fd9aa'
    )


def test_canonical_voice_exposes_exact_prior_days_outside_recent_coverage(
    monkeypatch,
):
    import daemon_core.memory.reader as memory_reader

    scene = SimpleNamespace(
        revised_at=100.0,
        hot_souvenirs=(),
        revision=4,
    )
    _freeze_inputs(monkeypatch, scene=scene)
    monkeypatch.setattr(
        memory_reader,
        "load_prior_dialogue",
        lambda *_args, **_kwargs: (
            "## 2026-07-23T23:00:00+02:00 — user\n\n"
            "Un détail exact de la veille."
        ),
    )

    messages, context = compose_canonical_voice_messages(
        "u",
        "Tu te souviens de la fin ?",
        platform="telegram",
        message_received_at=123.0,
    )

    system = messages[0]["content"]
    assert '# Exact recent past' in system
    assert "Un détail exact de la veille." in system
    assert "Un détail exact de la veille." not in context.dialogue_coverage.dialogue
    assert context.prior_dialogue.endswith("Un détail exact de la veille.")


def test_canonical_scene_exposes_current_topics_only_to_exact_canary_user(
    monkeypatch,
):
    import daemon_core.scene_model as scene_model

    scene = SimpleNamespace(
        revised_at=100.0,
        hot_souvenirs=(),
        revision=4,
    )
    _freeze_inputs(monkeypatch, scene=scene)
    render_options = []
    monkeypatch.setattr(
        scene_model,
        "render_scene_model_for_voice",
        lambda *_args, **kwargs: render_options.append(kwargs) or "Scène rendue.",
    )
    monkeypatch.setenv("DAEMON_CORE_CURRENT_TOPICS", "canary")
    monkeypatch.setenv("DAEMON_CORE_CURRENT_TOPICS_USER_IDS", "u")

    compose_canonical_voice_messages(
        "u",
        "Et maintenant ?",
        platform="telegram",
        message_received_at=123.0,
    )
    compose_canonical_voice_messages(
        "other",
        "Et maintenant ?",
        platform="telegram",
        message_received_at=123.0,
    )

    assert render_options[0]["include_current_topics"] is True
    assert render_options[1]["include_current_topics"] is False


def test_canonical_scene_cannot_hide_a_current_fact_omitted_by_projection(
    monkeypatch,
):
    import daemon_core.held_atlas_sovereign as memory_library

    scene = SimpleNamespace(
        revised_at=100.0,
        hot_souvenirs=(),
        revision=4,
    )
    _freeze_inputs(monkeypatch, scene=scene)
    monkeypatch.setattr(
        memory_library,
        "load_active_context",
        lambda *_args: (
            "# Contexte actif\n\n"
            "## Sujets actifs\n"
            "- Les 6 et 7 août, Client Alpha est la mission prioritaire.\n"
            "- Le 6 août, un sync Chronodrive est prévu à midi.\n"
            "- La propale Helexia est à remettre le 7 août."
        ),
    )

    messages, context = compose_canonical_voice_messages(
        "u",
        "Qu'est-ce que j'ai à faire aujourd'hui ?",
        platform="telegram",
        message_received_at=123.0,
    )

    prompt = messages[0]["content"]
    assert context.situation_source == "scene"
    assert "Scène rendue." in prompt
    assert "Les 6 et 7 août, Client Alpha est la mission prioritaire." in prompt
    assert "Le 6 août, un sync Chronodrive est prévu à midi." in prompt
    assert "La propale Helexia est à remettre le 7 août." in prompt


def test_canonical_scene_injects_delta_older_than_the_recent_tail(monkeypatch):
    import daemon_core.held_atlas_sovereign as memory_library

    scene = SimpleNamespace(
        revised_at=100.0,
        hot_souvenirs=(),
        revision=4,
        dialogue_watermark_source_id="src-watermark",
    )
    _freeze_inputs(monkeypatch, scene=scene)
    chunks = [
        (
            f"## 2026-07-25T08:00:{index:02d}+02:00 — "
            f"{'user' if index % 2 == 0 else 'assistant'}\n\nTour {index}."
        )
        for index in range(40)
    ]
    scan = "\n\n".join(chunks)
    recent = "\n\n".join(chunks[-24:])
    delta = "\n\n".join(chunks[-30:])
    monkeypatch.setattr(
        memory_library,
        "load_recent_dialogue",
        lambda *_args, max_turns: scan if max_turns == 200 else recent,
    )
    monkeypatch.setattr(
        memory_library,
        "inspect_current_scene_model",
        lambda *_args, **_kwargs: CurrentSceneModelView(
            scene,
            delta,
            "ready",
        ),
    )

    messages, context = compose_canonical_voice_messages(
        "u",
        "Message courant",
        platform="telegram",
        message_received_at=123.0,
    )

    dialogue = "\n".join(
        message["content"]
        for message in messages
        if message["role"] in {"user", "assistant"}
    )
    assert "Tour 10." in dialogue
    assert "Tour 9." not in dialogue
    assert len(context.dialogue_coverage.expected_turn_ids) == 30
    assert len(context.dialogue_coverage.visible_turn_ids) == 30
    assert context.dialogue_coverage.missing_turn_ids == ()


def test_canonical_scene_recovers_from_an_unprovable_gap_with_bounded_seed(
    monkeypatch,
):
    import daemon_core.held_atlas_sovereign as memory_library

    scene = SimpleNamespace(revised_at=100.0, hot_souvenirs=(), revision=4)
    _freeze_inputs(monkeypatch, scene=scene)
    monkeypatch.setattr(
        memory_library,
        "inspect_current_scene_model",
        lambda *_args, **_kwargs: CurrentSceneModelView(
            scene,
            None,
            "unprovable_delta",
        ),
    )

    messages, context = compose_canonical_voice_messages(
        "u",
        "Message courant",
        platform="openclaw",
        message_received_at=123.0,
    )

    assert context.scene_model is None
    assert context.scene_delta == ""
    assert context.situation_source == "seed_recovery:unprovable_delta"
    assert context.dialogue_coverage.status == "recent_only"
    assert context.dialogue_coverage.exact is True
    assert context.hot_memory.souvenir_names == ()
    assert "Contexte actif." in messages[0]["content"]
    assert "État de conversation." in messages[0]["content"]


def test_personal_context_canary_separates_queries_on_both_hosts(monkeypatch):
    from daemon_core import semantic_atlas
    from daemon_core.thin_sovereign import PERSONAL_RECOGNITION_THRESHOLD
    _freeze_inputs(monkeypatch, scene=None)
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12", "canary")
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS", "synthetic-u")
    monkeypatch.setenv("DAEMON_CORE_PERSONAL_CONTEXT_USER_IDS", "synthetic-u")
    monkeypatch.setenv("DAEMON_CORE_PERSONAL_CONTEXT_PLATFORMS", "telegram,openclaw")
    seen=[]
    def select(need, dialogue, *args, **kwargs):
        seen.append((need, dialogue))
        return SimpleNamespace(paths=("theme/a",), duration_s=0.0, cache_misses=0, corpus_fingerprint="f"*64)
    monkeypatch.setattr(semantic_atlas, "select_semantic_atlas_paths", select)
    for host in ("telegram", "openclaw"):
        monkeypatch.delenv("DAEMON_CORE_PERSONAL_CONTEXT", raising=False)
        reference,_ = compose_canonical_voice_messages("synthetic-u", "Et son style ?", platform=host, message_received_at=123.0)
        assert "Bonjour" in seen[-1][0]
        dialogue=seen[-1][1]
        monkeypatch.setenv("DAEMON_CORE_PERSONAL_CONTEXT", "canary")
        candidate,_ = compose_canonical_voice_messages("synthetic-u", "Et son style ?", platform=host, message_received_at=123.0)
        assert seen[-1] == ("Et son style ?", dialogue)
        assert candidate[-1] == reference[-1]
        assert candidate[:-2] == reference[:-2]
        assert candidate[-2]["content"].replace(PERSONAL_RECOGNITION_THRESHOLD+"\n\n", "", 1) == reference[-2]["content"]


def test_personal_context_canary_requires_exact_user_and_host(monkeypatch):
    from daemon_core.personal_context import personal_context_canary_enabled
    monkeypatch.setenv("DAEMON_CORE_PERSONAL_CONTEXT", "canary")
    monkeypatch.setenv("DAEMON_CORE_PERSONAL_CONTEXT_USER_IDS", "synthetic-u")
    monkeypatch.setenv("DAEMON_CORE_PERSONAL_CONTEXT_PLATFORMS", "telegram")
    assert personal_context_canary_enabled("synthetic-u", "telegram")
    assert not personal_context_canary_enabled("other", "telegram")
    assert not personal_context_canary_enabled("synthetic-u", "openclaw")
    assert not personal_context_canary_enabled("synthetic-u", None)
    monkeypatch.setenv("DAEMON_CORE_PERSONAL_CONTEXT_USER_IDS", "synthetic-u,*")
    assert not personal_context_canary_enabled("synthetic-u", "telegram")
