import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.memory.coverage import RecoveryNeed
from daemon_core.memory.forensics import (
    ForensicRecovery,
    attempt_l0_chronology_recovery,
    attempt_l0_forensic_recovery,
    chronology_tool_payload,
    episodic_archive_catalog,
    failed_recall_tool_payload,
    personal_fact_tool_payload,
)
from daemon_core.providers.base import LLMProvider


class ForensicProvider(LLMProvider):
    def __init__(
        self,
        evidence: str,
        evidence_status: str = "found",
        relation_status: str = "established",
        conclusion: str = "Sam est né le 12 mars 2015.",
    ):
        self.evidence = evidence
        self.evidence_status = evidence_status
        self.relation_status = relation_status
        self.conclusion = conclusion
        self.calls = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.calls.append(messages)
        system = messages[0]["content"]
        if 'plan an exceptional forensic search' in system:
            return json.dumps(
                {
                    "should_search": True,
                    "files": ["alex_2026-07-01.md", "../hors-zone.md"],
                    "reason": "La scène vise explicitement cette journée.",
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "status": self.evidence_status,
                "source_file": "alex_2026-07-01.md",
                "evidence": self.evidence,
                "relation_status": self.relation_status,
                "conclusion": self.conclusion,
                "reason": "Citation trouvée dans le tour utilisateur.",
                "confidence": 0.95,
            },
            ensure_ascii=False,
        )


class AbstainingForensicProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        return json.dumps(
            {
                "should_search": False,
                "files": [],
                "reason": "Aucune période ne peut être défendue.",
                "confidence": 0.8,
            },
            ensure_ascii=False,
        )


class ChronologyProvider(LLMProvider):
    def __init__(
        self,
        *,
        start_turn: str = "turn-0001",
        end_turn: str = "turn-0003",
        evidence_status: str = "found",
        relation_status: str = "established",
        conclusion: str = "Le sujet précédent était la chaleur.",
    ) -> None:
        self.start_turn = start_turn
        self.end_turn = end_turn
        self.evidence_status = evidence_status
        self.relation_status = relation_status
        self.conclusion = conclusion
        self.calls = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.calls.append(messages)
        system = messages[0]["content"]
        if 'bound an exceptional chronological retrieval' in system:
            return json.dumps(
                {
                    "should_search": True,
                    "files": ["alex_2026-07-01.md"],
                    "reason": "La demande vise le fil de cette journée.",
                    "confidence": 0.95,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "status": self.evidence_status,
                "source_file": "alex_2026-07-01.md",
                "start_turn": self.start_turn,
                "end_turn": self.end_turn,
                "relation_status": self.relation_status,
                "conclusion": self.conclusion,
                "reason": "La plage contient le pivot et le sujet qui le précède.",
                "confidence": 0.96,
            },
            ensure_ascii=False,
        )


class EpisodicAnchorProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.calls.append(messages)
        system = messages[0]["content"]
        if 'plan an exceptional forensic search' in system:
            prompt = messages[1]["content"]
            assert "avant l'épisode des cartons du 18 juillet" not in prompt
            assert "Retour de Belmont" in prompt
            assert '"span_start": "2026-07-18T04:00:28"' in prompt
            assert '"span_end": "2026-07-21T04:00:28"' in prompt
            return json.dumps(
                {
                    "should_search": True,
                    "archive": "theme/chroniques/2026-07-18",
                    "files": [],
                    "reason": "L'archive du retour couvre cette journée.",
                    "confidence": 0.96,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "status": "found",
                "source_file": "alex_2026-07-19.md",
                "evidence": (
                    "Je prévois déjà de regarder la finale de la Coupe du "
                    "Monde sur mon iPad à l'aéroport"
                ),
                "relation_status": "not_established",
                "conclusion": (
                    "La finale devait être regardée à l'aéroport, pas dans l'avion."
                ),
                "reason": "Le tour utilisateur distingue les deux lieux.",
                "confidence": 0.99,
            },
            ensure_ascii=False,
        )


def _write_l0(memory_root: Path) -> None:
    root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    root.mkdir(parents=True)
    (root / "alex_2026-07-01.md").write_text(
        "## 2026-07-01T09:00:00 — user\n\n"
        "Sam est né le 12 mars 2015.\n\n"
        "## 2026-07-01T09:00:03 — assistant\n\n"
        "Je vais supposer qu'il préfère les fraises.\n",
        encoding="utf-8",
    )


