from datetime import date, datetime
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import daemon_core.held_atlas_sovereign as held_atlas
from daemon_core.canonical_voice import (
    CANONICAL_HISTORY_TURNS,
    compose_canonical_voice_messages,
)
from daemon_core.held_atlas_sovereign import (
    ExactSouvenirRead,
    ExactMemoryAtlas,
    build_exact_memory_atlas,
    collection_coverage_checkpoint,
    inspect_current_scene_model,
    parse_scene_coverage,
    read_exact_active_souvenirs,
    scene_coverage_checkpoint,
    judge_unread_souvenirs_before_l0,
)
from daemon_core.scene_model import (
    SceneItem,
    SceneSnapshot,
    build_scene_sources,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _scene(*, revised_at: float = 100.0) -> SceneSnapshot:
    absorbed_dialogue = '## 2026-07-20T09:00:00 — user\n\nUne présence reste à confirmer.\n'
    source = next(iter(build_scene_sources(recent_dialogue=absorbed_dialogue).values()))
    return SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="trace-1",
        source_turn_started_at=80.0,
        revised_at=revised_at,
        revision=3,
        provider={"model": "terra"},
        dialogue_watermark_source_id=source.source_id,
        dialogue_watermark_locator=source.locator,
        reading="Une transition proche organise la situation.",
        conversation="L'échange reste ouvert.",
        items=(
            SceneItem(
                item_id="scene-1",
                text='Une présence reste à confirmer.',
                epistemic="uncertain",
                sources=(source.source_id,),
            ),
        ),
        sources=(source,),
    )


def test_exact_atlas_preserves_catalog_boundaries_and_excludes_cold_memory(tmp_path):
    _write(
        tmp_path / "capsules" / "index.md",
        "\n".join(
            [
                "- theme/alpha: Alpha exacte — détails A",
                "- theme/expired: Ancienne — ne plus utiliser",
                "- theme/chroniques/2026-W01: Récit passé",
            ]
        ),
    )
    _write(
        tmp_path / "capsules" / "theme" / "alpha.md",
        "---\nsubjects:\n  - Alpha\nfacets:\n  - exactitude\n---\n\n# Alpha\n\n- Fait A.",
    )
    _write(
        tmp_path / "capsules" / "theme" / "expired.md",
        "# Ancienne\nvalid_until: 2026-07-18\n\n- Fait ancien.",
    )
    _write(
        tmp_path / "capsules" / "theme" / "chroniques" / "2026-W01.md",
        "# Récit passé",
    )

    atlas = build_exact_memory_atlas(tmp_path, today=date(2026, 7, 19))

    assert atlas.souvenir_names == ("theme/alpha",)
    assert atlas.text == "- theme/alpha | subjects: Alpha | facets: exactitude"
    assert "Fait A" not in atlas.text
    assert "détails A" not in atlas.text
    assert atlas.expired_souvenir_names == ("theme/expired",)


def test_filtered_atlas_can_offer_and_read_one_historical_souvenir(tmp_path):
    historical = "theme/chroniques/2026-W01"
    _write(
        tmp_path / "capsules" / f"{historical}.md",
        "---\ntruth_scope: past_only\n---\n\n# Récit passé\n\nFait exact.",
    )

    full = build_exact_memory_atlas(tmp_path)
    filtered = build_exact_memory_atlas(
        tmp_path,
        selected_souvenir_names=(historical,),
    )
    read = read_exact_active_souvenirs([historical], tmp_path)

    assert full.souvenir_names == ()
    assert filtered.souvenir_names == (historical,)
    assert filtered.text == f"- {historical}"
    assert read.souvenir_names == (historical,)
    assert "truth_scope: past_only" in read.text
    assert "Fait exact" in read.text


