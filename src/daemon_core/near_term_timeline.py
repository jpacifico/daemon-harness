"""Typed, deterministic projection of the agendas already loaded for a turn.

The projection does not decide what matters and does not infer an event.  It
only gives every agenda entry a stable identity, preserves its source text and,
when the source exposes a calendar date, places it against the turn clock.
"""

from __future__ import annotations

import calendar
import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from daemon_core.agenda_governance import assess_personal_agenda_line
from daemon_core.memory_dates import ISO_DAY_RE, ISO_MONTH_RE, normalize_date_text

PERSONAL_AGENDA = "personal_agenda"
PROFESSIONAL_AGENDA = "professional_calendar"
NEAR_TERM_DAYS = 14
RECENTLY_CROSSED_DAYS = 7

_ISO_DATE_RE = ISO_DAY_RE
_MONTH_YEAR_RE = ISO_MONTH_RE
_PROFESSIONAL_TIME_RE = re.compile(
    r"^(?P<start>\d{1,2}:\d{2})\s*[–-]\s*(?P<end>\d{1,2}:\d{2})\s+"
)
_PROFESSIONAL_DAY_RE = re.compile(
    r"^##\s+(?:[A-Za-zÀ-ÿ]+\s+)?(?P<day>\d{1,2})\s+"
    r"(?P<month>[A-Za-zÀ-ÿ]+)(?:\s+(?P<year>20\d{2}))?\s*$"
)
_PROFESSIONAL_ISO_DAY_RE = re.compile(
    r"^##\s+(?P<date>20\d{2}-\d{2}-\d{2})(?:\s+.*)?$"
)

_FRENCH_MONTHS = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


@dataclass(frozen=True)
class TimelineSource:
    name: str
    kind: str
    sha256: str
    metadata: tuple[str, ...] = ()


@dataclass(frozen=True)
class TimelineEntry:
    entry_id: str
    source_name: str
    source_kind: str
    raw: str
    display_text: str
    starts_on: str = ""
    ends_on: str = ""
    starts_at: str = ""
    ends_at: str = ""
    open_ended: bool = False
    approximate: bool = False
    source_order: int = 0


@dataclass(frozen=True)
class WithheldAgendaEntry:
    entry_id: str
    source_name: str
    source_kind: str
    sha256: str
    reason: str
    source_order: int = 0


@dataclass(frozen=True)
class NearTermTimeline:
    now: str
    sources: tuple[TimelineSource, ...]
    entries: tuple[TimelineEntry, ...]
    withheld_entries: tuple[WithheldAgendaEntry, ...]
    source_bullet_count: int

    @property
    def complete(self) -> bool:
        return self.source_bullet_count == (
            len(self.entries) + len(self.withheld_entries)
        )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _entry_id(kind: str, raw: str, occurrence: int) -> str:
    identity = f"{kind}\0{raw}\0{occurrence}"
    return f"timeline-{_sha256(identity)[:16]}"


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_month_bounds(value: str) -> tuple[date, date] | None:
    matches = list(_MONTH_YEAR_RE.finditer(value))
    if not matches:
        return None
    first, last = matches[0], matches[-1]
    year, month = int(last["year"]), int(last["month"])
    return (
        date(int(first["year"]), int(first["month"]), 1),
        date(year, month, calendar.monthrange(year, month)[1]),
    )


