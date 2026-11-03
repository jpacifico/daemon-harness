"""Locale-independent calendar syntax for generated memory, never exact sources.

No relative-date resolution or semantic inference belongs here. A curator owns
that judgment. Only fully specified calendar values are reformatted; precision,
qualifiers, original source text and unrelated existing lines are preserved.
"""
from __future__ import annotations

import re
from datetime import date, datetime

MEMORY_DATE_RULES = """Date format for generated memory:
- Write calendar dates as YYYY-MM-DD, months known without a day as YYYY-MM,
  and years known without a month as YYYY. Never invent missing precision.
- Use ISO 8601 for timestamps, retaining the supplied time and timezone.
- Use a complete date at EACH end of a period; preserve approximate, uncertain,
  recurring and open-ended qualifications. Never guess an ambiguous numeric date.
- Apply this to new or factually revised agenda entries, notes, factual prose,
  history and date properties, regardless of the conversation's language.
- Keep existing unchanged factual lines and exact quotations verbatim. Never
  reformat source excerpts, paths, links, identifiers, titles or personal aliases.
"""

_MONTH_GROUPS = (
    ('january', 'janvier'), ('february', 'février', 'fevrier'),
    ('march', 'mars'), ('april', 'avril'), ('may', 'mai'), ('june', 'juin'),
    ('july', 'juillet'), ('august', 'août', 'aout'),
    ('september', 'septembre'), ('october', 'octobre'),
    ('november', 'novembre'), ('december', 'décembre', 'decembre'),
)
MONTH_NUMBERS = {word: number for number, words in enumerate(_MONTH_GROUPS, 1)
                 for word in words}
_MONTH = '(?:' + '|'.join(sorted(MONTH_NUMBERS, key=len, reverse=True)) + ')'
_YEAR = r'[1-9]\d{3}'
_DAY = r'\d{1,2}(?:st|nd|rd|th|er)?'
# A range with a shared month/year is syntax, not an inferred date. More complex
# ranges must supply both complete endpoints rather than losing the first one.
_RANGE_SEPARATOR = r'\s*(?:[-–]|to|through|au)\s*'
_DAY_RANGE = rf'{_DAY}(?:{_RANGE_SEPARATOR}{_DAY})?'
_DATE = re.compile(
    rf'(?<![\w/])(?:'
    rf'(?P<timestamp>{_YEAR}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}'
    rf'(?::\d{{2}}(?:\.\d{{1,6}})?)?(?:Z|[+-]\d{{2}}:\d{{2}})?)|'
    rf'(?P<iso>{_YEAR}-\d{{2}}-\d{{2}})(?!\d)|'
    rf'(?P<ym>{_YEAR}-\d{{2}})(?![-\d])|'
    rf'(?P<days>{_DAY_RANGE})\s+(?P<month>{_MONTH})\s+(?P<year>{_YEAR})|'
    rf'(?P<emonth>{_MONTH})\s+(?P<edays>{_DAY_RANGE}),?\s+(?P<eyear>{_YEAR})|'
    rf'(?P<onlymonth>{_MONTH})\s+(?P<onlyyear>{_YEAR})'
    rf')(?!\w)', re.IGNORECASE,
)
ISO_DAY_RE = re.compile(rf'\b(?P<date>{_YEAR}-\d{{2}}-\d{{2}})(?=\b|T\d{{2}}:\d{{2}})')
ISO_MONTH_RE = re.compile(rf'\b(?P<year>{_YEAR})-(?P<month>\d{{2}})(?![-\d])\b')
_PARTIAL_RANGE_PREFIX = re.compile(
    rf'(?<![-\w])(?:{_DAY}\s+{_MONTH}|{_DAY})\s*(?:[-–]|to|through|au|and|et|&)\s*$',
    re.IGNORECASE,
)
# Only these properties are dates. Other YAML values may be aliases or sources.
_DATE_PROPERTY = re.compile(
    r'^(?P<prefix>\s*(?:date|created|updated|created_at|updated_at|valid_until|'
    r'span_start|span_end|starts_on|ends_on|confirmed_on|evidence_date):\s*)'
    r'(?P<value>.*)$'
)
_PROTECTED_INLINE = re.compile(
    r'(`+)[^`\n]*?\1|\[\[[^\n]*?\]\]|!?\[[^\]\n]*\]\([^\n]*?\)|'
    r'https?://\S+|(?:[\w.~:/-]+/)[^\s<>]*|'
    r'"[^"\n]*"|“[^”\n]*”|«[^»\n]*»|‘[^’\n]*’|'
    r"(?<!\w)'[^'\n]*'(?!\w)"
)