def test_exact_souvenir_read_is_bounded_and_never_normalizes_guessed_paths(tmp_path):
    _write(tmp_path / "capsules" / "theme" / "alpha.md", "# Alpha\n\n- Fait exact.")

    result = read_exact_active_souvenirs(
        ["theme/alpha", "[[theme/alpha]]", "theme/inconnue"],
        tmp_path,
    )

    assert result.souvenir_names == ("theme/alpha",)
    assert result.rejected_paths == ("[[theme/alpha]]", "theme/inconnue")
    assert "Fait exact" in result.text

    with pytest.raises(ValueError, match="Too many souvenir paths"):
        read_exact_active_souvenirs(
            [f"theme/{index}" for index in range(9)],
            tmp_path,
        )


def test_exact_atlas_and_read_expose_one_sourced_open_collection(tmp_path):
    source = "levels/L0_ground_zero/conversations/example#turn-1"
    _write(
        tmp_path / "levels" / "L0_ground_zero" / "conversations" / "example.md",
        "# L0\n\n## turn-1\n\nObjets sauvés.\n",
    )
    _write(
        tmp_path / "capsules" / "theme" / "famille" / "demenagement.md",
        "---\ncollection:\n  coverage: open\n  source_refs:\n"
        f"    - '[[{source}]]'\n---\n# Déménagement\n\n- ordinateur rétro sauvé.\n",
    )
    _write(
        tmp_path / "capsules" / "theme" / "loisirs" / "jdr.md",
        "# JDR\n\n- Manuels anciens.\n\n## Relations\n"
        "- member_of [[theme/famille/demenagement]] : objet à sauver\n"
        f"  - source: [[{source}]]\n",
    )
    _write(
        tmp_path / "capsules" / "theme" / "loisirs" / "oasis.md",
        "# Oasis\n\n- Coffrets collector.\n\n## Relations\n"
        "- member_of [[theme/famille/demenagement]] : objet sauvé\n"
        f"  - source: [[{source}]]\n",
    )
    _write(
        tmp_path / "capsules" / "theme" / "restaurants" / "oasis.md",
        "# Restaurant Oasis\n\n- Une adresse sans rapport.\n",
    )

    atlas = build_exact_memory_atlas(tmp_path)
    exact = read_exact_active_souvenirs(
        ["theme/famille/demenagement"],
        tmp_path,
        collection_path="theme/famille/demenagement",
    )

    assert (
        "theme/famille/demenagement | collection: open (2 known members)"
        in atlas.text
    )
    assert atlas.collection_names == ("theme/famille/demenagement",)
    assert exact.souvenir_names == (
        "theme/famille/demenagement",
        "theme/loisirs/jdr",
        "theme/loisirs/oasis",
    )
    assert "theme/restaurants/oasis" not in exact.souvenir_names
    assert exact.collection_page is not None
    assert exact.collection_page.coverage == "open"
    assert exact.collection_page.known_members_complete is True
    assert exact.collection_page.claimed_exhaustive is False
    assert 'never present this list as exhaustive' in (
        collection_coverage_checkpoint(exact.collection_page)
    )


def test_collection_read_requires_center_in_paths_and_preserves_bound(tmp_path):
    _write(
        tmp_path / "capsules" / "theme" / "episode.md",
        "---\ncollection:\n  coverage: open\n---\n# Episode\n",
    )

    with pytest.raises(ValueError, match="also be present"):
        read_exact_active_souvenirs(
            ["theme/other"],
            tmp_path,
            collection_path="theme/episode",
        )

    with pytest.raises(ValueError, match="No bounded souvenir slot"):
        read_exact_active_souvenirs(
            ["theme/episode", *[f"theme/{index}" for index in range(7)]],
            tmp_path,
            collection_path="theme/episode",
        )