def _personal_entries(
    text: str,
) -> tuple[
    TimelineSource,
    list[TimelineEntry],
    list[WithheldAgendaEntry],
    int,
]:
    entries: list[TimelineEntry] = []
    withheld: list[WithheldAgendaEntry] = []
    metadata: list[str] = []
    occurrences: dict[str, int] = {}
    bullet_count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("- "):
            if not stripped.startswith("#"):
                metadata.append(stripped)
            continue
        bullet_count += 1
        raw = stripped[2:].strip()
        occurrence = occurrences.get(raw, 0)
        occurrences[raw] = occurrence + 1
        assessment = assess_personal_agenda_line(stripped)
        if not assessment.operational:
            withheld.append(
                WithheldAgendaEntry(
                    entry_id=_entry_id(PERSONAL_AGENDA, raw, occurrence),
                    source_name="agenda personnel",
                    source_kind=PERSONAL_AGENDA,
                    sha256=_sha256(raw),
                    reason=assessment.reason,
                    source_order=bullet_count - 1,
                )
            )
            continue
        fields = [field.strip() for field in raw.split("|")]
        horizon = normalize_date_text(fields[0] if fields else raw)
        dates = [match.group("date") for match in _ISO_DATE_RE.finditer(horizon)]
        lowered = horizon.casefold()
        month_bounds = _parse_month_bounds(horizon) if not dates else None
        if dates:
            starts_on = dates[0]
            if len(dates) > 1:
                ends_on = dates[1]
            elif re.search(r"\b(?:semaine\s+du|week\s+of)\b", lowered):
                week_start = _parse_iso(starts_on)
                ends_on = (
                    (week_start + timedelta(days=6)).isoformat()
                    if week_start is not None
                    else starts_on
                )
            else:
                ends_on = starts_on
        elif month_bounds and re.search(r"jusqu|fin |\b(?:until|through|end of)\b", lowered) and len(list(_MONTH_YEAR_RE.finditer(horizon))) == 1:
            starts_on = ""
            ends_on = month_bounds[1].isoformat()
        elif month_bounds:
            starts_on = month_bounds[0].isoformat()
            ends_on = month_bounds[1].isoformat()
        else:
            starts_on = ""
            ends_on = ""
        open_ended = bool(re.match(r"(?:à compter du|a compter du|from|starting(?: on)?)\b", lowered)) and len(dates) <= 1 and not re.search(r"\b(?:until|through|to)\b", lowered)
        approximate = lowered.startswith(("vers ", "vers le ", "around ", "about ", "approximately "))
        date_free_horizon = _ISO_DATE_RE.sub("", horizon)
        date_free_horizon = re.sub(
            r"\b(?:à compter du|a compter du|vers le|vers|du|au|à|from|starting on|starting|around|about|approximately|to)\b",
            " ",
            date_free_horizon,
            flags=re.IGNORECASE,
        )
        date_free_horizon = " ".join(date_free_horizon.split()).strip(" ,-–")
        remainder = " | ".join(fields[1:]).strip()
        if starts_on:
            if open_ended:
                label = f"from {starts_on}"
            elif approximate:
                label = f"around {starts_on}"
            else:
                label = starts_on
            if ends_on and ends_on != starts_on:
                label += f" to {ends_on}"
            if date_free_horizon:
                label += f" ({date_free_horizon})"
            display = f"{label} | {remainder}" if remainder else label
        else:
            display = raw
        entries.append(
            TimelineEntry(
                entry_id=_entry_id(PERSONAL_AGENDA, raw, occurrence),
                source_name="agenda personnel",
                source_kind=PERSONAL_AGENDA,
                raw=raw,
                display_text=display,
                starts_on=starts_on,
                ends_on=ends_on,
                open_ended=open_ended,
                approximate=approximate,
                source_order=len(entries),
            )
        )
    return (
        TimelineSource(
            name="agenda personnel",
            kind=PERSONAL_AGENDA,
            sha256=_sha256(text),
            metadata=tuple(metadata),
        ),
        entries,
        withheld,
        bullet_count,
    )


def _document_year(text: str, now: datetime) -> int:
    for line in text.splitlines():
        if "synchro" not in line.casefold():
            continue
        match = re.search(r"\b(20\d{2})\b", line)
        if match:
            return int(match.group(1))
    return now.year


