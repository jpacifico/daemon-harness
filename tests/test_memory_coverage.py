import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.memory.coverage import (
    RecoveryNeed,
    append_memory_coverage_trace,
    assess_memory_coverage,
    build_coverage_sources,
)
from daemon_core.memory.planning import MemoryNeed, MemoryPlan
from daemon_core.providers.base import LLMProvider


class CoverageProvider(LLMProvider):
    def __init__(self, payload: dict):
        self.payload = payload
        self.messages = None

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages = messages
        return json.dumps(self.payload, ensure_ascii=False)


def _sam_plan() -> MemoryPlan:
    return MemoryPlan(
        subjects=["Sam"],
        intent="personnaliser un déjeuner",
        needs=[
            MemoryNeed(
                description="préférences alimentaires confirmées de Sam",
                subjects=["Sam"],
                required=True,
            ),
            MemoryNeed(
                description="contexte actuel du déjeuner",
                subjects=["Sam"],
                required=True,
            ),
        ],
    )


def test_coverage_judge_maps_each_need_to_real_sources():
    provider = CoverageProvider(
        {
            "plan_status": "adequate",
            "overall_status": "covered",
            "needs": [
                {
                    "need_index": 0,
                    "status": "covered",
                    "sources": ["capsule:theme/gouts_sam", "capsule:inventée"],
                    "reason": "Les goûts sont explicitement attribués à Sam.",
                },
                {
                    "need_index": 1,
                    "status": "covered",
                    "sources": ["active_context"],
                    "reason": "Le contexte du repas est présent.",
                },
            ],
            "unplanned_needs": [],
            "attribution_issues": [],
            "recovery_needs": [],
            "reason": "Dossier suffisant.",
            "confidence": 0.94,
        }
    )
    sources = build_coverage_sources(
        active_context="Sam déjeune à la maison aujourd'hui.",
        souvenirs={
            "theme/gouts_sam": "Sam aime les pâtes et les plats simples."
        },
    )

    result = assess_memory_coverage(
        message="Que commander pour Sam ?",
        recent_dialogue="",
        plan=_sam_plan(),
        sources=sources,
        provider=provider,
    )

    assert result.plan_status == "adequate"
    assert result.overall_status == "covered"
    assert result.needs[0].sources == ["capsule:theme/gouts_sam"]
    assert result.needs[1].sources == ["active_context"]
    assert 'You never respond to the user' in provider.messages[0]["content"]
    assert "préférences alimentaires confirmées de Sam" in provider.messages[1]["content"]


def test_coverage_judge_can_find_plan_gap_and_wrong_attribution():
    provider = CoverageProvider(
        {
            "plan_status": "incomplete",
            "overall_status": "missing",
            "needs": [
                {
                    "need_index": 0,
                    "status": "missing",
                    "sources": [],
                    "reason": "La souvenir parle de Alex, pas de Sam.",
                },
                {
                    "need_index": 1,
                    "status": "partial",
                    "sources": ["active_context"],
                    "reason": "Le lieu est connu mais pas les contraintes du repas.",
                },
            ],
            "unplanned_needs": [
                {
                    "description": "contraintes alimentaires actuelles de Sam",
                    "subjects": ["Sam"],
                    "required": True,
                }
            ],
            "attribution_issues": [
                "Les goûts chargés appartiennent à Alex et non à Sam."
            ],
            "recovery_needs": [
                "goûts alimentaires confirmés de Sam",
                "contraintes alimentaires actuelles de Sam",
            ],
            "reason": "Le dossier ne permet pas une recommandation personnelle fiable.",
            "confidence": 0.9,
        }
    )

    result = assess_memory_coverage(
        message="Que commander pour Sam ?",
        recent_dialogue="Alex aime la cuisine japonaise.",
        plan=_sam_plan(),
        sources=build_coverage_sources(
            active_context="Déjeuner à la maison.",
            souvenirs={"theme/gouts_alex": "Alex aime les sushis."},
        ),
        provider=provider,
    )

    assert result.plan_status == "incomplete"
    assert result.needs[0].status == "missing"
    assert result.unplanned_needs[0].subjects == ["Sam"]
    assert result.attribution_issues
    assert len(result.recovery_needs) == 2


