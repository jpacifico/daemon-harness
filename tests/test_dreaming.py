import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.continuity import record_continuity_stage, technical_ref
from daemon_core.dreaming import (
    apply_dreaming_report_file,
    apply_dreaming_result,
    latest_dreaming_report,
    load_l0_window,
    load_memory_snapshot,
    load_memory_mutation_evidence,
    run_dreaming,
    save_dreaming_report,
)
from daemon_core.memory.writer import append_ground_zero
from daemon_core.providers.base import LLMProvider


class DreamingProvider(LLMProvider):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages = messages
        return json.dumps(self.payload)


class NonJsonProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.calls += 1
        return "Réponse de test du Daemon Harness. Configure LLM_PROVIDER=openai ou local pour utiliser un vrai modèle."


class SequencedProvider(LLMProvider):
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.messages: list[list[dict]] = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages.append(messages)
        return self.outputs[len(self.messages) - 1]


def _record_memory_mutation(
    *,
    user_id: str,
    turn_id: str,
    user_text: str,
    source_started_at: float,
    changed_paths: list[str],
    context_paths: list[str] | None = None,
) -> None:
    user_ref = append_ground_zero(
        user_id,
        "user",
        user_text,
        turn_id=turn_id,
        return_reference=True,
    )
    assistant_ref = append_ground_zero(
        user_id,
        "assistant",
        "Accusé de traitement.",
        turn_id=turn_id,
        return_reference=True,
    )
    record_continuity_stage(
        turn_id,
        "memory_mutation",
        "completed",
        reason_code="memory_files_changed",
        user_ref=technical_ref(user_id),
        source_turn_started_at=source_started_at,
        changed_paths=changed_paths,
        context_paths=context_paths or [],
        user_l0_ref=user_ref.as_dict(),
        assistant_l0_ref=assistant_ref.as_dict(),
    )