def _professional_heading_date(
    line: str,
    *,
    document_year: int,
    now: datetime,
) -> date | None:
    iso = _PROFESSIONAL_ISO_DAY_RE.match(line)
    if iso:
        return _parse_iso(iso.group("date"))
    match = _PROFESSIONAL_DAY_RE.match(line)
    if not match:
        return None
    month = _FRENCH_MONTHS.get(match.group("month").casefold())
    if month is None:
        return None
    year = int(match.group("year") or document_year)
    try:
        candidate = date(year, month, int(match.group("day")))
    except ValueError:
        return None
    if not match.group("year") and candidate < now.date() - timedelta(days=31):
        try:
            candidate = candidate.replace(year=year + 1)
        except ValueError:
            return None
    return candidate


def _professional_entries(
    text: str,
    *,
    now: datetime,
) -> tuple[TimelineSource, list[TimelineEntry], int]:
    entries: list[TimelineEntry] = []
    metadata: list[str] = []
    occurrences: dict[str, int] = {}
    bullet_count = 0
    current_day: date | None = None
    document_year = _document_year(text, now)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            parsed = _professional_heading_date(
                stripped,
                document_year=document_year,
                now=now,
            )
            if parsed is not None:
                current_day = parsed
                continue
        if not stripped.startswith("- "):
            if not stripped.startswith("#"):
                metadata.append(stripped)
            continue
        bullet_count += 1
        raw = stripped[2:].strip()
        occurrence_key = f"{current_day}\0{raw}"
        occurrence = occurrences.get(occurrence_key, 0)
        occurrences[occurrence_key] = occurrence + 1
        time_match = _PROFESSIONAL_TIME_RE.match(raw)
        starts_at = time_match.group("start") if time_match else ""
        ends_at = time_match.group("end") if time_match else ""
        day_text = current_day.isoformat() if current_day else ""
        display = f"{day_text} | {raw}" if day_text else raw
        entries.append(
            TimelineEntry(
                entry_id=_entry_id(
                    PROFESSIONAL_AGENDA,
                    occurrence_key,
                    occurrence,
                ),
                source_name="agenda professionnel",
                source_kind=PROFESSIONAL_AGENDA,
                raw=raw,
                display_text=display,
                starts_on=day_text,
                ends_on=day_text,
                starts_at=starts_at,
                ends_at=ends_at,
                source_order=len(entries),
            )
        )
    return (
        TimelineSource(
            name="agenda professionnel",
            kind=PROFESSIONAL_AGENDA,
            sha256=_sha256(text),
            metadata=tuple(metadata),
        ),
        entries,
        bullet_count,
    )


def compile_near_term_timeline(
    *,
    now: datetime,
    personal_agenda: str = "",
    professional_agenda: str = "",
) -> NearTermTimeline:
    """Compile the two controlled Markdown agenda formats without rereading."""

    sources: list[TimelineSource] = []
    entries: list[TimelineEntry] = []
    withheld_entries: list[WithheldAgendaEntry] = []
    bullet_count = 0
    if personal_agenda.strip():
        source, parsed, withheld, count = _personal_entries(personal_agenda)
        sources.append(source)
        entries.extend(parsed)
        withheld_entries.extend(withheld)
        bullet_count += count
    if professional_agenda.strip():
        source, parsed, count = _professional_entries(
            professional_agenda,
            now=now,
        )
        sources.append(source)
        entries.extend(parsed)
        bullet_count += count
    return NearTermTimeline(
        now=now.isoformat(timespec="seconds"),
        sources=tuple(sources),
        entries=tuple(entries),
        withheld_entries=tuple(withheld_entries),
        source_bullet_count=bullet_count,
    )


def _entry_date(entry: TimelineEntry) -> date | None:
    return _parse_iso(entry.starts_on)


def _entry_end_date(entry: TimelineEntry) -> date | None:
    return _parse_iso(entry.ends_on)