def _write_chronology_l0(memory_root: Path) -> None:
    root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    root.mkdir(parents=True)
    (root / "alex_2026-07-01.md").write_text(
        "## 2026-07-01T18:50:00 — user\n\n"
        "Il fait encore 34 degrés, je vais rester au frais.\n\n"
        "## 2026-07-01T18:50:05 — assistant\n\n"
        "Oui, la sortie peut attendre que la chaleur retombe.\n\n"
        "## 2026-07-01T18:53:00 — user\n\n"
        "Et côté MagicCon pour 2027 ?\n\n"
        "## 2026-07-01T18:53:05 — assistant\n\n"
        "Regardons les événements annoncés.\n",
        encoding="utf-8",
    )


def _write_return_episode(memory_root: Path) -> None:
    chronicle_root = memory_root / "capsules" / "theme" / "chroniques"
    chronicle_root.mkdir(parents=True)
    (chronicle_root / "2026-07-18.md").write_text(
        "---\n"
        "memory_type: episodic_archive\n"
        "truth_scope: past_only\n"
        "span_start: 2026-07-18T04:00:28\n"
        "span_end: 2026-07-21T04:00:28\n"
        "---\n\n"
        "# Chronique du retour\n\n"
        "Le retour de Belmont a été retardé par EasyJet.\n",
        encoding="utf-8",
    )
    (memory_root / "capsules" / "index.md").write_text(
        "- theme/chroniques/2026-07-18: Retour de Belmont et arrivée à Lyon\n",
        encoding="utf-8",
    )


