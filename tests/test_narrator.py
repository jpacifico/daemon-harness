import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.narrator import run_narrator
from daemon_core.providers.base import LLMProvider


def _patch_memory_root(tmp_path, monkeypatch) -> Path:
    memory_root = tmp_path / "memory"
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    return memory_root


class NarratorProvider(LLMProvider):
    def __init__(self, text: str) -> None:
        self.text = text
        self.messages = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.messages = messages
        return self.text


class BrokenProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        raise RuntimeError("provider indisponible")


def test_narrator_writes_trajectory(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    provider = NarratorProvider("Il sort d'une semaine dense ; il entre dans trois semaines de congés.")

    result = run_narrator("garden", provider=provider)

    trajectory_file = memory_root / "users" / "garden" / "trajectoire.md"
    assert result == "Il sort d'une semaine dense ; il entre dans trois semaines de congés."
    assert trajectory_file.read_text(encoding="utf-8").strip() == result


def test_narrator_prompt_contains_previous_trajectory_and_context(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    user_root = memory_root / "users" / "garden"
    user_root.mkdir(parents=True)
    (user_root / "trajectoire.md").write_text("Arc précédent.", encoding="utf-8")
    (user_root / "contexte_actif.md").write_text("- Semaine éprouvante, train à 6h.", encoding="utf-8")
    provider = NarratorProvider("Arc précédent.")

    run_narrator("garden", provider=provider)

    system = provider.messages[0]["content"]
    user = provider.messages[1]["content"]
    assert "narrator" in system
    assert "montage" in system
    assert "Arc précédent." in user
    assert "Semaine éprouvante" in user


def test_narrator_keeps_previous_trajectory_when_provider_fails(tmp_path, monkeypatch):
    memory_root = _patch_memory_root(tmp_path, monkeypatch)
    user_root = memory_root / "users" / "garden"
    user_root.mkdir(parents=True)
    (user_root / "trajectoire.md").write_text("Arc précédent.\n", encoding="utf-8")

    result = run_narrator("garden", provider=BrokenProvider())

    assert result == ""
    assert (user_root / "trajectoire.md").read_text(encoding="utf-8") == "Arc précédent.\n"
