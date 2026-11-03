import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from daemon_core.souvenir_selector import SouvenirSelection, expand_domain_selection, select_souvenirs, select_souvenirs_for_memory_plan, select_souvenirs_minimal
from daemon_core.memory.planning import MemoryNeed, MemoryPlan
from daemon_core.memory.reader import list_zoom_souvenirs, load_souvenir_index, load_domain_souvenir_index, load_root_souvenir_index
from daemon_core.providers.base import LLMProvider


class SelectionProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        return '{"selected":["examples/remote_work","theme/communication_style"],"ambiguity":"none","reason":"Demande sur les habitudes de travail et le style de réponse.","confidence":0.91}'


class BrokenSelectionProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        return "pas du JSON"

def test_minimal_selection_has_no_keyword_logic():
    result = select_souvenirs_minimal()
    assert result.source == "minimal"
    assert result.selected == []


def test_llm_selection_uses_structured_provider_output():
    result = select_souvenirs("Comment j'organise mon remote ?", SelectionProvider())
    assert result.source == "llm"
    assert result.selected == ["examples/remote_work"]
    assert result.confidence == 0.91
    assert result.ambiguity == "none"


def test_llm_selection_can_report_material_ambiguity():
    class AmbiguousSelectionProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            return (
                '{"selected":["examples/remote_work","examples/restaurant_simple"],'
                '"ambiguity":"material",'
                '"reason":"Deux souvenirs plausibles conduisent à des réponses différentes.",'
                '"confidence":0.78}'
            )

    result = select_souvenirs("Quel horaire ?", AmbiguousSelectionProvider())

    assert result.selected == ["examples/remote_work", "examples/restaurant_simple"]
    assert result.ambiguity == "material"


def test_selection_prompt_receives_current_time_and_agenda():
    captured = {}

    class CapturingSelectionProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            captured["system"] = messages[0]["content"]
            captured["user"] = messages[1]["content"]
            return '{"selected":[],"ambiguity":"none","reason":"Agenda suffisant.","confidence":0.5}'

    select_souvenirs(
        "Rappelle-moi mon planning d'ici ce soir",
        CapturingSelectionProvider(),
        user_agenda="- Vendredi | Sam | dessin | 18h15-19h45 | récupération vers 19h45.",
    )

    assert 'Current time' in captured["user"]
    assert 'Short agenda' in captured["user"]
    assert "18h15-19h45" in captured["user"]
    assert 'schedules' in captured["system"]
    assert 'memory is a palette' in captured["system"]
    assert 'cold episodic memory' in captured["system"]
    assert 'Never select them to answer a question about agenda' in captured["system"]


def test_selection_prompt_requires_complete_personal_coverage(tmp_path, monkeypatch):
    captured = {}
    memory_root = tmp_path / "memory"
    for name in (
        "theme/preferences/alimentaires",
        "theme/quotidien/repas_livraison",
    ):
        path = memory_root / "capsules" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)

    class CapturingSelectionProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            captured["system"] = messages[0]["content"]
            return (
                '{"selected":["theme/preferences/alimentaires","theme/quotidien/repas_livraison"],'
                '"ambiguity":"none","reason":"Goûts durables et contexte ponctuel.","confidence":0.93}'
            )

    result = select_souvenirs(
        "Que me suggères-tu de commander pour déjeuner ?",
        CapturingSelectionProvider(),
    )

    assert result.selected == ["theme/preferences/alimentaires", "theme/quotidien/repas_livraison"]
    assert 'Establish the memory coverage required' in captured["system"]
    assert 'Select for coverage, not resemblance' in captured["system"]
    assert 'even if the final response will be brief' in captured["system"]
    assert 'Rank sources' in captured["system"]
    assert 'do not stop at the first souvenir' in captured["system"]


def test_llm_selection_falls_back_to_minimal_when_json_is_invalid():
    result = select_souvenirs("Réserve-moi un restaurant pour ce soir", BrokenSelectionProvider())
    assert result.source == "minimal"
    assert result.selected == []


