import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.context_router import route_context_depth
from daemon_core.memory.reader import list_available_souvenirs, load_souvenir_index
from daemon_core.providers.base import LLMProvider


class RadarRouteProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        return '{"context_depth":"light","interaction_mode":"conversation","scene_register":"relationnel","candidate_capsules":["examples/remote_work","unknown/souvenir"],"memory_plan":{"subjects":["Alex"],"facets":["travail"],"needs":[{"description":"habitudes de travail à distance de Alex","subjects":["Alex"],"required":true}],"intent":"reprendre son organisation personnelle"},"reason":"Souvenir utile possible.","confidence":0.86}'


class BrokenRouteProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        return "pas du JSON"


def test_context_router_parses_radar_route_with_candidate_souvenirs():
    route = route_context_depth(
        "On reprend mon organisation remote",
        RadarRouteProvider(),
        recent_dialogue="On parlait de travail à distance.",
        user_agenda="- Vendredi | Sam | dessin | 18h15-19h45 | récupérer après l'activité vers 19h45.",
    )

    assert route.mode == "contextual"
    assert route.context_depth == "contextual"
    assert route.interaction_mode == "conversation"
    assert route.scene_register == "relationnel"
    assert route.candidate_souvenirs == ["examples/remote_work"]
    assert route.confidence == 0.86
    assert route.active_request == ""
    assert route.ambiguity == "none"
    assert route.needs_temporal_memory is False
    assert route.memory_plan.subjects == ["Alex"]
    assert route.memory_plan.facets == ["travail"]
    assert route.memory_plan.intent == "reprendre son organisation personnelle"
    assert len(route.memory_plan.needs) == 1
    assert route.memory_plan.needs[0].description == "habitudes de travail à distance de Alex"
    assert route.memory_plan.needs[0].subjects == ["Alex"]
    assert route.memory_plan.needs[0].required is True


def test_context_router_keeps_material_ambiguity_and_active_request():
    class AmbiguousRouteProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            return (
                '{"context_depth":"light","interaction_mode":"conversation",'
                '"candidate_capsules":["examples/remote_work","examples/restaurant_simple"],'
                '"active_request":"Identifier l’horaire réellement demandé.",'
                '"ambiguity":"material",'
                '"reason":"Deux interprétations conduisent à des réponses différentes.",'
                '"confidence":0.82}'
            )

    route = route_context_depth("Quel horaire ?", AmbiguousRouteProvider())

    assert route.mode == "contextual"
    assert route.context_depth == "contextual"
    assert route.candidate_souvenirs == ["examples/remote_work", "examples/restaurant_simple"]
    assert route.active_request == "Identifier l’horaire réellement demandé."
    assert route.ambiguity == "material"
    assert route.scene_register == "pratique"


def test_context_router_exposes_semantic_temporal_memory_verdict():
    class TemporalRouteProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            return json.dumps(
                {
                    "context_depth": "light",
                    "needs_temporal_memory": True,
                    "reason": "Le message précise une étape datée à conserver.",
                }
            )

    route = route_context_depth("Le départ est finalement vendredi.", TemporalRouteProvider())

    assert route.needs_temporal_memory is True
    assert route.mode == "fast"


def test_context_router_falls_back_to_contextual_when_invalid():
    route = route_context_depth("Bonjour", BrokenRouteProvider())

    assert route.mode == "contextual"
    assert route.context_depth == "contextual"
    assert route.scene_register == "pratique"
    assert route.confidence == 0.0


