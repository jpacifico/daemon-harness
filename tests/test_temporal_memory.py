import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.providers.base import LLMProvider
from daemon_core.temporal_memory import run_temporal_curator, temporal_curator_enabled


class StaticProvider(LLMProvider):
    def __init__(self, result: dict):
        self.result = result
        self.messages = None

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages = messages
        result = dict(self.result)
        result.setdefault("source_posture", "direct_fact")
        return json.dumps(result, ensure_ascii=False)


class SequencedProvider(LLMProvider):
    def __init__(self, *results: dict):
        self.results = list(results)
        self.messages = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages.append(messages)
        result = dict(self.results.pop(0))
        if "should_update" in result:
            result.setdefault("source_posture", "direct_fact")
        return json.dumps(result, ensure_ascii=False)


def _patch_memory_root(monkeypatch, memory_root: Path) -> None:
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)


@pytest.mark.parametrize(
    "message,dialogue",
    [
        ("Le cours commence le 8 septembre.", "Nous parlions du violoncelle."),
        ("The lesson starts on September 8.", "We were talking about the cello."),
    ],
)
def test_translated_curator_preserves_exact_inputs_and_output_schema(message, dialogue):
    from daemon_core.temporal_memory import _build_temporal_curator_messages

    agenda = "# Agenda\n\n- Chaque mardi | Alex | violoncelle, sauf vacances"
    messages = _build_temporal_curator_messages(agenda, dialogue, message)
    assert [item["role"] for item in messages] == ["system", "user"]
    for exact in (agenda, dialogue, message):
        assert messages[1]["content"].count(exact) == 1
    schema = json.loads(messages[0]["content"].rsplit("\n", 1)[-1])
    assert set(schema) == {
        "source_posture", "should_update", "agenda_md", "removed_existing_lines", "reason",
    }
    assert schema["source_posture"] == "direct_fact|reported_memory_claim|none"
    assert schema["should_update"] is False
    assert schema["agenda_md"] is None
    assert set(schema["removed_existing_lines"][0]) == {"line", "evidence"}
    assert "`En cours`" in messages[0]["content"]


