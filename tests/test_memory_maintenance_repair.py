import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from daemon_core import memory_agent
from daemon_core.memory.metadata import parse_souvenir_metadata
from daemon_core.souvenir_selector import SouvenirSelectionUnavailable


class Responses:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, messages, temperature=0.4):
        self.calls.append(messages)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response)


@pytest.fixture
def notes(monkeypatch, tmp_path):
    root = tmp_path / "memory"
    for module in ("memory_agent", "memory.reader", "memory.writer"):
        monkeypatch.setattr(f"daemon_core.{module}.MEMORY_ROOT", root)
    monkeypatch.setenv("DAEMON_CORE_MEMORY_SITUATIONS", "canary")
    monkeypatch.setenv("DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS", "repair-test")
    paths = []
    for index in range(6):
        path = root / "capsules" / "theme" / "project" / f"story{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Story {index}\n\n## Faits\n- Exact fact {index}.\n")
        paths.append(path)
    return paths


def maintain(provider, selected):
    return memory_agent.run_memory_agent(
        user_id="repair-test", provider=provider,
        user_message="Le dossier avance.", assistant_response="D’accord.",
        selected_souvenirs=selected, recent_dialogue="", followups_enabled=False,
    )


def test_aggregate_without_a_file_is_resolved_to_exact_sources(notes):
    provider = Responses(
        {"selected": ["theme/project/story2", "theme/project/story0"]},
        {"capsule_updates": []},
    )
    result = maintain(provider, ["theme/project"])
    assert not notes[0].parent.with_suffix(".md").exists()
    assert result.consulted_souvenirs == ["theme/project/story2", "theme/project/story0"]
    assert len(provider.calls) == 2
    prompt = provider.calls[1][1]["content"]
    assert notes[2].read_text().strip() in prompt
    assert notes[0].read_text().strip() in prompt
    assert notes[1].read_text().strip() not in prompt
    assert not result.changed_paths


def test_leaves_keep_the_four_body_limit_without_an_extra_call(notes):
    provider = Responses({"capsule_updates": []})
    result = maintain(provider, [f"theme/project/story{i}" for i in range(6)])
    assert len(provider.calls) == 1
    assert result.consulted_souvenirs == [f"theme/project/story{i}" for i in range(4)]


def test_empty_model_selection_is_not_a_loading_failure(notes):
    provider = Responses({"selected": []}, {"capsule_updates": []})
    result = maintain(provider, ["theme/project"])
    assert result.consulted_souvenirs == []
    assert len(provider.calls) == 2
    assert not result.changed_paths


@pytest.mark.parametrize("selection", [
    RuntimeError("offline"),
    {},
    {"selected": "bad schema"},
    {"selected": ["theme/project"]},  # Aggregate is not a loadable body.
    {"selected": ["theme/missing"]},
])
def test_failed_zoom_cannot_turn_missing_sources_into_an_empty_memory(notes, selection):
    before = {path: path.read_bytes() for path in notes}
    provider = Responses(selection)
    with pytest.raises(SouvenirSelectionUnavailable):
        maintain(provider, ["theme/project"])
    assert len(provider.calls) == 1  # No writing judge was called.
    assert {path: path.read_bytes() for path in notes} == before
    assert set(notes[0].parents[2].rglob("*.md")) == set(before)


def test_missing_selected_leaf_stops_before_the_writing_judge(notes):
    provider = Responses()
    with pytest.raises(SouvenirSelectionUnavailable):
        maintain(provider, ["theme/project/story0", "theme/missing"])
    assert provider.calls == []


def test_failed_implicit_selection_also_defers_maintenance(notes):
    provider = Responses(RuntimeError("offline"))
    with pytest.raises(SouvenirSelectionUnavailable):
        maintain(provider, None)
    assert len(provider.calls) == 1


def test_scoped_discovery_addition_survives_the_protected_merge(notes):
    target = notes[0]
    old_fact = "- 2026-05-02 : Le chantier attend un passage de l’électricien."
    new_fact = "- 2026-10-08 : Le chantier est terminé."
    current = "---\nsubjects: [Camille]\nfacets: [chantier]\n---\n# Chantier\n\n## Faits\n" + old_fact + "\n"
    target.write_text(current)
    provider = Responses({
        "content_md": current + new_fact,
        "removed_existing_lines": [],
        "discovery_checks": [{
            "fact": old_fact,
            "other_endpoint": "passage de l’électricien",
            "inverse_need": "Pour quoi attendait-on le passage de l’électricien ?",
            "covered_by": [],
            "add_subject": "électricien",
            "add_facet": None,
        }],
    })
    result = memory_agent._apply_memory_agent_result(
        user_id="repair-test",
        data={"capsule_updates": [{"name": "theme/project/story0", "content_md": current + new_fact}]},
        user_message="Le chantier est terminé.", provider=provider,
    )
    final = target.read_text()
    assert result.updated_souvenirs == ["theme/project/story0"]
    assert old_fact in final and new_fact in final
    assert parse_souvenir_metadata(final).subjects == ["Camille", "électricien"]
    assert "It can belong to the souvenir's own scope" in provider.calls[0][0]["content"]
    assert len(provider.calls) == 1


def test_reference_merge_contract_is_unchanged_without_opt_in(notes, monkeypatch):
    captured = []
    original = memory_agent._merge_existing_souvenir

    def capture(**kwargs):
        captured.append(kwargs["situation_notes_enabled"])
        return original(**kwargs)

    monkeypatch.setattr(memory_agent, "_merge_existing_souvenir", capture)
    monkeypatch.setenv("DAEMON_CORE_MEMORY_SITUATIONS_USER_IDS", "another-user")
    current = notes[0].read_text()
    provider = Responses({"content_md": current, "discovery_checks": []})
    memory_agent._apply_memory_agent_result(
        user_id="repair-test", user_message="Le chantier avance.", provider=provider,
        data={"capsule_updates": [{"name": "theme/project/story0", "content_md": current + "\nProposal"}]},
    )
    assert captured == [False]
    assert provider.calls[0] == memory_agent._build_souvenir_merge_messages(
        name="theme/project/story0", current=current.strip(),
        proposed=(current + "\nProposal").strip(), user_message="Le chantier avance.",
    )
