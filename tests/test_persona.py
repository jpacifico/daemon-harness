import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core import persona


def test_load_daemon_persona_uses_default_when_nothing_local(tmp_path, monkeypatch):
    monkeypatch.setattr(persona, "MEMORY_ROOT", tmp_path / "memory")

    loaded = persona.load_daemon_persona("garden")

    # The versioned seed defines character, without name or grammatical agreement.
    assert "conversational presence" in loaded
    assert "someone, not a service" in loaded
    assert "Écho" not in loaded  # The seed has no user-specific identity.


def test_per_user_persona_takes_precedence(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr(persona, "MEMORY_ROOT", memory_root)
    # With both a shared base and a personal persona, the personal one wins.
    (memory_root / "core").mkdir(parents=True)
    (memory_root / "core" / "persona.md").write_text("Socle partagé du foyer.", encoding="utf-8")
    persona.write_daemon_persona("Tu es Écho, à moi seul.", user_id="garden")

    assert persona.load_daemon_persona("garden") == "Tu es Écho, à moi seul."
    # Another user without a personal persona falls back to the shared base.
    assert persona.load_daemon_persona("autre") == "Socle partagé du foyer."


def test_shared_base_used_when_no_per_user(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr(persona, "MEMORY_ROOT", memory_root)
    (memory_root / "core").mkdir(parents=True)
    (memory_root / "core" / "persona.md").write_text("Socle partagé.", encoding="utf-8")

    assert persona.load_daemon_persona("garden") == "Socle partagé."


def test_write_targets_per_user_file(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr(persona, "MEMORY_ROOT", memory_root)

    path = persona.write_daemon_persona("Voix évoluée.", user_id="garden")

    assert path == memory_root / "users" / "garden" / "persona.md"
    assert path.read_text(encoding="utf-8").strip() == "Voix évoluée."


def test_write_conversation_manner_targets_per_user_file(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    monkeypatch.setattr(persona, "MEMORY_ROOT", memory_root)

    path = persona.write_conversation_manner(
        "# Manière\n\nUne réponse simple reste simple.",
        user_id="garden",
    )

    assert path == memory_root / "users" / "garden" / "conversation.md"
    assert persona.load_conversation_manner("garden") == (
        "# Manière\n\nUne réponse simple reste simple."
    )


def test_write_conversation_manner_rejects_oversized_replacement(
    tmp_path,
    monkeypatch,
):
    import pytest

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(persona, "MEMORY_ROOT", memory_root)

    with pytest.raises(ValueError):
        persona.write_conversation_manner(
            "x" * (persona.MAX_CONVERSATION_MANNER_CHARS + 1),
            user_id="garden",
        )

    assert not persona.conversation_manner_path("garden").exists()
