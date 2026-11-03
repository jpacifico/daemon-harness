import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from daemon_core import first_meeting as meeting
from daemons_companion.desktop.protocol import DesktopUI
from daemons_companion.hermes.first_install import initialize_private_memory
from daemon_core.scene_model import SceneSource

USER = "1234567"
SOURCE = SceneSource("src-user", "l0_turn", "2026-10-20T12:00:00 — user", "exact-hash", "Help me prepare a meeting.")


def complete(root, **overrides):
    value = {"complete": True, "sources": [SOURCE.source_id], **overrides}
    return meeting.record_completion(root, USER, json.dumps({"first_meeting": value}),
                                     {SOURCE.source_id: SOURCE}, trace_id="test-turn")


def test_missing_metadata_is_not_a_new_user(tmp_path):
    assert meeting.render(tmp_path, USER) == ""
    assert not list(tmp_path.iterdir())


def test_first_meeting_lifecycle_is_shared_idempotent_and_keeps_language(tmp_path):
    meeting.initialize(tmp_path, USER, "fr")
    assert "French" in meeting.render(tmp_path, USER)
    assert "## First meeting" in meeting.render(tmp_path, USER)
    assert meeting.render(tmp_path, "7654321") == ""
    assert complete(tmp_path)
    before = (tmp_path / "users" / USER / "first_meeting.json").read_bytes()
    meeting.initialize(tmp_path, USER, "en")
    assert not complete(tmp_path)
    assert "## First meeting" not in meeting.render(tmp_path, USER)
    assert "French" in meeting.render(tmp_path, USER)
    assert (tmp_path / "users" / USER / "first_meeting.json").read_bytes() == before
    assert not list(tmp_path.rglob("*.md"))


@pytest.mark.parametrize("decision", [{"complete": "true"}, {"sources": []}, {"sources": ["unknown"]},
    {"sources": [SOURCE.source_id, SOURCE.source_id]}, {"sources": [None]}, {"complete": False}])
def test_completion_requires_explicit_boolean_and_exact_known_user_sources(tmp_path, decision):
    meeting.initialize(tmp_path, USER, "en")
    assert not complete(tmp_path, **decision)
    assert meeting.read_state(tmp_path, USER)["phase"] == "active"


def test_assistant_or_derived_source_cannot_complete_meeting(tmp_path):
    meeting.initialize(tmp_path, USER, "en")
    raw = json.dumps({"first_meeting": {"complete": True, "sources": [SOURCE.source_id]}})
    for source in [replace(SOURCE, locator="date — assistant"), replace(SOURCE, kind="active_context")]:
        assert not meeting.record_completion(tmp_path, USER, raw, {SOURCE.source_id: source}, trace_id="test")


@pytest.mark.parametrize("kind", ["file", "parent", "oversize", "foreign", "invalid"])
def test_invalid_or_redirected_metadata_preserves_ordinary_voice(tmp_path, kind):
    meeting.initialize(tmp_path, USER, "en")
    path = tmp_path / "users" / USER / "first_meeting.json"
    if kind == "file":
        other = tmp_path / "outside"
        path.rename(other)
        path.symlink_to(other)
    elif kind == "parent":
        parent = path.parent
        other = tmp_path / "outside"
        parent.rename(other)
        parent.symlink_to(other)
    elif kind == "oversize":
        path.write_text(" " * 9000)
    elif kind == "foreign":
        value = json.loads(path.read_text())
        value["user_id"] = "another"
        path.write_text(json.dumps(value))
    else:
        path.write_text("{bad")
    assert meeting.render(tmp_path, USER) == ""
    assert not complete(tmp_path)


@pytest.mark.parametrize("platform", ["telegram", "openclaw"])
def test_both_hosts_compose_same_guidance_without_changing_current_message(tmp_path, monkeypatch, platform):
    from tests.test_canonical_voice import _freeze_inputs
    from daemon_core.canonical_voice import compose_canonical_voice_messages
    import daemon_core.held_atlas_sovereign as library
    _freeze_inputs(monkeypatch, scene=None)
    monkeypatch.setattr(library, "MEMORY_ROOT", tmp_path)
    def compose():
        return compose_canonical_voice_messages(USER, "Help me right now", platform=platform, message_received_at=123.0)[0]
    before = compose()
    meeting.initialize(tmp_path, USER, "fr")
    during = compose()
    assert during[-1] == before[-1]
    assert "First meeting is open" in json.dumps(during)
    assert complete(tmp_path)
    after = compose()
    assert after[-1] == before[-1]
    assert "First meeting is open" not in json.dumps(after)
    assert "French" in json.dumps(after)