def test_coverage_parser_marks_omitted_need_unassessed():
    provider = CoverageProvider(
        {
            "plan_status": "adequate",
            "overall_status": "partial",
            "needs": [{"need_index": 0, "status": "covered", "sources": []}],
        }
    )

    result = assess_memory_coverage(
        message="Question",
        recent_dialogue="",
        plan=_sam_plan(),
        sources={},
        provider=provider,
    )

    assert result.needs[1].status == "unassessed"


def test_current_user_message_can_cover_a_new_fact_without_recovery():
    provider = CoverageProvider(
        {
            "plan_status": "adequate",
            "overall_status": "covered",
            "needs": [
                {
                    "need_index": 0,
                    "status": "covered",
                    "sources": ["current_user_message"],
                    "reason": "Alex vient de formuler sa posture.",
                }
            ],
            "recovery_needs": [],
            "reason": "Le message courant suffit pour ce tour.",
        }
    )
    plan = MemoryPlan(
        subjects=["Alex"],
        needs=[
            MemoryNeed(
                description="posture actuelle de Alex dans son projet",
                subjects=["Alex"],
            )
        ],
    )

    result = assess_memory_coverage(
        message="Je suis l'architecte, les agents exécutent.",
        recent_dialogue="",
        plan=plan,
        sources={},
        provider=provider,
    )

    assert result.overall_status == "covered"
    assert result.needs[0].sources == ["current_user_message"]
    assert result.recovery_needs == []
    assert '<source id="current_user_message">' in provider.messages[1]["content"]


