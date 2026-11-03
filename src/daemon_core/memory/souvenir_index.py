import re

from daemon_core.memory.reader import RESERVED_SOUVENIR_NAMES


MAX_LLM_INDEX_LINE_CHARS = 500


def merge_souvenir_index(existing: str, proposed: str) -> str:
    lines = []
    positions_by_name = {}
    for source in (existing, proposed):
        for line in source.splitlines():
            normalized = line.strip().lower()
            if not normalized:
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            name = index_entry_name(line)
            if name in RESERVED_SOUVENIR_NAMES:
                continue
            if name in positions_by_name:
                lines[positions_by_name[name]] = line.rstrip()
                continue
            positions_by_name[name] = len(lines)
            lines.append(line.rstrip())
    return "\n".join(lines).strip() + "\n"


def index_entry_name(line: str) -> str:
    text = line.strip()
    if text.startswith("-"):
        text = text[1:].strip()
    return text.split(":", 1)[0].strip()


def existing_souvenir_index_line(index_content: str, name: str) -> str:
    for line in index_content.splitlines():
        if index_entry_name(line) == name:
            return line.rstrip() + "\n"
    return ""


def validated_souvenir_index_line(name: str, raw_line: object) -> str:
    """Accept an LLM-authored index line after structural validation only."""
    if not isinstance(raw_line, str):
        return ""
    lines = [line.strip() for line in raw_line.splitlines() if line.strip()]
    if len(lines) != 1:
        return ""
    line = lines[0]
    if (
        not line.startswith("- ")
        or len(line) > MAX_LLM_INDEX_LINE_CHARS
        or index_entry_name(line) != name
        or ":" not in line
    ):
        return ""
    return line + "\n"


def souvenir_index_entry(name: str, content: str, preferred_anchor_lines: list[str] | None = None) -> str:
    description = _souvenir_subject(content) or _first_markdown_heading(content) or name
    anchors = souvenir_anchor_summary(content, preferred_anchor_lines=preferred_anchor_lines)
    if anchors and anchors.lower() not in description.lower():
        description = f"{description} — {anchors}"
    return f"- {name}: {description}\n"


def souvenir_anchor_summary(
    content: str,
    max_chars: int = 170,
    preferred_anchor_lines: list[str] | None = None,
) -> str:
    recall_lines = _prioritize_recall_lines(
        preferred_anchor_lines or [],
        _souvenir_recall_lines(content),
    )
    if not recall_lines:
        return ""
    anchors = []
    used = 0
    for line in recall_lines:
        cleaned = _clean_anchor_line(line)
        if not cleaned:
            continue
        next_len = len(cleaned) if not anchors else len(cleaned) + 3
        if anchors and used + next_len > max_chars:
            break
        anchors.append(cleaned)
        used += next_len
        if used >= max_chars:
            break
    return " ; ".join(anchors)


def _prioritize_recall_lines(preferred: list[str], fallback: list[str]) -> list[str]:
    lines = []
    seen = set()
    for line in [*preferred, *fallback]:
        cleaned = _clean_anchor_line(line)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            lines.append(cleaned)
            seen.add(key)
    return lines


def _souvenir_recall_lines(content: str) -> list[str]:
    recall_lines = []
    current_section = ""
    useful_sections = {"## résumé", "## sous-capsules", "## liens proches", "## faits"}
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current_section = stripped.lower()
            continue
        if current_section not in useful_sections or not stripped.startswith("- "):
            continue
        recall_lines.append(stripped[2:].strip())
    return recall_lines


def _clean_anchor_line(line: str) -> str:
    cleaned = re.sub(r"\s+", " ", line).strip()
    return cleaned.rstrip(".")


def _souvenir_subject(content: str) -> str:
    for line in content.splitlines():
        text = line.strip()
        if text.lower().startswith("sujet:"):
            return text.split(":", 1)[1].strip()
    return ""


def _first_markdown_heading(content: str) -> str:
    for line in content.splitlines():
        text = line.strip()
        if text.startswith("#"):
            return text.lstrip("#").strip()
    return ""