def test_the_welcome_stops_after_three_user_turns_before_the_scene_closes_it(tmp_path, monkeypatch):
    from tests.test_canonical_voice import _freeze_inputs
    from daemon_core.canonical_voice import compose_canonical_voice_messages
    import daemon_core.held_atlas_sovereign as library
    _freeze_inputs(monkeypatch, scene=None)
    monkeypatch.setattr(library, "MEMORY_ROOT", tmp_path)
    meeting.initialize(tmp_path, USER, "fr")

    def compose(user_turns, current):
        dialogue = "\n\n".join(
            f"## 2026-10-24T17:0{i}:00+02:00 — user\n\nmessage {i}\n\n"
            f"## 2026-10-24T17:0{i}:30+02:00 — assistant\n\nreply {i}"
            for i in range(user_turns))
        # L0 is written before inference: the current message closes the window.
        dialogue += f"\n\n## 2026-10-24T17:09:00+02:00 — user\n\n{current}"
        monkeypatch.setattr(library, "load_recent_dialogue", lambda *a, **k: dialogue)
        return json.dumps(compose_canonical_voice_messages(USER, current, platform="telegram")[0])

    assert "First meeting is open" in compose(2, "À Lyon")
    assert "First meeting is open" not in compose(3, "ouais")
    assert meeting.is_active(tmp_path, USER)
    assert "French" in compose(3, "ouais")


def test_user_turns_counts_user_headers_like_the_dialogue_reader():
    dialogue = ("## 2026-10-24T17:00:00 — user\n\n/start\n\n"
                "## 2026-10-24T17:00:05 — assistant\n\nSalut, moi c'est Aster.\n\n"
                "## 2026-10-24T17:01:00 — User\n\nMorgan")
    assert meeting.user_turns(dialogue) == 2


@pytest.mark.parametrize("layout", [False, True])
def test_only_fresh_memory_is_seeded_and_retries_do_not_reset(tmp_path, layout):
    bundle = tmp_path / "bundle"
    template = bundle / "memory/templates/users/conversation.md"
    template.parent.mkdir(parents=True)
    template.write_text("Synthetic manner")
    template.with_name("persona.md").write_text("Synthetic persona")
    scope = SimpleNamespace(home=tmp_path / "home", attempt=tmp_path, validate_home=lambda _: None)
    initialize_private_memory(scope, bundle, USER, root_folder_memory=layout, conversation_language="fr")
    root = scope.home / "data/memory"
    if layout:
        root /= ".daemon"
    assert meeting.read_state(root, USER)["phase"] == "active"
    assert (root / "users" / USER / "persona.md").read_text() == "Synthetic persona"
    assert (root / "users" / USER / "conversation.md").read_text() == "Synthetic manner"
    assert not (root / "core/persona.md").exists()
    complete(root)
    initialize_private_memory(scope, bundle, USER, root_folder_memory=layout, conversation_language="en")
    assert meeting.read_state(root, USER)["phase"] == "complete"
    assert meeting.read_state(root, USER)["language"] == "fr"


def test_existing_memory_does_not_acquire_a_meeting_marker(tmp_path):
    bundle = tmp_path / "bundle"
    template = bundle / "memory/templates/users/conversation.md"
    template.parent.mkdir(parents=True)
    template.write_text("Synthetic manner")
    template.with_name("persona.md").write_text("Synthetic persona")
    scope = SimpleNamespace(home=tmp_path / "home", attempt=tmp_path, validate_home=lambda _: None)
    root = scope.home / "data/memory"
    root.mkdir(parents=True)
    (root / "known.md").write_text("An existing synthetic memory.")
    initialize_private_memory(scope, bundle, USER, conversation_language="fr")
    assert meeting.read_state(root, USER) is None


@pytest.mark.parametrize("language,choice,expected", [("fr", 1, "fr"), ("en", 1, "fr"), ("fr", 0, "en")])
def test_desktop_language_prefill_and_independent_choice(monkeypatch, language, choice, expected):
    monkeypatch.setenv("DAEMONS_CONVERSATION_LANGUAGE", language)
    ui = DesktopUI(io.StringIO(), io.StringIO())
    seen = []
    ui.choose = lambda title, options, default=0: (seen.append((title, options, default)) or choice)
    ui.first_meeting_preferences()
    assert seen == [("Conversation language", ("English", "Français"), int(language == "fr"))]
    assert ui.conversation_language == expected


