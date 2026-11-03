import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.memory import relation_shadow, writer
from daemon_core.memory.relation_shadow import (
    memory_collection_bootstrap_shadow_enabled,
    memory_relation_shadow_enabled,
    prepare_memory_relation_shadow_context,
    read_memory_relation_shadow_events,
    record_memory_relation_shadow_result,
    validate_memory_collection_updates,
    validate_memory_relation_updates,
)
from daemon_core.memory.relation_outbox import run_relation_publication_dry_run
from daemon_core.memory.writer import append_ground_zero
from daemon_core.memory_agent import (
    MemoryAgentResult,
    _build_memory_agent_messages,
    run_memory_agent,
)
from daemon_core.providers.base import LLMProvider


MEMBER = "theme/loisirs/collection/coffrets_oasis"
COLLECTION = "theme/famille/demenagement_parents"


def _l0_reference(tmp_path, monkeypatch, message, *, turn_id="turn-1"):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr(writer, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(relation_shadow, "MEMORY_ROOT", memory_root)
    reference = append_ground_zero(
        "shadow-user",
        "user",
        message,
        turn_id=turn_id,
        return_reference=True,
    )
    return memory_root, reference


def _valid_update(message):
    return {
        "action": "upsert",
        "kind": "member_of",
        "epistemic": "established",
        "source": MEMBER,
        "target": COLLECTION,
        "role": "objet sauve avant le demenagement",
        "evidence": message,
    }


def test_relation_shadow_is_disabled_without_explicit_user_scope(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_MEMORY_RELATION_SHADOW", "shadow")
    monkeypatch.delenv(
        "DAEMON_CORE_MEMORY_RELATION_SHADOW_USER_IDS",
        raising=False,
    )

    assert memory_relation_shadow_enabled("garden") is False

    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_RELATION_SHADOW_USER_IDS",
        "garden",
    )
    assert memory_relation_shadow_enabled("garden") is True
    assert memory_relation_shadow_enabled("another") is False


def test_collection_bootstrap_shadow_requires_its_own_explicit_scope(monkeypatch):
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_COLLECTION_BOOTSTRAP_SHADOW",
        "shadow",
    )
    monkeypatch.delenv(
        "DAEMON_CORE_MEMORY_COLLECTION_BOOTSTRAP_SHADOW_USER_IDS",
        raising=False,
    )

    assert memory_collection_bootstrap_shadow_enabled("garden") is False

    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_COLLECTION_BOOTSTRAP_SHADOW_USER_IDS",
        "garden",
    )
    assert memory_collection_bootstrap_shadow_enabled("garden") is True
    assert memory_collection_bootstrap_shadow_enabled("another") is False


def test_relation_shadow_prompt_is_absent_by_default_and_bounded_when_enabled():
    baseline = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="",
        recent_dialogue="",
        user_message="Je garde ce coffret pour le demenagement.",
        assistant_response="D'accord.",
    )[0]["content"]
    candidate = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="",
        recent_dialogue="",
        user_message="Je garde ce coffret pour le demenagement.",
        assistant_response="D'accord.",
        relation_shadow_enabled=True,
    )[0]["content"]

    assert "relation_updates" not in baseline
    assert "Relations d'appartenance" not in baseline
    assert '"relation_updates"' in candidate
    assert 'At most three' in candidate
    assert 'abstention is normal' in candidate
    assert 'implicitly `source`' in candidate


def test_collection_bootstrap_contract_is_separate_from_relation_contract():
    relation_only = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="",
        recent_dialogue="",
        user_message="Ce coffret appartient a cet episode.",
        assistant_response="D'accord.",
        relation_shadow_enabled=True,
    )[0]["content"]
    candidate = _build_memory_agent_messages(
        user_context="",
        conversation_manner="",
        current_discussion="",
        souvenir_catalog="",
        recent_dialogue="",
        user_message="Ce coffret appartient a cet episode.",
        assistant_response="D'accord.",
        relation_shadow_enabled=True,
        collection_bootstrap_enabled=True,
    )[0]["content"]

    assert "collection_update" not in relation_only
    assert 'Declaration of a collection center' not in relation_only
    assert '"collection_update"' in candidate
    assert 'at least one `upsert member_of`' in candidate
    assert 'center already marked' in candidate


