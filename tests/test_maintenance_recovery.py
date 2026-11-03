import json

import pytest

from daemon_core.maintenance_errors import require_memory_update_success
from daemon_core.runtime_attestation import build_runtime_attestation


def test_temporal_provider_failure_retains_retry_boundary(monkeypatch):
    from daemon_core import temporal_memory

    class Offline:
        def complete(self, **kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr(temporal_memory, "load_user_agenda", lambda user: "")
    with require_memory_update_success(), pytest.raises(RuntimeError, match="offline"):
        temporal_memory.run_temporal_curator("synthetic", "A meeting tomorrow", Offline(), "")
    # Context does not leak into unrelated or legacy callers.
    assert not temporal_memory.run_temporal_curator("synthetic", "A meeting tomorrow", Offline(), "").updated


def test_ready_rechecks_local_embedding_dependency_without_generation(monkeypatch, tmp_path):
    from daemon_core.semantic_atlas import OllamaEmbeddingProvider

    env = {"DAEMON_CORE_MEMORY_ROOT": str(tmp_path), "DAEMON_CORE_ATLAS_EMBEDDING_TOP12": "canary"}
    probes = []

    def probe(self):
        probes.append(self.timeout_s)
        if len(probes) == 1:
            raise RuntimeError("offline")

    monkeypatch.setattr(OllamaEmbeddingProvider, "verify_model", probe)
    assert not build_runtime_attestation(environ=env)["checks"]["semantic_atlas_dependency"]["ok"]
    assert build_runtime_attestation(environ=env)["checks"]["semantic_atlas_dependency"]["ok"]
    assert probes == [0.5, 0.5]


@pytest.mark.parametrize("error", [RuntimeError("provider offline"), ValueError("semantic rejection")])
def test_merge_failure_distinguishes_unavailable_provider_from_refusal(monkeypatch, tmp_path, error):
    from daemon_core import memory_agent

    monkeypatch.setattr(memory_agent, "MEMORY_ROOT", tmp_path)
    path = tmp_path / "note.md"
    path.write_text("# Original\n\nSupported fact.\n")
    before = path.read_bytes()

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(memory_agent, "_merge_existing_souvenir", fail)
    with require_memory_update_success():
        if isinstance(error, RuntimeError):
            with pytest.raises(RuntimeError, match="provider offline"):
                memory_agent._write_souvenir_update_safely("synthetic", path, "theme/test", "proposal", "source", object())
        else:
            assert memory_agent._write_souvenir_update_safely("synthetic", path, "theme/test", "proposal", "source", object())[0] == "rejected"
    assert path.read_bytes() == before


def test_known_failure_is_degraded_even_during_retry_backoff(tmp_path):
    from daemon_core.runtime_attestation import _after_turn_processing_failed

    pending = tmp_path / "pending/hermes/synthetic/job.json"
    pending.parent.mkdir(parents=True)
    pending.write_text(json.dumps({"attempts": 1, "last_error": "RuntimeError", "available_at": 9999999999}))
    assert _after_turn_processing_failed(tmp_path, "synthetic")
    pending.unlink()
    assert not _after_turn_processing_failed(tmp_path, "synthetic")


def _agenda_fixture(tmp_path, monkeypatch, line):
    from tests.test_temporal_memory import _patch_memory_root

    memory_root = tmp_path / "memory"
    _patch_memory_root(monkeypatch, memory_root)
    agenda = memory_root / "users" / "synthetic" / "agenda.md"
    agenda.parent.mkdir(parents=True)
    agenda.write_text(f"# Agenda\n\n{line}\n", encoding="utf-8")
    return agenda


@pytest.mark.parametrize("refusal", ["unestablished_correction", "undeclared_loss", "unbounded_horizon"])
def test_durable_agenda_refusal_is_a_result_not_a_retryable_failure(tmp_path, monkeypatch, refusal):
    from daemon_core.temporal_memory import run_temporal_curator
    from tests.test_temporal_memory import SequencedProvider

    original = "- 2026-10-12 | reprise du vélo avec Léna | matin"
    agenda = _agenda_fixture(tmp_path, monkeypatch, original)
    before = agenda.read_bytes()
    message = "On a fait la boucle des étangs, 32 km."
    replacement = "- 2026-10-12 | boucle des étangs avec Léna | 32 km"
    proposals = {
        "unestablished_correction": (
            {"should_update": True, "agenda_md": f"# Agenda\n\n{replacement}",
             "removed_existing_lines": [{"line": original, "evidence": "la boucle des étangs"}]},
            {"judgments": [{"line": original, "authorized": False, "evidence": "", "reason": "coexist"}]},
        ),
        "undeclared_loss": (
            {"should_update": True, "agenda_md": f"# Agenda\n\n{replacement}", "removed_existing_lines": []},
        ),
        "unbounded_horizon": (
            {"should_update": True, "removed_existing_lines": [],
             "agenda_md": f"# Agenda\n\n{original}\n- En cours | vélo | Léna aime les châtaignes"},
        ),
    }
    expected = {
        "unestablished_correction": "correction d'agenda non explicitement établie",
        "undeclared_loss": "perte d'agenda non autorisée",
        "unbounded_horizon": "horizon d'agenda non opérationnel",
    }
    with require_memory_update_success():
        result = run_temporal_curator("synthetic", message, SequencedProvider(*proposals[refusal]), "")
    assert result.updated is False
    assert expected[refusal] in result.reason
    assert result.rejected_paths == ("users/synthetic/agenda.md",)
    assert agenda.read_bytes() == before


@pytest.mark.parametrize("failure", ["curator_json", "judge_json", "concurrent_edit"])
def test_durable_agenda_failures_still_retry(tmp_path, monkeypatch, failure):
    import json as _json

    from daemon_core import temporal_memory
    from daemon_core.providers.base import LLMProvider

    original = "- 2026-10-12 | reprise du vélo avec Léna | matin"
    agenda = _agenda_fixture(tmp_path, monkeypatch, original)
    replacement = "- 2026-10-12 | boucle des étangs avec Léna | 32 km"
    proposal = _json.dumps({"should_update": True, "source_posture": "direct_fact",
                            "agenda_md": f"# Agenda\n\n{replacement}",
                            "removed_existing_lines": [{"line": original, "evidence": "la boucle des étangs"}]})
    replies = {
        "curator_json": ["not json"],
        "judge_json": [proposal, "not json"],
        "concurrent_edit": [proposal, _json.dumps({"judgments": [
            {"line": original, "authorized": True, "evidence": "la boucle des étangs", "reason": "corrected"}]})],
    }[failure]

    class Replies(LLMProvider):
        def complete(self, messages, temperature=0.4):
            reply = replies.pop(0)
            if failure == "concurrent_edit" and not replies:
                agenda.write_text("# Agenda\n\n- 2026-10-13 | dentiste | 14h30\n", encoding="utf-8")
            return reply

    with require_memory_update_success(), pytest.raises(ValueError):
        temporal_memory.run_temporal_curator(
            "synthetic", "On a fait la boucle des étangs, 32 km.", Replies(), "")
