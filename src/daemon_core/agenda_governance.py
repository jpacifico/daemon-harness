"""Small, shared boundary for the operational personal agenda.

``agenda.md`` is a current timeline, not a second souvenir store.  A row may be
served as agenda authority only when its horizon can be placed on a calendar
or is an explicit recurrence.  Legacy rows remain preserved until migration,
but they are withheld from the voice and cannot be introduced again.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date

from daemon_core.memory_dates import (
    ISO_DAY_RE, ISO_MONTH_RE, MemoryDateError, normalize_date_text,
)

_ISO_DATE_RE = ISO_DAY_RE
_MONTH_YEAR_RE = ISO_MONTH_RE
_RECURRENCE_RE = re.compile(
    r"^(?:every|each|daily|weekly|monthly|yearly|annually|chaque|tous?\s+les|toutes?\s+les)\b",
    re.IGNORECASE,
)
_WEEKDAY_RE = re.compile(
    r"^(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)(?:\b|s\b)",
    re.IGNORECASE,
)
_FRENCH_DATE_RE = re.compile(
    r"\b(?P<day>[0-3]?\d)(?:er)?\s+"
    r"(?P<month>janvier|f[ée]vrier|mars|avril|mai|juin|juillet|"
    r"ao[uû]t|septembre|octobre|novembre|d[ée]cembre)"
    r"(?:\s+(?P<year>20\d{2}))?\b",
    re.IGNORECASE,
)
_TEMPORAL_CUE_RE = re.compile(
    r"(?:\b20\d{2}-\d{2}-\d{2}\b|"
    r"\b\d{1,2}\s*h(?:\s*\d{2})?\b|"
    r"\b(?:janvier|fevrier|mars|avril|mai|juin|juillet|aout|"
    r"septembre|octobre|novembre|decembre)\b|"
    r"\b(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b|"
    r"\b(?:aujourd'hui|demain|apres-demain|hier|avant|apres|jusqu|"
    r"chaque|tous les|toutes les|rendez-vous|prevu|prevue|planifie|"
    r"planifiee|horaire|echeance|devra|doit)\b)"
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TOKEN_STOPWORDS = frozenset(
    {
        "ainsi",
        "alors",
        "apres",
        "autre",
        "avant",
        "avec",
        "avoir",
        "aura",
        "aux",
        "car",
        "chez",
        "comme",
        "choix",
        "dans",
        "date",
        "devra",
        "doit",
        "elle",
        "entre",
        "est",
        "etre",
        "faire",
        "fait",
        "heure",
        "horaire",
        "jour",
        "lieu",
        "mais",
        "non",
        "pas",
        "pendant",
        "pour",
        "pris",
        "rendez",
        "sans",
        "sera",
        "son",
        "sur",
        "tot",
        "travail",
        "une",
        "vous",
    }
)
_MONTH_NUMBERS = {
    "janvier": 1,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
}


@dataclass(frozen=True)
class AgendaLineAssessment:
    line: str
    horizon: str
    operational: bool
    reason: str


@dataclass(frozen=True)
class SouvenirAgendaConflict:
    souvenir_line: str
    agenda_line: str


def assess_personal_agenda_line(line: str) -> AgendaLineAssessment:
    """Classify one Markdown bullet without interpreting its event body."""

    stripped = line.strip()
    if not stripped.startswith("- "):
        return AgendaLineAssessment(stripped, "", False, "not_a_bullet")
    raw = stripped[2:].strip()
    horizon = raw.split("|", 1)[0].strip()
    if not horizon:
        return AgendaLineAssessment(stripped, horizon, False, "missing_horizon")

    try:
        calendar_horizon = normalize_date_text(horizon)
    except MemoryDateError:
        return AgendaLineAssessment(stripped, horizon, False, "invalid_absolute_date")
    iso_dates = [match.group("date") for match in _ISO_DATE_RE.finditer(calendar_horizon)]
    if iso_dates:
        if all(_valid_iso_date(value) for value in iso_dates):
            return AgendaLineAssessment(stripped, horizon, True, "absolute_date")
        return AgendaLineAssessment(
            stripped,
            horizon,
            False,
            "invalid_absolute_date",
        )
    if _MONTH_YEAR_RE.search(calendar_horizon):
        return AgendaLineAssessment(stripped, horizon, True, "bounded_month")
    if _RECURRENCE_RE.search(horizon) or _WEEKDAY_RE.search(horizon):
        return AgendaLineAssessment(
            stripped,
            horizon,
            True,
            "explicit_recurrence",
        )
    return AgendaLineAssessment(stripped, horizon, False, "unbounded_horizon")


def invalid_new_agenda_lines(
    current: str,
    proposed: str,
) -> tuple[AgendaLineAssessment, ...]:
    """Return newly introduced or rewritten rows without a usable horizon.

    Existing legacy rows are tolerated byte-for-byte so one valid update is not
    blocked by old contamination.  The voice projection withholds those rows
    independently until the provenance-preserving migration removes them.
    """

    existing = Counter(_agenda_bullets(current))
    proposed_counts = Counter(_agenda_bullets(proposed))
    additions = list((proposed_counts - existing).elements())
    return tuple(
        assessment
        for line in additions
        if not (assessment := assess_personal_agenda_line(line)).operational
    )


def souvenir_agenda_conflicts(
    *,
    current_souvenir: str,
    proposed_souvenir: str,
    agenda: str,
) -> tuple[SouvenirAgendaConflict, ...]:
    """Legacy lexical diagnostic, not an authority at the memory write boundary.

    Kept for reproducibility of the 2026-10-10 false-positive report. Semantic
    ownership is judged in the existing MemoryAgent and souvenir merge calls.
    """

    existing = Counter(_markdown_bullets(current_souvenir))
    proposed = Counter(_markdown_bullets(proposed_souvenir))
    additions = list((proposed - existing).elements())
    governed = [
        line
        for line in _agenda_bullets(agenda)
        if assess_personal_agenda_line(line).operational
    ]
    conflicts = []
    for souvenir_line in additions:
        if not _has_temporal_cue(souvenir_line):
            continue
        souvenir_tokens = _semantic_tokens(souvenir_line)
        souvenir_dates = _date_keys(souvenir_line)
        if len(souvenir_tokens) < 2:
            continue
        for agenda_line in governed:
            agenda_tokens = _agenda_event_tokens(agenda_line)
            shared = souvenir_tokens & agenda_tokens
            smaller = min(len(souvenir_tokens), len(agenda_tokens))
            lexical_match = (
                len(shared) >= 2
                and bool(smaller)
                and len(shared) / smaller >= 0.5
            )
            date_match = bool(
                shared and souvenir_dates & _date_keys(agenda_line)
            )
            if lexical_match or date_match:
                conflicts.append(
                    SouvenirAgendaConflict(
                        souvenir_line=souvenir_line,
                        agenda_line=agenda_line,
                    )
                )
                break
    return tuple(conflicts)


def _agenda_bullets(content: str) -> list[str]:
    return [
        line.strip()
        for line in content.splitlines()
        if line.strip().startswith("- ")
    ]


def _markdown_bullets(content: str) -> list[str]:
    return [
        line.strip()
        for line in content.splitlines()
        if line.strip().startswith("- ")
    ]


def _ascii(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def _has_temporal_cue(value: str) -> bool:
    return _TEMPORAL_CUE_RE.search(_ascii(value).replace("’", "'")) is not None


def _semantic_tokens(value: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall(_ascii(value))
        if len(token) >= 3
        and not token.isdigit()
        and token not in _TOKEN_STOPWORDS
        and token not in _MONTH_NUMBERS
    }


def _agenda_event_tokens(value: str) -> set[str]:
    raw = value.strip().removeprefix("- ")
    fields = [field.strip() for field in raw.split("|")]
    event = " | ".join(fields[2:]) if len(fields) >= 3 else raw
    return _semantic_tokens(event)


def _date_keys(value: str) -> set[str]:
    keys = set()
    try:
        value = normalize_date_text(value)
    except MemoryDateError:
        return keys
    for match in _ISO_DATE_RE.finditer(value):
        raw = match.group("date")
        if _valid_iso_date(raw):
            keys.add(raw)
            keys.add("--" + raw[5:])
    for match in _FRENCH_DATE_RE.finditer(value):
        month = _MONTH_NUMBERS.get(_ascii(match.group("month")))
        day = int(match.group("day"))
        year = match.group("year")
        if month is None or not 1 <= day <= 31:
            continue
        month_day = f"--{month:02d}-{day:02d}"
        keys.add(month_day)
        if year:
            raw = f"{int(year):04d}-{month:02d}-{day:02d}"
            if _valid_iso_date(raw):
                keys.add(raw)
    return keys


def _valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


__all__ = [
    "AgendaLineAssessment",
    "SouvenirAgendaConflict",
    "assess_personal_agenda_line",
    "souvenir_agenda_conflicts",
    "invalid_new_agenda_lines",
]