def test_relation_shadow_derives_exact_obsidian_l0_provenance(
    tmp_path,
    monkeypatch,
):
    message = "Les coffrets Oasis font partie de ce que je sauve du demenagement."
    _, reference = _l0_reference(tmp_path, monkeypatch, message)

    context = prepare_memory_relation_shadow_context(
        current_user_l0=reference,
        user_message=message,
    )

    assert context is not None
    assert context.current_user_l0 == reference
    assert context.source_ref.startswith(
        "levels/L0_ground_zero/conversations/shadow-user_"
    )
    assert context.source_ref.endswith(" — user")
    assert ".md#" not in context.source_ref


def test_relation_shadow_refuses_a_reference_for_another_message(
    tmp_path,
    monkeypatch,
):
    _, reference = _l0_reference(tmp_path, monkeypatch, "Message exact.")

    context = prepare_memory_relation_shadow_context(
        current_user_l0=reference,
        user_message="Autre message.",
    )

    assert context is None


def test_relation_shadow_records_valid_membership_without_mutating_souvenirs(
    tmp_path,
    monkeypatch,
):
    message = "Les coffrets Oasis font partie de ce que je sauve du demenagement."
    memory_root, reference = _l0_reference(tmp_path, monkeypatch, message)
    context = prepare_memory_relation_shadow_context(
        current_user_l0=reference,
        user_message=message,
    )
    assert context is not None

    receipt = record_memory_relation_shadow_result(
        user_id="shadow-user",
        context=context,
        user_message=message,
        raw_updates=[_valid_update(message)],
        available_souvenirs=[MEMBER, COLLECTION],
    )

    assert receipt.status == "recorded"
    assert receipt.accepted_count == 1
    assert receipt.rejected_count == 0
    events = read_memory_relation_shadow_events("shadow-user")
    assert len(events) == 1
    assert events[0]["accepted"][0]["source_refs"] == [context.source_ref]
    assert not (memory_root / "capsules").exists()
    assert not (memory_root / "exports").exists()


def test_relation_shadow_refuses_invalid_evidence_unknown_paths_and_duplicates(
    tmp_path,
    monkeypatch,
):
    message = "Les coffrets Oasis font partie de ce que je sauve du demenagement."
    _, reference = _l0_reference(tmp_path, monkeypatch, message)
    context = prepare_memory_relation_shadow_context(
        current_user_l0=reference,
        user_message=message,
    )
    assert context is not None
    invalid_evidence = _valid_update(message)
    invalid_evidence["evidence"] = "citation absente"
    unknown = _valid_update(message)
    unknown["target"] = "theme/inconnu/collection"
    duplicate = _valid_update(message)

    receipt = record_memory_relation_shadow_result(
        user_id="shadow-user",
        context=context,
        user_message=message,
        raw_updates=[
            invalid_evidence,
            unknown,
            _valid_update(message),
        ],
        available_souvenirs=[MEMBER, COLLECTION],
    )

    assert receipt.status == "partial"
    assert receipt.accepted_count == 1
    assert receipt.rejected_count == 2
    event = read_memory_relation_shadow_events("shadow-user")[0]
    assert [item["reason_code"] for item in event["rejected"]] == [
        "evidence_not_exact",
        "unknown_target_capsule",
    ]

    duplicate_receipt = record_memory_relation_shadow_result(
        user_id="shadow-user",
        context=context,
        user_message=message,
        raw_updates=[duplicate],
        available_souvenirs=[MEMBER, COLLECTION],
    )
    assert duplicate_receipt.status == "duplicate"