def test_radar_router_prompt_receives_souvenir_map_and_scene():
    captured = {}

    class CapturingProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            captured["system"] = messages[0]["content"]
            captured["user"] = messages[1]["content"]
            return '{"context_depth":"contextual","interaction_mode":"conversation","candidate_capsules":["examples/remote_work"],"reason":"Carte mémoire utile.","confidence":0.9}'

    route_context_depth(
        "Tu vois la référence musicale ?",
        CapturingProvider(),
        moment_memory="- Sujet actif : chaussures rétro / référence musicale",
        current_discussion="- Focus : cadeau familial",
        user_agenda="- Vendredi | Sam | dessin | 18h15-19h45.",
        recent_dialogue="J'ai parlé de chaussures rétro offertes.",
    )

    assert "You are Daemon Harness's memory radar" in captured["system"]
    assert 'short root map of existing souvenirs' in captured["system"]
    assert "candidate_capsules" in captured["system"]
    assert "active_request" in captured["system"]
    assert "ambiguity" in captured["system"]
    assert "memory_plan" in captured["system"]
    assert "Stance Register" not in captured["system"]
    assert "needs_stance_register" not in captured["system"]
    assert 'semantic contract' in captured["system"]
    assert 'distinguish their needs' in captured["system"]
    assert 'Compact map of views' in captured["user"]
    assert "scene_register" in captured["system"]
    assert 'Scene registers' in captured["system"]
    assert 'memory is a palette' in captured["system"]
    assert "A personal response requires the person's memory" in captured["system"]
    assert 'governs its length, never this coverage' in captured["system"]
    assert 'Rank sources' in captured["system"]
    assert 'A conversational opening may also require it' in captured[
        "system"
    ]
    assert 'exact content of a named work' in captured["system"]
    assert 'it never replaces this verification' in captured["system"]
    assert 'they already supplied the guiding material' in captured["system"]
    assert 'episodic archives of the past' in captured["system"]
    assert 'never a source of current state' in captured["system"]
    assert 'Do not propose them for agenda, schedules, tasks' in captured["system"]
    assert 'Current time' in captured["user"]
    assert 'Short agenda' in captured["user"]
    assert "18h15-19h45" in captured["user"]
    assert 'Root map of available souvenirs' in captured["user"]
    assert "examples/remote_work" in captured["user"]
    assert "chaussures rétro / référence musicale" in captured["user"]
    assert "final_capsules" not in captured["system"]
    assert 'Flat index of souvenirs' not in captured["user"]
    assert 'composes the decision in its actual situation' not in captured["system"]


def test_radar_ignores_legacy_stance_register_verdict():
    class LegacyProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            return json.dumps(
                {
                    "context_depth": "light",
                    "needs_stance_register": True,
                    "needs_carnet": True,
                    "reason": "Ancien contrat.",
                }
            )

    route = route_context_depth("Tu es là ?", LegacyProvider())

    assert route.needs_stance_register is False
    assert route.needs_carnet is False


