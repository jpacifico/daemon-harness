import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.after_turn import (
    AfterTurnHooks,
    AfterTurnRequest,
    AfterTurnStageError,
    complete_turn,
    complete_turn_and_wait,
    record_user_turn,
)


def _request(**overrides):
    values = {
        "user_id": "u",
        "user_message": "question",
        "assistant_response": "réponse",
        "scene_scope": "telegram__u",
        "source_trace_id": "trace-1",
        "source_turn_started_at": 123.0,
    }
    values.update(overrides)
    return AfterTurnRequest(**values)


def test_after_turn_contract_normalizes_souvenirs_and_silence():
    request = _request(
        assistant_response=" NO_REPLY ",
        library_active=True,
        selected_souvenirs=("theme/hot", "theme/read"),
        read_souvenirs=("theme/read", "", "theme/other", "theme/read"),
    )

    assert request.assistant_is_visible is False
    assert request.read_souvenirs == ("theme/read", "theme/other")
    assert request.memory_souvenirs == (
        "theme/read",
        "theme/other",
        "theme/hot",
    )


def test_after_turn_preserves_unknown_selection_for_memory_fallback():
    request = _request(selected_souvenirs=None, library_active=False)

    assert request.selected_souvenirs is None
    assert request.memory_souvenirs is None


def test_record_user_turn_delegates_exact_content_to_writer():
    writes = []

    result = record_user_turn(
        "u",
        "contenu exact",
        writer=lambda **kwargs: writes.append(kwargs) or "path",
    )

    assert result == "path"
    assert writes == [
        {"user_id": "u", "role": "user", "content": "contenu exact"}
    ]


def test_after_turn_orders_working_state_memory_scene_and_followup():
    order = []
    request = _request()

    result = complete_turn(
        request,
        AfterTurnHooks(
            record_l0=lambda turn: order.append("l0"),
            revise_working_state=lambda turn: order.append("working"),
            update_memory=lambda turn: order.append("memory") or "updated",
            revise_scene=lambda turn: order.append("scene"),
            after_memory=lambda memory_result: order.append(
                f"after:{memory_result}"
            ),
        ),
        spawn=lambda target, name: target(),
    )

    assert order == ["l0", "working", "memory", "scene", "after:updated"]
    assert result.memory_result == "updated"
    assert result.working_state_started is True
    assert result.scene_started is True


def test_after_turn_does_not_revise_scene_when_memory_fails():
    order = []

    def fail_memory(turn):
        order.append("memory")
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        complete_turn(
            _request(),
            AfterTurnHooks(
                update_memory=fail_memory,
                revise_working_state=lambda turn: order.append("working"),
                revise_scene=lambda turn: order.append("scene"),
                on_error=lambda stage, exc: order.append(f"error:{stage}"),
            ),
            spawn=lambda target, name: target(),
        )

    assert order == ["working", "memory", "error:memory"]


def test_after_turn_scene_waits_for_working_state():
    targets = {}
    order = []

    def capture_spawn(target, name):
        targets[name] = target

    result = complete_turn(
        _request(),
        AfterTurnHooks(
            update_memory=lambda turn: order.append("memory"),
            revise_working_state=lambda turn: order.append("working"),
            revise_scene=lambda turn: order.append("scene"),
        ),
        spawn=capture_spawn,
    )

    working_name = next(name for name in targets if "working-state" in name)
    scene_name = next(name for name in targets if "scene" in name)
    targets[working_name]()
    targets[scene_name]()

    assert order == ["memory", "working", "scene"]
    assert result.working_state_started is True
    assert result.scene_started is True


def test_complete_turn_and_wait_acknowledges_child_stages():
    order = []

    result = complete_turn_and_wait(
        _request(),
        AfterTurnHooks(
            update_memory=lambda turn: order.append("memory") or "updated",
            revise_working_state=lambda turn: order.append("working"),
            revise_scene=lambda turn: order.append("scene"),
        ),
    )

    assert order.index("working") < order.index("scene")
    assert order.index("memory") < order.index("scene")
    assert result.memory_result == "updated"


def test_complete_turn_and_wait_surfaces_child_failure():
    def fail_scene(turn):
        raise RuntimeError("scene down")

    with pytest.raises(AfterTurnStageError) as raised:
        complete_turn_and_wait(
            _request(),
            AfterTurnHooks(
                update_memory=lambda turn: None,
                revise_scene=fail_scene,
            ),
        )

    assert raised.value.stage == "scene"
    assert isinstance(raised.value.cause, RuntimeError)