def test_relation_shadow_accepts_souvenirs_proposed_in_the_same_judgment(
    tmp_path,
    monkeypatch,
):
    message = "Je rattache ce carnet a mon projet de roman."
    _, reference = _l0_reference(tmp_path, monkeypatch, message)
    context = prepare_memory_relation_shadow_context(
        current_user_l0=reference,
        user_message=message,
    )
    assert context is not None
    update = {
        "action": "upsert",
        "kind": "member_of",
        "epistemic": "established",
        "source": "theme/projet/roman/carnet",
        "target": "theme/projet/roman",
        "role": "matiere de travail du roman",
        "evidence": message,
    }

    receipt = record_memory_relation_shadow_result(
        user_id="shadow-user",
        context=context,
        user_message=message,
        raw_updates=[update],
        available_souvenirs=[],
        proposed_souvenirs=["theme/projet/roman/carnet", "theme/projet/roman"],
    )

    assert receipt.status == "recorded"


def test_relation_shadow_refuses_membership_not_judged_established():
    message = "Je prendrai peut-etre la platine pour le demenagement."
    update = _valid_update(message)
    update["epistemic"] = "possible"

    validation = validate_memory_relation_updates(
        [update],
        user_message=message,
        available_souvenirs=[MEMBER, COLLECTION],
        source_ref=(
            "levels/L0_ground_zero/conversations/eval#"
            "2026-08-06T12:00:00 — user"
        ),
    )

    assert validation.accepted == ()
    assert validation.rejected[0]["reason_code"] == "membership_not_established"


def test_collection_bootstrap_accepts_only_a_sourced_target_with_a_member():
    message = "Les coffrets Oasis font partie des objets sauves du demenagement."
    source_ref = (
        "levels/L0_ground_zero/conversations/eval#"
        "2026-08-06T12:00:00 — user"
    )
    relation_validation = validate_memory_relation_updates(
        [_valid_update(message)],
        user_message=message,
        available_souvenirs=[MEMBER, COLLECTION],
        source_ref=source_ref,
    )
    proposal = {
        "action": "declare_open",
        "epistemic": "established",
        "target": COLLECTION,
        "evidence": message,
    }

    accepted = validate_memory_collection_updates(
        [proposal],
        user_message=message,
        available_souvenirs=[MEMBER, COLLECTION],
        source_ref=source_ref,
        accepted_relations=relation_validation.accepted,
    )
    orphan = validate_memory_collection_updates(
        [proposal],
        user_message=message,
        available_souvenirs=[MEMBER, COLLECTION],
        source_ref=source_ref,
        accepted_relations=[],
    )

    assert accepted.rejected == ()
    assert accepted.accepted[0]["target"] == COLLECTION
    assert accepted.accepted[0]["source_refs"] == [source_ref]
    assert orphan.accepted == ()
    assert orphan.rejected[0]["reason_code"] == (
        "collection_without_accepted_member"
    )


def test_collection_bootstrap_records_v2_envelope_without_mutating_souvenirs(
    tmp_path,
    monkeypatch,
):
    message = "Les coffrets Oasis font partie des objets sauves du demenagement."
    memory_root, reference = _l0_reference(tmp_path, monkeypatch, message)
    context = prepare_memory_relation_shadow_context(
        current_user_l0=reference,
        user_message=message,
    )
    assert context is not None

    receipt = record_memory_relation_shadow_result(
        user_id="shadow-user",
        context=context,
        user_message=message,
        raw_updates=[_valid_update(message)],
        raw_collection_updates=[
            {
                "action": "declare_open",
                "epistemic": "established",
                "target": COLLECTION,
                "evidence": message,
            }
        ],
        available_souvenirs=[MEMBER, COLLECTION],
    )

    assert receipt.status == "recorded"
    assert receipt.accepted_count == 1
    assert receipt.accepted_collection_count == 1
    event = read_memory_relation_shadow_events("shadow-user")[0]
    assert event["schema"] == "daemon.memory-relation-shadow.v2"
    assert event["accepted_collections"][0]["source_refs"] == [
        context.source_ref
    ]
    assert not (memory_root / "capsules").exists()