def test_common_ground_canary_joins_atlas_and_exact_read_for_telegram(
    tmp_path,
    monkeypatch,
):
    path = "common_ground/shared/" + "a" * 64
    content = """---
subjects:
  - atelier nocturne
facets:
  - shared-language
---

# Épisode Exact

Une origine vérifiée.
"""
    monkeypatch.setenv("DAEMON_CORE_COMMON_GROUND_LIBRARY", "canary")
    monkeypatch.setenv(
        "DAEMON_CORE_COMMON_GROUND_LIBRARY_USER_IDS",
        "person-1",
    )
    monkeypatch.setenv(
        "DAEMON_CORE_COMMON_GROUND_LIBRARY_PLATFORMS",
        "telegram",
    )
    monkeypatch.setattr(
        held_atlas,
        "resolve_common_ground_library",
        lambda **_kwargs: SimpleNamespace(
            library_entries=((path, content),),
            rejections=(),
        ),
    )

    atlas = build_exact_memory_atlas(
        tmp_path,
        user_id="person-1",
        platform="telegram",
    )
    exact = read_exact_active_souvenirs(
        [path],
        tmp_path,
        user_id="person-1",
        platform="telegram",
    )
    openclaw_atlas = build_exact_memory_atlas(
        tmp_path,
        user_id="person-1",
        platform="openclaw",
    )

    assert atlas.common_ground_names == (path,)
    assert "subjects: atelier nocturne" in atlas.text
    assert exact.souvenirs == ((path, content),)
    assert openclaw_atlas.common_ground_names == ()
    assert openclaw_atlas.text == ""


def test_common_ground_failure_preserves_the_ordinary_library(
    tmp_path,
    monkeypatch,
    caplog,
):
    _write(
        tmp_path / "capsules" / "theme" / "alpha.md",
        "---\nsubjects:\n  - Alpha\n---\n\n# Alpha\n\n- Fait exact.",
    )
    monkeypatch.setenv("DAEMON_CORE_COMMON_GROUND_LIBRARY", "canary")
    monkeypatch.setenv(
        "DAEMON_CORE_COMMON_GROUND_LIBRARY_USER_IDS",
        "person-1",
    )
    monkeypatch.setattr(
        held_atlas,
        "resolve_common_ground_library",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("broken")),
    )

    atlas = build_exact_memory_atlas(
        tmp_path,
        user_id="person-1",
        platform="telegram",
    )

    assert atlas.souvenir_names == ("theme/alpha",)
    assert atlas.common_ground_names == ()
    assert "Common Ground library unavailable" in caplog.text


def test_scene_coverage_preserves_the_voice_judgment_without_inferring_in_code():
    coverage = parse_scene_coverage(
        {
            "scene": {
                "established": ["Alex et Sam partent ensemble ce soir."],
                "uncertain": ["La présence des parents au repas n'est pas établie."],
                "need": "Connaître les goûts des personnes certainement concernées.",
                "handling": {
                    "mode": "clarify",
                    "plan": "Est-ce que tes parents dîneront aussi avec vous ?",
                },
            }
        }
    )

    assert coverage.established == ("Alex et Sam partent ensemble ce soir.",)
    assert coverage.uncertain == (
        "La présence des parents au repas n'est pas établie.",
    )
    assert coverage.as_dict()["need"].startswith("Connaître les goûts")
    assert coverage.handling_mode == "clarify"
    assert coverage.as_dict()["handling"]["plan"].startswith("Est-ce que")
    assert 'natural question' in scene_coverage_checkpoint(coverage)
    assert "Est-ce que tes parents dîneront aussi avec vous ?" in (
        scene_coverage_checkpoint(coverage)
    )
    assert 'neither choose nor commit to any branch' in (
        scene_coverage_checkpoint(coverage)
    )
    assert 'wait without composing those choices' in (
        scene_coverage_checkpoint(coverage)
    )
    assert 'the unknown neither downgrades nor erases the established fact' in (
        scene_coverage_checkpoint(coverage)
    )