def test_souvenir_catalog_uses_filesystem_as_source_of_truth(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    theme_root = souvenirs_root / "theme"
    theme_root.mkdir(parents=True)

    (souvenirs_root / "index.md").write_text(
        "- theme/indexed: Description enrichie depuis l'index\n"
        "- theme/missing: Entrée obsolète sans fichier\n",
        encoding="utf-8",
    )
    (theme_root / "indexed.md").write_text("# Titre indexé\n", encoding="utf-8")
    (theme_root / "unindexed.md").write_text("# Titre non indexé\n", encoding="utf-8")

    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)

    catalog = load_souvenir_index()

    assert "- theme/indexed: Description enrichie depuis l'index" in catalog
    assert "- theme/unindexed: Titre non indexé" in catalog
    assert "theme/missing" not in catalog


def _build_two_stage_memory(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    souvenirs_root = memory_root / "capsules"
    (souvenirs_root / "theme" / "famille").mkdir(parents=True)
    (souvenirs_root / "theme" / "projet").mkdir(parents=True)

    (souvenirs_root / "index.md").write_text(
        "- theme/famille: Carte famille. — Sam, garde alternée\n"
        "- theme/famille/organisation: Garde alternée détaillée. — vendredi\n"
        "- theme/preferences: Préférences personnelles durables\n"
        "- theme/solo: Note isolée sans sous-souvenirs\n"
        "- theme/projet/note_orpheline: Note d'un domaine sans carte\n",
        encoding="utf-8",
    )
    (souvenirs_root / "theme" / "famille.md").write_text(
        "# Carte famille\n\n## Liens proches\n- theme/preferences : préférences durables.\n",
        encoding="utf-8",
    )
    (souvenirs_root / "theme" / "famille" / "organisation.md").write_text("# Garde alternée\n", encoding="utf-8")
    (souvenirs_root / "theme" / "preferences.md").write_text("# Préférences durables\n", encoding="utf-8")
    (souvenirs_root / "theme" / "solo.md").write_text("# Note isolée\n", encoding="utf-8")
    (souvenirs_root / "theme" / "projet" / "note_orpheline.md").write_text("# Note orpheline\n", encoding="utf-8")

    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)


def test_root_souvenir_index_hides_deep_entries_and_marks_domains(tmp_path, monkeypatch):
    _build_two_stage_memory(tmp_path, monkeypatch)

    root_catalog = load_root_souvenir_index()

    assert "- theme/famille: Carte famille. — Sam, garde alternée [domain: 1 souvenirs]" in root_catalog
    assert "- theme/solo: Note isolée sans sous-souvenirs" in root_catalog
    assert "theme/famille/organisation" not in root_catalog
    assert "- theme/projet: Domain. — note_orpheline [domain: 1 souvenirs]" in root_catalog


def test_domain_souvenir_index_expands_full_subtree(tmp_path, monkeypatch):
    _build_two_stage_memory(tmp_path, monkeypatch)

    zoomed = load_domain_souvenir_index(["theme/famille"])

    assert "- theme/famille: Carte famille. — Sam, garde alternée" in zoomed
    assert "- theme/famille/organisation: Garde alternée détaillée. — vendredi" in zoomed
    assert "- theme/preferences: Préférences personnelles durables" in zoomed
    assert "theme/solo" not in zoomed


def test_zoom_exposes_only_one_hop_of_explicit_souvenir_links(tmp_path, monkeypatch):
    _build_two_stage_memory(tmp_path, monkeypatch)

    assert list_zoom_souvenirs(["theme/famille"]) == [
        "theme/famille",
        "theme/famille/organisation",
        "theme/preferences",
    ]


