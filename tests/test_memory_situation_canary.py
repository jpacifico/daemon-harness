import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from daemon_core.memory_agent import run_memory_agent, memory_situation_notes_enabled


def test_canary_is_named_opt_in_and_keeps_the_same_single_memory_call(monkeypatch):
    class NoopProvider:
        def __init__(self):
            self.calls = []

        def complete(self, messages, temperature=0.4):
            self.calls.append(messages)
            return '{"source_posture":"none","capsule_updates":[]}'

    def maintain():
        provider = NoopProvider()
        result = run_memory_agent(
            user_id="synthetic-situation-canary", provider=provider,
            user_message="Un nouveau fait.", assistant_response="D’accord.",
            selected_souvenirs=[], recent_dialogue="", followups_enabled=False,
        )
        assert len(provider.calls) == 1
        assert not result.changed_paths
        return provider.calls[0]

    monkeypatch.delenv("DAEMON_CORE_MEMORY_SITUATIONS", raising=False)
    monkeypatch.delenv("DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS", raising=False)
    reference = maintain()
    monkeypatch.setenv("DAEMON_CORE_MEMORY_SITUATIONS", "canary")
    for value in ("", "*", "someone-else", "synthetic-situation-canary-extra"):
        monkeypatch.setenv("DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS", value)
        assert maintain() == reference
    monkeypatch.setenv("DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS", "synthetic-situation-canary")
    candidate = maintain()
    assert candidate[1:] == reference[1:]
    assert candidate[0]["content"].startswith(reference[0]["content"])
    assert len(candidate[0]["content"]) > len(reference[0]["content"])
    monkeypatch.setenv("DAEMON_CORE_MEMORY_SITUATIONS", "off")
    assert maintain() == reference
    assert not memory_situation_notes_enabled("")


def test_canary_does_not_bypass_reported_claim_guard(monkeypatch):
    class UnsafeProposal:
        def complete(self, messages, temperature=0.4):
            return '{"source_posture":"reported_memory_claim","active_context_md":"must not write","capsule_updates":[{"name":"theme/test/claim","content_md":"must not write"}]}'

    monkeypatch.setenv("DAEMON_CORE_MEMORY_SITUATIONS", "canary")
    monkeypatch.setenv("DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS", "synthetic-situation-claim")
    result = run_memory_agent(
        user_id="synthetic-situation-claim", provider=UnsafeProposal(),
        user_message="Tu avais dit que tu t'en souvenais.", assistant_response="Je vérifie.",
        selected_souvenirs=[], recent_dialogue="", followups_enabled=False,
    )
    assert result.source_posture == "reported_memory_claim"
    assert not result.changed_paths
    assert not result.updated_souvenirs
