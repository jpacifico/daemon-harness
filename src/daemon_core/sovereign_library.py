"""Exact-memory library structures, readers and isolated test workspaces.

The standalone response composer and its Codex JSON runner are retired.
Shared library types and the thin composer's tool boundary remain unchanged.
"""

from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import yaml

from daemon_core.single_pass_sovereign import (
    load_active_memory_entries,
)
from daemon_core.sovereign import YIELD_SENTINEL


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIBRARY_TOOL = PROJECT_ROOT / "scripts" / "memory_library.py"

LIBRARY_TOOL_BOUNDARY = f"""
# Limits of this turn

The only authorized executions are the exact personal-library commands
specified above; they are part of your internal memory. You may perform no
other file read, command, web search, external action, reminder or service
access. If a correct response requires one of those other actions, respond
with exactly `{YIELD_SENTINEL}`, alone.
""".strip()


@dataclass(frozen=True)
class ActiveMemoryLibrary:
    workspace_root: Path
    root: Path
    souvenir_names: tuple[str, ...]
    source_chars: int
    sha256: str
    expired_souvenir_names: tuple[str, ...] = ()
    routing_metadata: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = ()

    @property
    def catalog_text(self) -> str:
        return self.render_catalog()

    def render_catalog(
        self,
        *,
        collection_coverage: dict[str, str] | None = None,
    ) -> str:
        metadata = {
            name: (subjects, facets)
            for name, subjects, facets in self.routing_metadata
        }
        collection_coverage = collection_coverage or {}
        lines = []
        for name in self.souvenir_names:
            subjects, facets = metadata.get(name, ((), ()))
            qualifiers = []
            if subjects:
                qualifiers.append("subjects: " + ", ".join(subjects))
            if facets:
                qualifiers.append("facets: " + ", ".join(facets))
            coverage = collection_coverage.get(name)
            if coverage in {"open", "closed"}:
                qualifiers.append("collection: " + coverage)
            suffix = " | " + " | ".join(qualifiers) if qualifiers else ""
            lines.append(f"- {name}{suffix}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ExactHotMemory:
    souvenirs: tuple[tuple[str, str], ...]
    rejected_paths: tuple[str, ...] = ()

    @property
    def souvenir_names(self) -> tuple[str, ...]:
        return tuple(name for name, _content in self.souvenirs)

    @property
    def rendered_chars(self) -> int:
        return len(self.text)

    @property
    def text(self) -> str:
        return "\n\n".join(
            f"## [[{name}]]\n\n{content}" for name, content in self.souvenirs
        )


@contextmanager
def isolated_active_memory_library(
    memory_root: Path,
    *,
    today: date | None = None,
    additional_entries: tuple[tuple[str, str], ...] = (),
) -> Iterator[ActiveMemoryLibrary]:
    """Materialize active exact bodies in a disposable library-only workspace."""

    entries, expired = load_active_memory_entries(memory_root, today=today)
    entries = list(entries)
    known_names = {name for name, _content in entries}
    for raw_name, raw_content in additional_entries:
        name = _clean_additional_library_name(raw_name)
        content = str(raw_content).strip()
        if not content:
            raise ValueError(f"Empty active-memory library entry: {name}")
        if name in known_names:
            raise ValueError(f"Duplicate active-memory library entry: {name}")
        known_names.add(name)
        entries.append((name, content))
    with tempfile.TemporaryDirectory(prefix="daemon-sovereign-library-") as tmp:
        workspace = Path(tmp)
        root = workspace / "library"
        root.mkdir()
        digest = hashlib.sha256()
        for name, content in entries:
            destination = root / f"{name}.md"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content + "\n", encoding="utf-8")
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content.encode("utf-8"))
            digest.update(b"\0")
        yield ActiveMemoryLibrary(
            workspace_root=workspace,
            root=root,
            souvenir_names=tuple(name for name, _content in entries),
            source_chars=sum(len(content) for _name, content in entries),
            sha256=digest.hexdigest(),
            expired_souvenir_names=tuple(expired),
            routing_metadata=tuple(
                (name, *frontmatter_routing_metadata(content))
                for name, content in entries
            ),
        )


def _clean_additional_library_name(value: str) -> str:
    name = str(value).strip().removesuffix(".md")
    path = Path(name)
    if (
        not name
        or path.is_absolute()
        or "\\" in name
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"Invalid active-memory library entry: {value}")
    return path.as_posix()


def frontmatter_routing_metadata(
    content: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not content.startswith("---\n"):
        return (), ()
    parts = content.split("---", 2)
    if len(parts) < 3:
        return (), ()
    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return (), ()
    if not isinstance(data, dict):
        return (), ()
    return _metadata_values(data.get("subjects")), _metadata_values(data.get("facets"))


def _metadata_values(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(
        text
        for item in values
        if (text := str(item).strip()) and text.casefold() != "none"
    )


def read_exact_hot_memory(
    library: ActiveMemoryLibrary,
    paths: list[str] | tuple[str, ...],
    *,
    max_souvenirs: int = 8,
    max_chars: int = 50_000,
) -> ExactHotMemory:
    """Read only exact active paths previously chosen by the held-thought judge."""

    allowed = set(library.souvenir_names)
    souvenirs = []
    rejected = []
    seen = set()
    for raw_path in paths:
        name = str(raw_path).strip().removesuffix(".md")
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or ".." in Path(name).parts
            or name not in allowed
        ):
            rejected.append(str(raw_path))
            continue
        if name in seen:
            continue
        seen.add(name)
        if len(souvenirs) >= max_souvenirs:
            rejected.append(name)
            continue
        content = (library.root / f"{name}.md").read_text(encoding="utf-8").strip()
        souvenirs.append((name, content))
    hot_memory = ExactHotMemory(tuple(souvenirs), tuple(rejected))
    if hot_memory.rendered_chars > max_chars:
        raise ValueError(
            "Exact hot memory exceeds its explicit bound "
            f"({hot_memory.rendered_chars} > {max_chars} chars)."
        )
    return hot_memory