def test_forensics_returns_only_exact_user_evidence(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    _write_l0(memory_root)
    monkeypatch.setattr("daemon_core.memory.forensics.MEMORY_ROOT", memory_root)
    provider = ForensicProvider("Sam est né le 12 mars 2015.")

    result = attempt_l0_forensic_recovery(
        user_id="alex",
        message="Quand Sam est-il né ?",
        recent_dialogue="On parlait de sa date de naissance.",
        recovery_needs=[
            RecoveryNeed(
                description="date de naissance confirmée de Sam",
                subjects=["Sam"],
                facets=["identité"],
            )
        ],
        provider=provider,
    )

    assert result.status == "found"
    assert result.source_file == "alex_2026-07-01.md"
    assert result.evidence == "Sam est né le 12 mars 2015."
    assert result.relation_status == "established"
    assert result.conclusion == "Sam est né le 12 mars 2015."
    assert result.plan.files == ["alex_2026-07-01.md"]
    assert len(provider.calls) == 2


def test_episodic_archive_catalog_uses_spans_and_compact_index_summary(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _write_return_episode(memory_root)
    malformed = memory_root / "capsules" / "theme" / "chroniques" / "broken.md"
    malformed.write_text("---\nspan_start: [\n---\n", encoding="utf-8")
    monkeypatch.setattr("daemon_core.memory.forensics.MEMORY_ROOT", memory_root)

    catalog = episodic_archive_catalog()

    assert catalog == [
        {
            "archive": "theme/chroniques/2026-07-18",
            "span_start": "2026-07-18T04:00:28",
            "span_end": "2026-07-21T04:00:28",
            "summary": "Retour de Belmont et arrivée à Lyon",
        }
    ]


def test_forensic_planner_uses_the_episode_boundary_instead_of_a_theme_match(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _write_return_episode(memory_root)
    l0_root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    l0_root.mkdir(parents=True)
    for day in ("16", "17", "18"):
        (l0_root / f"alex_2026-07-{day}.md").write_text(
            f"## 2026-07-{day}T12:00:00 — user\n\nAutre étape du séjour.\n",
            encoding="utf-8",
        )
    (l0_root / "alex_2026-07-19.md").write_text(
        "## 2026-07-19T12:00:00 — user\n\n"
        "Je prévois déjà de regarder la finale de la Coupe du Monde sur mon "
        "iPad à l'aéroport\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.forensics.MEMORY_ROOT", memory_root)
    provider = EpisodicAnchorProvider()

    result = attempt_l0_forensic_recovery(
        user_id="alex",
        message="Besoin recherché : lieu prévu pour regarder la finale",
        recent_dialogue=(
            "Je crois que le jour de notre retour de Bordeaux, j'avais prévu de "
            "regarder la finale dans l'avion."
        ),
        recovery_needs=[RecoveryNeed(description="lieu prévu pour voir la finale")],
        provider=provider,
    )

    assert result.status == "found"
    assert result.plan.archive == "theme/chroniques/2026-07-18"
    assert result.plan.files == [
        "alex_2026-07-18.md",
        "alex_2026-07-19.md",
    ]
    assert result.source_file == "alex_2026-07-19.md"
    assert result.relation_status == "not_established"
    assert "aéroport" in result.evidence
    assert len(provider.calls) == 2


def test_forensics_returns_exact_counterevidence_as_a_structured_verdict(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    root.mkdir(parents=True)
    (root / "alex_2026-07-01.md").write_text(
        "## 2026-07-01T09:00:00 — user\n\n"
        "Je prévois de regarder la finale sur mon iPad à l'aéroport.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.forensics.MEMORY_ROOT", memory_root)
    provider = ForensicProvider(
        "Je prévois de regarder la finale sur mon iPad à l'aéroport.",
        relation_status="not_established",
        conclusion=(
            "La finale devait être regardée à l'aéroport, pas dans l'avion."
        ),
    )

    result = attempt_l0_forensic_recovery(
        user_id="alex",
        message="Avais-je prévu de regarder la finale dans l'avion ?",
        recent_dialogue="",
        recovery_needs=[RecoveryNeed(description="lieu prévu pour voir la finale")],
        provider=provider,
    )
    payload = personal_fact_tool_payload(result)

    assert result.status == "found"
    assert result.relation_status == "not_established"
    assert "aéroport" in result.conclusion
    assert payload["status"] == "found"
    assert payload["evidence_status"] == "exact_evidence_found"
    assert payload["relation_status"] == "not_established"
    assert payload["response_constraint"].startswith('Do not confirm')
    assert "reason" not in payload


def test_personal_fact_payload_hides_not_found_diagnostic():
    recovery = ForensicRecovery(
        attempted=True,
        status="not_found",
        reason="Le fait réel est à l'aéroport.",
        confidence=0.99,
        relation_status="not_established",
    )

    payload = personal_fact_tool_payload(recovery)

    assert payload["status"] == "not_found"
    assert payload["evidence"] == ""
    assert payload["conclusion"] == ""
    assert "reason" not in payload
    assert "aéroport" not in json.dumps(payload, ensure_ascii=False)


def test_forensics_rejects_assistant_statement_as_evidence(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    _write_l0(memory_root)
    monkeypatch.setattr("daemon_core.memory.forensics.MEMORY_ROOT", memory_root)
    provider = ForensicProvider(
        "Je vais supposer qu'il préfère les fraises.",
        conclusion="Sam préfère les fraises.",
    )

    result = attempt_l0_forensic_recovery(
        user_id="alex",
        message="Est-ce que Sam aime les fraises ?",
        recent_dialogue="",
        recovery_needs=[RecoveryNeed(description="goût confirmé de Sam")],
        provider=provider,
    )

    assert result.status == "invalid_evidence"
    assert result.evidence == ""
    assert result.confidence == 0.0


def test_forensics_abstains_when_no_period_is_defensible(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    _write_l0(memory_root)
    monkeypatch.setattr("daemon_core.memory.forensics.MEMORY_ROOT", memory_root)

    result = attempt_l0_forensic_recovery(
        user_id="alex",
        message="Quel est mon plus vieux goût musical ?",
        recent_dialogue="",
        recovery_needs=[RecoveryNeed(description="préférence musicale ancienne")],
        provider=AbstainingForensicProvider(),
    )

    assert result.status == "abstained"
    assert result.plan.files == []
    assert "Aucune période" in result.reason


def test_chronology_returns_one_exact_contiguous_turn_range(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _write_chronology_l0(memory_root)
    monkeypatch.setattr("daemon_core.memory.forensics.MEMORY_ROOT", memory_root)
    provider = ChronologyProvider()

    result = attempt_l0_chronology_recovery(
        user_id="alex",
        message="Quel sujet était discuté juste avant Magic ?",
        recent_dialogue="La discussion Magic vient de se terminer.",
        provider=provider,
    )

    assert result.status == "found"
    assert result.source_file == "alex_2026-07-01.md"
    assert "34 degrés" in result.evidence
    assert "côté MagicCon" in result.evidence
    assert "événements annoncés" not in result.evidence
    assert result.relation_status == "established"
    assert result.conclusion == "Le sujet précédent était la chaleur."
    assert len(provider.calls) == 2
    assert "turn-0003" in provider.calls[1][1]["content"]
    assert 'nearest pivot' in provider.calls[1][0]["content"]


def test_chronology_not_found_has_a_total_fail_closed_contract(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _write_chronology_l0(memory_root)
    monkeypatch.setattr("daemon_core.memory.forensics.MEMORY_ROOT", memory_root)

    result = attempt_l0_chronology_recovery(
        user_id="alex",
        message="Quel sujet était discuté juste avant ce pivot ?",
        recent_dialogue="",
        provider=ChronologyProvider(evidence_status="not_found"),
    )
    payload = chronology_tool_payload(result)

    assert result.status == "not_found"
    assert result.relation_status == "not_established"
    assert result.conclusion == ""
    assert result.evidence == ""
    assert payload["status"] == "not_found"
    assert payload["relation_status"] == "not_established"
    assert payload["conclusion"] == ""
    assert payload["response_constraint"].startswith(
        'No usable exact evidence'
    )


def test_chronology_partial_without_an_exact_range_stays_unproven(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    _write_chronology_l0(memory_root)
    monkeypatch.setattr("daemon_core.memory.forensics.MEMORY_ROOT", memory_root)

    result = attempt_l0_chronology_recovery(
        user_id="alex",
        message="Quel sujet était discuté juste avant ce pivot ?",
        recent_dialogue="",
        provider=ChronologyProvider(evidence_status="partial"),
    )

    assert result.status == "not_found"
    assert result.relation_status == "not_established"
    assert result.conclusion == ""
    assert result.evidence == ""


def test_failed_recall_payload_is_a_material_boundary():
    payload = failed_recall_tool_payload(
        evidence_kind="conversation_chronology"
    )

    assert payload["status"] == "error"
    assert payload["evidence_status"] == "error"
    assert payload["relation_status"] == "not_established"
    assert payload["evidence"] == ""
    assert payload["conclusion"] == ""
    assert payload["confidence"] == 0.0
    assert payload["error_code"] == "forensic_lookup_failed"
    assert 'Do not assert' in payload["response_constraint"]
    assert 'material boundary' in payload["boundary"]
    assert "reason" not in payload


def test_chronology_separates_exact_evidence_from_an_unproven_claim(
    tmp_path,
    monkeypatch,
):
    memory_root = tmp_path / "memory"
    root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    root.mkdir(parents=True)
    (root / "alex_2026-07-01.md").write_text(
        "## 2026-07-01T19:49:00 — user\n\n"
        "Le backlog est à terminer pour demain matin.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.forensics.MEMORY_ROOT", memory_root)
    provider = ChronologyProvider(
        start_turn="turn-0001",
        end_turn="turn-0001",
        relation_status="not_established",
        conclusion=(
            "La finalisation est prévue le matin ; l'heure de présentation "
            "n'est pas établie."
        ),
    )

    result = attempt_l0_chronology_recovery(
        user_id="alex",
        message="Ai-je dit que je présenterais le backlog demain matin ?",
        recent_dialogue="",
        provider=provider,
    )

    assert result.status == "found"
    assert result.relation_status == "not_established"
    assert "présentation" in result.conclusion
    assert "terminer pour demain matin" in result.evidence
    payload = chronology_tool_payload(result)
    assert payload["evidence_status"] == "exact_evidence_found"
    assert payload["response_constraint"].startswith('Do not confirm')


def test_chronology_rejects_an_unbounded_turn_range(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    root.mkdir(parents=True)
    turns = []
    for index in range(12):
        role = "user" if index % 2 == 0 else "assistant"
        turns.append(
            f"## 2026-07-01T18:{index:02d}:00 — {role}\n\nTour {index}."
        )
    (root / "alex_2026-07-01.md").write_text(
        "\n\n".join(turns) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.forensics.MEMORY_ROOT", memory_root)

    result = attempt_l0_chronology_recovery(
        user_id="alex",
        message="Retrouve la frontière.",
        recent_dialogue="",
        provider=ChronologyProvider(
            start_turn="turn-0001",
            end_turn="turn-0012",
        ),
    )

    assert result.status == "invalid_evidence"
    assert result.evidence == ""
    assert result.confidence == 0.0