class _RelationProvider(LLMProvider):
    def __init__(self, message, *, posture="direct_fact"):
        self.calls = 0
        self.messages = []
        self.message = message
        self.posture = posture

    def complete(self, messages, temperature=0.4):
        self.calls += 1
        self.messages = messages
        relation = _valid_update(self.message)
        relation.pop("source")
        return json.dumps(
            {
                "reason": "Relation proposee en ombre.",
                "source_posture": self.posture,
                "user_context_md": None,
                "conversation_update": {
                    "should_update": False,
                    "evidence": "",
                    "conversation_md": None,
                },
                "persona_update": {
                    "should_update": False,
                    "evidence": "",
                    "persona_md": None,
                },
                "agenda_md": None,
                "active_context_md": None,
                "immediate_focus_md": None,
                "capsule_index_md": None,
                "capsule_updates": [
                    {
                        "name": MEMBER,
                        "content_md": "# Capsule\n\n## Faits\n- Fait preserve.",
                        "index_line": f"- {MEMBER}: coffrets Oasis",
                        "relation_updates": [relation],
                    }
                ],
            }
        )


class _CollectionBootstrapProvider(_RelationProvider):
    def complete(self, messages, temperature=0.4):
        self.calls += 1
        self.messages = messages
        relation = _valid_update(self.message)
        relation.pop("source")
        return json.dumps(
            {
                "reason": "Centre et relation proposes en ombre.",
                "source_posture": self.posture,
                "user_context_md": None,
                "conversation_update": {
                    "should_update": False,
                    "evidence": "",
                    "conversation_md": None,
                },
                "persona_update": {
                    "should_update": False,
                    "evidence": "",
                    "persona_md": None,
                },
                "agenda_md": None,
                "active_context_md": None,
                "immediate_focus_md": None,
                "capsule_index_md": None,
                "capsule_updates": [
                    {
                        "name": MEMBER,
                        "content_md": "# Membre\n\n## Faits\n- Fait preserve.",
                        "index_line": f"- {MEMBER}: coffrets Oasis",
                        "relation_updates": [relation],
                    },
                    {
                        "name": COLLECTION,
                        "content_md": "# Centre\n\n## Resume\n- Episode preserve.",
                        "index_line": f"- {COLLECTION}: demenagement",
                        "collection_update": {
                            "action": "declare_open",
                            "epistemic": "established",
                            "evidence": self.message,
                        },
                    },
                ],
            }
        )


class _TopologyBundleProvider(_RelationProvider):
    def complete(self, messages, temperature=0.4):
        self.calls += 1
        self.messages = messages
        return json.dumps(
            {
                "reason": "Topologie proposee dans une mutation complete.",
                "source_posture": self.posture,
                "user_context_md": None,
                "conversation_update": {
                    "should_update": False,
                    "evidence": "",
                    "conversation_md": None,
                },
                "persona_update": {
                    "should_update": False,
                    "evidence": "",
                    "persona_md": None,
                },
                "agenda_md": None,
                "active_context_md": None,
                "immediate_focus_md": None,
                "capsule_index_md": None,
                "capsule_updates": [
                    {
                        "name": COLLECTION,
                        "content_md": "# Centre\n\n## Resume\n- Episode preserve.",
                        "index_line": f"- {COLLECTION}: demenagement",
                        "topology_updates": {
                            "collections": [
                                {
                                    "action": "declare_open",
                                    "target": COLLECTION,
                                    "epistemic": "established",
                                    "evidence": self.message,
                                }
                            ],
                            "relations": [_valid_update(self.message)],
                        },
                    }
                ],
            }
        )


def _stub_memory_agent_reads(monkeypatch):
    for name in (
        "load_user_context",
        "load_conversation_manner",
        "load_daemon_persona",
        "load_user_agenda",
        "load_active_context",
        "load_immediate_focus",
        "load_recent_dialogue",
        "load_named_souvenir_index",
        "load_souvenir_structural_catalog",
        "load_souvenir_facet_label_catalog",
    ):
        monkeypatch.setattr(f"daemon_core.memory_agent.{name}", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        "daemon_core.memory_agent.list_available_souvenirs",
        lambda: [MEMBER, COLLECTION],
    )
    monkeypatch.setattr(
        "daemon_core.memory_agent.load_named_souvenirs",
        lambda _names: {},
    )
    monkeypatch.setattr(
        "daemon_core.memory_agent._apply_memory_agent_result",
        lambda *_args, **_kwargs: MemoryAgentResult(reason="shadow test"),
    )


