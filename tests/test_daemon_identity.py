from __future__ import annotations

import json
from pathlib import Path
import stat

import pytest

from daemon_core import persona
from daemon_core.daemon_identity import (
    load_daemon_identity,
    save_daemon_identity,
    validate_name,
)
from daemon_core.thin_sovereign import build_thin_sovereign_messages
from daemon_core.voice_brief import _compile_identity


@pytest.mark.parametrize(
    "name,gender",
    [("Iris", "feminine"), ("Noé", "masculine")],
)
def test_identity_is_private_and_separate_from_unchanged_persona(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    gender: str,
) -> None:
    monkeypatch.setattr(persona, "MEMORY_ROOT", tmp_path)
    assert save_daemon_identity(tmp_path, name, gender) == "created"
    assert load_daemon_identity(tmp_path).name == name
    assert load_daemon_identity(tmp_path).gender == gender
    assert persona.load_daemon_persona("telegram-user") == persona.DEFAULT_DAEMON_PERSONA
    assert not list(tmp_path.rglob("persona.md"))
    target = tmp_path / "core" / "daemon_identity.json"
    assert json.loads(target.read_text()) == {"name": name, "gender": gender}
    if target.stat().st_mode:
        assert stat.S_IMODE(target.stat().st_mode) & 0o077 == 0


def test_existing_voice_files_remain_byte_identical_across_identity_updates(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core" / "persona.md"
    personal = tmp_path / "users" / "test-user" / "persona.md"
    manner = tmp_path / "users" / "test-user" / "conversation.md"
    core.parent.mkdir(parents=True)
    personal.parent.mkdir(parents=True)
    core.write_bytes(b"Existing shared voice\n")
    personal.write_bytes(b"Existing personal voice\n")
    manner.write_bytes(b"Existing conversational balance\n")
    before = {path: path.read_bytes() for path in (core, personal, manner)}

    assert save_daemon_identity(tmp_path, "Iris", "feminine") == "created"
    assert save_daemon_identity(tmp_path, "Iris", "feminine") == "unchanged"
    assert save_daemon_identity(tmp_path, "Noé", "masculine") == "updated"

    assert {path: path.read_bytes() for path in before} == before


def test_default_prompt_is_byte_identical_without_identity_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAEMON_CORE_AGENT_NAME", raising=False)
    baseline = build_thin_sovereign_messages(
        message="Hello",
        daemon_persona=persona.DEFAULT_DAEMON_PERSONA,
        conversation_manner="",
        recent_dialogue="",
    )
    unconfigured = build_thin_sovereign_messages(
        message="Hello",
        daemon_persona=persona.DEFAULT_DAEMON_PERSONA,
        conversation_manner="",
        recent_dialogue="",
        agent_name=load_daemon_identity(Path("/path/that/does/not/exist")).name,
        agent_gender="",
    )
    assert unconfigured == baseline


def test_without_identity_the_name_is_neutral_unless_the_environment_names_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAEMON_CORE_AGENT_NAME", raising=False)
    assert load_daemon_identity(tmp_path).name == "Daemon"
    monkeypatch.setenv("DAEMON_CORE_AGENT_NAME", "Noé")
    assert load_daemon_identity(tmp_path).name == "Noé"
    save_daemon_identity(tmp_path, "Aster", "feminine")
    assert load_daemon_identity(tmp_path).name == "Aster"


def test_runtime_files_name_no_particular_daemon() -> None:
    root = Path(__file__).resolve().parents[1]
    named = [
        str(path.relative_to(root))
        for folder in ("src", "integrations", "server", "memory")
        for path in (root / folder).rglob("*")
        if path.is_file()
        and not {"node_modules", "__pycache__"} & set(path.parts)
        and "lyra" in path.read_bytes().decode("utf-8", "ignore").lower()
    ]
    assert named == []


def test_configured_identity_changes_only_the_existing_identity_sentence() -> None:
    baseline = build_thin_sovereign_messages(
        message="Hello",
        daemon_persona=persona.DEFAULT_DAEMON_PERSONA,
        conversation_manner="",
        recent_dialogue="",
        agent_name="Iris",
    )
    configured = build_thin_sovereign_messages(
        message="Hello",
        daemon_persona=persona.DEFAULT_DAEMON_PERSONA,
        conversation_manner="",
        recent_dialogue="",
        agent_name="Noé",
        agent_gender="masculine",
    )
    assert len(baseline) == len(configured)
    assert configured[0]["content"] == baseline[0]["content"].replace(
        "You are Iris.",
        "You are Noé (refer to yourself using masculine grammatical agreement).",
        1,
    )
    assert configured[1:] == baseline[1:]
    raw_identity = configured[0]["content"].split("# Identity\n\n", 1)[1].split(
        "\n\n# ", 1
    )[0]
    compiled = _compile_identity(raw_identity)
    assert compiled.agent_name == "Noé"
    assert compiled.persona == persona.DEFAULT_DAEMON_PERSONA


@pytest.mark.parametrize(
    "name",
    ["", " " * 3, "X" * 41, "A\nB", 'A"B', "A;B", "A$B"],
)
def test_invalid_name_is_rejected_before_writing(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        validate_name(name)
    with pytest.raises(ValueError):
        save_daemon_identity(tmp_path, name, "feminine")
    assert not list(tmp_path.rglob("daemon_identity.json"))


def test_invalid_gender_and_malformed_existing_state_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        save_daemon_identity(tmp_path, "Iris", "other")
    target = tmp_path / "core" / "daemon_identity.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"name": "Iris", "unexpected": true}\n')
    with pytest.raises(ValueError, match="unknown format"):
        save_daemon_identity(tmp_path, "Iris", "feminine")
    assert "unexpected" in target.read_text()


def test_identity_writer_refuses_symbolic_link(tmp_path: Path) -> None:
    external = tmp_path / "external.json"
    external.write_text("do not overwrite\n")
    target = tmp_path / "memory" / "core" / "daemon_identity.json"
    target.parent.mkdir(parents=True)
    target.symlink_to(external)
    with pytest.raises(ValueError, match="symbolic link"):
        save_daemon_identity(tmp_path / "memory", "Iris", "feminine")
    assert external.read_text() == "do not overwrite\n"
