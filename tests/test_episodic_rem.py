import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.episodic_rem import (  # noqa: E402
    EPISODIC_REM_ENV,
    EPISODIC_REM_USER_IDS_ENV,
    EpisodicState,
    episode_state_path,
    episodic_rem_enabled,
    read_episode_state,
    resolve_selected_episode,
    run_episodic_rem,
    write_episode_state,
)
from daemon_core.providers.base import LLMProvider  # noqa: E402


NOW = datetime(2026, 8, 26, 22, 0, 0)
START = datetime(2026, 8, 26, 18, 0, 0)


class EpisodicProvider(LLMProvider):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages = []
        self.calls = 0

    def complete(self, messages: list[dict], temperature: float = 0.0) -> str:
        self.messages = messages
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


def _memory(tmp_path: Path) -> Path:
    root = tmp_path / "memory"
    (root / "capsules").mkdir(parents=True)
    (root / "capsules" / "index.md").write_text("", encoding="utf-8")
    return root


def _write_l0(root: Path) -> None:
    l0 = root / "levels" / "L0_ground_zero" / "conversations"
    l0.mkdir(parents=True)
    (l0 / "garden_2026-08-26.md").write_text(
        "## 2026-08-26T19:00:00 — user\n\n"
        "J'ai découvert la forme réelle du chantier aujourd'hui.\n\n"
        "## 2026-08-26T19:01:00 — assistant\n\n"
        "Le décor vient de se préciser.\n\n"
        "## 2026-08-26T19:02:00 — user\n\n"
        "Le périmètre bouge, je vais commencer prudemment.\n\n"
        "## 2026-08-26T20:00:00 — user\n\n"
        "J'ai lancé le morceau dans la playlist.\n\n"
        "## 2026-08-26T20:01:00 — assistant\n\n"
        "La playlist devient un endroit où revenir.\n\n"
        "## 2026-08-26T20:02:00 — user\n\n"
        "Oui, notre atelier musical.\n\n"
        "## 2026-08-26T21:00:00 — user\n\n"
        "Bonne soirée.\n",
        encoding="utf-8",
    )


def _scene_payload() -> dict:
    return {
        "reason": "Deux scènes distinctes.",
        "revisit_from": None,
        "scenes": [
            {
                "title": "Le chantier prend sa forme réelle",
                "subjects": ["Utilisateur", "Travail"],
                "facets": ["premier cadrage", "incertitude"],
                "established": [
                    {
                        "claim": "L'utilisateur a découvert la forme réelle du chantier.",
                        "source_refs": ["2026-08-26T19:00:00"],
                    },
                    {
                        "claim": "Le périmètre bouge et il veut commencer prudemment.",
                        "source_refs": ["2026-08-26T19:02:00"],
                    },
                ],
                "open_readings": [
                    {
                        "reading": "Le décor du travail vient de se préciser.",
                        "status": "open",
                        "source_refs": ["2026-08-26T19:01:00"],
                    }
                ],
                "future_value": "Éclairer une évolution future du chantier.",
                "anchors": ["chantier", "périmètre mouvant", "prudence"],
                "uncertainties": ["La suite effective n'est pas encore connue."],
            },
            {
                "title": "La playlist devient un atelier partagé",
                "subjects": ["Utilisateur", "Iris"],
                "facets": ["musique", "création partagée"],
                "established": [
                    {
                        "claim": "L'utilisateur a lancé un morceau dans la playlist.",
                        "source_refs": ["2026-08-26T20:00:00"],
                    },
                    {
                        "claim": "Il a qualifié la playlist d'atelier musical partagé.",
                        "source_refs": ["2026-08-26T20:02:00"],
                    },
                ],
                "open_readings": [
                    {
                        "reading": "La playlist peut être un endroit où revenir.",
                        "status": "partly_confirmed",
                        "source_refs": ["2026-08-26T20:01:00"],
                    }
                ],
                "future_value": "Reconnaître l'origine de l'atelier musical partagé.",
                "anchors": ["playlist", "atelier musical", "endroit où revenir"],
                "uncertainties": [],
            },
        ],
    }


def _prepare(root: Path) -> None:
    _write_l0(root)
    write_episode_state("garden", EpisodicState(START), memory_root=root)