def _stub_memory_agent_context_reads(monkeypatch, souvenirs):
    for name in (
        "load_user_context",
        "load_conversation_manner",
        "load_daemon_persona",
        "load_user_agenda",
        "load_active_context",
        "load_immediate_focus",
        "load_recent_dialogue",
        "load_named_souvenir_index",
        "load_souvenir_structural_catalog",
        "load_souvenir_facet_label_catalog",
    ):
        monkeypatch.setattr(
            f"daemon_core.memory_agent.{name}",
            lambda *_args, **_kwargs: "",
        )
    monkeypatch.setattr(
        "daemon_core.memory_agent.list_available_souvenirs",
        lambda: list(souvenirs),
    )
    monkeypatch.setattr(
        "daemon_core.memory_agent.load_named_souvenirs",
        lambda names: {name: souvenirs[name] for name in names if name in souvenirs},
    )


class _TopologyRemovalProvider(LLMProvider):
    def __init__(self, message):
        self.calls = 0
        self.message = message

    def complete(self, messages, temperature=0.4):
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "reason": "Correction de l'appartenance.",
                    "source_posture": "direct_fact",
                    "conversation_update": {"should_update": False},
                    "persona_update": {"should_update": False},
                    "capsule_updates": [
                        {
                            "name": MEMBER,
                            "content_md": (
                                "# Membre\n\n## Faits\n"
                                "- Le coffret reste conserve.\n"
                                "- Il ne fait plus partie du demenagement."
                            ),
                            "index_line": f"- {MEMBER}: coffret conserve",
                        }
                    ],
                }
            )
        relation_line = f"- member_of [[{COLLECTION}]] : objet sauve"
        return json.dumps(
            {
                "content_md": (
                    "# Membre\n\n## Faits\n"
                    "- Le coffret reste conserve.\n"
                    "- Il ne fait plus partie du demenagement."
                ),
                "removed_existing_lines": [
                    {"line": relation_line, "evidence": self.message}
                ],
                "reason": "Suppression explicitement autorisee.",
            }
        )


class _PerformativeTopologyRemovalProvider(_TopologyRemovalProvider):
    def complete(self, messages, temperature=0.4):
        if self.calls == 0:
            return super().complete(messages, temperature)
        self.calls += 1
        relation_line = f"- member_of [[{COLLECTION}]] : objet sauve"
        return json.dumps(
            {
                "content_md": (
                    "# Membre\n\n## Faits\n"
                    "- Le coffret reste conserve.\n"
                    "- Il ne fait plus partie du demenagement.\n\n"
                    "## Relations\n"
                    f"{relation_line}"
                ),
                "removed_existing_lines": [],
                "existing_relation_decisions": [
                    {
                        "line": relation_line,
                        "action": "remove",
                        "evidence": self.message,
                    }
                ],
                "reason": "Suppression explicitement autorisee.",
            }
        )


def test_memory_agent_reuses_one_call_and_only_returns_a_shadow_receipt(
    tmp_path,
    monkeypatch,
):
    message = "Les coffrets Oasis font partie de ce que je sauve du demenagement."
    memory_root, reference = _l0_reference(tmp_path, monkeypatch, message)
    _stub_memory_agent_reads(monkeypatch)
    provider = _RelationProvider(message)

    result = run_memory_agent(
        user_id="shadow-user",
        provider=provider,
        user_message=message,
        assistant_response="Je le garde en tete.",
        selected_souvenirs=[],
        followups_enabled=False,
        current_user_l0=reference,
        relation_shadow_enabled_override=True,
    )

    assert provider.calls == 1
    assert "relation_updates" in provider.messages[0]["content"]
    assert result.relation_shadow_status == "recorded"
    assert result.relation_shadow_accepted_count == 1
    assert not (memory_root / "capsules").exists()