def test_domain_zoom_can_select_a_linked_preference_souvenir(tmp_path, monkeypatch):
    _build_two_stage_memory(tmp_path, monkeypatch)

    class LinkedPreferenceProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            assert "theme/preferences" in messages[1]["content"]
            return (
                '{"selected":["theme/famille/organisation","theme/preferences"],'
                '"ambiguity":"none","reason":"Organisation et préférences sont complémentaires.",'
                '"confidence":0.91}'
            )

    result = expand_domain_selection(
        SouvenirSelection(selected=["theme/famille"], reason="Radar.", source="radar"),
        message="Quel choix conviendrait à la famille ?",
        provider=LinkedPreferenceProvider(),
    )

    assert result.selected == ["theme/famille/organisation", "theme/preferences"]


def test_select_souvenirs_zooms_into_selected_domain(tmp_path, monkeypatch):
    _build_two_stage_memory(tmp_path, monkeypatch)

    class TwoStageProvider(LLMProvider):
        def __init__(self):
            self.catalogs = []
            self.systems = []

        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            self.systems.append(messages[0]["content"])
            self.catalogs.append(messages[1]["content"])
            if len(self.catalogs) == 1:
                return '{"selected":["theme/famille"],"ambiguity":"none","reason":"Domaine familial pertinent.","confidence":0.9}'
            return '{"selected":["theme/famille/organisation"],"ambiguity":"none","reason":"La note précise répond.","confidence":0.88}'

    provider = TwoStageProvider()
    result = select_souvenirs("Quand est-ce que je récupère Sam ?", provider)

    assert result.selected == ["theme/famille/organisation"]
    assert result.source == "llm+zoom"
    assert len(provider.catalogs) == 2
    assert "theme/famille/organisation" not in provider.catalogs[0]
    assert "theme/famille/organisation" in provider.catalogs[1]
    assert 'You are at the first level' in provider.systems[0]
    assert 'You are at the final zoom' in provider.systems[1]


def test_expand_domain_selection_keeps_leaf_selection_without_llm_call(tmp_path, monkeypatch):
    _build_two_stage_memory(tmp_path, monkeypatch)

    class ForbiddenProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            raise AssertionError("Le zoom ne doit pas être appelé pour des feuilles.")

    selection = SouvenirSelection(selected=["theme/solo"], reason="Radar.", source="radar")
    result = expand_domain_selection(selection, message="Question", provider=ForbiddenProvider())

    assert result is selection


def test_expand_domain_selection_keeps_domain_cards_when_zoom_fails(tmp_path, monkeypatch):
    _build_two_stage_memory(tmp_path, monkeypatch)

    selection = SouvenirSelection(selected=["theme/famille"], reason="Radar.", source="radar")
    result = expand_domain_selection(selection, message="Question", provider=BrokenSelectionProvider())

    assert result.selected == ["theme/famille"]
    assert "Zoom unavailable" in result.reason