def test_episodic_rem_writes_sourced_obsidian_scenes_and_advances(
    tmp_path,
    monkeypatch,
):
    root = _memory(tmp_path)
    _prepare(root)
    monkeypatch.delenv(EPISODIC_REM_ENV, raising=False)
    provider = EpisodicProvider(_scene_payload())

    result = run_episodic_rem(
        "garden", provider, memory_root=root, now=NOW, force=True
    )

    assert result["status"] == "ecrite"
    assert result["scene_count"] == 2
    assert len(result["written"]) == 2
    paths = [root / "capsules" / f"{name}.md" for name in result["episodes"]]
    assert all(path.is_file() for path in paths)
    first = paths[0].read_text(encoding="utf-8")
    assert "memory_type: episodic_scene" in first
    assert "truth_scope: past_only" in first
    assert "## Établi" in first
    assert "\n## Lectures ouvertes\n" in first
    assert "Iris" not in first
    assert (
        "[[levels/L0_ground_zero/conversations/garden_2026-08-26.md#"
        "2026-08-26T19:00:00 — user]]"
    ) in first
    index = (root / "capsules" / "index.md").read_text(encoding="utf-8")
    assert index.count("- theme/scenes_episodiques/2026/08/") == 2
    assert "- theme/scenes_episodiques: Sourced past episodic scenes" in index
    state = read_episode_state("garden", memory_root=root, now=NOW)
    assert state.assessed_through == datetime(2026, 8, 26, 21, 0)
    assert state.revisit_from is None
    assert 'zero to 3 independent scenes' in provider.messages[0]["content"]


def test_episodic_rem_abstention_advances_without_creating_scene(tmp_path):
    root = _memory(tmp_path)
    _prepare(root)
    provider = EpisodicProvider(
        {"reason": "Rien d'épisodique.", "scenes": [], "revisit_from": None}
    )

    result = run_episodic_rem(
        "garden", provider, memory_root=root, now=NOW, force=True
    )

    assert result["status"] == "abstention"
    assert not (root / "capsules" / "theme" / "scenes_episodiques").exists()
    state = read_episode_state("garden", memory_root=root, now=NOW)
    assert state.assessed_through == datetime(2026, 8, 26, 21, 0)


def test_episodic_rem_rejects_wrong_role_without_advancing(tmp_path):
    root = _memory(tmp_path)
    _prepare(root)
    payload = _scene_payload()
    payload["scenes"][0]["established"][0]["source_refs"] = [
        "2026-08-26T19:01:00"
    ]

    with pytest.raises(ValueError, match="exact user turn"):
        run_episodic_rem(
            "garden",
            EpisodicProvider(payload),
            memory_root=root,
            now=NOW,
            force=True,
        )

    state = read_episode_state("garden", memory_root=root, now=NOW)
    assert state.assessed_through == START
    assert not (root / "capsules" / "theme" / "scenes_episodiques").exists()


def test_episodic_rem_replay_is_idempotent_and_repairs_index(tmp_path):
    root = _memory(tmp_path)
    _prepare(root)
    first = run_episodic_rem(
        "garden",
        EpisodicProvider(_scene_payload()),
        memory_root=root,
        now=NOW,
        force=True,
    )
    (root / "capsules" / "index.md").write_text("", encoding="utf-8")
    write_episode_state("garden", EpisodicState(START), memory_root=root)

    second = run_episodic_rem(
        "garden",
        EpisodicProvider(_scene_payload()),
        memory_root=root,
        now=NOW,
        force=True,
    )

    assert second["status"] == "deja-evaluee"
    assert second["written"] == []
    assert second["episodes"] == first["episodes"]
    files = list(
        (root / "capsules" / "theme" / "scenes_episodiques").rglob("*.md")
    )
    assert len(files) == 2
    index = (root / "capsules" / "index.md").read_text(encoding="utf-8")
    assert index.count("- theme/scenes_episodiques/2026/08/") == 2


def test_episode_identity_does_not_depend_on_optional_open_reading(tmp_path):
    root = _memory(tmp_path)
    _prepare(root)
    first = run_episodic_rem(
        "garden",
        EpisodicProvider(_scene_payload()),
        memory_root=root,
        now=NOW,
        force=True,
    )
    write_episode_state("garden", EpisodicState(START), memory_root=root)
    revised = _scene_payload()
    revised["scenes"][0]["open_readings"] = []

    second = run_episodic_rem(
        "garden",
        EpisodicProvider(revised),
        memory_root=root,
        now=NOW,
        force=True,
    )

    assert second["episodes"][0] == first["episodes"][0]
    files = list(
        (root / "capsules" / "theme" / "scenes_episodiques").rglob("*.md")
    )
    assert len(files) == 2


