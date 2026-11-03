"""Bounded, read-only Dreaming audit for memory relation deltas.

The LLM judges whether a proposed or existing relation is semantically sound.
This module only bounds the supplied subgraph and validates the returned
protocol. It deliberately has no memory write path.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable

from daemon_core.providers.base import LLMProvider


RELATION_DREAMING_SCHEMA = "daemon.memory-relation-dreaming.v1"
MAX_BATCH_ITEMS = 8
MAX_NEIGHBORS_PER_ITEM = 12
MAX_REASON_CHARS = 600
MAX_ITEM_CHARS = 12_000
MAX_COVERAGE_CHARS = 8_000
MAX_COVERAGE_FACTS = 16
MAX_SOURCES_PER_FACT = 4
ALLOWED_DISPOSITIONS = frozenset(
    {"confirm", "reject", "defer", "noop", "repair"}
)
ALLOWED_ACTIONS = frozenset({"upsert", "remove", "none"})


class RelationDreamingProtocolError(ValueError):
    """Raised when a batch or a model response violates the audit protocol."""


@dataclass(frozen=True)
class RelationDreamingDecision:
    item_id: str
    disposition: str
    action: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "disposition": self.disposition,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RelationDreamingAudit:
    decisions: tuple[RelationDreamingDecision, ...]
    raw: str


def audit_relation_batch(
    items: Iterable[dict[str, Any]],
    *,
    provider: LLMProvider,
) -> RelationDreamingAudit:
    """Judge one bounded batch without applying any returned action."""

    bounded_items = validate_relation_dreaming_items(items)
    messages = build_relation_dreaming_messages(bounded_items)
    raw = provider.complete(messages=messages, temperature=0.0)
    decisions = parse_relation_dreaming_output(raw, items=bounded_items)
    return RelationDreamingAudit(decisions=decisions, raw=raw)


def build_relation_dreaming_messages(
    items: Iterable[dict[str, Any]],
    *,
    evidence_coverage_enabled: bool = False,
) -> list[dict[str, str]]:
    """Render the fixed audit contract and its one-hop evidence payload."""

    bounded_items = validate_relation_dreaming_items(items)
    if evidence_coverage_enabled:
        for item in bounded_items:
            _validate_evidence_coverage(item)
    payload = [
        _model_item(
            item,
            include_evidence_coverage=evidence_coverage_enabled,
        )
        for item in bounded_items
    ]
    system = """You audit a small delta of the Daemon memory graph.

You judge meaning. Code will check only your response's structure.
You have only the evidence and one-hop neighborhood supplied here. Invent
no souvenir, fact or relation, and never request the entire vault.

All items concern the typed relation member_of: the source souvenir must
itself be a member of the target episode or collection. The role specifies
the nature of that membership; it cannot turn member_of into causality,
motivation, association, a shared topic or a mere narrative link. A source
motivating the addition of another object to a collection does not make
that source a member of the collection.

Sources and caution:
- user_evidence is the user's exact L0 statement for this turn;
- assistant_context helps understand the turn but never proves a fact;
- direct_fact can establish a link only if the words and states prove it;
- reported_memory_claim means the user asks to retrieve a memory:
  this message does not on its own prove that the memory exists;
- none means no evidentiary user statement is available;
- an intention, possibility, shared name or thematic proximity does not
  suffice to create membership.

Decisions:
- confirm: the MemoryAgent proposal is proven and must be applied;
- reject: the proposal is false, speculative or targets the wrong souvenir;
- defer: the supplied material does not allow a decision;
- noop: the requested action is already satisfied by the current state, or
  maintenance mode finds that the current edge needs no repair;
- repair: in maintenance mode only, apply upsert or remove to correct a
  missing edge or one that has become false, with sufficient evidence.

Return only this JSON object:
{"schema":"daemon.memory-relation-dreaming.v1","decisions":[{"item_id":"...","disposition":"confirm|reject|defer|noop|repair","action":"upsert|remove|none","reason":"brief evidence-grounded reason"}]}

Exactly one decision per item, in the same order. Never repeat source or
target paths in the output."""
    if evidence_coverage_enabled:
        system += """

Structured historical coverage:
- evidence_coverage enumerates the source souvenir's durable facts;
- each entry associates a fact with zero or more exact L0 quotations;
- for historical backfill, these quotations may come from several L0 turns,
  including when the link is submitted as a proposal;
- an empty sources list means this fact is unproven: an upsert of the grouped
  souvenir must then remain defer;
- a quotation sources a fact, but you must still judge whether that fact
  semantically belongs to the target collection;
- if all relevant facts are directly proven members of the target, the souvenir
  grouping them may be member_of even if no quotation names the grouping;
- a mixed souvenir never becomes a whole member merely because all its lines
  are sourced;
- if source_state contains a fact absent from evidence_coverage, remain defer.