def test_coverage_trace_is_append_only_and_per_user(tmp_path, monkeypatch):
    monkeypatch.setattr("daemon_core.memory.coverage.MEMORY_ROOT", tmp_path / "memory")
    assessment = assess_memory_coverage(
        message="Question",
        recent_dialogue="",
        plan=MemoryPlan(),
        sources={},
        provider=CoverageProvider(
            {
                "plan_status": "adequate",
                "overall_status": "covered",
                "needs": [],
                "reason": "Aucun besoin personnel.",
            }
        ),
    )

    path = append_memory_coverage_trace(
        user_id="telegram:123",
        message="Question",
        plan=MemoryPlan(),
        selected_souvenirs=[],
        assessment=assessment,
    )
    append_memory_coverage_trace(
        user_id="telegram:123",
        message="Question suivante",
        plan=MemoryPlan(),
        selected_souvenirs=[],
        assessment=assessment,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert path.name == "telegram_123.jsonl"
    assert len(lines) == 2
    assert json.loads(lines[1])["message"] == "Question suivante"


class RecoveryScriptedProvider(LLMProvider):
    """Script the selector and coverage judge according to the received prompt."""

    def __init__(self):
        self.calls = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.calls.append(messages)
        system = messages[0]["content"]
        if 'micro-selector' in system:
            return json.dumps(
                {
                    "selected": ["theme/quotidien/sortie_vr"],
                    "ambiguity": "none",
                    "reason": "La souvenir répond au manque exact.",
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "plan_status": "adequate",
                "overall_status": "covered",
                "needs": [
                    {
                        "need_index": 0,
                        "status": "covered",
                        "sources": ["capsule:theme/quotidien/sortie_vr"],
                        "reason": "La pièce récupérée couvre le besoin.",
                    }
                ],
                "recovery_needs": [],
                "reason": "Couvert après récupération.",
                "confidence": 0.9,
            },
            ensure_ascii=False,
        )


def test_recovery_finds_missing_piece_and_rejudges(tmp_path, monkeypatch):
    from daemon_core.memory.coverage import MemoryCoverage, attempt_memory_recovery

    memory_root = tmp_path / "memory"
    quotidien = memory_root / "capsules" / "theme" / "quotidien"
    quotidien.mkdir(parents=True)
    (memory_root / "capsules" / "index.md").write_text(
        "- theme/quotidien/sortie_vr: Sortie VR avec Sam cette semaine\n",
        encoding="utf-8",
    )
    (quotidien / "sortie_vr.md").write_text(
        "---\nsubjects: [Alex, Sam]\nfacets: [loisirs partagés]\n---\n"
        "# Sortie VR\n\n- La base VR motive Sam.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)

    assessment = MemoryCoverage(
        overall_status="missing",
        plan_status="adequate",
        recovery_needs=[
            RecoveryNeed(
                description="Retrouver l'activité mentionnée pour cet après-midi.",
                subjects=["Sam"],
                facets=["loisirs partagés"],
            )
        ],
    )
    provider = RecoveryScriptedProvider()
    recovery, after = attempt_memory_recovery(
        message="Je t'ai dit ce que je faisais cet après-midi ?",
        recent_dialogue="",
        plan=MemoryPlan(
            subjects=["Alex"],
            facets=["travail"],
            needs=[
                MemoryNeed(
                    description="activité de cet après-midi",
                    subjects=["Alex"],
                )
            ],
        ),
        assessment=assessment,
        sources={"active_context": "Sam est avec Alex cette semaine."},
        already_selected=["theme/famille/organisation"],
        provider=provider,
    )

    assert recovery.attempted
    assert recovery.souvenirs_found == ["theme/quotidien/sortie_vr"]
    assert recovery.status_before == "missing"
    assert recovery.status_after == "covered"
    assert after is not None and after.overall_status == "covered"
    assert recovery.coverage_after is after
    selection_prompt = "\n".join(
        message["content"] for message in provider.calls[0]
    )
    assert '"subjects": [\n    "Sam"' in selection_prompt
    assert '"facets": [\n    "loisirs partagés"' in selection_prompt


def test_recovery_reports_honestly_when_nothing_new_is_found(tmp_path, monkeypatch):
    from daemon_core.memory.coverage import MemoryCoverage, attempt_memory_recovery

    memory_root = tmp_path / "memory"
    (memory_root / "capsules").mkdir(parents=True)
    (memory_root / "capsules" / "index.md").write_text(
        "- theme/quotidien/sortie_vr: Sortie VR\n", encoding="utf-8"
    )
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)

    assessment = MemoryCoverage(
        overall_status="missing",
        plan_status="adequate",
        recovery_needs=[RecoveryNeed(description="Une pièce introuvable.")],
    )
    recovery, after = attempt_memory_recovery(
        message="Question",
        recent_dialogue="",
        plan=MemoryPlan(subjects=["Alex"], facets=["loisirs partagés"]),
        assessment=assessment,
        sources={},
        already_selected=["theme/quotidien/sortie_vr"],
        provider=RecoveryScriptedProvider(),
    )

    assert recovery.attempted
    assert recovery.souvenirs_found == []
    assert recovery.status_after == "missing"
    assert after is None


def test_coverage_voice_gate_only_fires_on_factual_recall_scenes():
    from daemon_core.memory.coverage import coverage_voice_gate

    plan_required = MemoryPlan(
        subjects=["Sam"],
        needs=[MemoryNeed(description="l'heure du dessin", required=True)],
    )
    plan_optional = MemoryPlan(
        subjects=["Sam"],
        needs=[MemoryNeed(description="détail facultatif", required=False)],
    )
    assert coverage_voice_gate("pratique", plan_required) is True
    assert coverage_voice_gate("relationnel", plan_required) is False
    assert coverage_voice_gate("pratique", plan_optional) is False
    assert coverage_voice_gate("pratique", MemoryPlan()) is False


def test_voice_dossier_is_empty_on_covered_and_sober_on_missing():
    from daemon_core.memory.coverage import MemoryCoverage, NeedCoverage, build_coverage_voice_dossier

    plan = MemoryPlan(
        subjects=["Alex"],
        needs=[
            MemoryNeed(description="les deux machines à laver prévues", required=True),
            MemoryNeed(description="le troisième objectif de la matinée", required=True),
        ],
    )
    covered = MemoryCoverage(
        overall_status="covered",
        plan_status="adequate",
        needs=[NeedCoverage(0, "covered"), NeedCoverage(1, "covered")],
    )
    assert build_coverage_voice_dossier(covered, plan) == ""

    partial = MemoryCoverage(
        overall_status="partial",
        plan_status="adequate",
        needs=[NeedCoverage(0, "covered"), NeedCoverage(1, "missing")],
        attribution_issues=["Le troisième objectif n'est attribué à personne."],
    )
    dossier = build_coverage_voice_dossier(partial, plan)
    assert "les deux machines à laver prévues" in dossier
    assert "le troisième objectif de la matinée" in dossier
    assert "Do not assert" in dossier
    assert "your positions, ideas and presence" in dossier
    assert "attribué à personne" in dossier
