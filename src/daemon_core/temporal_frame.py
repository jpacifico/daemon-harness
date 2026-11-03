"""Deterministic temporal provenance shared by every voice path."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


PARIS_TIMEZONE = ZoneInfo("Europe/Paris")
ENGLISH_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


@dataclass(frozen=True)
class _DialogueTurn:
    occurred_at: datetime | None
    role: str
    body: str


@dataclass(frozen=True)
class TemporalFrame:
    message_received_at: datetime
    previous_turn_at: datetime | None = None
    previous_turn_role: str = ""
    working_state_revised_at: datetime | None = None

    @property
    def elapsed_seconds(self) -> int | None:
        if self.previous_turn_at is None:
            return None
        elapsed = (
            self.message_received_at - self.previous_turn_at
        ).total_seconds()
        return int(elapsed) if elapsed >= 0 else None

    def render(self) -> str:
        lines = [
            f'Now : {_format_timestamp(self.message_received_at)}',
        ]
        if self.previous_turn_at is None:
            lines.append('Last completed turn : timestamp unavailable.')
        else:
            role = (
                f" ({self.previous_turn_role})"
                if self.previous_turn_role
                else ""
            )
            lines.append(
                f'Last completed turn{role} : {_format_timestamp(self.previous_turn_at)}'
            )
            if self.elapsed_seconds is not None:
                lines.append(
                    'Time elapsed since that turn : '
                    + _format_duration(self.elapsed_seconds)
                )
        if self.working_state_revised_at is not None:
            lines.append(
                'Working snapshot revised : '
                + _format_timestamp(self.working_state_revised_at)
            )
        lines.append(
            'Revisable state describes its revision time, not necessarily the present. Compare it with the message and elapsed interval; mention these reference points only when timing matters in the exchange.'
        )
        return "\n".join(lines)


def build_temporal_frame(
    recent_dialogue: str,
    current_message: str,
    *,
    message_received_at: datetime | float | None = None,
    working_state_revised_at: datetime | float | None = None,
) -> TemporalFrame:
    """Build one clock frame from raw L0 provenance, without semantic judgment."""

    turns = _parse_dialogue_turns(recent_dialogue)
    if (
        turns
        and turns[-1].role == "user"
        and turns[-1].body.strip() == current_message.strip()
    ):
        turns.pop()
    previous = next(
        (turn for turn in reversed(turns) if turn.occurred_at is not None),
        None,
    )
    return TemporalFrame(
        message_received_at=resolve_reference_time(message_received_at),
        previous_turn_at=previous.occurred_at if previous else None,
        previous_turn_role=previous.role if previous else "",
        working_state_revised_at=(
            resolve_reference_time(working_state_revised_at)
            if working_state_revised_at is not None
            else None
        ),
    )


def resolve_reference_time(
    value: datetime | float | None = None,
) -> datetime:
    if isinstance(value, datetime):
        current = value
    elif isinstance(value, (int, float)):
        current = datetime.fromtimestamp(value, tz=PARIS_TIMEZONE)
    else:
        forced = os.getenv("DAEMON_CORE_EVAL_NOW", "").strip()
        try:
            current = datetime.fromisoformat(forced) if forced else None
        except ValueError:
            current = None
        current = current or datetime.now(PARIS_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=PARIS_TIMEZONE)
    return current.astimezone(PARIS_TIMEZONE)


def format_reference_timestamp(value: datetime | float | None = None) -> str:
    """Render one unambiguous local clock with its calendar weekday."""

    current = resolve_reference_time(value)
    weekday = ENGLISH_WEEKDAYS[current.weekday()]
    return (
        f"{current.isoformat(timespec='seconds')} "
        f"(weekday={weekday}, timezone=Europe/Paris)"
    )


def _parse_dialogue_turns(recent_dialogue: str) -> list[_DialogueTurn]:
    turns: list[_DialogueTurn] = []
    timestamp: datetime | None = None
    role = ""
    lines: list[str] = []

    def flush() -> None:
        if role:
            turns.append(
                _DialogueTurn(
                    occurred_at=timestamp,
                    role=role,
                    body="\n".join(lines).strip(),
                )
            )

    for line in recent_dialogue.splitlines():
        if line.startswith("## ") and " — " in line:
            flush()
            raw_timestamp, raw_role = line[3:].rsplit(" — ", 1)
            timestamp = _parse_timestamp(raw_timestamp.strip())
            role = raw_role.strip().casefold()
            lines = []
        elif role:
            lines.append(line)
    flush()
    return turns


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=PARIS_TIMEZONE)
    return parsed.astimezone(PARIS_TIMEZONE)


def _format_timestamp(value: datetime) -> str:
    return format_reference_timestamp(value)


def _format_duration(total_seconds: int) -> str:
    remaining = max(0, total_seconds)
    units = []
    for size, singular, plural in (
        (86_400, "day", "days"),
        (3_600, "hour", "hours"),
        (60, "minute", "minutes"),
        (1, "second", "seconds"),
    ):
        count, remaining = divmod(remaining, size)
        if count:
            units.append(f"{count} {singular if count == 1 else plural}")
    return ", ".join(units) if units else "less than one second"
