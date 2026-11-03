from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core.temporal_frame import (
    build_temporal_frame,
    format_reference_timestamp,
)


PARIS = ZoneInfo("Europe/Paris")


def test_temporal_frame_excludes_the_current_l0_turn_and_exposes_a_cold_resume():
    dialogue = (
        "## 2026-07-21T06:44:29 — assistant\n\nBon voyage.\n\n"
        "## 2026-07-25T08:27:40 — user\n\nSalut toi"
    )

    frame = build_temporal_frame(
        dialogue,
        "Salut toi",
        message_received_at=datetime(2026, 7, 25, 8, 27, 40, tzinfo=PARIS),
        working_state_revised_at=datetime(
            2026, 7, 21, 6, 45, tzinfo=PARIS
        ),
    )

    assert frame.previous_turn_at == datetime(
        2026, 7, 21, 6, 44, 29, tzinfo=PARIS
    )
    assert frame.previous_turn_role == "assistant"
    assert frame.elapsed_seconds == 351_791
    rendered = frame.render()
    assert (
        "2026-07-25T08:27:40+02:00 "
        "(weekday=Saturday, timezone=Europe/Paris)"
    ) in rendered
    assert "2026-07-25T08:27:40+02:00" in rendered
    assert (
        "2026-07-21T06:44:29+02:00 "
        "(weekday=Tuesday, timezone=Europe/Paris)"
    ) in rendered
    assert "2026-07-21T06:44:29+02:00" in rendered
    assert '4 days, 1 hour, 43 minutes, 11 seconds' in rendered
    assert (
        'Working snapshot revised : 2026-07-21T06:45:00+02:00 (weekday=Tuesday, timezone=Europe/Paris)'
    ) in rendered
    assert 'not necessarily the present' in rendered


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        (
            "2026-07-25T08:27:10",
            datetime(2026, 7, 25, 8, 27, 40, tzinfo=PARIS),
            '30 seconds',
        ),
        (
            "2026-07-24T22:00:00",
            datetime(2026, 7, 25, 8, 0, 0, tzinfo=PARIS),
            '10 hours',
        ),
        (
            "2026-07-21T06:44:29",
            datetime(2026, 7, 25, 8, 27, 40, tzinfo=PARIS),
            '4 days, 1 hour, 43 minutes, 11 seconds',
        ),
    ],
)
def test_temporal_frame_scales_without_a_silence_threshold(
    previous,
    current,
    expected,
):
    frame = build_temporal_frame(
        f"## {previous} — assistant\n\nRéponse",
        "Nouveau message",
        message_received_at=current,
    )

    assert f'Time elapsed since that turn : {expected}' in frame.render()


def test_temporal_frame_keeps_an_old_working_state_as_dated_context():
    frame = build_temporal_frame(
        "## 2026-07-21T06:44:29 — assistant\n\nRéponse",
        "Salut",
        message_received_at=datetime(2026, 7, 25, 8, 27, 40, tzinfo=PARIS),
        working_state_revised_at=datetime(
            2026, 7, 21, 6, 45, tzinfo=PARIS
        ),
    )

    assert frame.working_state_revised_at is not None
    assert 'Working snapshot revised' in frame.render()


def test_reference_timestamp_keeps_weekday_and_timezone_consistent():
    rendered = format_reference_timestamp(
        datetime(2026, 7, 25, 10, 0, tzinfo=PARIS)
    )

    assert rendered == (
        "2026-07-25T10:00:00+02:00 "
        "(weekday=Saturday, timezone=Europe/Paris)"
    )
