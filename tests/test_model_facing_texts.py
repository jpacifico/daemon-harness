"""English daemon prose must not translate exact user evidence or tool contracts."""

import copy
import json
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from test_hermes_memory_trace import plugin as hermes_plugin
from server.routes import openclaw
from daemon_core.dreaming import build_dreaming_messages
from daemon_core import dreaming
from daemon_core.memory.forensics import _render_chronology_catalog
from daemon_core.memory_agent import _build_memory_agent_messages
from daemon_core.rem import _build_messages as rem_messages
from daemon_core.scene_model import _scene_stage_retry_messages
from daemon_core.temporal_memory import _build_temporal_curator_messages


SCENE = {
    "established": ["Élise garde son expression exacte : libération."],
    "uncertain": [],
    "need": "Lire la souvenir de cet épisode.",
    "handling": {"mode": "direct", "plan": "Répondre à partir du texte exact."},
}


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("paths", [], "A list of non-empty exact paths is required."),
        ("scene", None, "A structured scene assessment is required."),
        ("established", None, "Scene field established must be a list."),
        ("uncertain", [""], "Scene field uncertain contains an invalid value."),
        ("need", "", "The evidence need this read must resolve is required."),
        ("handling", None, "Structured response handling is required."),
        ("handling", {"mode": "invalid"}, "Response handling must be direct, clarify, condition or offer."),
        ("handling", {"mode": "direct", "plan": ""}, "The uncertainty-handling plan is required."),
    ],
)
def test_both_adapters_deliver_the_same_english_validation_error(
    monkeypatch, field, value, expected,
):
    monkeypatch.setattr(hermes_plugin, "_souvenir_read_tool_enabled", lambda *args: True)
    provider = hermes_plugin.DaemonCoreMemoryProvider()
    provider._harness = object()
    provider._user_id = "language-audit"
    provider._platform = "telegram"
    arguments = {"paths": ["theme/test/episode"], "scene": copy.deepcopy(SCENE)}
    target = arguments if field in {"paths", "scene"} else arguments["scene"]
    target[field] = value
    original = copy.deepcopy(arguments)
    hermes = json.loads(provider._handle_tool_call("daemon_read_souvenirs", arguments))

    app = FastAPI()
    app.include_router(openclaw.router)
    with TestClient(app) as client:
        response = client.post("/openclaw/tool", json={
            "user_id": "language-audit", "name": "daemon_read_souvenirs",
            "arguments": arguments,
        })
    assert response.status_code == 200
    assert hermes == response.json()["result"] == {"status": "error", "reason": expected}
    assert arguments == original


def test_memory_prompt_translates_explanations_not_markdown_or_user_words():
    source = "# Capsule: theme/test/episode\nSujet: épisode.\n## Faits\n- Mon mot est « libération »."
    messages = _build_memory_agent_messages(
        user_context="", conversation_manner="", current_discussion="",
        souvenir_catalog="", recent_dialogue="", user_message=source,
        assistant_response="Je comprends.", relevant_souvenirs={"theme/test/episode": source},
    )
    system, user = [message["content"] for message in messages]
    assert "Sujet: short sentence that describes the souvenir exactly." in system
    assert "Portée: what this souvenir is allowed to contain." in system
    assert "Hors portée: what belongs elsewhere." in system
    assert "valid_until: YYYY-MM-DD" in system
    assert "## Faits" in system and "## Dernière mise à jour" in system
    assert source in user
    assert "libération" in user


def test_nightly_and_temporal_envelopes_preserve_exact_source_language():
    source = "Élise : je veux conserver exactement le mot « libération »."
    dreaming = build_dreaming_messages(
        user_id="language-audit", since_hours=24, l0_recent=source, memory_snapshot=source,
    )
    rem = rem_messages("language-audit", source, "", "", "theme/chroniques/test", "test")
    temporal = _build_temporal_curator_messages("", "", source)
    assert "valid_until: YYYY-MM-DD" in dreaming[0]["content"]
    assert "description — anchors" in dreaming[0]["content"]
    assert "<short description> — <concrete anchors:" in rem[0]["content"]
    assert "# Agenda\n\n- No explicit event." in temporal[1]["content"]
    for messages in (dreaming, rem, temporal):
        assert source in messages[1]["content"]


def test_retry_instruction_is_english_but_rejected_output_remains_exact():
    rejected = '{"reason":"Élise reste à confirmer"}'
    messages = _scene_stage_retry_messages(
        [], "single projection", rejected_raw=rejected,
        rejection_reason="established_authority_missing",
    )
    assert messages[0] == {"role": "assistant", "content": rejected}
    assert 'judgment "single projection"' in messages[1]["content"]
    assert "Add an authorized exact source" in messages[1]["content"]


def test_empty_source_wrappers_are_english_without_fabricating_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(dreaming, "MEMORY_ROOT", tmp_path / "memory")
    assert dreaming._snapshot_file("context.md", tmp_path / "missing.md") == (
        "## context.md\nEmpty file."
    )
    assert dreaming._snapshot_theme_souvenirs() == "## Thematic souvenirs\nNo thematic souvenir."
    assert _render_chronology_catalog({}) == "No turn."
