import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.memory.relation_shadow import (
    memory_topology_bundle_shadow_enabled,
    validate_memory_collection_updates,
    validate_memory_relation_updates,
)
from daemon_core.memory.graph import parse_souvenir_graph
from daemon_core.memory_agent import (
    _apply_memory_agent_result,
    _build_memory_agent_messages,
    _proposed_collection_updates,
    _proposed_relation_updates,
    _with_authorized_relation_removals,
)
from daemon_core.providers.base import LLMProvider


MEMBER = "theme/loisirs/musique/maxi_oasis"
CENTER = "theme/famille/demenagement_parents/objets_a_sauver"
CARRIER = CENTER
MESSAGE = "Les coffrets Oasis font partie des objets que je veux sauver."
SOURCE_REF = "levels/L0_ground_zero/conversations/test#2026-08-06T12:00:00 — user"


def _complete_update(*, name=CARRIER, topology=None):
    update = {
        "name": name,
        "content_md": f"# Capsule: {name}\n\n## Faits\n- Ensemble confirme.",
        "index_line": f"- {name}: ensemble confirme",
    }
    if topology is not None:
        update["topology_updates"] = topology
    return update


def _bundle():
    return {
        "collections": [
            {
                "action": "declare_open",
                "target": CENTER,
                "epistemic": "established",
                "evidence": MESSAGE,
            }
        ],
        "relations": [
            {
                "action": "upsert",
                "kind": "member_of",
                "source": MEMBER,
                "target": CENTER,
                "role": "objet sauve",
                "epistemic": "established",
                "evidence": MESSAGE,
            }
        ],
    }


def test_topology_bundle_shadow_requires_its_own_user_scope(monkeypatch):
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_TOPOLOGY_BUNDLE_SHADOW",
        "shadow",
    )
    monkeypatch.delenv(
        "DAEMON_CORE_MEMORY_TOPOLOGY_BUNDLE_SHADOW_USER_IDS",
        raising=False,
    )

    assert memory_topology_bundle_shadow_enabled("garden") is False

    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_TOPOLOGY_BUNDLE_SHADOW_USER_IDS",
        "garden",
    )
    assert memory_topology_bundle_shadow_enabled("garden") is True
    assert memory_topology_bundle_shadow_enabled("another") is False


def test_topology_bundle_contract_replaces_only_the_nested_shadow_contract():
    candidate = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="",
        recent_dialogue="",
        user_message=MESSAGE,
        assistant_response="D'accord.",
        relation_shadow_enabled=True,
        collection_bootstrap_enabled=True,
        topology_bundle_enabled=True,
    )[0]["content"]

    assert 'Membership topology' in candidate
    assert '"topology_updates"' in candidate
    assert 'Declaration of a collection center' not in candidate
    assert 'implicitly `source`' not in candidate
    assert 'never replaces a necessary mutation' in candidate


def test_bundle_can_name_existing_endpoints_without_rewriting_them():
    data = {"capsule_updates": [_complete_update(topology=_bundle())]}

    relations = _proposed_relation_updates(
        data,
        topology_bundle_enabled=True,
    )
    relation_validation = validate_memory_relation_updates(
        relations,
        user_message=MESSAGE,
        available_souvenirs=(MEMBER, CENTER),
        source_ref=SOURCE_REF,
    )
    collections = _proposed_collection_updates(
        data,
        topology_bundle_enabled=True,
    )
    collection_validation = validate_memory_collection_updates(
        collections,
        user_message=MESSAGE,
        available_souvenirs=(MEMBER, CENTER),
        source_ref=SOURCE_REF,
        accepted_relations=relation_validation.accepted,
    )

    assert len(relation_validation.accepted) == 1
    assert relation_validation.accepted[0]["source"] == MEMBER
    assert len(collection_validation.accepted) == 1
    assert collection_validation.accepted[0]["target"] == CENTER


def test_bundle_mode_keeps_reading_the_legacy_nested_shape():
    data = {
        "capsule_updates": [
            {
                **_complete_update(name=MEMBER),
                "relation_updates": [
                    {
                        "action": "upsert",
                        "kind": "member_of",
                        "target": CENTER,
                        "role": "objet sauve",
                        "epistemic": "established",
                        "evidence": MESSAGE,
                    }
                ],
            },
            {
                **_complete_update(name=CENTER),
                "collection_update": {
                    "action": "declare_open",
                    "epistemic": "established",
                    "evidence": MESSAGE,
                },
            },
        ]
    }

    relations = _proposed_relation_updates(
        data,
        topology_bundle_enabled=True,
    )
    collections = _proposed_collection_updates(
        data,
        topology_bundle_enabled=True,
    )

    assert relations[0]["source"] == MEMBER
    assert collections[0]["target"] == CENTER