def test_scene_coverage_requires_a_structured_model_judgment():
    with pytest.raises(ValueError, match="structured scene assessment"):
        parse_scene_coverage({"paths": ["theme/preferences/alimentaires"]})

    with pytest.raises(ValueError, match="evidence need this read must resolve"):
        parse_scene_coverage(
            {"scene": {"established": [], "uncertain": [], "need": ""}}
        )

    provisional = parse_scene_coverage(
        {
            "scene": {
                "established": [],
                "uncertain": ["Le destinataire exact reste ouvert."],
                "need": "Lire ses préférences.",
                "handling": {"mode": "direct", "plan": "Répondre directement."},
            }
        }
    )

    assert provisional.handling_mode == "direct"
    assert 'proposed before reading does not resolve' in scene_coverage_checkpoint(
        provisional
    )
    assert 'clarify or condition' in scene_coverage_checkpoint(provisional)


def test_scene_coverage_condition_precedes_the_dependent_proposal():
    coverage = parse_scene_coverage(
        {
            "scene": {
                "established": ["Le trajet peut être préparé sans réservation."],
                "uncertain": ["L'heure exacte de départ reste ouverte."],
                "need": "Lire les contraintes de trajet.",
                "handling": {
                    "mode": "condition",
                    "plan": "Si vous partez après 18 h, prévoyez une marge supplémentaire.",
                },
            }
        }
    )

    checkpoint = scene_coverage_checkpoint(coverage)
    assert 'begin with this condition' in checkpoint
    assert 'If the exact bodies resolve' in checkpoint
    assert 'With no material unknown' in checkpoint


def test_scene_coverage_offer_commits_the_model_authored_topic_invitation():
    coverage = parse_scene_coverage(
        {
            "scene": {
                "established": ["Le sujet précédent est clos, mais l'échange reste ouvert."],
                "uncertain": [],
                "need": "Trouver un autre terrain concret.",
                "handling": {
                    "mode": "offer",
                    "plan": "J'ai une piste sur les mondes miniatures : ça t'intéresse ?",
                },
            }
        }
    )

    checkpoint = scene_coverage_checkpoint(coverage)
    assert coverage.handling_mode == "offer"
    assert 'Now respond with exactly' in checkpoint
    assert "J'ai une piste sur les mondes miniatures" in checkpoint
    assert 'Add no elaboration' in checkpoint

    provisional_offer = parse_scene_coverage(
        {
            "scene": {
                "established": [],
                "uncertain": ["Une personne concernée reste inconnue."],
                "need": "Choisir un terrain.",
                "handling": {
                    "mode": "offer",
                    "plan": "J'ai une piste : ça t'intéresse ?",
                },
            }
        }
    )

    assert 'proposed before reading does not resolve' in scene_coverage_checkpoint(
        provisional_offer
    )


def test_library_preflight_selects_complementary_unread_souvenirs():
    class Provider:
        def complete(self, messages, temperature=0.0):
            assert "quoi d'autre" in messages[1]["content"]
            assert "theme/musique/oasis" in messages[1]["content"]
            return (
                '{"paths":["theme/jeux/manuels", "theme/musique/oasis"],'
                '"reason":"Les manuels peuvent compléter l’objet déjà cité.",'
                '"confidence":0.96}'
            )

    result = judge_unread_souvenirs_before_l0(
        "Quels objets avait-il sauvés, quoi d'autre après Oasis ?",
        ExactMemoryAtlas(
            text=(
                "- theme/musique/oasis | subjects: Alex | facets: musique\n"
                "- theme/jeux/manuels | subjects: Alex | facets: jeux de rôle"
            ),
            souvenir_names=("theme/musique/oasis", "theme/jeux/manuels"),
            rendered_chars=140,
            sha256="atlas",
        ),
        ["theme/musique/oasis"],
        Provider(),
    )

    assert result.paths == ("theme/jeux/manuels",)
    assert result.confidence == 0.96