def test_memory_agent_bootstrap_reuses_the_same_single_llm_call(
    tmp_path,
    monkeypatch,
):
    message = "Les coffrets Oasis font partie de ce que je sauve du demenagement."
    memory_root, reference = _l0_reference(tmp_path, monkeypatch, message)
    _stub_memory_agent_reads(monkeypatch)
    provider = _CollectionBootstrapProvider(message)

    result = run_memory_agent(
        user_id="shadow-user",
        provider=provider,
        user_message=message,
        assistant_response="Je le garde en tete.",
        selected_souvenirs=[],
        followups_enabled=False,
        current_user_l0=reference,
        relation_shadow_enabled_override=True,
        collection_bootstrap_enabled_override=True,
    )

    assert provider.calls == 1
    assert "collection_update" in provider.messages[0]["content"]
    assert result.relation_shadow_status == "recorded"
    assert result.relation_shadow_accepted_count == 1
    assert result.collection_shadow_accepted_count == 1
    assert not (memory_root / "capsules").exists()


def test_memory_agent_topology_bundle_reuses_one_call_and_full_carrier(
    tmp_path,
    monkeypatch,
):
    message = "Les coffrets Oasis font partie de ce que je sauve du demenagement."
    memory_root, reference = _l0_reference(tmp_path, monkeypatch, message)
    _stub_memory_agent_reads(monkeypatch)
    provider = _TopologyBundleProvider(message)

    result = run_memory_agent(
        user_id="shadow-user",
        provider=provider,
        user_message=message,
        assistant_response="Je le garde en tete.",
        selected_souvenirs=[],
        followups_enabled=False,
        current_user_l0=reference,
        relation_shadow_enabled_override=True,
        collection_bootstrap_enabled_override=True,
        topology_bundle_enabled_override=True,
    )

    assert provider.calls == 1
    assert "topology_updates" in provider.messages[0]["content"]
    assert result.relation_shadow_status == "recorded"
    assert result.relation_shadow_accepted_count == 1
    assert result.collection_shadow_accepted_count == 1
    assert not (memory_root / "capsules").exists()


def test_relation_retraction_canary_cannot_bypass_the_topology_bundle(
    tmp_path,
    monkeypatch,
):
    message = "Les coffrets Oasis font partie de ce que je sauve du demenagement."
    _, reference = _l0_reference(tmp_path, monkeypatch, message)
    _stub_memory_agent_reads(monkeypatch)
    captured = {}

    def _capture_apply(*_args, **kwargs):
        captured.update(kwargs)
        return MemoryAgentResult(reason="captured")

    monkeypatch.setattr(
        "daemon_core.memory_agent._apply_memory_agent_result",
        _capture_apply,
    )
    provider = _TopologyBundleProvider(message)

    run_memory_agent(
        user_id="shadow-user",
        provider=provider,
        user_message=message,
        assistant_response="Je le garde en tete.",
        selected_souvenirs=[],
        followups_enabled=False,
        current_user_l0=reference,
        relation_shadow_enabled_override=True,
        collection_bootstrap_enabled_override=True,
        topology_bundle_enabled_override=False,
        performative_relation_retractions_enabled_override=True,
    )

    assert captured["derive_relation_removals"] is False
    assert captured["performative_relation_retractions"] is False


def test_authorized_souvenir_correction_records_removal_then_outbox_rebuilds(
    tmp_path,
    monkeypatch,
):
    message = "Ce coffret ne fait finalement plus partie du demenagement."
    memory_root, reference = _l0_reference(tmp_path, monkeypatch, message)
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)
    source_ref = (
        f"{reference.path.removesuffix('.md')}#"
        "2026-08-06T12:00:00 — user"
    )
    source_content = (
        "# Membre\n\n## Faits\n- Le coffret reste conserve.\n\n"
        "## Relations\n"
        f"- member_of [[{COLLECTION}]] : objet sauve\n"
        f"  - source: [[{source_ref}]]\n"
    )
    target_content = (
        "---\ncollection:\n  coverage: open\n---\n"
        "# Demenagement\n"
    )
    souvenirs = {MEMBER: source_content, COLLECTION: target_content}
    for name, content in souvenirs.items():
        path = memory_root / "capsules" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _stub_memory_agent_context_reads(monkeypatch, souvenirs)
    provider = _TopologyRemovalProvider(message)

    result = run_memory_agent(
        user_id="shadow-user",
        provider=provider,
        user_message=message,
        assistant_response="Je corrige.",
        selected_souvenirs=[MEMBER, COLLECTION],
        followups_enabled=False,
        current_user_l0=reference,
        relation_shadow_enabled_override=True,
        collection_bootstrap_enabled_override=True,
        topology_bundle_enabled_override=True,
    )

    assert provider.calls == 2
    assert result.relation_shadow_status == "recorded"
    assert result.relation_shadow_accepted_count == 1
    event = read_memory_relation_shadow_events("shadow-user")[0]
    assert event["accepted"][0]["action"] == "remove"
    assert event["accepted"][0]["source"] == MEMBER
    assert event["accepted"][0]["target"] == COLLECTION
    updated = (memory_root / "capsules" / f"{MEMBER}.md").read_text(
        encoding="utf-8"
    )
    assert "member_of" not in updated

    receipt = run_relation_publication_dry_run(
        user_id="shadow-user",
        event_id=result.relation_shadow_event_id,
        memory_root=memory_root,
        receipt_root=tmp_path / "receipts",
    )

    assert receipt.status == "unchanged"
    assert receipt.unchanged_count == 1