def test_root_level_parallel_topology_is_not_accepted():
    data = {
        "capsule_updates": [],
        "topology_updates": _bundle(),
    }

    assert _proposed_relation_updates(
        data,
        topology_bundle_enabled=True,
    ) == []
    assert _proposed_collection_updates(
        data,
        topology_bundle_enabled=True,
    ) == []


def test_incomplete_carrier_is_rejected_before_semantic_validation():
    data = {
        "capsule_updates": [
            {
                "name": CARRIER,
                "topology_updates": _bundle(),
            }
        ]
    }

    relations = _proposed_relation_updates(
        data,
        topology_bundle_enabled=True,
    )
    collections = _proposed_collection_updates(
        data,
        topology_bundle_enabled=True,
    )

    assert relations == "topology_carrier_incomplete"
    assert collections == "topology_carrier_incomplete"


def test_multiple_topology_bundles_are_rejected():
    data = {
        "capsule_updates": [
            _complete_update(topology=_bundle()),
            _complete_update(
                name="theme/projet/autre",
                topology={"collections": [], "relations": []},
            ),
        ]
    }

    assert _proposed_relation_updates(
        data,
        topology_bundle_enabled=True,
    ) == "multiple_topology_bundles"
    assert _proposed_collection_updates(
        data,
        topology_bundle_enabled=True,
    ) == "multiple_topology_bundles"


def _existing_member_content(*, fact="Un coffret Oasis est conserve."):
    return (
        "# Capsule: theme/loisirs/musique/maxi_oasis\n\n"
        "## Faits\n"
        f"- {fact}\n\n"
        "## Relations\n"
        f"- member_of [[{CENTER}]] : objet sauve\n"
        "  - source: [[levels/L0_ground_zero/conversations/test#turn-1]]\n"
    )


class _MergeProvider(LLMProvider):
    def __init__(self, payload):
        self.payload = payload
        self.messages = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages.append(messages)
        return json.dumps(self.payload, ensure_ascii=False)


def _apply_existing_member_update(
    tmp_path,
    monkeypatch,
    *,
    provider,
    user_message,
    proposed_content,
    performative_relation_retractions=False,
):
    memory_root = tmp_path / "memory"
    path = memory_root / "capsules" / f"{MEMBER}.md"
    path.parent.mkdir(parents=True)
    path.write_text(_existing_member_content(), encoding="utf-8")
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)
    result = _apply_memory_agent_result(
        user_id="test-user",
        data={
            "capsule_updates": [
                {
                    "name": MEMBER,
                    "content_md": proposed_content,
                    "index_line": f"- {MEMBER}: coffret Oasis",
                }
            ]
        },
        user_message=user_message,
        provider=provider,
        derive_relation_removals=True,
        performative_relation_retractions=performative_relation_retractions,
    )
    return result, path


def test_authorized_markdown_removal_becomes_one_sourced_topology_removal(
    tmp_path,
    monkeypatch,
):
    message = "Le coffret Oasis ne faisait finalement pas partie des objets sauves."
    relation_line = f"- member_of [[{CENTER}]] : objet sauve"
    provider = _MergeProvider(
        {
            "content_md": (
                "# Capsule: theme/loisirs/musique/maxi_oasis\n\n"
                "## Faits\n"
                "- Un coffret Oasis est conserve.\n"
                "- Il ne faisait pas partie des objets sauves."
            ),
            "removed_existing_lines": [
                {"line": relation_line, "evidence": message}
            ],
            "reason": "Correction explicite.",
        }
    )

    result, path = _apply_existing_member_update(
        tmp_path,
        monkeypatch,
        provider=provider,
        user_message=message,
        proposed_content=(
            "# Capsule: theme/loisirs/musique/maxi_oasis\n\n"
            "## Faits\n- Il ne faisait pas partie des objets sauves."
        ),
    )

    assert result.updated_souvenirs == [MEMBER]
    assert result.authorized_relation_removals == [
        {
            "action": "remove",
            "kind": "member_of",
            "epistemic": "established",
            "source": MEMBER,
            "target": CENTER,
            "role": "",
            "evidence": message,
        }
    ]
    assert parse_souvenir_graph(
        MEMBER,
        path.read_text(encoding="utf-8"),
    ).relations == ()
    assert 'bullets in `## Relations`' in provider.messages[0][0]["content"]