def test_situated_context_composes_the_present_before_stable_profile(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_SITUATED_CONTEXT", "on")
    captured = {}

    class SituatedProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            captured["system"] = messages[0]["content"]
            captured["user"] = messages[1]["content"]
            return json.dumps(
                {
                    "context_depth": "contextual",
                    "active_request": "Choisir un déjeuner adapté à la scène présente.",
                    "ambiguity": "material",
                    "scene_frame": {
                        "established": ["Alex et Sam déjeunent ensemble."],
                        "uncertain": ["Les parents participent peut-être."],
                        "decision_effect": "Le nombre de convives change la commande.",
                        "resolution_mode": "clarify",
                    },
                    "memory_plan": {
                        "subjects": ["Alex", "Sam", "parents de Alex"],
                        "facets": ["goûts alimentaires"],
                        "needs": [],
                        "intent": "Choisir sans supposer qui déjeune sur place.",
                    },
                    "reason": "La présence des parents au déjeuner reste inconnue.",
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            )

    route = route_context_depth(
        "Choisis-moi quelque chose à commander ce midi.",
        SituatedProvider(),
        user_context="Les parents de Alex habitent à Belmont.",
        moment_memory="Alex et Sam séjournent chez ses parents à Belmont.",
        user_agenda="Alex et Sam sont arrivés à Belmont lundi.",
    )

    assert 'composes the decision in its actual situation' in captured["system"]
    assert 'never alone proves' in captured["system"]
    assert 'counterfactually test the plausible compositions' in captured["system"]
    assert 'An option is not robust merely' in captured["system"]
    assert 'must never serve to choose participants arbitrarily' in captured[
        "system"
    ]
    assert 'Build the plan from this composed scene' in captured["system"]
    assert captured["user"].index('Present situation') < captured["user"].index(
        'Stable facts'
    )
    assert route.ambiguity == "material"
    assert route.memory_plan.subjects == [
        "Alex",
        "Sam",
        "parents de Alex",
    ]
    assert route.scene_frame.established == ["Alex et Sam déjeunent ensemble."]
    assert route.scene_frame.uncertain == ["Les parents participent peut-être."]
    assert route.scene_frame.resolution_mode == "clarify"


def test_radar_final_selection_shadow_exposes_leaves_and_parses_proposal(
    tmp_path, monkeypatch
):
    memory_root = tmp_path / "memory"
    leaf = memory_root / "capsules" / "theme" / "famille" / "gouts_sam.md"
    leaf.parent.mkdir(parents=True)
    leaf.write_text(
        "---\nsubjects: [Sam]\nfacets: [goûts alimentaires]\n---\n"
        "# Goûts de Sam\n",
        encoding="utf-8",
    )
    (memory_root / "capsules" / "index.md").write_text(
        "- theme/famille/gouts_sam: Goûts alimentaires de Sam\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setenv("DAEMON_CORE_RADAR_FINAL_SELECTION", "shadow")
    captured = {}

    class ShadowProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            captured["system"] = messages[0]["content"]
            captured["user"] = messages[1]["content"]
            return json.dumps(
                {
                    "context_depth": "contextual",
                    "candidate_capsules": ["theme/famille/gouts_sam"],
                    "final_capsules": [
                        "theme/famille/gouts_sam",
                        "theme/inconnue",
                    ],
                    "final_selection_complete": True,
                    "final_selection_reason": "La feuille couvre entièrement la demande.",
                    "memory_plan": {
                        "subjects": ["Sam"],
                        "facets": ["goûts alimentaires"],
                    },
                    "reason": "La feuille précise couvre le besoin.",
                    "confidence": 0.94,
                },
                ensure_ascii=False,
            )

    route = route_context_depth("Que commander pour Sam ?", ShadowProvider())

    assert "final_capsules" in captured["system"]
    assert "final_selection_complete" in captured["system"]
    assert 'Flat index of souvenirs' in captured["user"]
    assert "theme/famille/gouts_sam" in captured["user"]
    assert route.radar_final_souvenirs == ["theme/famille/gouts_sam"]
    assert route.radar_final_complete is True
    assert route.radar_final_reason == "La feuille couvre entièrement la demande."


def test_templates_are_not_active_souvenirs():
    available = list_available_souvenirs()
    catalog = load_souvenir_index()

    assert "templates/card" not in available
    assert "templates/note" not in available
    assert "templates/card" not in catalog
    assert "templates/note" not in catalog


def test_radar_keeps_requested_initiative_distinct_from_closure():
    class InitiativeProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            return (
                '{"context_depth":"light","free_initiative_requested":true,'
                '"fil_clos":false,"active_request":"proposer un sujet neuf",'
                '"reason":"La personne confie le prochain mouvement au daemon.","confidence":0.94}'
            )

    route = route_context_depth("À toi de proposer un sujet.", InitiativeProvider())

    assert route.free_initiative_requested is True
    assert route.fil_clos is False


def test_context_route_keeps_old_verdicts_compatible_with_empty_memory_plan():
    route = route_context_depth("Bonjour", BrokenRouteProvider())

    assert route.memory_plan.is_empty is True


def test_radar_routes_explicit_conversation_changes_to_memory_judgment():
    class CapturingProvider(LLMProvider):
        def __init__(self):
            self.messages = []

        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            self.messages = messages
            return '{"context_depth":"contextual","reason":"Réglage durable.","confidence":0.9}'

    provider = CapturingProvider()

    route = route_context_depth(
        "À partir de maintenant, réponds plus brièvement.",
        provider,
    )

    assert route.mode == "contextual"
    assert 'change how you respond' in provider.messages[0]["content"]
    assert 'this adjustment must be able to be remembered' in provider.messages[0]["content"]


def test_context_router_cleans_memory_plan():
    class NoisyPlanProvider(LLMProvider):
        def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
            return json.dumps(
                {
                    "context_depth": "contextual",
                    "memory_plan": {
                        "subjects": [" Sam ", "sam", 42],
                        "needs": [
                            {
                                "description": "  goûts   alimentaires confirmés  ",
                                "subjects": ["Sam"],
                                "required": True,
                            },
                            {"description": "", "required": True},
                            "invalide",
                        ],
                        "intent": "  personnaliser   le déjeuner ",
                    },
                    "reason": "Test.",
                },
                ensure_ascii=False,
            )

    route = route_context_depth("Que commander pour Sam ?", NoisyPlanProvider())

    assert route.memory_plan.subjects == ["Sam"]
    assert route.memory_plan.intent == "personnaliser le déjeuner"
    assert [need.description for need in route.memory_plan.needs] == [
        'goûts alimentaires confirmés'
    ]