def test_saturated_pass_advances_only_through_last_written_scene(tmp_path):
    root = _memory(tmp_path)
    _prepare(root)
    payload = _scene_payload()
    payload["scenes"].append(
        {
            "title": "Une troisième scène bornée",
            "subjects": ["Utilisateur"],
            "facets": ["transition"],
            "established": [
                {
                    "claim": "Le chantier a pris une forme réelle.",
                    "source_refs": ["2026-08-26T19:00:00"],
                },
                {
                    "claim": "Un morceau a rejoint la playlist.",
                    "source_refs": ["2026-08-26T20:00:00"],
                },
            ],
            "open_readings": [],
            "future_value": "Conserver cette transition.",
            "anchors": ["chantier", "morceau", "transition"],
            "uncertainties": [],
        }
    )

    result = run_episodic_rem(
        "garden",
        EpisodicProvider(payload),
        memory_root=root,
        now=NOW,
        force=True,
    )

    assert result["scene_count"] == 3
    assert result["window_end"] == "2026-08-26T21:00:00"
    assert result["assessed_through"] == "2026-08-26T20:02:00"


def test_episodic_rem_dry_run_writes_nothing(tmp_path):
    root = _memory(tmp_path)
    _prepare(root)
    state_before = episode_state_path("garden", root).read_text(encoding="utf-8")

    result = run_episodic_rem(
        "garden",
        EpisodicProvider(_scene_payload()),
        memory_root=root,
        now=NOW,
        force=True,
        dry_run=True,
    )

    assert result["status"] == "dry-run"
    assert len(result["previews"]) == 2
    assert episode_state_path("garden", root).read_text(encoding="utf-8") == state_before
    assert not (root / "capsules" / "theme" / "scenes_episodiques").exists()


def test_episodic_rem_keeps_only_a_bounded_revisit(tmp_path):
    root = _memory(tmp_path)
    _prepare(root)
    payload = {
        "reason": "Épisode ouvert.",
        "scenes": [],
        "revisit_from": "2026-08-26T19:00:00",
    }

    result = run_episodic_rem(
        "garden",
        EpisodicProvider(payload),
        memory_root=root,
        now=NOW,
        force=True,
    )

    assert result["status"] == "abstention"
    state = read_episode_state("garden", memory_root=root, now=NOW)
    assert state.assessed_through == datetime(2026, 8, 26, 21, 0)
    assert state.revisit_from == datetime(2026, 8, 26, 19, 0)


def test_episode_canary_is_user_scoped(monkeypatch):
    monkeypatch.setenv(EPISODIC_REM_ENV, "canary")
    monkeypatch.setenv(EPISODIC_REM_USER_IDS_ENV, "garden")

    assert episodic_rem_enabled("garden") is True
    assert episodic_rem_enabled("other") is False


def test_selected_episode_must_lead_and_have_past_boundary(tmp_path):
    root = _memory(tmp_path)
    path = (
        root
        / "capsules"
        / "theme"
        / "scenes_episodiques"
        / "2026"
        / "08"
        / "scene.md"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nmemory_type: episodic_scene\ntruth_scope: past_only\n---\n\n"
        "# Une scène\n\n## Établi\n\n- Fait passé.\n",
        encoding="utf-8",
    )

    assert resolve_selected_episode(
        ("theme/other", "theme/scenes_episodiques/2026/08/scene"), memory_root=root
    ) == ("", "")
    name, body = resolve_selected_episode(
        ("theme/scenes_episodiques/2026/08/scene", "theme/other"), memory_root=root
    )
    assert name == "theme/scenes_episodiques/2026/08/scene"
    assert body.startswith("# Une scène")


def test_episode_revisit_older_than_bound_is_rejected(tmp_path):
    root = _memory(tmp_path)
    _prepare(root)
    old = START - timedelta(minutes=1)
    payload = {
        "reason": "Épisode ouvert.",
        "scenes": [],
        "revisit_from": old.isoformat(),
    }

    with pytest.raises(ValueError, match="exact L0 turn"):
        run_episodic_rem(
            "garden",
            EpisodicProvider(payload),
            memory_root=root,
            now=NOW,
            force=True,
        )