def test_performative_relation_retraction_flows_through_memory_agent_result(
    tmp_path,
    monkeypatch,
):
    message = "Le coffret Oasis ne faisait finalement pas partie des objets sauves."
    relation_line = f"- member_of [[{CENTER}]] : objet sauve"
    provider = _MergeProvider(
        {
            "content_md": _existing_member_content(),
            "removed_existing_lines": [],
            "existing_relation_decisions": [
                {
                    "line": relation_line,
                    "action": "remove",
                    "evidence": message,
                }
            ],
            "discovery_checks": [],
            "reason": "Correction explicite.",
        }
    )

    result, path = _apply_existing_member_update(
        tmp_path,
        monkeypatch,
        provider=provider,
        user_message=message,
        proposed_content=(
            "# Capsule: theme/loisirs/musique/maxi_oasis\n\n"
            "## Faits\n- Un coffret Oasis est conserve."
        ),
        performative_relation_retractions=True,
    )

    assert result.updated_souvenirs == [MEMBER]
    assert result.authorized_relation_removals == [
        {
            "action": "remove",
            "kind": "member_of",
            "epistemic": "established",
            "source": MEMBER,
            "target": CENTER,
            "role": "",
            "evidence": message,
        }
    ]
    assert parse_souvenir_graph(
        MEMBER,
        path.read_text(encoding="utf-8"),
    ).relations == ()
    assert "existing_relation_decisions" in provider.messages[0][0]["content"]


def test_relation_omission_without_exact_authorization_rejects_souvenir_update(
    tmp_path,
    monkeypatch,
):
    original = _existing_member_content()
    provider = _MergeProvider(
        {
            "content_md": (
                "# Capsule: theme/loisirs/musique/maxi_oasis\n\n"
                "## Faits\n- Un coffret Oasis est conserve."
            ),
            "removed_existing_lines": [],
            "reason": "Omission accidentelle.",
        }
    )

    result, path = _apply_existing_member_update(
        tmp_path,
        monkeypatch,
        provider=provider,
        user_message="Le coffret est toujours important.",
        proposed_content="# Capsule\n\n## Faits\n- Le coffret est important.",
    )

    assert result.updated_souvenirs == []
    assert result.skipped_souvenirs == [MEMBER]
    assert result.authorized_relation_removals == []
    assert path.read_text(encoding="utf-8") == original


def test_factual_correction_preserves_membership_without_derived_removal(
    tmp_path,
    monkeypatch,
):
    relation_line = f"- member_of [[{CENTER}]] : objet sauve"
    provider = _MergeProvider(
        {
            "content_md": (
                "# Capsule: theme/loisirs/musique/maxi_oasis\n\n"
                "## Faits\n- Deux coffrets Oasis sont conserves.\n\n"
                "## Relations\n"
                f"{relation_line}\n"
                "  - source: [[levels/L0_ground_zero/conversations/test#turn-1]]"
            ),
            "removed_existing_lines": [
                {
                    "line": "- Un coffret Oasis est conserve.",
                    "evidence": "J'en conserve deux",
                }
            ],
            "reason": "Quantite corrigee, relation conservee.",
        }
    )

    result, path = _apply_existing_member_update(
        tmp_path,
        monkeypatch,
        provider=provider,
        user_message="J'en conserve deux, toujours parmi les objets sauves.",
        proposed_content=(
            "# Capsule\n\n## Faits\n- Deux coffrets Oasis sont conserves.\n\n"
            "## Relations\n"
            f"{relation_line}"
        ),
    )

    assert result.updated_souvenirs == [MEMBER]
    assert result.authorized_relation_removals == []
    relations = parse_souvenir_graph(MEMBER, path.read_text(encoding="utf-8")).relations
    assert len(relations) == 1
    assert relations[0].target == CENTER


def test_authorized_removal_does_not_duplicate_a_model_authored_remove():
    raw = [
        {
            "action": "remove",
            "kind": "member_of",
            "epistemic": "established",
            "source": MEMBER,
            "target": CENTER,
            "role": "",
            "evidence": MESSAGE,
        }
    ]

    combined = _with_authorized_relation_removals(raw, list(raw))

    assert combined == raw
