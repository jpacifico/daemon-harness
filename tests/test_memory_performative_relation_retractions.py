import hashlib
import json

import pytest

from daemon_core.memory_agent import (
    _apply_performative_relation_removals,
    _authorized_performative_relation_retractions,
    _eligible_member_of_retractions,
    _merge_existing_souvenir,
)
from daemon_core.memory.relation_shadow import (
    memory_performative_relation_retractions_enabled,
)
from daemon_core.providers.base import LLMProvider


FIXTURE = "tests/memory_relation_retraction_cases.json"
FIXTURE_SHA256 = "52811cae8694dfa4968e8b063fbed1d074dbd8a05819c66e87b738e5cdf22e1e"
RELATION = "- member_of [[theme/projet/atlas]] : element du projet"
SOURCE = "  - source: [[levels/L0_ground_zero/conversations/eval#turn-1]]"
FACT = "- Le carnet contient trois croquis."


class StaticProvider(LLMProvider):
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0
        self.messages = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.calls += 1
        self.messages.append(messages)
        return json.dumps(self.payload, ensure_ascii=False)


def _souvenir(*, include_relation: bool = True) -> str:
    relation = f"\n\n## Relations\n{RELATION}\n{SOURCE}" if include_relation else ""
    return f"# Capsule: theme/projet/carnet\n\n## Faits\n{FACT}{relation}"


def test_canary_flag_requires_an_exact_user_scope(monkeypatch):
    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_PERFORMATIVE_RELATION_RETRACTIONS",
        "canary",
    )
    monkeypatch.delenv(
        "DAEMON_CORE_MEMORY_PERFORMATIVE_RELATION_RETRACTIONS_USER_IDS",
        raising=False,
    )
    assert memory_performative_relation_retractions_enabled("garden") is False

    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_PERFORMATIVE_RELATION_RETRACTIONS_USER_IDS",
        "garden",
    )
    assert memory_performative_relation_retractions_enabled("garden") is True
    assert memory_performative_relation_retractions_enabled("another") is False

    monkeypatch.setenv(
        "DAEMON_CORE_MEMORY_PERFORMATIVE_RELATION_RETRACTIONS_USER_IDS",
        "*",
    )
    assert memory_performative_relation_retractions_enabled("garden") is False


def test_relation_retraction_fixture_is_frozen_before_llm_calls():
    with open(FIXTURE, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    with open(FIXTURE, encoding="utf-8") as handle:
        payload = json.load(handle)

    assert digest == FIXTURE_SHA256
    assert len(payload["cases"]) == 6
    assert sum(case["split"] == "development" for case in payload["cases"]) == 3
    assert sum(case["split"] == "holdout" for case in payload["cases"]) == 3


def test_only_missing_root_member_of_lines_are_eligible():
    current = (
        _souvenir()
        + "\n- related_to [[theme/projet/autre]] : voisinage\n"
        + "  - member_of [[theme/projet/interdit]] : provenance imbriquee"
    )
    proposed = "# Capsule: theme/projet/carnet\n\n## Faits\n- Un autre fait."

    eligible = _eligible_member_of_retractions(
        current=current,
        proposed=proposed,
    )

    assert eligible == (RELATION,)
    assert FACT not in eligible


def test_present_relation_is_not_eligible():
    assert _eligible_member_of_retractions(
        current=_souvenir(),
        proposed=_souvenir(),
    ) == ()


def test_relation_decision_requires_one_exact_sourced_remove():
    message = "Ce carnet ne fait plus partie du projet Atlas."

    accepted = _authorized_performative_relation_retractions(
        [{"line": RELATION, "action": "remove", "evidence": message}],
        eligible_lines=(RELATION,),
        user_message=message,
    )
    duplicate = _authorized_performative_relation_retractions(
        [
            {"line": RELATION, "action": "remove", "evidence": message},
            {"line": RELATION, "action": "remove", "evidence": message},
        ],
        eligible_lines=(RELATION,),
        user_message=message,
    )
    invented = _authorized_performative_relation_retractions(
        [{"line": FACT, "action": "remove", "evidence": message}],
        eligible_lines=(RELATION,),
        user_message=message,
    )

    assert accepted == {RELATION: message}
    assert duplicate == {}
    assert invented == {}


def test_relation_removal_cannot_remove_an_ordinary_fact():
    content = _souvenir()

    cleaned = _apply_performative_relation_removals(
        content,
        {RELATION, FACT},
    )

    assert FACT in cleaned
    assert RELATION not in cleaned
    assert SOURCE not in cleaned


def test_structured_relation_decision_is_executed_in_one_existing_call():
    message = "Ce carnet ne fait plus partie du projet Atlas."
    provider = StaticProvider(
        {
            "content_md": _souvenir(),
            "discovery_checks": [],
            "removed_existing_lines": [],
            "existing_relation_decisions": [
                {
                    "line": RELATION,
                    "action": "remove",
                    "evidence": message,
                }
            ],
            "reason": "Correction explicite.",
        }
    )

    merged = _merge_existing_souvenir(
        provider=provider,
        name="theme/projet/carnet",
        current=_souvenir(),
        proposed=_souvenir(include_relation=False),
        user_message=message,
        derive_relation_removals=True,
        performative_relation_retractions=True,
    )

    assert provider.calls == 1
    assert FACT in merged.content
    assert RELATION not in merged.content
    assert SOURCE not in merged.content
    assert merged.applied_relation_retractions == (
        {"line": RELATION, "action": "remove", "evidence": message},
    )
    prompt = provider.messages[0][0]["content"]
    assert "existing_relation_decisions" in prompt
    assert 'Ordinary facts are never eligible' in prompt


def test_ordinary_fact_correction_keeps_the_historical_authorization_path():
    old_fact = "- La repetition est prevue samedi."
    new_fact = "- La repetition est prevue dimanche."
    current = _souvenir().replace(FACT, old_fact)
    proposed = _souvenir().replace(FACT, new_fact)
    message = "La repetition aura lieu dimanche et non samedi."
    provider = StaticProvider(
        {
            "content_md": proposed,
            "discovery_checks": [],
            "removed_existing_lines": [
                {"line": old_fact, "evidence": message}
            ],
            "existing_relation_decisions": [],
            "reason": "Correction du jour seulement.",
        }
    )

    merged = _merge_existing_souvenir(
        provider=provider,
        name="theme/projet/carnet",
        current=current,
        proposed=proposed,
        user_message=message,
        derive_relation_removals=True,
        performative_relation_retractions=True,
    )

    assert old_fact not in merged.content
    assert new_fact in merged.content
    assert RELATION in merged.content
    assert merged.applied_relation_retractions == ()


def test_missing_or_duplicate_relation_decision_fails_closed():
    message = "Ce carnet ne fait plus partie du projet Atlas."
    provider = StaticProvider(
        {
            "content_md": _souvenir(include_relation=False),
            "discovery_checks": [],
            "removed_existing_lines": [
                {"line": RELATION, "evidence": message}
            ],
            "existing_relation_decisions": [],
            "reason": "Decision manquante.",
        }
    )

    with pytest.raises(ValueError, match="perte d'information non autorisée"):
        _merge_existing_souvenir(
            provider=provider,
            name="theme/projet/carnet",
            current=_souvenir(),
            proposed=_souvenir(include_relation=False),
            user_message=message,
            derive_relation_removals=True,
            performative_relation_retractions=True,
        )