def test_temporal_curator_keeps_distinct_travel_legs(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    _patch_memory_root(monkeypatch, memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    sunday = "- 2026-07-19 | Alex et Sam | retour à Lyon | soir"
    burgundy = "- 2026-07-21 au 2026-07-24 | Alex | séjour en région voisine"
    agenda.write_text(f"# Agenda\n\n{sunday}\n{burgundy}\n", encoding="utf-8")
    provider = StaticProvider(
        {
            "should_update": True,
            "agenda_md": (
                f"# Agenda\n\n{sunday}\n{burgundy}\n"
                "- 2026-07-24 | Alex | retour de région voisine | vendredi"
            ),
            "removed_existing_lines": [],
            "reason": "Une seconde étape de retour est ajoutée sans écraser la première.",
        }
    )

    result = run_temporal_curator("garden", "Nous rentrerons vendredi.", provider)

    assert result.updated is True
    saved = agenda.read_text(encoding="utf-8")
    assert sunday in saved
    assert burgundy in saved
    assert "retour de région voisine" in saved
    assert "several nearby events" in provider.messages[0]["content"]
    assert "several dates" in provider.messages[0]["content"]
    assert "entire period" in provider.messages[0]["content"]


def test_temporal_curator_refuses_unjustified_agenda_loss(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    _patch_memory_root(monkeypatch, memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    sunday = "- 2026-07-19 | Alex et Sam | retour à Lyon | soir"
    agenda.write_text(f"# Agenda\n\n{sunday}\n", encoding="utf-8")
    provider = StaticProvider(
        {
            "should_update": True,
            "agenda_md": "# Agenda\n\n- 2026-07-24 | Alex | retour | vendredi",
            "removed_existing_lines": [],
            "reason": "Remplacement erroné.",
        }
    )

    result = run_temporal_curator("garden", "Nous rentrerons vendredi.", provider)

    assert result.updated is False
    assert "perte d'agenda non autorisée" in result.reason
    assert sunday in agenda.read_text(encoding="utf-8")


def test_temporal_curator_refuses_new_unbounded_agenda_memory(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _patch_memory_root(monkeypatch, memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    existing = "- 2026-08-21 | Alex | travailler pour Acme | journée"
    agenda.write_text(f"# Agenda\n\n{existing}\n", encoding="utf-8")
    invalid = (
        "- En cours | Alex et Sam | brûlerie | "
        "le café est bon et Sam aime le chocolat"
    )
    provider = StaticProvider(
        {
            "should_update": True,
            "agenda_md": f"# Agenda\n\n{existing}\n{invalid}",
            "removed_existing_lines": [],
            "reason": "Mauvais classement d'un souvenir durable.",
        }
    )

    result = run_temporal_curator(
        "garden",
        "Le café est bon et Sam aime leur chocolat.",
        provider,
    )

    assert result.updated is False
    assert "horizon d'agenda non opérationnel" in result.reason
    assert invalid not in agenda.read_text(encoding="utf-8")
    assert existing in agenda.read_text(encoding="utf-8")
    assert "`En cours`" in provider.messages[0]["content"]


def test_temporal_curator_accepts_evidenced_precision_of_existing_line(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _patch_memory_root(monkeypatch, memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    original = "- 2026-07-19 | retour à Lyon | horaire du vol à préciser"
    updated = "- 2026-07-19 | retour à Lyon | vol à 21h"
    agenda.write_text(f"# Agenda\n\n{original}\n", encoding="utf-8")
    provider = SequencedProvider(
        {
            "should_update": True,
            "agenda_md": f"# Agenda\n\n{updated}",
            "removed_existing_lines": [
                {"line": original, "evidence": "Le vol est prévu pour 21h"}
            ],
            "reason": "L'horaire est précisé.",
        },
        {
            "judgments": [
                {
                    "line": original,
                    "authorized": True,
                    "evidence": "Le vol est prévu pour 21h",
                    "reason": "Le dernier message précise explicitement l'horaire.",
                }
            ]
        },
    )

    result = run_temporal_curator("garden", "Le vol est prévu pour 21h.", provider)

    assert result.updated is True
    assert updated in agenda.read_text(encoding="utf-8")
    curator_system = provider.messages[0][0]["content"]
    guard_system = provider.messages[1][0]["content"]
    assert "change only that component" in curator_system
    assert "preserve the rest" in curator_system
    assert "preserving every independent, uncorrected clause" in guard_system
    assert "does not need to repeat the entire old line" in guard_system
    assert "can still be true together" in guard_system


def test_temporal_curator_accepts_monotonic_enrichment_of_same_event(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _patch_memory_root(monkeypatch, memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    original = (
        "- 2026-08-13 après-midi | Alex | atelier Client | report confirmé"
    )
    enriched = (
        "- 2026-08-13 après-midi | Alex | atelier Client (Projet Orion) | "
        "report confirmé"
    )
    evidence = "Projet Orion est le nom du projet pour cet atelier Client"
    agenda.write_text(f"# Agenda\n\n{original}\n", encoding="utf-8")
    provider = SequencedProvider(
        {
            "should_update": True,
            "agenda_md": f"# Agenda\n\n{enriched}",
            "removed_existing_lines": [
                {"line": original, "evidence": evidence}
            ],
            "reason": "Le même événement reçoit un intitulé supplémentaire.",
        },
        {
            "judgments": [
                {
                    "line": original,
                    "authorized": True,
                    "evidence": evidence,
                    "reason": (
                        "La proposition conserve chaque fait existant et ajoute "
                        "une précision directement établie."
                    ),
                }
            ]
        },
    )

    result = run_temporal_curator(
        "garden",
        f"Je te l'avais déjà précisé : {evidence}.",
        provider,
    )

    assert result.updated is True
    assert result.source_posture == "direct_fact"
    assert agenda.read_text(encoding="utf-8").strip() == (
        f"# Agenda\n\n{enriched}"
    )
    assert len(provider.messages) == 2
    curator_system = provider.messages[0][0]["content"]
    guard_system = provider.messages[1][0]["content"]
    assert "A is B" in curator_system
    assert "Judge the asserted content, not its preamble" in curator_system
    assert "monotonic enrichment" in guard_system


def test_temporal_curator_accepts_explicit_scope_correction_when_rest_is_preserved(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _patch_memory_root(monkeypatch, memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    original = "- Du 3 au 31 août 2026 | Mission Beta | rythme à temps plein"
    updated = "- Du 3 au 6 août 2026 | Mission Beta | rythme à temps plein"
    agenda.write_text(f"# Agenda\n\n{original}\n", encoding="utf-8")
    evidence = "Il se termine donc définitivement le jeudi 6 août 2026"
    provider = SequencedProvider(
        {
            "should_update": True,
            "agenda_md": f"# Agenda\n\n{updated}",
            "removed_existing_lines": [{"line": original, "evidence": evidence}],
            "reason": "La borne de fin est corrigée.",
        },
        {
            "judgments": [
                {
                    "line": original,
                    "authorized": True,
                    "evidence": f"« {evidence} »",
                    "reason": "La nouvelle borne est explicite et le reste est préservé.",
                }
            ]
        },
    )

    result = run_temporal_curator(
        "garden",
        (
            "Correction : le rythme prévu du 3 au 31 août est annulé après le "
            "jeudi 6 août. Il se termine donc définitivement le jeudi 6 août 2026."
        ),
        provider,
    )

    assert result.updated is True
    assert agenda.read_text(encoding="utf-8").strip() == "# Agenda\n\n- Du 2026-08-03 au 2026-08-06 | Mission Beta | rythme à temps plein"


def test_temporal_curator_still_refuses_a_non_literal_quoted_evidence(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _patch_memory_root(monkeypatch, memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    original = "- Du 3 au 31 août 2026 | Mission Beta | rythme à temps plein"
    updated = "- Du 3 au 6 août 2026 | Mission Beta | rythme à temps plein"
    agenda.write_text(f"# Agenda\n\n{original}\n", encoding="utf-8")
    provider = SequencedProvider(
        {
            "should_update": True,
            "agenda_md": f"# Agenda\n\n{updated}",
            "removed_existing_lines": [
                {
                    "line": original,
                    "evidence": "« Le rythme se termine le 6 août »",
                }
            ],
            "reason": "La date est supposée corrigée.",
        }
    )

    result = run_temporal_curator(
        "garden",
        "En fait, cela pourrait peut-être se terminer au début du mois.",
        provider,
    )

    assert result.updated is False
    assert "perte d'agenda non autorisée" in result.reason
    assert agenda.read_text(encoding="utf-8").strip() == f"# Agenda\n\n{original}"


def test_temporal_curator_refuses_ambiguous_narrowing_of_a_broader_fact(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _patch_memory_root(monkeypatch, memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    original = "- Août 2026 | Mission Beta | rythme à temps plein, à revoir en septembre"
    narrowed = "- Jusqu'au jeudi 2026-08-06 | Mission Beta | temps plein"
    agenda.write_text(f"# Agenda\n\n{original}\n", encoding="utf-8")
    provider = SequencedProvider(
        {
            "should_update": True,
            "agenda_md": f"# Agenda\n\n{narrowed}",
            "removed_existing_lines": [
                {
                    "line": original,
                    "evidence": "Surtout Mission Beta full time avant jeudi",
                }
            ],
            "reason": "La priorité avant jeudi est prise pour une nouvelle limite.",
        },
        {
            "judgments": [
                {
                    "line": original,
                    "authorized": False,
                    "evidence": "Surtout Mission Beta full time avant jeudi",
                    "reason": "Le message décrit une priorité locale, pas la fin du rythme mensuel.",
                }
            ]
        },
    )

    result = run_temporal_curator(
        "garden",
        "Surtout Mission Beta full time avant jeudi.",
        provider,
    )

    assert result.updated is False
    assert "non explicitement établie" in result.reason
    assert result.rejected_paths == ("users/garden/agenda.md",)
    assert agenda.read_text(encoding="utf-8").strip() == f"# Agenda\n\n{original}"
    guard_system = provider.messages[1][0]["content"]
    assert "if the old line and the latest message can still be true" in guard_system


def test_temporal_curator_does_not_rewrite_identical_agenda(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    _patch_memory_root(monkeypatch, memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    content = "# Agenda\n\n- Samedi | déjeuner familial"
    agenda.write_text(content + "\n", encoding="utf-8")
    provider = StaticProvider(
        {
            "should_update": True,
            "agenda_md": content,
            "removed_existing_lines": [],
            "reason": "Aucun changement matériel.",
        }
    )
    before = agenda.stat().st_mtime_ns

    result = run_temporal_curator("garden", "Le déjeuner est samedi.", provider)

    assert result.updated is False
    assert result.unchanged_paths == ("users/garden/agenda.md",)
    assert agenda.stat().st_mtime_ns == before


def test_temporal_curator_does_not_promote_a_reported_memory_claim(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _patch_memory_root(monkeypatch, memory_root)
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    original = "- 2026-08-05 | présenter le backlog | horaire non précisé"
    agenda.write_text(f"# Agenda\n\n{original}\n", encoding="utf-8")
    provider = StaticProvider(
        {
            "source_posture": "reported_memory_claim",
            "should_update": True,
            "agenda_md": (
                "# Agenda\n\n"
                "- 2026-08-05 matin | présenter le backlog | matin"
            ),
            "removed_existing_lines": [
                {
                    "line": original,
                    "evidence": "Je t'ai déjà dit que c'était demain matin",
                }
            ],
            "reason": "Le message demande de vérifier une parole passée.",
        }
    )

    result = run_temporal_curator(
        "garden",
        "Je t'ai déjà dit que c'était demain matin, revérifie.",
        provider,
    )

    assert result.updated is False
    assert agenda.read_text(encoding="utf-8").strip() == f"# Agenda\n\n{original}"
    assert "reported_memory_claim" in provider.messages[0]["content"]


def test_temporal_curator_is_user_scoped(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_TEMPORAL_CURATOR", "on")
    monkeypatch.setenv("DAEMON_CORE_TEMPORAL_CURATOR_USER_IDS", "garden")

    assert temporal_curator_enabled("garden") is True
    assert temporal_curator_enabled("other") is False


def test_temporal_curator_does_not_inherit_retired_single_pass_scope(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_TEMPORAL_CURATOR", "on")
    monkeypatch.delenv("DAEMON_CORE_TEMPORAL_CURATOR_USER_IDS", raising=False)
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS_USER_IDS", "garden")

    assert temporal_curator_enabled("garden") is False


@pytest.mark.parametrize('written_date', ['14 November 2026', 'November 14, 2026', '14 novembre 2026', '2026-11-14T09:30:00+01:00'])
def test_curator_persists_iso_then_projects_the_exact_day(tmp_path, monkeypatch, written_date):
    from datetime import datetime
    from daemon_core.near_term_timeline import compile_near_term_timeline
    _patch_memory_root(monkeypatch, tmp_path)
    provider = StaticProvider({'should_update': True,
        'agenda_md': '# Agenda\n\n- ' + written_date + ' | Alex | Room Cedar booked',
        'removed_existing_lines': []})
    message = 'Room Cedar is booked for ' + written_date
    result = run_temporal_curator('date-test', message, provider, recent_dialogue='')
    assert result.updated and not result.rejected_paths
    saved = (tmp_path / 'users/date-test/agenda.md').read_text()
    assert '2026-11-14' in saved
    assert message in provider.messages[1]['content']
    timeline = compile_near_term_timeline(now=datetime(2026, 11, 14), personal_agenda=saved)
    assert timeline.entries[0].starts_on == timeline.entries[0].ends_on == '2026-11-14'