def test_memory_plan_crosses_subject_and_facet_before_final_judgment(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    souvenir_root = memory_root / "capsules" / "theme" / "famille"
    souvenir_root.mkdir(parents=True)
    (memory_root / "capsules" / "index.md").write_text(
        "- theme/famille/gouts_sam: Goûts alimentaires de Sam\n"
        "- theme/famille/gouts_alex: Goûts alimentaires de Alex\n"
        "- theme/famille/rythme_sam: Rythme quotidien de Sam\n",
        encoding="utf-8",
    )
    (souvenir_root / "gouts_sam.md").write_text(
        "---\nsubjects: [Sam]\nfacets: [goûts alimentaires]\n---\n"
        "# Goûts alimentaires de Sam\n",
        encoding="utf-8",
    )
    (souvenir_root / "gouts_alex.md").write_text(
        "---\nsubjects: [Alex]\nfacets: [goûts alimentaires]\n---\n"
        "# Goûts alimentaires de Alex\n",
        encoding="utf-8",
    )
    (souvenir_root / "rythme_sam.md").write_text(
        "---\nsubjects: [Sam]\nfacets: [rythme quotidien]\n---\n"
        "# Rythme de Sam\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)

    class PlannedSelectionProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            catalog = messages[1]["content"]
            # The tree takes precedence: every candidate's subtree stays visible.
            # The final judgment selects within it using the plan.
            assert "theme/famille/gouts_sam" in catalog
            assert "theme/famille/gouts_alex" in catalog
            assert "theme/famille/rythme_sam" in catalog
            assert 'préférences alimentaires confirmées de Sam' in catalog
            return json.dumps(
                {
                    "selected": ["theme/famille/gouts_sam"],
                    "ambiguity": "none",
                    "reason": "Le sujet et la facette correspondent exactement.",
                    "confidence": 0.97,
                },
                ensure_ascii=False,
            )

    selection = select_souvenirs_for_memory_plan(
        plan=MemoryPlan(
            subjects=["Sam"],
            facets=["goûts alimentaires"],
            needs=[
                MemoryNeed(
                    description='préférences alimentaires confirmées de Sam',
                    subjects=["Sam"],
                )
            ],
            intent="personnaliser un repas",
        ),
        seed_candidates=["theme/famille"],
        message="Que commander pour Sam ?",
        provider=PlannedSelectionProvider(),
    )

    assert selection is not None
    assert selection.selected == ["theme/famille/gouts_sam"]
    assert selection.source == "memory-plan"


def test_memory_plan_projection_never_hides_a_tree_leaf(tmp_path, monkeypatch):
    """Regression: a relevant domain leaf had facets absent from the plan.

    It was excluded from the selectable list, so expansion loaded only maps."""
    memory_root = tmp_path / "memory"
    quotidien = memory_root / "capsules" / "theme" / "quotidien"
    loisirs = memory_root / "capsules" / "theme" / "loisirs"
    quotidien.mkdir(parents=True)
    loisirs.mkdir(parents=True)
    (memory_root / "capsules" / "index.md").write_text(
        "- theme/quotidien/sortie_vr: Option sortie VR avec Sam cette semaine\n"
        "- theme/loisirs/jeux_partages: Jeux partagés avec Sam\n",
        encoding="utf-8",
    )
    # The relevant leaf has facets that do NOT overlap the plan.
    (quotidien / "sortie_vr.md").write_text(
        "---\nsubjects: [Alex, Sam]\nfacets: [sortie père-fils, décision en attente]\n---\n"
        "# Sortie VR\n\n- La base VR motive Sam.\n",
        encoding="utf-8",
    )
    # Projection adds a note from outside the subtree through a cross-link.
    (loisirs / "jeux_partages.md").write_text(
        "---\nsubjects: [Alex, Sam]\nfacets: [loisirs partagés]\n---\n"
        "# Jeux partagés\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)

    class TreeSovereignProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            catalog = messages[1]["content"]
            # The correct domain's leaf remains visible despite different facets.
            assert "theme/quotidien/sortie_vr" in catalog
            # Projection also added the path from outside the subtree.
            assert "theme/loisirs/jeux_partages" in catalog
            return json.dumps(
                {
                    "selected": ["theme/quotidien/sortie_vr"],
                    "ambiguity": "none",
                    "reason": "La feuille du domaine répond directement.",
                    "confidence": 0.95,
                },
                ensure_ascii=False,
            )

    selection = select_souvenirs_for_memory_plan(
        plan=MemoryPlan(
            subjects=["Alex", "Sam"],
            facets=["loisirs partagés"],
            needs=[
                MemoryNeed(
                    description="l'activité mentionnée pour cet après-midi",
                    subjects=["Alex", "Sam"],
                )
            ],
            intent="rappeler une activité déjà évoquée",
        ),
        seed_candidates=["theme/quotidien"],
        message="Je t'ai dit ce que je faisais cet après-midi ?",
        provider=TreeSovereignProvider(),
    )

    assert selection is not None
    assert selection.selected == ["theme/quotidien/sortie_vr"]
