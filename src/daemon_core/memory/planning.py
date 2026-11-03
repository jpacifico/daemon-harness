from __future__ import annotations

from dataclasses import dataclass, field


MAX_MEMORY_SUBJECTS = 6
MAX_MEMORY_FACETS = 8
MAX_MEMORY_NEEDS = 8
MAX_SUBJECT_CHARS = 120
MAX_NEED_CHARS = 400
MAX_INTENT_CHARS = 400


@dataclass
class MemoryNeed:
    """One semantic piece of personal context needed for the current reply."""

    description: str
    subjects: list[str] = field(default_factory=list)
    required: bool = True

    @classmethod
    def from_raw(cls, raw: object) -> MemoryNeed | None:
        if not isinstance(raw, dict):
            return None
        description = _clean_text(raw.get("description"), MAX_NEED_CHARS)
        if not description:
            return None
        return cls(
            description=description,
            subjects=_clean_text_list(
                raw.get("subjects"),
                max_items=MAX_MEMORY_SUBJECTS,
                max_chars=MAX_SUBJECT_CHARS,
            ),
            required=raw.get("required", True) is not False,
        )


@dataclass
class MemoryPlan:
    """Open semantic contract between scene understanding and memory recall."""

    subjects: list[str] = field(default_factory=list)
    facets: list[str] = field(default_factory=list)
    needs: list[MemoryNeed] = field(default_factory=list)
    intent: str = ""

    @classmethod
    def from_raw(cls, raw: object) -> MemoryPlan:
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            return cls()

        needs: list[MemoryNeed] = []
        raw_needs = raw.get("needs", [])
        if isinstance(raw_needs, list):
            for item in raw_needs:
                need = MemoryNeed.from_raw(item)
                if need is None:
                    continue
                if any(
                    existing.description.casefold() == need.description.casefold()
                    and existing.subjects == need.subjects
                    for existing in needs
                ):
                    continue
                needs.append(need)
                if len(needs) >= MAX_MEMORY_NEEDS:
                    break

        return cls(
            subjects=_clean_text_list(
                raw.get("subjects"),
                max_items=MAX_MEMORY_SUBJECTS,
                max_chars=MAX_SUBJECT_CHARS,
            ),
            facets=_clean_text_list(
                raw.get("facets"),
                max_items=MAX_MEMORY_FACETS,
                max_chars=MAX_SUBJECT_CHARS,
            ),
            needs=needs,
            intent=_clean_text(raw.get("intent"), MAX_INTENT_CHARS),
        )

    @property
    def is_empty(self) -> bool:
        return not self.subjects and not self.facets and not self.needs and not self.intent


def _clean_text(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_chars].strip()


def _clean_text_list(value: object, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item, max_chars)
        key = text.casefold()
        if not text or key in seen:
            continue
        cleaned.append(text)
        seen.add(key)
        if len(cleaned) >= max_items:
            break
    return cleaned