The source_ref paths serve only to identify quotations. They never constitute
evidence without their exact quote."""
    user = (
        '# Bounded relation batch\n\n'
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def validate_relation_dreaming_items(
    items: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Validate the bounded input shape without judging relation semantics."""

    values = tuple(items)
    if not values or len(values) > MAX_BATCH_ITEMS:
        raise RelationDreamingProtocolError(
            f"A batch must contain between 1 and {MAX_BATCH_ITEMS} items."
        )
    seen: set[str] = set()
    for position, item in enumerate(values):
        if not isinstance(item, dict):
            raise RelationDreamingProtocolError(
                f"Item {position} must be an object."
            )
        item_id = _required_text(item, "id", 160)
        if item_id in seen:
            raise RelationDreamingProtocolError(f"Duplicate item id: {item_id}.")
        seen.add(item_id)
        _choice(item, "mode", {"proposal", "maintenance"})
        _choice(
            item,
            "source_posture",
            {"direct_fact", "reported_memory_claim", "none"},
        )
        _choice(item, "proposed_action", ALLOWED_ACTIONS)
        _choice(item, "existing_state", {"present", "absent"})
        source = _required_text(item, "source", 400)
        target = _required_text(item, "target", 400)
        if source == target:
            raise RelationDreamingProtocolError(
                f"Item {item_id} cannot relate a souvenir to itself."
            )
        _optional_text(item, "role", 400)
        _optional_text(item, "user_evidence", 2_000)
        _optional_text(item, "assistant_context", 2_000)
        _required_text(item, "source_state", 4_000)
        _required_text(item, "target_state", 4_000)
        relations = item.get("existing_relations")
        if not isinstance(relations, list) or not all(
            isinstance(value, str) and len(value) <= 800 for value in relations
        ):
            raise RelationDreamingProtocolError(
                f"Item {item_id} has invalid existing_relations."
            )
        neighbors = item.get("neighbors")
        if not isinstance(neighbors, list) or len(neighbors) > MAX_NEIGHBORS_PER_ITEM:
            raise RelationDreamingProtocolError(
                f"Item {item_id} has an invalid one-hop neighborhood."
            )
        for neighbor in neighbors:
            if not isinstance(neighbor, dict):
                raise RelationDreamingProtocolError(
                    f"Item {item_id} has a non-object neighbor."
                )
            if set(neighbor) != {"name", "summary"}:
                raise RelationDreamingProtocolError(
                    f"Item {item_id} has an unbounded neighbor shape."
                )
            _required_text(neighbor, "name", 400)
            _required_text(neighbor, "summary", 1_500)
        if len(json.dumps(_model_item(item), ensure_ascii=False)) > MAX_ITEM_CHARS:
            raise RelationDreamingProtocolError(f"Item {item_id} is too large.")
    return values