def test_current_scene_model_keeps_exact_delta_and_rejects_future_snapshot(monkeypatch):
    absorbed = '## 2026-07-20T09:00:00 — user\n\nUne présence reste à confirmer.\n'
    stale_time = datetime.fromtimestamp(110).isoformat(timespec="seconds")
    monkeypatch.setattr(
        "daemon_core.scene_model.read_scene_model",
        lambda *_args: _scene(revised_at=100.0),
    )

    current = inspect_current_scene_model(
        "garden",
        "telegram__garden",
        absorbed + f"\n## {stale_time} — assistant\n\nRéponse plus récente",
        message_received_at=120.0,
    )

    assert current.usable is True
    assert current.snapshot.revision == 3
    assert "Réponse plus récente" in current.delta
    assert 'Une présence reste à confirmer' not in current.delta
    future = inspect_current_scene_model(
        "garden",
        "telegram__garden",
        "",
        message_received_at=90.0,
    )
    assert future.status == "future_revision"
    assert future.usable is False


def test_scene_inspection_keeps_an_unprovable_gap_distinct_from_absence(monkeypatch):
    monkeypatch.setattr(
        "daemon_core.scene_model.read_scene_model",
        lambda *_args: _scene(revised_at=100.0),
    )

    view = inspect_current_scene_model(
        "garden",
        "telegram__garden",
        "## 2026-07-20T10:00:00 — assistant\n\nWatermark absent.",
        message_received_at=120.0,
    )

    assert view.status == "unprovable_delta"
    assert view.snapshot is not None
    assert view.delta is None
    assert view.usable is False


def test_live_composer_extends_only_the_thin_exact_dialogue_horizon(monkeypatch):
    seen = []
    monkeypatch.setattr(
        held_atlas,
        "load_recent_dialogue",
        lambda *_args, **kwargs: seen.append(kwargs["max_turns"]) or "",
    )
    monkeypatch.setattr(held_atlas, "load_user_agenda", lambda *_args: "")
    monkeypatch.setattr(held_atlas, "load_user_context", lambda *_args: "")
    monkeypatch.setattr(held_atlas, "load_active_context", lambda *_args: "")
    monkeypatch.setattr(held_atlas, "load_conversation_state", lambda *_args: "")
    monkeypatch.setattr(held_atlas, "load_immediate_focus", lambda *_args: "")
    monkeypatch.setattr(held_atlas, "load_daemon_persona", lambda *_args: "Persona.")
    monkeypatch.setattr(held_atlas, "load_conversation_manner", lambda *_args: "")
    monkeypatch.setattr(
        held_atlas,
        "build_exact_memory_atlas",
        lambda *_args, **_kwargs: ExactMemoryAtlas("", (), 0, "atlas"),
    )
    monkeypatch.setattr(
        held_atlas,
        "read_exact_active_souvenirs",
        lambda *_args, **_kwargs: ExactSouvenirRead((), (), 0),
    )

    compose_canonical_voice_messages("garden", "Et maintenant ?")

    assert seen[0] == CANONICAL_HISTORY_TURNS
    assert CANONICAL_HISTORY_TURNS == 24