def test_relation_only_canary_executes_through_the_real_memory_agent_path(
    tmp_path,
    monkeypatch,
):
    message = "Ce coffret ne fait finalement plus partie du demenagement."
    memory_root, reference = _l0_reference(tmp_path, monkeypatch, message)
    monkeypatch.setattr("daemon_core.memory_agent.MEMORY_ROOT", memory_root)
    source_ref = (
        f"{reference.path.removesuffix('.md')}#"
        "2026-08-06T12:00:00 — user"
    )
    source_content = (
        "# Membre\n\n## Faits\n- Le coffret reste conserve.\n\n"
        "## Relations\n"
        f"- member_of [[{COLLECTION}]] : objet sauve\n"
        f"  - source: [[{source_ref}]]\n"
    )
    target_content = (
        "---\ncollection:\n  coverage: open\n---\n"
        "# Demenagement\n"
    )
    souvenirs = {MEMBER: source_content, COLLECTION: target_content}
    for name, content in souvenirs.items():
        path = memory_root / "capsules" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _stub_memory_agent_context_reads(monkeypatch, souvenirs)
    provider = _PerformativeTopologyRemovalProvider(message)

    result = run_memory_agent(
        user_id="shadow-user",
        provider=provider,
        user_message=message,
        assistant_response="Je corrige.",
        selected_souvenirs=[MEMBER, COLLECTION],
        followups_enabled=False,
        current_user_l0=reference,
        relation_shadow_enabled_override=True,
        collection_bootstrap_enabled_override=True,
        topology_bundle_enabled_override=True,
        performative_relation_retractions_enabled_override=True,
    )

    assert provider.calls == 2
    assert result.relation_shadow_status == "recorded"
    assert result.relation_shadow_accepted_count == 1
    event = read_memory_relation_shadow_events("shadow-user")[0]
    assert event["accepted"][0]["action"] == "remove"
    assert event["accepted"][0]["source"] == MEMBER
    assert event["accepted"][0]["target"] == COLLECTION
    updated = (memory_root / "capsules" / f"{MEMBER}.md").read_text(
        encoding="utf-8"
    )
    assert "member_of" not in updated


def test_reported_memory_claim_cannot_emit_a_shadow_relation(
    tmp_path,
    monkeypatch,
):
    message = "Je t'ai deja dit que ces coffrets faisaient partie du demenagement."
    _, reference = _l0_reference(tmp_path, monkeypatch, message)
    _stub_memory_agent_reads(monkeypatch)
    provider = _RelationProvider(message, posture="reported_memory_claim")

    result = run_memory_agent(
        user_id="shadow-user",
        provider=provider,
        user_message=message,
        assistant_response="Je vais verifier.",
        selected_souvenirs=[],
        followups_enabled=False,
        current_user_l0=reference,
        relation_shadow_enabled_override=True,
    )

    assert provider.calls == 1
    assert result.relation_shadow_status == "abstained"
    assert read_memory_relation_shadow_events("shadow-user")[0]["accepted"] == []