def timeline_group_key(entry: TimelineEntry, today: date) -> str:
    start = _entry_date(entry)
    end = _entry_end_date(entry)
    if start is None:
        if end is None:
            return "undated"
        if end >= today:
            return "current"
        if 1 <= (today - end).days <= RECENTLY_CROSSED_DAYS:
            return "recently_crossed"
        return "earlier"
    if (entry.open_ended and start <= today) or (
        end is not None and start <= today <= end and end != start
    ):
        return "current"
    delta = (start - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 1 < delta <= NEAR_TERM_DAYS:
        return "near"
    if delta > NEAR_TERM_DAYS:
        return "later"
    if end is not None and 1 <= (today - end).days <= RECENTLY_CROSSED_DAYS:
        return "recently_crossed"
    return "earlier"


def _entry_sort_key(entry: TimelineEntry) -> tuple[str, str, str, int]:
    return (
        entry.starts_on or "9999-12-31",
        entry.starts_at or "99:99",
        entry.source_kind,
        entry.source_order,
    )


def _date_label(value: date, prefix: str) -> str:
    return f"{prefix} — {value.isoformat()} ({_WEEKDAYS[value.weekday()]})"


def render_near_term_timeline(
    timeline: NearTermTimeline,
    *,
    include_now: bool = True,
) -> str:
    """Render one ordered view while keeping every source entry visible once."""

    now = datetime.fromisoformat(timeline.now)
    today = now.date()
    groups: dict[str, list[TimelineEntry]] = {
        "recently_crossed": [],
        "current": [],
        "today": [],
        "tomorrow": [],
        "near": [],
        "later": [],
        "undated": [],
        "earlier": [],
    }
    for entry in timeline.entries:
        groups[timeline_group_key(entry, today)].append(entry)
    for values in groups.values():
        values.sort(key=_entry_sort_key)

    sections: list[str] = []
    if include_now:
        sections.append(
            "## Now\n"
            f"{timeline.now} (weekday={_WEEKDAYS[today.weekday()]}, "
            f"timezone={now.tzname() or 'local'})."
        )
    headings = (
        (
            "recently_crossed",
            (
                "## Recently crossed dated horizons\n"
                "These source rows describe dates that have already passed. They do "
                "not prove that the event occurred; unless the row itself gives an "
                "outcome, its outcome remains unknown. Never present the scheduled "
                "event as still upcoming."
            ),
        ),
        ("current", "## Current dated horizons"),
        ("today", "## " + _date_label(today, "Today")),
        (
            "tomorrow",
            "## " + _date_label(today + timedelta(days=1), "Tomorrow"),
        ),
        ("near", f"## Next {NEAR_TERM_DAYS} days"),
        ("later", "## Later dated horizons"),
        (
            "undated",
            "## Recurring or bounded horizons without a resolved calendar date",
        ),
    )
    for key, heading in headings:
        values = groups[key]
        if not values:
            continue
        if key == "recently_crossed":
            lines = [
                f"- [{entry.source_name}; date crossed "
                f"{(_entry_end_date(entry) + timedelta(days=1)).isoformat()}] "
                f"{entry.display_text}"
                for entry in values
            ]
        else:
            lines = [
                f"- [{entry.source_name}] {entry.display_text}"
                for entry in values
            ]
        sections.append(heading + "\n" + "\n".join(lines))

    provenance: list[str] = []
    for source in timeline.sources:
        provenance.append(f"- {source.name}")
        provenance.extend(f"  - {line}" for line in source.metadata)
    if provenance:
        sections.append("## Source freshness and provenance\n" + "\n".join(provenance))
    return "\n\n".join(sections).strip()


__all__ = [
    "NEAR_TERM_DAYS",
    "RECENTLY_CROSSED_DAYS",
    "NearTermTimeline",
    "TimelineEntry",
    "TimelineSource",
    "WithheldAgendaEntry",
    "compile_near_term_timeline",
    "render_near_term_timeline",
    "timeline_group_key",
]