def test_load_l0_window_reads_last_24h_across_dates(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    l0_root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0_root.mkdir(parents=True)
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    (l0_root / "garden_2026-05-14.md").write_text(
        "\n## 2026-05-14T09:00:00 — user\n\nTrop ancien.\n"
        "\n## 2026-05-14T11:00:00 — assistant\n\nDans la fenêtre hier.\n",
        encoding="utf-8",
    )
    (l0_root / "garden_2026-05-15.md").write_text(
        "\n## 2026-05-15T09:30:00 — user\n\nDans la fenêtre aujourd'hui.\n",
        encoding="utf-8",
    )

    text = load_l0_window("garden", since_hours=24, now=datetime(2026, 5, 15, 10, 0, 0))

    assert "Trop ancien" not in text
    assert "Dans la fenêtre hier" in text
    assert "Dans la fenêtre aujourd'hui" in text


def test_load_l0_window_accepts_mixed_local_and_offset_timestamps(
    monkeypatch,
    tmp_path,
):
    memory_root = tmp_path / "memory"
    l0_root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0_root.mkdir(parents=True)
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    (l0_root / "garden_2026-05-14.md").write_text(
        "\n## 2026-05-14T12:00:00+00:00 — user\n\nEntrée avec fuseau.\n",
        encoding="utf-8",
    )
    (l0_root / "garden_2026-05-15.md").write_text(
        "\n## 2026-05-15T10:00:00 — assistant\n\nEntrée locale.\n",
        encoding="utf-8",
    )

    text = load_l0_window(
        "garden",
        since_hours=48,
        now=datetime(2026, 5, 15, 12, 0, 0),
    )

    assert "Entrée avec fuseau" in text
    assert "Entrée locale" in text


def test_load_l0_window_keeps_complete_48h_above_30k(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    l0_root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0_root.mkdir(parents=True)
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    body = "x" * 31_000
    (l0_root / "garden_2026-05-14.md").write_text(
        "\n## 2026-05-14T12:00:00 — user\n\nPREMIERE_ENTREE_48H.\n"
        f"\n## 2026-05-14T13:00:00 — assistant\n\n{body}\n",
        encoding="utf-8",
    )

    text = load_l0_window(
        "garden",
        since_hours=48,
        now=datetime(2026, 5, 15, 10, 0, 0),
    )

    assert len(text) > 30_000
    assert "PREMIERE_ENTREE_48H" in text


def test_load_l0_window_honors_an_explicit_limit(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    l0_root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0_root.mkdir(parents=True)
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    (l0_root / "garden_2026-05-15.md").write_text(
        "\n## 2026-05-15T09:00:00 — user\n\n" + ("x" * 500) + "\n",
        encoding="utf-8",
    )

    text = load_l0_window(
        "garden",
        since_hours=24,
        now=datetime(2026, 5, 15, 10, 0, 0),
        max_chars=80,
    )

    assert len(text) == 80


def test_run_dreaming_dry_run_does_not_write(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    user_root = memory_root / "users" / "garden"
    user_root.mkdir(parents=True)
    context = user_root / "context.md"
    context.write_text("# Context\n\n- Ancien fait.\n", encoding="utf-8")
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    provider = DreamingProvider(
        {
            "reason": "Oubli évident.",
            "confidence": 0.9,
            "report_md": "Ajout proposé.",
            "updates": [
                {
                    "path": "memory/users/garden/context.md",
                    "action": "update",
                    "reason": "Ajouter le fait récent.",
                    "content_md": "# Context\n\n- Ancien fait.\n- Nouveau fait.\n",
                }
            ],
        }
    )

    result = run_dreaming("garden", provider=provider, dry_run=True)

    assert result.dry_run is True
    assert result.updates[0].applied is False
    assert context.read_text(encoding="utf-8") == "# Context\n\n- Ancien fait.\n"
    assert "Ancien fait" in provider.messages[1]["content"]


def test_dreaming_cannot_read_or_mutate_rem_episodes(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    episode = (
        memory_root
        / "capsules"
        / "theme"
        / "scenes_episodiques"
        / "2026"
        / "scene.md"
    )
    regular = memory_root / "capsules" / "theme" / "travail" / "projet.md"
    episode.parent.mkdir(parents=True)
    regular.parent.mkdir(parents=True)
    episode.write_text("# SCENE_REM_SECRETE\n", encoding="utf-8")
    regular.write_text("# CAPSULE_ORDINAIRE\n", encoding="utf-8")
    index = memory_root / "capsules" / "index.md"
    index.write_text(
        "- theme/scenes_episodiques/2026/scene: Ligne REM intacte\n"
        "- theme/travail/projet: Projet\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    snapshot = load_memory_snapshot("garden")
    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={
            "updates": [
                {
                    "path": "memory/capsules/theme/scenes_episodiques/2026/scene.md",
                    "action": "update",
                    "reason": "Tentative interdite.",
                    "content_md": "# SCENE_REM_MODIFIEE",
                },
                {
                    "path": "memory/capsules/index.md",
                    "action": "update",
                    "reason": "Tentative de dilution.",
                    "content_md": "- theme/travail/projet: Projet\n",
                },
            ]
        },
    )

    assert "CAPSULE_ORDINAIRE" in snapshot
    assert "SCENE_REM_SECRETE" not in snapshot
    assert episode.read_text(encoding="utf-8") == "# SCENE_REM_SECRETE\n"
    assert "Ligne REM intacte" in index.read_text(encoding="utf-8")
    assert len(result.rejected_updates) == 2


def test_dreaming_cannot_read_or_rewrite_archived_preferences(
    monkeypatch,
    tmp_path,
):
    memory_root = tmp_path / "memory"
    user_root = memory_root / "users" / "garden"
    user_root.mkdir(parents=True)
    preferences = user_root / "preferences.md"
    preferences.write_text(
        "# Manuel historique\n\n- TRACE_ARCHIVEE.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    provider = DreamingProvider(
        {
            "reason": "Aucune réparation.",
            "confidence": 1.0,
            "report_md": "Aucun changement.",
            "updates": [],
        }
    )

    run_dreaming("garden", provider=provider, dry_run=True)

    prompt = "\n".join(message["content"] for message in provider.messages)
    assert "TRACE_ARCHIVEE" not in prompt
    assert "memory/users/<user_id>/preferences.md" not in prompt

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={
            "updates": [
                {
                    "path": "memory/users/garden/preferences.md",
                    "action": "update",
                    "reason": "Tentative de réécriture.",
                    "content_md": "# Manuel remplacé\n",
                }
            ]
        },
    )

    assert result.updates == []
    assert len(result.rejected_updates) == 1
    assert "TRACE_ARCHIVEE" in preferences.read_text(encoding="utf-8")


def test_run_dreaming_targets_recent_mutations_with_prior_l0_provenance(
    monkeypatch,
    tmp_path,
):
    import daemon_core.dreaming as dreaming
    import daemon_core.memory.writer as writer

    memory_root = tmp_path / "memory"
    receipt_root = tmp_path / "receipts"
    user_root = memory_root / "users" / "garden"
    user_root.mkdir(parents=True)
    (user_root / "agenda.md").write_text(
        '# Agenda\n\n- Août | rythme confirmé.\n',
        encoding="utf-8",
    )
    souvenir_root = memory_root / "capsules" / "theme"
    souvenir_root.mkdir(parents=True)
    (souvenir_root / "target.md").write_text(
        "# Capsule ciblée\n\n- Fait ciblé.",
        encoding="utf-8",
    )
    (souvenir_root / "unrelated.md").write_text(
        "# Capsule étrangère\n\n- Fait qui ne doit pas être chargé.",
        encoding="utf-8",
    )
    monkeypatch.setattr(dreaming, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(writer, "MEMORY_ROOT", memory_root)
    monkeypatch.setenv("DAEMON_CORE_CONTINUITY_RECEIPT_DIR", str(receipt_root))
    now = datetime.now()

    _record_memory_mutation(
        user_id="garden",
        turn_id="old-proof",
        user_text="La règle large reste valable.",
        source_started_at=(now - timedelta(hours=48)).timestamp(),
        changed_paths=["capsules/theme/target.md"],
        context_paths=["capsules/theme/target.md"],
    )
    _record_memory_mutation(
        user_id="garden",
        turn_id="recent-write",
        user_text="Cette semaine a une priorité locale.",
        source_started_at=(now - timedelta(hours=1)).timestamp(),
        changed_paths=["capsules/theme/target.md"],
        context_paths=["capsules/theme/target.md"],
    )
    provider = DreamingProvider(
        {
            "reason": "Vérification ciblée.",
            "confidence": 0.8,
            "report_md": "Aucune correction nécessaire.",
            "unresolved_conflicts": [],
            "updates": [],
        }
    )

    result = run_dreaming(
        "garden",
        provider=provider,
        since_hours=24,
        dry_run=True,
        now=now,
    )

    prompt = provider.messages[1]["content"]
    assert "## agenda.md" in prompt
    assert '## Turn old-proof' in prompt
    assert '## Turn recent-write' in prompt
    assert "La règle large reste valable" in prompt
    assert "Cette semaine a une priorité locale" in prompt
    assert "# Capsule ciblée" in prompt
    assert "Fait qui ne doit pas être chargé" not in prompt
    assert result.input_summary["continuity_journals"] == 1
    assert result.input_summary["mutation_evidence_events"] == 2
    assert result.input_summary["targeted_memory_paths"] == 1
    assert result.input_summary["l0_chars"] > 0
    assert (
        result.input_summary["prompt_chars"]
        > result.input_summary["memory_snapshot_chars"]
    )


def test_dreaming_reads_mutations_from_multiple_receipt_journals(
    monkeypatch,
    tmp_path,
):
    import daemon_core.dreaming as dreaming
    import daemon_core.memory.writer as writer

    memory_root = tmp_path / "memory"
    source_receipt_roots = [
        tmp_path / "hermes-receipts",
        tmp_path / "openclaw-receipts",
    ]
    monkeypatch.setattr(dreaming, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(writer, "MEMORY_ROOT", memory_root)
    monkeypatch.setenv(
        "DREAMING_CONTINUITY_RECEIPT_DIRS",
        os.pathsep.join(str(path) for path in source_receipt_roots),
    )
    now = datetime.now()

    for index, receipt_root in enumerate(source_receipt_roots):
        monkeypatch.setenv(
            "DAEMON_CORE_CONTINUITY_RECEIPT_DIR",
            str(receipt_root),
        )
        turn_id = f"source-{index}"
        _record_memory_mutation(
            user_id="garden",
            turn_id=turn_id,
            user_text=f"Fait source {index}.",
            source_started_at=(now - timedelta(hours=1)).timestamp() + index,
            changed_paths=[f"capsules/theme/source_{index}.md"],
        )

    hermes_journal = source_receipt_roots[0] / "continuity_receipts.jsonl"
    openclaw_journal = source_receipt_roots[1] / "continuity_receipts.jsonl"
    duplicate_event = json.loads(
        next(
            line
            for line in hermes_journal.read_text(encoding="utf-8").splitlines()
            if '"stage":"memory_mutation"' in line
        )
    )
    duplicate_event["at"] = "2099-01-01T00:00:00.000Z"
    with openclaw_journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(duplicate_event) + "\n")

    evidence = load_memory_mutation_evidence(
        "garden",
        since_hours=48,
        now=now,
    )

    assert len(evidence) == 2
    assert {item.trace_id for item in evidence} == {"source-0", "source-1"}
    assert {path for item in evidence for path in item.changed_paths} == {
        "capsules/theme/source_0.md",
        "capsules/theme/source_1.md",
    }


@pytest.mark.parametrize("failure", ["missing", "invalid"])
def test_dreaming_refuses_a_partial_explicit_receipt_set(
    monkeypatch,
    tmp_path,
    failure,
):
    valid_root = tmp_path / "valid-receipts"
    invalid_root = tmp_path / "invalid-receipts"
    valid_root.mkdir()
    (valid_root / "continuity_receipts.jsonl").write_text("", encoding="utf-8")
    if failure == "invalid":
        invalid_root.mkdir()
        (invalid_root / "continuity_receipts.jsonl").write_text(
            "{invalid-json}\n",
            encoding="utf-8",
        )
    monkeypatch.setenv(
        "DREAMING_CONTINUITY_RECEIPT_DIRS",
        os.pathsep.join((str(valid_root), str(invalid_root))),
    )

    with pytest.raises(RuntimeError, match="Configured Dreaming continuity"):
        load_memory_mutation_evidence("garden")


def test_explicit_receipt_dir_keeps_historical_tolerant_read(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv(
        "DREAMING_CONTINUITY_RECEIPT_DIRS",
        os.pathsep.join((str(tmp_path / "host-a"), str(tmp_path / "host-b"))),
    )

    evidence = load_memory_mutation_evidence(
        "garden",
        receipt_dir=tmp_path / "historical-single-journal",
    )

    assert evidence == []


def test_dreaming_keeps_every_recently_modified_path_beyond_40_events(
    monkeypatch,
    tmp_path,
):
    import daemon_core.dreaming as dreaming
    import daemon_core.memory.writer as writer

    memory_root = tmp_path / "memory"
    receipt_root = tmp_path / "receipts"
    monkeypatch.setattr(dreaming, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(writer, "MEMORY_ROOT", memory_root)
    monkeypatch.setenv(
        "DAEMON_CORE_CONTINUITY_RECEIPT_DIR",
        str(receipt_root),
    )
    now = datetime.now()
    for index in range(41):
        turn_id = f"turn-{index:02d}"
        _record_memory_mutation(
            user_id="garden",
            turn_id=turn_id,
            user_text=f"Fait {index}.",
            source_started_at=(now - timedelta(hours=1)).timestamp() + index,
            changed_paths=[f"capsules/theme/capsule_{index:02d}.md"],
        )

    evidence = load_memory_mutation_evidence(
        "garden",
        since_hours=48,
        now=now,
    )

    changed_paths = {
        path for item in evidence for path in item.changed_paths
    }
    assert len(evidence) == 41
    assert changed_paths == {
        f"capsules/theme/capsule_{index:02d}.md" for index in range(41)
    }


def test_targeted_snapshot_keeps_every_explicitly_modified_souvenir(
    monkeypatch,
    tmp_path,
):
    import daemon_core.dreaming as dreaming

    memory_root = tmp_path / "memory"
    souvenir_root = memory_root / "capsules" / "theme"
    souvenir_root.mkdir(parents=True)
    targeted_paths = set()
    for index in range(dreaming.MAX_SOUVENIRS_IN_PROMPT + 1):
        relative = f"capsules/theme/capsule_{index:03d}.md"
        targeted_paths.add(relative)
        content = f"# Capsule {index:03d}\n"
        if index == 0:
            content += ("x" * 2_300) + "\nFIN_CAPSULE_CIBLEE\n"
        (memory_root / relative).write_text(
            content,
            encoding="utf-8",
        )
    monkeypatch.setattr(dreaming, "MEMORY_ROOT", memory_root)

    snapshot = dreaming._snapshot_theme_souvenirs(targeted_paths)

    assert snapshot.count("### memory/capsules/theme/") == len(targeted_paths)
    assert "# Capsule 080" in snapshot
    assert "FIN_CAPSULE_CIBLEE" in snapshot

    general_snapshot = dreaming._snapshot_theme_souvenirs()
    assert general_snapshot.count("### memory/capsules/theme/") == 80
    assert "# Capsule 080" not in general_snapshot
    assert "FIN_CAPSULE_CIBLEE" not in general_snapshot
    assert "...[truncated]" in general_snapshot


def test_run_dreaming_returns_diagnostic_when_provider_is_not_json(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    provider = NonJsonProvider()

    result = run_dreaming("garden", provider=provider, dry_run=True)

    assert result.updates == []
    assert result.rejected_updates[0].reason == "Réponse provider non JSON."
    assert "provider n'a pas retourné de JSON strict" in result.reason
    assert "Réponse de test" in result.report_md
    assert "Tentatives : 2" in result.report_md
    assert "Taille de la dernière sortie" in result.report_md
    assert provider.calls == 2


def test_run_dreaming_retries_one_malformed_envelope_without_applying_it(
    monkeypatch,
    tmp_path,
):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    provider = SequencedProvider(
        [
            '{"reason":"sortie interrompue",',
            json.dumps(
                {
                    "reason": "Aucune correction nécessaire.",
                    "confidence": 0.9,
                    "report_md": "La mémoire est cohérente.",
                    "updates": [],
                }
            ),
        ]
    )

    result = run_dreaming("garden", provider=provider, dry_run=True)

    assert result.reason == "Aucune correction nécessaire."
    assert result.rejected_updates == []
    assert len(provider.messages) == 2
    assert provider.messages[0][-1]["role"] == "user"
    assert provider.messages[1][-1]["role"] == "user"
    assert 'strict JSON object' in provider.messages[1][-1]["content"]
    assert any("récupérée au second essai" in note for note in result.review_notes)


def test_dreaming_cli_exits_nonzero_for_non_json_provider(
    monkeypatch,
    tmp_path,
):
    import daemon_core.dreaming as dreaming
    import daemon_core.registre as registre

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(dreaming, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(registre, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(
        dreaming,
        "build_memory_provider",
        lambda: NonJsonProvider(),
    )
    monkeypatch.setattr(
        dreaming,
        "_load_dotenv_if_available",
        lambda: None,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dreaming",
            "--user-id",
            "garden",
            "--no-save-report",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        dreaming.main()

    assert raised.value.code == 2
    assert registre.needs_retry("dreaming", "garden") is True


def test_apply_dreaming_result_writes_only_when_apply(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    user_root = memory_root / "users" / "garden"
    user_root.mkdir(parents=True)
    focus = user_root / "focus_immediat.md"
    focus.write_text("# Focus immédiat\n\n- Ancien fil.\n", encoding="utf-8")
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={
            "reason": "Correction.",
            "confidence": 0.8,
            "report_md": "Focus ajusté.",
            "updates": [
                {
                    "path": "memory/users/garden/focus_immediat.md",
                    "action": "update",
                    "reason": "Fil corrigé.",
                    "content_md": "# Focus immédiat\n\n- Fil corrigé.\n",
                }
            ],
        },
    )

    assert result.updates[0].applied is True
    assert "Fil corrigé" in focus.read_text(encoding="utf-8")


def test_dreaming_cannot_write_the_temporal_curator_agenda(
    monkeypatch,
    tmp_path,
):
    memory_root = tmp_path / "memory"
    agenda = memory_root / "users" / "garden" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    original = "# Agenda\n\n- 2026-08-30 | Alex | rendez-vous\n"
    agenda.write_text(original, encoding="utf-8")
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={
            "updates": [
                {
                    "path": "memory/users/garden/agenda.md",
                    "action": "update",
                    "reason": "Tentative de réécriture nocturne.",
                    "content_md": (
                        original
                        + "- En cours | Alex | souvenir sans horizon\n"
                    ),
                }
            ]
        },
    )

    assert result.updates == []
    assert "non autorisé" in result.rejected_updates[0].reason
    assert agenda.read_text(encoding="utf-8") == original


def test_dreaming_prompt_assigns_agenda_exclusively_to_temporal_curator():
    from daemon_core.dreaming import build_dreaming_messages

    messages = build_dreaming_messages(
        user_id="garden",
        since_hours=24,
        l0_recent="",
        memory_snapshot="",
    )
    system = messages[0]["content"]

    assert 'belongs exclusively to Temporal Curator' in system
    allowed = system.split('Allowed paths:', 1)[1].split(
        '- Always return', 1
    )[0]
    assert "agenda.md" not in allowed


def test_dreaming_prewarms_once_after_all_applied_souvenir_updates(
    monkeypatch,
    tmp_path,
):
    import daemon_core.dreaming as dreaming
    import daemon_core.semantic_atlas as semantic_atlas

    memory_root = tmp_path / "memory"
    souvenirs = memory_root / "capsules" / "theme"
    souvenirs.mkdir(parents=True)
    for name in ("alpha", "beta"):
        (souvenirs / f"{name}.md").write_text(
            f"# {name}\n\n## Faits\n- Ancien fait.\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(dreaming, "MEMORY_ROOT", memory_root)
    observed = {"requests": [], "flushes": 0}

    class Prewarmer:
        def request(self, names):
            observed["requests"].append(tuple(names))

        def flush_if_pending(self, *, force=False):
            assert force is True
            observed["flushes"] += 1

    monkeypatch.setattr(
        semantic_atlas,
        "coalesced_semantic_atlas_prewarmer",
        lambda user_id, root: Prewarmer(),
    )

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={
            "updates": [
                {
                    "path": f"memory/capsules/theme/{name}.md",
                    "action": "update",
                    "reason": "Révision.",
                    "content_md": (
                        f"# {name}\n\n## Faits\n- Ancien fait.\n- Nouveau fait.\n"
                    ),
                }
                for name in ("alpha", "beta")
            ]
        },
    )

    assert all(update.applied for update in result.updates[:2])
    assert observed == {
        "requests": [("theme/alpha", "theme/beta")],
        "flushes": 1,
    }


def test_dreaming_noop_does_not_invalidate_scene_but_real_repair_does(
    monkeypatch,
    tmp_path,
):
    import daemon_core.dreaming as dreaming
    import daemon_core.scene_model as scene_model

    memory_root = tmp_path / "memory"
    user_root = memory_root / "users" / "garden"
    user_root.mkdir(parents=True)
    focus = user_root / "focus_immediat.md"
    original = "# Focus immédiat\n\n- Fil exact."
    focus.write_text(original + "\n", encoding="utf-8")
    monkeypatch.setattr(dreaming, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(scene_model, "MEMORY_ROOT", memory_root)

    noop = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={
            "updates": [
                {
                    "path": "memory/users/garden/focus_immediat.md",
                    "action": "update",
                    "reason": "Contenu déjà exact.",
                    "content_md": original,
                }
            ]
        },
    )

    marker = scene_model.scene_invalidation_path("garden")
    assert noop.updates[0].changed is False
    assert noop.updates[0].applied is False
    assert marker.exists() is False

    repaired = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={
            "updates": [
                {
                    "path": "memory/users/garden/focus_immediat.md",
                    "action": "update",
                    "reason": "Réparation réelle.",
                    "content_md": "# Focus immédiat\n\n- Fil réparé.",
                }
            ]
        },
    )

    assert repaired.updates[0].changed is True
    assert repaired.updates[0].applied is True
    assert marker.exists() is True


def test_dreaming_refuses_to_overwrite_a_file_changed_since_its_snapshot(
    monkeypatch,
    tmp_path,
):
    import daemon_core.dreaming as dreaming
    import daemon_core.scene_model as scene_model

    memory_root = tmp_path / "memory"
    context = memory_root / "users" / "garden" / "context.md"
    context.parent.mkdir(parents=True)
    original = "# Contexte\n\n- État au début du dreaming.\n"
    context.write_text(original, encoding="utf-8")
    base = hashlib.sha256(original.encode("utf-8")).hexdigest()
    context.write_text(
        "# Contexte\n\n- Nouvelle information arrivée pendant le dreaming.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dreaming, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(scene_model, "MEMORY_ROOT", memory_root)

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        expected_revisions={"memory/users/garden/context.md": base},
        data={
            "updates": [
                {
                    "path": "memory/users/garden/context.md",
                    "action": "update",
                    "reason": "Proposition fondée sur l'ancien état.",
                    "content_md": "# Contexte\n\n- Réparation calculée trop tôt.",
                }
            ]
        },
    )

    assert result.updates == []
    assert "modifié depuis la lecture" in result.rejected_updates[0].reason
    assert "Nouvelle information arrivée" in context.read_text(encoding="utf-8")
    assert scene_model.scene_invalidation_path("garden").exists() is False


def test_apply_dreaming_result_rejects_unsafe_paths(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={
            "updates": [
                {"path": "memory/../README.md", "action": "update", "reason": "", "content_md": "no"},
                {"path": "memory/users/other/context.md", "action": "update", "reason": "", "content_md": "no"},
                {"path": "memory/capsules/examples/demo.md", "action": "update", "reason": "", "content_md": "no"},
                {"path": "/tmp/outside.md", "action": "update", "reason": "", "content_md": "no"},
            ]
        },
    )

    assert result.updates == []
    assert len(result.rejected_updates) == 4
    assert not (tmp_path / "README.md").exists()
    assert not (memory_root / "users" / "other" / "context.md").exists()


def test_apply_dreaming_result_rejects_drastic_shrink(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    souvenir = memory_root / "capsules" / "theme" / "loisirs" / "jeux.md"
    souvenir.parent.mkdir(parents=True)
    souvenir.write_text("# Capsule\n\n" + ("- Fait conservé.\n" * 60), encoding="utf-8")
    before = souvenir.read_text(encoding="utf-8")
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={
            "updates": [
                {
                    "path": "memory/capsules/theme/loisirs/jeux.md",
                    "action": "update",
                    "reason": "Résumé trop agressif.",
                    "content_md": "# Capsule\n\n- Résumé bref.\n",
                }
            ]
        },
    )

    assert result.updates == []
    assert result.rejected_updates[0].reason == "Réécriture trop appauvrissante refusée."
    assert souvenir.read_text(encoding="utf-8") == before


def test_apply_dreaming_result_limits_update_count(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    updates = [
        {
            "path": f"memory/capsules/theme/projet/item_{index}.md",
            "action": "create",
            "reason": "Test.",
            "content_md": f"# Capsule {index}\n",
        }
        for index in range(7)
    ]

    result = apply_dreaming_result(user_id="garden", dry_run=True, data={"updates": updates})

    assert len(result.updates) == 6
    assert result.updates[-1].path == "memory/capsules/index.md"
    assert len(result.rejected_updates) == 2
    assert result.rejected_updates[0].reason == "Limite de 5 mises à jour dépassée."


def test_save_dreaming_report_writes_json_and_markdown(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    result = apply_dreaming_result(
        user_id="garden",
        dry_run=True,
        data={
            "reason": "Ajout prudent.",
            "confidence": 0.7,
            "report_md": "Une mise à jour légère est proposée.",
            "updates": [
                {
                    "path": "memory/users/garden/context.md",
                    "action": "update",
                    "reason": "Test.",
                    "content_md": "# Context\n\n- Fait.\n",
                }
            ],
        },
    )
    result.input_summary = {
        "l0_chars": 31_000,
        "continuity_journals": 2,
        "mutation_evidence_events": 4,
        "targeted_memory_paths": 3,
        "memory_snapshot_chars": 12_000,
        "prompt_chars": 50_000,
    }

    json_path, markdown_path = save_dreaming_report(
        result=result,
        user_id="garden",
        since_hours=24,
        now=datetime(2026, 5, 15, 10, 30, 0),
    )

    assert json_path.exists()
    assert markdown_path.exists()
    latest_json_path = memory_root / "levels" / "dreaming" / "latest" / "garden.json"
    latest_markdown_path = memory_root / "levels" / "dreaming" / "latest" / "garden.md"
    assert latest_json_path.exists()
    assert latest_markdown_path.exists()
    assert json_path.name == "garden_20260515T103000_dry-run_24h.json"
    assert markdown_path.name == "garden_20260515T103000_dry-run_24h.md"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert saved["input_summary"] == result.input_summary
    assert "## Entrées Vérifiées" in markdown
    assert "Journaux de continuité lus : `2`" in markdown
    assert saved["report_json_path"].endswith("garden_20260515T103000_dry-run_24h.json")
    assert saved["latest_json_path"].endswith("memory/levels/dreaming/latest/garden.json")
    assert saved["updates"][0]["path"] == "memory/users/garden/context.md"
    markdown = markdown_path.read_text(encoding="utf-8")
    assert latest_json_path.read_text(encoding="utf-8") == json_path.read_text(encoding="utf-8")
    assert latest_markdown_path.read_text(encoding="utf-8") == markdown
    assert "# Rapport Dreaming Quotidien" in markdown
    assert "Mises à jour proposées : `1`" in markdown
    assert "## Mises à jour proposées" in markdown
    assert "`memory/users/garden/context.md`" in markdown
    assert "```diff" in markdown
    assert "+- Fait." in markdown


def test_apply_dreaming_report_file_applies_exact_reviewed_json(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    user_root = memory_root / "users" / "garden"
    user_root.mkdir(parents=True)
    context = user_root / "context.md"
    context.write_text("# Context\n\n- Ancien fait.\n", encoding="utf-8")
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    report_path = tmp_path / "reviewed.json"
    report_path.write_text(
        json.dumps(
            {
                "reason": "Rapport validé.",
                "confidence": 0.9,
                "report_md": "Application exacte du dry-run relu.",
                "updates": [
                    {
                        "path": "memory/users/garden/context.md",
                        "action": "update",
                        "reason": "Ajout validé.",
                        "content_md": "# Context\n\n- Ancien fait.\n- Fait validé.\n",
                        "applied": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = apply_dreaming_report_file(user_id="garden", report_path=report_path)

    assert result.dry_run is False
    assert result.updates[0].applied is True
    assert "Fait validé" in context.read_text(encoding="utf-8")


def test_dreaming_preserves_existing_index_when_llm_line_is_missing(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    souvenir = memory_root / "capsules" / "theme" / "loisirs" / "jeux.md"
    souvenir.parent.mkdir(parents=True)
    souvenir.write_text(
        "# Capsule: theme/loisirs/jeux\nSujet: Jeux de test.\n\n## Faits\n- Ancien fait.\n",
        encoding="utf-8",
    )
    index = memory_root / "capsules" / "index.md"
    index.write_text("- theme/loisirs/jeux: Jeux de test. — Ancien fait\n", encoding="utf-8")
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={
            "updates": [
                {
                    "path": "memory/capsules/theme/loisirs/jeux.md",
                    "action": "update",
                    "reason": "Ajout souvenir.",
                    "content_md": "# Capsule: theme/loisirs/jeux\nSujet: Jeux de test.\n\n## Faits\n- Ancien fait.\n- Nouveau fait.\n",
                }
            ]
        },
    )

    assert result.review_notes == []
    assert [update.path for update in result.updates] == [
        "memory/capsules/theme/loisirs/jeux.md",
    ]
    assert index.read_text(encoding="utf-8") == "- theme/loisirs/jeux: Jeux de test. — Ancien fait\n"


def test_dreaming_prefers_full_souvenir_index_line_from_llm(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    souvenir = memory_root / "capsules" / "theme" / "loisirs" / "playlist.md"
    souvenir.parent.mkdir(parents=True)
    souvenir.write_text(
        "# Playlist\nSujet: Playlist commune.\n\n## Faits\n- Street Spirit.\n",
        encoding="utf-8",
    )
    index = memory_root / "capsules" / "index.md"
    index.write_text(
        "- theme/loisirs/playlist: Playlist commune. — Street Spirit\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={"updates": [{
            "path": "memory/capsules/theme/loisirs/playlist.md",
            "action": "update",
            "reason": "Ajout de morceaux.",
            "content_md": (
                "# Playlist\nSujet: Playlist commune.\n\n## Faits\n"
                "- Street Spirit.\n- High Hopes.\n- Gas Panic!.\n"
            ),
            "index_line": (
                "- theme/loisirs/playlist: Playlist commune — "
                "Street Spirit, High Hopes, Gas Panic!"
            ),
        }]},
    )

    assert index.read_text(encoding="utf-8") == (
        "- theme/loisirs/playlist: Playlist commune — "
        "Street Spirit, High Hopes, Gas Panic!\n"
    )


def test_dreaming_dry_run_keeps_existing_index_without_llm_line(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    souvenir = memory_root / "capsules" / "theme" / "loisirs" / "jeux.md"
    souvenir.parent.mkdir(parents=True)
    souvenir.write_text(
        "# Capsule: theme/loisirs/jeux\nSujet: Jeux de test.\n\n## Faits\n- Ancien fait.\n",
        encoding="utf-8",
    )
    index = memory_root / "capsules" / "index.md"
    index.write_text("- theme/loisirs/jeux: Jeux de test. — Ancien fait\n", encoding="utf-8")
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=True,
        data={
            "updates": [
                {
                    "path": "memory/capsules/theme/loisirs/jeux.md",
                    "action": "update",
                    "reason": "Ajout souvenir.",
                    "content_md": "# Capsule: theme/loisirs/jeux\nSujet: Jeux de test.\n\n## Faits\n- Ancien fait.\n- Nouveau fait.\n",
                }
            ]
        },
    )

    assert result.review_notes == []
    assert len(result.updates) == 1
    assert index.read_text(encoding="utf-8") == "- theme/loisirs/jeux: Jeux de test. — Ancien fait\n"


def _make_souvenir(memory_root: Path, name: str, expire: str | None = None) -> Path:
    souvenir = memory_root / "capsules" / (name + ".md")
    souvenir.parent.mkdir(parents=True, exist_ok=True)
    header = f"# Capsule: {name}\nSujet: test.\nPortée: test.\nHors portée: rien.\n"
    if expire:
        header += f"valid_until: {expire}\n"
    souvenir.write_text(header + "\n## Faits\n- Un fait éphémère.\n", encoding="utf-8")
    index = memory_root / "capsules" / "index.md"
    index.write_text(f"- {name}: souvenir éphémère\n- theme/travail/projet: projet durable\n", encoding="utf-8")
    return souvenir


def test_apply_dreaming_result_archives_expired_souvenir(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    souvenir = _make_souvenir(memory_root, "theme/quotidien/vacances_ete", expire="2026-07-24")
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={"updates": [
            {"path": "memory/capsules/theme/quotidien/vacances_ete.md", "action": "archive", "reason": "valid_until 2026-07-24 dépassé"}
        ]},
    )

    assert result.updates[0].applied is True
    assert not souvenir.exists()  # Removed from active memory.
    archived = memory_root / "archive" / "capsules" / "theme" / "quotidien" / "vacances_ete.md"
    assert archived.exists() and "Un fait éphémère" in archived.read_text(encoding="utf-8")  # Preserved.
    index = (memory_root / "capsules" / "index.md").read_text(encoding="utf-8")
    assert "vacances_ete" not in index  # Index entry removed.
    assert "theme/travail/projet" in index  # Other entries remain.


def test_apply_dreaming_result_archive_dry_run_keeps_souvenir(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    souvenir = _make_souvenir(memory_root, "theme/quotidien/vacances_ete", expire="2026-07-24")
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=True,
        data={"updates": [
            {"path": "memory/capsules/theme/quotidien/vacances_ete.md", "action": "archive", "reason": "valid_until dépassé"}
        ]},
    )

    assert result.updates[0].applied is False
    assert souvenir.exists()  # Dry run moves nothing.


def test_apply_dreaming_result_archive_rejects_non_theme_or_missing(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={"updates": [
            {"path": "memory/users/garden/context.md", "action": "archive", "reason": "interdit"},
            {"path": "memory/capsules/theme/quotidien/inexistante.md", "action": "archive", "reason": "absente"},
        ]},
    )

    assert result.updates == []
    assert len(result.rejected_updates) == 2


def test_dreaming_prompt_includes_souvenir_recalls_and_consolidation_rules(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    recalls = memory_root / "levels" / "capsule_recalls.md"
    recalls.parent.mkdir(parents=True)
    recalls.write_text("- theme/solo: dernier rappel 2026-07-01 | rappels 3\n", encoding="utf-8")
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    provider = DreamingProvider({"reason": "Rien.", "confidence": 0.5, "report_md": "RAS.", "updates": []})

    run_dreaming("garden", provider=provider, dry_run=True)

    system = provider.messages[0]["content"]
    user = provider.messages[1]["content"]
    assert 'Duplicate consolidation' in system
    assert 'Souvenir recalls' in system
    assert 'bounded excerpts, not the complete history' in system
    assert 'The absence of original evidence' in system
    assert 'Do not remove, weaken, anonymize' in system
    assert 'Read L0 chronologically' in system
    assert 'infeasible, abandoned or completed' in system
    assert 'belongs in the agenda or focus while current' in system
    assert 'most recent exact user evidence' in system
    assert "dernier rappel 2026-07-01 | rappels 3" in user


def test_apply_dreaming_result_applies_duplicate_fusion(monkeypatch, tmp_path):
    memory_root = tmp_path / "memory"
    theme_root = memory_root / "capsules" / "theme" / "travail"
    theme_root.mkdir(parents=True)
    kept = theme_root / "presentation.md"
    kept.write_text("# Présentation\n\n## Faits\n- Prévue le 2026-07-02.\n", encoding="utf-8")
    duplicate = theme_root / "presentation_bis.md"
    duplicate.write_text("# Présentation bis\n\n## Faits\n- Reprise en urgence.\n", encoding="utf-8")
    (memory_root / "capsules" / "index.md").write_text(
        "- theme/travail/presentation: Présentation. — 2026-07-02\n"
        "- theme/travail/presentation_bis: Présentation bis. — urgence\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)

    result = apply_dreaming_result(
        user_id="garden",
        dry_run=False,
        data={"updates": [
            {
                "path": "memory/capsules/theme/travail/presentation.md",
                "action": "update",
                "reason": "Fusion du doublon.",
                "content_md": "# Présentation\n\n## Faits\n- Prévue le 2026-07-02.\n- Reprise en urgence.\n",
            },
            {
                "path": "memory/capsules/theme/travail/presentation_bis.md",
                "action": "archive",
                "reason": "doublon fusionné dans theme/travail/presentation",
            },
        ]},
    )

    assert all(update.applied for update in result.updates)
    assert "Reprise en urgence" in kept.read_text(encoding="utf-8")
    assert not duplicate.exists()
    archived = memory_root / "archive" / "capsules" / "theme" / "travail" / "presentation_bis.md"
    assert archived.exists()
    index = (memory_root / "capsules" / "index.md").read_text(encoding="utf-8")
    assert "presentation_bis" not in index
    assert "theme/travail/presentation" in index


def test_latest_dreaming_report_returns_newest_dry_run_for_user(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    old = root / "garden_20260514T090000_dry-run_24h.json"
    latest = root / "garden_20260515T090000_dry-run_24h.json"
    apply_report = root / "garden_20260516T090000_apply_24h.json"
    other_user = root / "other_20260517T090000_dry-run_24h.json"
    for path in (old, latest, apply_report, other_user):
        path.write_text("{}", encoding="utf-8")

    assert latest_dreaming_report("garden", report_root=root) == latest
    assert latest_dreaming_report("garden", mode="apply", report_root=root) == apply_report
    assert latest_dreaming_report("missing", report_root=root) is None