def test_live_composer_uses_thin_voice_with_a_fresh_scene(monkeypatch):
    snapshot = _scene(revised_at=100.0)
    absorbed = '## 2026-07-20T09:00:00 — user\n\nUne présence reste à confirmer.\n'
    assistant_time = datetime.fromtimestamp(95).isoformat(timespec="seconds")
    dialogue = absorbed + f"\n## {assistant_time} — assistant\n\nRéponse exacte"
    for name, value in {
        "DAEMON_CORE_SOVEREIGN_VOICE": "on",
        "DAEMON_CORE_SOVEREIGN_MODE": "delegate",
        "DAEMON_CORE_SOVEREIGN_PLATFORMS": "telegram",
        "DAEMON_CORE_SINGLE_PASS": "on",
        "DAEMON_CORE_SINGLE_PASS_USER_IDS": "garden",
        "DAEMON_CORE_SOVEREIGN_LIBRARY": "on",
        "DAEMON_CORE_SOVEREIGN_LIBRARY_USER_IDS": "garden",
        "DAEMON_CORE_THIN_SOVEREIGN_VOICE": "on",
        "DAEMON_CORE_THIN_SOVEREIGN_USER_IDS": "garden",
        "DAEMON_CORE_SCENE_MODEL": "canary",
        "DAEMON_CORE_SCENE_MODEL_USER_IDS": "garden",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "daemon_core.scene_model.read_scene_model",
        lambda *_args: snapshot,
    )
    monkeypatch.setattr(
        held_atlas,
        "load_recent_dialogue",
        lambda *_args, **_kwargs: dialogue,
    )
    monkeypatch.setattr(held_atlas, "load_user_agenda", lambda *_args: "")
    monkeypatch.setattr(held_atlas, "load_daemon_persona", lambda *_args: "Persona.")
    monkeypatch.setattr(held_atlas, "load_conversation_manner", lambda *_args: "Manière.")
    monkeypatch.setattr(
        held_atlas,
        "load_user_context",
        lambda *_args: "- Travaille généralement à distance en semaine.",
    )
    monkeypatch.setattr(
        held_atlas,
        "load_active_context",
        lambda *_args: "- La mission actuelle reste prioritaire aujourd'hui.",
    )
    for loader in (
        "load_trajectory",
        "load_conversation_state",
    ):
        monkeypatch.setattr(
            held_atlas,
            loader,
            lambda *_args, _loader=loader: pytest.fail(
                f"{_loader} must stay outside the thin prompt"
            ),
            raising=False,  # Also guards retired loader re-exports.
        )
    monkeypatch.setattr(
        held_atlas,
        "read_held_thought",
        lambda *_args, **_kwargs: pytest.fail(
            "held thought must stay outside the thin prompt"
        ),
        raising=False,  # Keep the guard for the retired API.
    )
    monkeypatch.setattr(
        held_atlas,
        "build_exact_memory_atlas",
        lambda *_args, **_kwargs: ExactMemoryAtlas(
            "- theme/a", ("theme/a",), 9, "atlas",
            common_ground_names=("common_ground/shared/a",),
        ),
    )
    monkeypatch.setattr(
        held_atlas,
        "read_exact_active_souvenirs",
        lambda *_args, **_kwargs: ExactSouvenirRead((), (), 0),
    )

    messages, context = compose_canonical_voice_messages(
        "garden",
        "Et maintenant ?",
        message_received_at=120.0,
    )

    rendered = "\n".join(message["content"] for message in messages)
    assert context.prompt_variant == "voice_brief_reference"
    assert context.scene_model is snapshot
    assert messages[0]["content"].startswith('# Identity')
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "system",
        "user",
    ]
    assert messages[-2]["content"].startswith('# Turn threshold')
    assert "Réponse exacte" in rendered
    assert 'Une présence reste à confirmer' in rendered
    assert "sources connectées" not in rendered
    assert "Travaille généralement à distance en semaine." in rendered
    assert '# Retained current reference points' in rendered
    assert "La mission actuelle reste prioritaire aujourd'hui." in rendered
    assert "# Etat vivant au seuil du tour" not in rendered
    assert '# Common Ground reading threshold' in messages[0]["content"]
    assert "the origin, earlier meaning" in messages[0]["content"]