def parse_relation_dreaming_output(
    raw: str,
    *,
    items: Iterable[dict[str, Any]],
) -> tuple[RelationDreamingDecision, ...]:
    """Parse one exact decision per known item and reject invented identities."""

    bounded_items = validate_relation_dreaming_items(items)
    try:
        payload = json.loads(_strip_json_fence(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise RelationDreamingProtocolError("Dreaming output is not JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "decisions"}:
        raise RelationDreamingProtocolError("Dreaming output has an invalid envelope.")
    if payload.get("schema") != RELATION_DREAMING_SCHEMA:
        raise RelationDreamingProtocolError("Dreaming output has an invalid schema.")
    raw_decisions = payload.get("decisions")
    if not isinstance(raw_decisions, list):
        raise RelationDreamingProtocolError("Dreaming decisions must be a list.")

    by_id = {str(item["id"]): item for item in bounded_items}
    decisions: dict[str, RelationDreamingDecision] = {}
    for raw_decision in raw_decisions:
        if not isinstance(raw_decision, dict):
            raise RelationDreamingProtocolError("Every decision must be an object.")
        if set(raw_decision) != {"item_id", "disposition", "action", "reason"}:
            raise RelationDreamingProtocolError(
                "A decision contains missing or model-authored fields."
            )
        item_id = _required_text(raw_decision, "item_id", 160)
        if item_id not in by_id:
            raise RelationDreamingProtocolError(f"Unknown item id: {item_id}.")
        if item_id in decisions:
            raise RelationDreamingProtocolError(f"Duplicate decision: {item_id}.")
        disposition = _choice(
            raw_decision,
            "disposition",
            ALLOWED_DISPOSITIONS,
        )
        action = _choice(raw_decision, "action", ALLOWED_ACTIONS)
        reason = _required_text(raw_decision, "reason", MAX_REASON_CHARS)
        _validate_decision_action(
            item=by_id[item_id],
            disposition=disposition,
            action=action,
        )
        decisions[item_id] = RelationDreamingDecision(
            item_id=item_id,
            disposition=disposition,
            action=action,
            reason=reason,
        )

    expected_order = [str(item["id"]) for item in bounded_items]
    missing = [item_id for item_id in expected_order if item_id not in decisions]
    if missing:
        raise RelationDreamingProtocolError(
            "Missing decisions: " + ", ".join(missing)
        )
    if len(decisions) != len(expected_order):
        raise RelationDreamingProtocolError("Unexpected decision count.")
    return tuple(decisions[item_id] for item_id in expected_order)


def _validate_decision_action(
    *,
    item: dict[str, Any],
    disposition: str,
    action: str,
) -> None:
    mode = str(item["mode"])
    proposed_action = str(item["proposed_action"])
    existing_state = str(item["existing_state"])
    if disposition == "confirm":
        if mode != "proposal" or action != proposed_action or action == "none":
            raise RelationDreamingProtocolError("confirm cannot apply this proposal.")
        if (action == "upsert" and existing_state == "present") or (
            action == "remove" and existing_state == "absent"
        ):
            raise RelationDreamingProtocolError("confirm must not duplicate current state.")
        return
    if disposition in {"reject", "defer"}:
        if action != "none":
            raise RelationDreamingProtocolError(
                f"{disposition} must use action none."
            )
        return
    if disposition == "noop":
        already_satisfied = (
            proposed_action == "upsert" and existing_state == "present"
        ) or (proposed_action == "remove" and existing_state == "absent")
        maintenance_needs_no_repair = (
            mode == "maintenance" and proposed_action == "none"
        )
        if action != "none" or not (
            already_satisfied or maintenance_needs_no_repair
        ):
            raise RelationDreamingProtocolError("noop does not match current state.")
        return
    if disposition == "repair":
        if mode != "maintenance" or action not in {"upsert", "remove"}:
            raise RelationDreamingProtocolError("repair is reserved for maintenance.")
        if (action == "upsert" and existing_state != "absent") or (
            action == "remove" and existing_state != "present"
        ):
            raise RelationDreamingProtocolError("repair does not change current state.")
        return
    raise RelationDreamingProtocolError(f"Unsupported disposition: {disposition}.")


def _model_item(
    item: dict[str, Any],
    *,
    include_evidence_coverage: bool = False,
) -> dict[str, Any]:
    payload = {
        "item_id": item.get("id"),
        "mode": item.get("mode"),
        "source_posture": item.get("source_posture"),
        "proposed_action": item.get("proposed_action"),
        "existing_state": item.get("existing_state"),
        "source": item.get("source"),
        "target": item.get("target"),
        "role": item.get("role"),
        "user_evidence": item.get("user_evidence"),
        "assistant_context": item.get("assistant_context"),
        "source_state": item.get("source_state"),
        "target_state": item.get("target_state"),
        "existing_relations": item.get("existing_relations"),
        "neighbors": item.get("neighbors"),
    }
    if include_evidence_coverage:
        payload["evidence_coverage"] = item.get("evidence_coverage")
    return payload


def _validate_evidence_coverage(item: dict[str, Any]) -> None:
    coverage = item.get("evidence_coverage")
    if (
        not isinstance(coverage, list)
        or not coverage
        or len(coverage) > MAX_COVERAGE_FACTS
    ):
        raise RelationDreamingProtocolError(
            f"Item {item.get('id')} has invalid evidence_coverage."
        )
    seen_facts: set[str] = set()
    for entry in coverage:
        if not isinstance(entry, dict) or set(entry) != {"fact", "sources"}:
            raise RelationDreamingProtocolError(
                f"Item {item.get('id')} has an invalid coverage entry."
            )
        fact = _required_text(entry, "fact", 1_000)
        if fact in seen_facts:
            raise RelationDreamingProtocolError(
                f"Item {item.get('id')} has duplicate coverage facts."
            )
        seen_facts.add(fact)
        sources = entry.get("sources")
        if not isinstance(sources, list) or len(sources) > MAX_SOURCES_PER_FACT:
            raise RelationDreamingProtocolError(
                f"Item {item.get('id')} has invalid coverage sources."
            )
        seen_refs: set[str] = set()
        for source in sources:
            if not isinstance(source, dict) or set(source) != {
                "source_ref",
                "quote",
            }:
                raise RelationDreamingProtocolError(
                    f"Item {item.get('id')} has an invalid coverage source."
                )
            source_ref = _required_text(source, "source_ref", 1_000)
            _required_text(source, "quote", 2_000)
            if source_ref in seen_refs:
                raise RelationDreamingProtocolError(
                    f"Item {item.get('id')} has duplicate coverage sources."
                )
            seen_refs.add(source_ref)
    if len(json.dumps(coverage, ensure_ascii=False)) > MAX_COVERAGE_CHARS:
        raise RelationDreamingProtocolError(
            f"Item {item.get('id')} has oversized evidence_coverage."
        )


def _required_text(value: dict[str, Any], key: str, maximum: int) -> str:
    text = value.get(key)
    if not isinstance(text, str) or not text.strip() or len(text) > maximum:
        raise RelationDreamingProtocolError(f"Invalid {key}.")
    return text.strip()


def _optional_text(value: dict[str, Any], key: str, maximum: int) -> str:
    text = value.get(key)
    if not isinstance(text, str) or len(text) > maximum:
        raise RelationDreamingProtocolError(f"Invalid {key}.")
    return text.strip()


def _choice(value: dict[str, Any], key: str, allowed: Iterable[str]) -> str:
    text = value.get(key)
    if not isinstance(text, str) or text not in allowed:
        raise RelationDreamingProtocolError(f"Invalid {key}: {text!r}.")
    return text


def _strip_json_fence(raw: str) -> str:
    text = str(raw).strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return text
