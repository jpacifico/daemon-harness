import json

from daemon_core.memory.forensics import (
    ForensicRecovery,
    MAX_EVIDENCE_CONTEXT_CHARS,
    _exact_quote_context,
    personal_fact_tool_payload,
)
from daemon_core.held_atlas_sovereign import verify_l0_evidence_coverage


def journal():
    return (
        "## 2026-04-14T10:00:00 — user\n\nMila fait de la céramique.\n\n"
        "## 2026-04-14T10:01:00 — assistant\n\nÀ quelle heure ?\n\n"
        "## 2026-04-14T10:02:00 — user\n\nElle y va à 15h.\n\n"
        "## 2026-04-14T10:03:00 — assistant\n\nD’accord.\n"
    )


def test_context_keeps_exact_attribution_roles_and_date():
    source = journal()
    context = _exact_quote_context(source, source, "Elle y va à 15h.")
    assert context == source.strip()
    assert "Mila fait de la céramique." in context
    assert "2026-04-14T10:02:00 — user" in context


def test_context_never_bridges_an_omitted_original_turn():
    visible = journal()
    original = visible.replace(
        "## 2026-04-14T10:02:00",
        "## 2026-04-14T10:01:30 — user\n\nPassons à Léa.\n\n## 2026-04-14T10:02:00",
    )
    assert _exact_quote_context(visible, original, "Elle y va à 15h.") == ""


def test_context_abstains_from_choosing_a_repeated_quote():
    source = journal() + journal().replace("2026-04-14", "2026-04-15")
    assert _exact_quote_context(source, source, "Elle y va à 15h.") == ""


def test_context_does_not_silently_clip_an_oversized_turn():
    source = journal().replace(
        "Mila fait", "x" * MAX_EVIDENCE_CONTEXT_CHARS + "Mila fait"
    )
    assert _exact_quote_context(source, source, "Elle y va à 15h.") == ""


def test_context_cannot_be_anchored_to_an_assistant_quote():
    source = journal()
    assert _exact_quote_context(source, source, "À quelle heure ?") == ""


def test_failed_payload_never_exposes_unverified_context():
    result = personal_fact_tool_payload(
        ForensicRecovery(
            status="invalid_evidence",
            evidence="invented",
            evidence_context="unverified",
        )
    )
    assert result["evidence"] == result["evidence_context"] == ""


def test_both_hosts_use_same_coverage_judgment_and_exact_context():
    from integrations.hermes_plugin import _verify_l0_evidence_coverage

    class Provider:
        def __init__(self):
            self.messages = []

        def complete(self, messages, temperature):
            self.messages.append(messages)
            return json.dumps({"complete": False, "reason": "Other person"})

    provider = Provider()
    context = journal().strip()
    kwargs = dict(
        need="Heure de Léa",
        evidence="Elle y va à 15h.",
        evidence_context=context,
        provider=provider,
    )
    assert _verify_l0_evidence_coverage(**kwargs) == (False, "Other person")
    assert verify_l0_evidence_coverage(**kwargs) == (False, "Other person")
    assert provider.messages[0] == provider.messages[1]
    assert context in provider.messages[0][1]["content"]
    assert "Assistant statements" in provider.messages[0][0]["content"]