class MemoryDateError(ValueError):
    """An explicit generated calendar value is invalid or incomplete."""


def normalize_date_text(text: str) -> str:
    """Canonicalize explicit FR/EN/ISO dates, preserving unknown precision.

    This function is for a declared date field or generated prose only. Call
    normalize_memory_dates for Markdown, so exact citations stay unchanged.
    """
    def replace(match: re.Match) -> str:
        if _PARTIAL_RANGE_PREFIX.search(text[:match.start()]):
            raise MemoryDateError('Each date in a range must have an explicit month and year')
        try:
            if match['timestamp']:
                datetime.fromisoformat(match['timestamp'])
                return match['timestamp']
            if match['iso']:
                return date.fromisoformat(match['iso']).isoformat()
            if match['ym']:
                year, month = map(int, match['ym'].split('-'))
                date(year, month, 1)
                return match['ym']
            month_word = match['month'] or match['emonth'] or match['onlymonth']
            month = MONTH_NUMBERS[month_word.casefold()]
            year = int(match['year'] or match['eyear'] or match['onlyyear'])
            days = match['days'] or match['edays']
            if days is None:
                return f'{year:04d}-{month:02d}'
            numbers = [int(value) for value in re.findall(r'\d+', days)]
            values = [date(year, month, number).isoformat() for number in numbers]
            if len(values) == 2:
                if values[0] > values[1]:
                    raise ValueError('reversed calendar range')
                separator = re.search(_RANGE_SEPARATOR, days, re.IGNORECASE)[0]
                return separator.join(values)
            return values[0]
        except ValueError as exc:
            raise MemoryDateError('Invalid calendar date: ' + match[0]) from exc
    return _DATE.sub(replace, text)


def _normalize_prose(text: str) -> str:
    parts, end = [], 0
    for match in _PROTECTED_INLINE.finditer(text):
        parts.extend((normalize_date_text(text[end:match.start()]), match[0]))
        end = match.end()
    parts.append(normalize_date_text(text[end:]))
    return ''.join(parts)


def normalize_memory_dates(content: str, *, current: str = '') -> str:
    """Format new generated lines only, without modifying exact source material.

    No batch migration: an unchanged existing line remains byte-for-byte intact,
    including one moved to History by its curator. Fences, quotes, links, titles,
    sources and non-date YAML properties are always opaque.
    """
    existing = set(current.splitlines())
    lines = content.splitlines(keepends=True)
    result, fence, sources_level = [], '', 0
    frontmatter = bool(lines and lines[0].strip() == '---')
    for number, line in enumerate(lines):
        body = line.rstrip('\r\n')
        ending = line[len(body):]
        stripped = body.lstrip()
        if frontmatter:
            if number and stripped == '---':
                frontmatter = False
            prop = _DATE_PROPERTY.fullmatch(body)
            if prop and body not in existing:
                value = prop['value']
                quote = value[:1] if value[:1] in {'"', "'"} and value[-1:] == value[:1] else ''
                inner = value[1:-1] if quote else value
                body = prop['prefix'] + quote + normalize_date_text(inner) + quote
            result.append(body + ending)
            continue
        marker = re.match(r'^(`{3,}|~{3,})', stripped)
        if marker:
            if not fence:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = ''
            result.append(line)
            continue
        heading = re.match(r'^(#{1,6})\s+(.*)', stripped)
        if heading:
            level = len(heading[1])
            if sources_level and level <= sources_level:
                sources_level = 0
            if heading[2].strip().casefold() in {'sources', 'source', 'citations', 'references'}:
                sources_level = level
        if (not fence and not sources_level and not heading and body not in existing
                and not stripped.startswith(('>', '    ', '<!--'))
                and not body.startswith(('    ', '\t'))):
            body = _normalize_prose(body)
        result.append(body + ending)
    return ''.join(result)