def test_live_composer_uses_a_bounded_thin_seed_without_a_scene(monkeypatch):
    dialogue = (
        "## 2026-07-21T06:44:29 — assistant\n\nBon voyage.\n\n"
        "## 2026-07-25T08:27:40 — user\n\nSalut toi"
    )
    for name, value in {
        "DAEMON_CORE_SOVEREIGN_VOICE": "on",
        "DAEMON_CORE_SOVEREIGN_MODE": "delegate",
        "DAEMON_CORE_SOVEREIGN_PLATFORMS": "telegram",
        "DAEMON_CORE_SINGLE_PASS": "on",
        "DAEMON_CORE_SINGLE_PASS_USER_IDS": "garden",
        "DAEMON_CORE_SOVEREIGN_LIBRARY": "on",
        "DAEMON_CORE_SOVEREIGN_LIBRARY_USER_IDS": "garden",
        "DAEMON_CORE_THIN_SOVEREIGN_VOICE": "on",
        "DAEMON_CORE_THIN_SOVEREIGN_USER_IDS": "garden",
        "DAEMON_CORE_SCENE_MODEL": "canary",
        "DAEMON_CORE_SCENE_MODEL_USER_IDS": "garden",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "daemon_core.scene_model.read_scene_model",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        held_atlas,
        "load_recent_dialogue",
        lambda *_args, **_kwargs: dialogue,
    )
    monkeypatch.setattr(
        held_atlas,
        "load_user_agenda",
        lambda *_args: (
            "# Agenda\n\n"
            "- 2026-07-26 | Alex | rappeler les billets retour | journée"
        ),
    )
    monkeypatch.setattr(
        held_atlas,
        "load_active_context",
        lambda *_args: (
            "# Contexte actif\n\n"
            "- Départ du 21 juillet avec Sam.\n\n"
            "## Dernière mise à jour\n\n2026-07-21"
        ),
    )
    monkeypatch.setattr(
        held_atlas,
        "load_conversation_state",
        lambda *_args: "La checklist du départ était le dernier fil.",
    )
    monkeypatch.setattr(held_atlas, "load_daemon_persona", lambda *_args: "Persona.")
    monkeypatch.setattr(
        held_atlas,
        "load_conversation_manner",
        lambda *_args: "Manière.",
    )
    monkeypatch.setattr(
        held_atlas,
        "load_user_context",
        lambda *_args: "- Travaille généralement à distance en semaine.",
    )
    for loader in (
        "load_trajectory",
        "load_immediate_focus",
    ):
        monkeypatch.setattr(
            held_atlas,
            loader,
            lambda *_args, _loader=loader: pytest.fail(
                f"{_loader} must stay outside the thin seed"
            ),
            raising=False,
        )
    monkeypatch.setattr(
        held_atlas,
        "read_current_held_thought",
        lambda *_args, **_kwargs: pytest.fail(
            "held thought must not become a parallel authority"
        ),
        raising=False,  # Keep the guard for the retired API.
    )
    monkeypatch.setattr(
        held_atlas,
        "build_exact_memory_atlas",
        lambda *_args, **_kwargs: ExactMemoryAtlas(
            "- theme/voyages/retour | subjects: Alex | facets: trajet",
            ("theme/voyages/retour",),
            62,
            "atlas",
        ),
    )
    monkeypatch.setattr(
        held_atlas,
        "read_exact_active_souvenirs",
        lambda names, *_args, **_kwargs: (
            ExactSouvenirRead((), (), 0)
            if not names
            else pytest.fail("a cold seed must not preload stale hot memories")
        ),
    )

    messages, context = compose_canonical_voice_messages(
        "garden",
        "Salut toi",
        message_received_at=datetime(
            2026, 7, 25, 8, 27, 40, tzinfo=ZoneInfo("Europe/Paris")
        ).timestamp(),
    )

    rendered = "\n".join(message["content"] for message in messages)
    assert context.prompt_variant == "voice_brief_reference"
    assert context.situation_source == "seed"
    assert context.scene_model is None
    assert context.held_thought is None
    assert context.hot_memory.souvenir_names == ()
    assert '# Situation at the turn threshold' in rendered
    assert "No materialized scene model" in rendered
    assert 'Dated and revisable active context' in rendered
    assert "2026-07-21" in rendered
    assert "rappeler les billets retour" in rendered
    assert '4 days, 1 hour, 43 minutes, 11 seconds' in rendered
    assert "theme/voyages/retour" in rendered
    assert "Travaille généralement à distance en semaine." in rendered
    assert "Memories absent from this seed remain available for consultation" in rendered
    assert "Last sync:" not in rendered
    assert "change franchement de centre" not in rendered
    assert "# Pensée tenue" not in rendered
