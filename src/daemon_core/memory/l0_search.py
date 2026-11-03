"""Read-only, bounded text navigation selected by the existing L0 judge.

Matches locate exact passages; neither lexical overlap nor a missed match is
an assertion about a personal fact. Only the evidence judge can establish it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import re
import unicodedata

MAX_SEARCH_DAYS = 366
MAX_SEARCH_FILES = 400
MAX_SEARCH_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_MATCH_FILES = 8
MAX_MATCH_WINDOWS = 12
MAX_CANDIDATE_WINDOWS = 512
MAX_WINDOW_CHARS = 6_000
MAX_MATCH_CHARS = 40_000


@dataclass(frozen=True)
class L0TextQuery:
    start: str
    end: str
    terms: tuple[str, ...]


def _fold(text: str) -> str:
    plain = "".join(
        c
        for c in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(c)
    )
    return " " + " ".join(re.findall(r"\w+", plain)) + " "


def parse_text_query(raw: object) -> L0TextQuery | None:
    if not isinstance(raw, dict):
        return None
    start, end, terms = raw.get("start"), raw.get("end"), raw.get("terms")
    if not all(
        isinstance(s, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s)
        for s in (start, end)
    ):
        return None
    try:
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None
    if (
        not 0 <= days < MAX_SEARCH_DAYS
        or not isinstance(terms, list)
        or not 1 <= len(terms) <= 3
    ):
        return None
    if any(
        not isinstance(term, str)
        or not 2 <= len(term.strip()) <= 80
        or not _fold(term).strip()
        for term in terms
    ):
        return None
    return L0TextQuery(start, end, tuple(dict.fromkeys(term.strip() for term in terms)))


@dataclass
class L0TextMatches:
    sources: dict[str, str] = field(default_factory=dict)
    exact_sources: dict[str, str] = field(default_factory=dict)
    scanned_files: int = 0
    scanned_bytes: int = 0
    truncated: bool = False


def search_l0_text(
    memory_root: Path, user_id: str, query: L0TextQuery, catalog: dict[str, int]
) -> L0TextMatches:
    from daemon_core.memory.forensics import _parse_chronology_turns
    from daemon_core.memory.writer import safe_memory_user_id

    result = L0TextMatches()
    root = memory_root / "levels/L0_ground_zero/conversations"
    if not root.resolve().is_relative_to(memory_root.resolve()):
        return result
    prefix = safe_memory_user_id(user_id) + "_"
    terms = tuple(_fold(term) for term in query.terms)
    candidates, originals = [], {}
    for name in sorted(catalog):
        if not name.startswith(prefix) or Path(name).name != name:
            continue
        day = name[len(prefix) :].removesuffix(".md")
        if (
            not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
            or not name.endswith(".md")
            or not query.start <= day <= query.end
        ):
            continue
        path = root / name
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.resolve().parent != root.resolve()
            ):
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                result.truncated = True
                continue
            if (
                result.scanned_files >= MAX_SEARCH_FILES
                or result.scanned_bytes + size > MAX_SEARCH_BYTES
            ):
                result.truncated = True
                break
            # Bound the actual read as well as stat (a journal can grow).
            with path.open("rb") as stream:
                raw = stream.read(
                    min(MAX_FILE_BYTES, MAX_SEARCH_BYTES - result.scanned_bytes) + 1
                )
            if (
                len(raw) > MAX_FILE_BYTES
                or result.scanned_bytes + len(raw) > MAX_SEARCH_BYTES
            ):
                result.truncated = True
                continue
            result.scanned_files += 1
            result.scanned_bytes += len(raw)
            content = raw.decode("utf-8")
        except (OSError, UnicodeError):
            result.truncated = True
            continue
        turns = _parse_chronology_turns(content)
        windows: list[tuple[int, int, int]] = []
        for i, turn in enumerate(turns):
            if turn.role != "user":
                continue
            # Include adjacent context for pronouns. Assistant text can locate
            # a scene, but it never becomes evidence of a user's fact.
            begin, end = max(0, i - 2), min(len(turns) - 1, i + 1)
            excerpt = content[turns[begin].start : turns[end].end].strip()
            if len(excerpt) > MAX_WINDOW_CHARS:
                result.truncated = True
                continue
            folded = _fold(excerpt)
            if not all(term in folded for term in terms):
                continue
            direct_hits = sum(term in _fold(turn.body) for term in terms)
            # Merge overlapping matches before ranking. Dropping a later
            # overlap can discard the user's answer to an earlier topic cue.
            if windows and begin <= windows[-1][1]:
                prior_begin, prior_end, prior_hits = windows[-1]
                combined = content[turns[prior_begin].start : turns[end].end].strip()
                if len(combined) <= MAX_WINDOW_CHARS:
                    windows[-1] = (
                        prior_begin,
                        max(prior_end, end),
                        max(prior_hits, direct_hits),
                    )
                    continue
            windows.append((begin, end, direct_hits))
        for begin, end, direct_hits in windows:
            excerpt = content[turns[begin].start : turns[end].end].strip()
            candidates.append((-direct_hits, len(excerpt), name, begin, end, excerpt))
            originals[name] = content
            if len(candidates) > 2 * MAX_CANDIDATE_WINDOWS:
                candidates = sorted(candidates)[:MAX_CANDIDATE_WINDOWS]
                result.truncated = True
    intervals: dict[str, list[tuple[int, int]]] = {}
    count, chars = 0, 0
    for _score, _size, name, begin, end, excerpt in sorted(candidates):
        if any(
            old_start <= begin and end <= old_end
            for old_start, old_end in intervals.get(name, [])
        ):
            continue
        if (
            count >= MAX_MATCH_WINDOWS
            or (name not in result.sources and len(result.sources) >= MAX_MATCH_FILES)
            or chars + len(excerpt) + 2 > MAX_MATCH_CHARS
        ):
            result.truncated = True
            continue
        separator = "\n\n" if name in result.sources else ""
        result.sources[name] = result.sources.get(name, "") + separator + excerpt
        result.exact_sources[name] = originals[name]
        intervals.setdefault(name, []).append((begin, end))
        count += 1
        chars += len(excerpt) + len(separator)
    return result