def test_scene_closes_meeting_in_existing_provider_call(tmp_path, monkeypatch):
    from tests.test_scene_model import _context, _combined_raw, _raw, _dependency_raw, SequenceProvider
    from daemon_core import scene_model
    monkeypatch.setattr(scene_model, "MEMORY_ROOT", tmp_path)
    monkeypatch.setattr(scene_model, "SHADOW_LOG", tmp_path / "scene-log.jsonl")
    meeting.initialize(tmp_path, USER, "fr")
    context = _context()
    key = next(iter(context["sources"]))
    value = json.loads(_combined_raw(_raw(), _dependency_raw()))
    value["first_meeting"] = {"complete": True, "sources": [key]}
    provider = SequenceProvider([json.dumps(value)])
    snapshot = scene_model.run_scene_model_revision(USER, "telegram__test", provider,
        source_trace_id="test-turn", source_turn_started_at=10.0, context=context, strict=True)
    assert snapshot is not None
    assert len(provider.prompts) == 1
    assert "First meeting metadata" in provider.prompts[0][0]["content"]
    assert meeting.read_state(tmp_path, USER)["phase"] == "complete"
    assert meeting.read_state(tmp_path, USER)["completed_by"]["sources"][0]["id"] == key


def test_invalid_optional_judgment_does_not_break_valid_scene(tmp_path, monkeypatch):
    from tests.test_scene_model import _context, ScriptedProvider, _raw
    from daemon_core import scene_model
    monkeypatch.setattr(scene_model, "MEMORY_ROOT", tmp_path)
    monkeypatch.setattr(scene_model, "SHADOW_LOG", tmp_path / "scene-log.jsonl")
    meeting.initialize(tmp_path, USER, "en")
    provider = ScriptedProvider(_raw())
    assert scene_model.run_scene_model_revision(USER, "telegram__test", provider,
        source_trace_id="test-turn", source_turn_started_at=10.0, context=_context(), strict=True)
    assert len(provider.prompts) == 1
    assert meeting.read_state(tmp_path, USER)["phase"] == "active"


def test_concurrent_completions_keep_one_valid_monotonic_state(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    meeting.initialize(tmp_path, USER, "en")
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: complete(tmp_path), range(8)))
    assert outcomes.count(True) == 1
    state = meeting.read_state(tmp_path, USER)
    assert state["phase"] == "complete"
    meeting.initialize(tmp_path, USER, "fr")
    assert meeting.read_state(tmp_path, USER) == state


def test_a_language_saved_by_an_earlier_attempt_does_not_change_the_proposal(monkeypatch):
    monkeypatch.setenv("DAEMONS_CONVERSATION_LANGUAGE", "en")
    ui = DesktopUI(io.StringIO(), io.StringIO())
    ui.conversation_language = "fr"  # restored from an earlier attempt's progress
    seen = []
    ui.choose = lambda title, options, default=0: (seen.append(default) or default)
    ui.first_meeting_preferences()
    ui.first_meeting_preferences()  # Back to this step in the same session
    assert seen == [0, 0] and ui.conversation_language == "en"


def test_the_welcome_answers_reach_durable_memory_even_on_a_light_route(tmp_path, monkeypatch):
    """A name given in the welcome needs no recalled context, but it must be kept."""
    import daemon_core.loop as loop
    from daemon_core.context_router import ContextRoute
    from tests.test_common_ground_shadow import _memory_root, _reference

    import daemon_core.memory.reader as reader

    root = _memory_root(monkeypatch, tmp_path)
    monkeypatch.setattr(reader, "MEMORY_ROOT", root)
    user = "777"
    calls = []
    monkeypatch.setattr(loop, "run_memory_agent", lambda **kwargs: calls.append(kwargs["user_message"]))
    harness = loop.DaemonCore(provider=object(), routing_provider=object(), memory_provider=object())
    light = ContextRoute(mode="fast", reason="réponse de présentation", context_depth="light")

    def exchange(number, message, reply):
        return harness.update_memory_after_response(
            user_id=user, message=message, response=reply, route=light, selected_souvenirs=[],
            source_trace_id=f"turn-{number}", source_turn_started_at=float(number),
            current_user_l0=_reference(user, "user", message, f"turn-{number}"),
            current_assistant_l0=_reference(user, "assistant", reply, f"turn-{number}"),
            defer_replaceable_projections=True)

    exchange(1, "Morgan", "Enchantée, Morgan. Où es-tu en ce moment ?")
    assert calls == []  # no first-meeting metadata: ordinary routing
    meeting.initialize(root, user, "fr")
    exchange(2, "À Lyon", "Lyon, belle base.")
    exchange(3, "ouais", "D'accord.")
    exchange(4, "je sais pas", "Pas de souci.")
    assert calls == ["À Lyon", "ouais"]
