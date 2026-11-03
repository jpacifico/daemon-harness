import json

import pytest

from daemon_core.memory.relation_dreaming import (
    RELATION_DREAMING_SCHEMA,
    RelationDreamingProtocolError,
    audit_relation_batch,
    build_relation_dreaming_messages,
    parse_relation_dreaming_output,
    validate_relation_dreaming_items,
)
from daemon_core.providers.base import LLMProvider


class StaticProvider(LLMProvider):
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, messages, temperature=0.4):
        self.calls.append((messages, temperature))
        return json.dumps(self.payload)


def _item(**overrides):
    value = {
        "id": "oasis-move",
        "mode": "proposal",
        "source_posture": "direct_fact",
        "proposed_action": "upsert",
        "existing_state": "absent",
        "source": "theme/loisirs/musique/coffrets_oasis",
        "target": "theme/famille/demenagement_parents",
        "role": "objet sauve",
        "user_evidence": "J'ai sauve les coffrets Oasis pendant ce demenagement.",
        "assistant_context": "Ils font partie de cet ensemble.",
        "source_state": "Les coffrets Oasis sont conserves.",
        "target_state": "Objets sauves pendant le demenagement.",
        "existing_relations": [],
        "neighbors": [
            {"name": "theme/loisirs/musique/oasis", "summary": "Le groupe."}
        ],
    }
    value.update(overrides)
    return value


def _payload(*decisions):
    return {"schema": RELATION_DREAMING_SCHEMA, "decisions": list(decisions)}


def _decision(
    item_id="oasis-move",
    disposition="confirm",
    action="upsert",
    reason="La preuve user etablit explicitement l'appartenance.",
):
    return {
        "item_id": item_id,
        "disposition": disposition,
        "action": action,
        "reason": reason,
    }


def test_audit_uses_one_provider_call_and_returns_no_write_capability():
    provider = StaticProvider(_payload(_decision()))

    audit = audit_relation_batch([_item()], provider=provider)

    assert len(provider.calls) == 1
    assert provider.calls[0][1] == 0.0
    assert audit.decisions[0].as_dict() == _decision()
    assert not hasattr(audit, "apply")


def test_prompt_exposes_only_bounded_one_hop_payload():
    messages = build_relation_dreaming_messages([_item()])

    assert len(messages) == 2
    assert 'no souvenir, fact or relation' in messages[0]["content"]
    assert 'itself be a member' in messages[0]["content"]
    assert 'cannot turn member_of' in messages[0]["content"]
    assert "theme/loisirs/musique/oasis" in messages[1]["content"]
    assert "neighbors" in messages[1]["content"]
    assert "children" not in messages[1]["content"]


def test_parser_preserves_input_order_even_if_model_reorders_decisions():
    first = _item()
    second = _item(
        id="cthulhu-move",
        source="theme/loisirs/jeux/appel_de_cthulhu",
    )
    raw = json.dumps(
        _payload(
            _decision(item_id="cthulhu-move"),
            _decision(item_id="oasis-move"),
        )
    )

    decisions = parse_relation_dreaming_output(raw, items=[first, second])

    assert [decision.item_id for decision in decisions] == [
        "oasis-move",
        "cthulhu-move",
    ]


@pytest.mark.parametrize(
    ("item", "decision"),
    [
        (_item(), _decision(disposition="reject", action="none")),
        (_item(), _decision(disposition="defer", action="none")),
        (
            _item(existing_state="present"),
            _decision(disposition="noop", action="none"),
        ),
        (
            _item(mode="maintenance", proposed_action="none"),
            _decision(disposition="repair", action="upsert"),
        ),
        (
            _item(
                mode="maintenance",
                proposed_action="none",
                existing_state="present",
            ),
            _decision(disposition="noop", action="none"),
        ),
        (
            _item(
                mode="maintenance",
                proposed_action="none",
                existing_state="present",
            ),
            _decision(disposition="repair", action="remove"),
        ),
    ],
)
def test_protocol_accepts_each_coherent_disposition(item, decision):
    parsed = parse_relation_dreaming_output(
        json.dumps(_payload(decision)),
        items=[item],
    )

    assert parsed[0].disposition == decision["disposition"]


@pytest.mark.parametrize(
    "payload",
    [
        _payload(),
        _payload(_decision(item_id="invented")),
        _payload(_decision(), _decision()),
        _payload({**_decision(), "source": "invented/path"}),
        _payload(_decision(disposition="reject", action="upsert")),
        _payload(_decision(disposition="repair", action="upsert")),
    ],
)
def test_parser_rejects_missing_invented_duplicate_or_incoherent_decisions(payload):
    with pytest.raises(RelationDreamingProtocolError):
        parse_relation_dreaming_output(json.dumps(payload), items=[_item()])


def test_noop_requires_action_to_be_already_satisfied():
    with pytest.raises(RelationDreamingProtocolError):
        parse_relation_dreaming_output(
            json.dumps(_payload(_decision(disposition="noop", action="none"))),
            items=[_item(existing_state="absent")],
        )


def test_validation_rejects_recursive_or_oversized_neighborhood_shape():
    recursive = _item(
        neighbors=[
            {
                "name": "theme/neighbor",
                "summary": "Un voisin.",
                "neighbors": ["theme/second-hop"],
            }
        ]
    )

    with pytest.raises(RelationDreamingProtocolError, match="unbounded"):
        validate_relation_dreaming_items([recursive])


def test_reported_memory_claim_is_transmitted_as_evidence_posture_not_rewritten():
    item = _item(
        source_posture="reported_memory_claim",
        user_evidence="Je te l'ai deja dit, retrouve-le.",
    )

    messages = build_relation_dreaming_messages([item])

    payload = json.loads(messages[1]["content"].split("\n\n", 1)[1])
    assert payload[0]["source_posture"] == "reported_memory_claim"
    assert payload[0]["user_evidence"] == "Je te l'ai deja dit, retrouve-le."
