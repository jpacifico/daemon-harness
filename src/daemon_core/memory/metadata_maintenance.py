"""Autonomous maintenance of memory-label vocabulary.

A conservative judge defaults to abstention. An operation journal and
reversible backups protect changes; exact note bodies remain byte-identical."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.metadata import (
    SouvenirMetadata,
    parse_souvenir_frontmatter,
    with_souvenir_metadata,
)
from daemon_core.memory.reader import (
    souvenir_facet_projection,
    souvenir_path,
    is_system_managed_souvenir,
)
from daemon_core.paths import MEMORY_ROOT
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider


logger = logging.getLogger("daemon_core.metadata_maintenance")

MAINTENANCE_INTERVAL_DAYS = 7


def maintenance_journal_path() -> Path:
    return MEMORY_ROOT / "levels" / "metadata_maintenance.jsonl"


def maybe_run_facet_maintenance(
    provider: LLMProvider,
    now: datetime | None = None,
) -> dict | None:
    """Run the weekly facet-vocabulary upkeep if it is due, else do nothing."""
    now = now or datetime.now()
    last = _last_run_at()
    if last is not None and now - last < timedelta(days=MAINTENANCE_INTERVAL_DAYS):
        return None
    merges = propose_facet_merges(provider)
    applied, skipped, backup_root = apply_facet_merges(merges)
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "proposed": merges,
        "applied": applied,
        "skipped": skipped,
        "backup_root": str(backup_root) if backup_root else "",
    }
    _append_journal(entry)
    logger.info(
        "Entretien des facettes | proposées=%s | appliquées=%s | ignorées=%s",
        len(merges),
        len(applied),
        len(skipped),
    )
    return entry


def propose_facet_merges(provider: LLMProvider) -> list[dict]:
    """LLM-judged merge map over the current facet catalog; abstention by default."""
    projection = _maintainable_facet_projection()
    if not projection:
        return []
    catalog = "\n".join(
        f"- {label} ({len(names)} souvenir(s)) : {', '.join(names[:4])}{'…' if len(names) > 4 else ''}"
        for label, names in sorted(projection.items(), key=lambda item: -len(item[1]))
    )
    messages = _build_merge_messages(catalog)
    log_llm_prompt(logger, "ENTRETIEN FACETTES (FUSIONS)", messages)
    raw = provider.complete(messages=messages, temperature=0.0)
    data = json.loads(_strip_json_fence(raw))
    labels = set(projection)
    merges = []
    for item in data.get("merges", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        source, target = item.get("from"), item.get("into")
        if source in labels and target in labels and source != target:
            merges.append(
                {
                    "from": source,
                    "into": target,
                    "reason": str(item.get("reason", ""))[:300],
                }
            )
    return merges


def apply_facet_merges(merges: list[dict]) -> tuple[list[str], dict[str, str], Path | None]:
    """Rewrite only the frontmatter of carrying souvenirs; bodies stay untouched."""
    if not merges:
        return [], {}, None
    mapping = {item["from"]: item["into"] for item in merges}
    projection = _maintainable_facet_projection()
    touched = sorted({name for label in mapping for name in projection.get(label, [])})
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_root = MEMORY_ROOT / "archive" / "metadata_maintenance" / stamp
    applied: list[str] = []
    skipped: dict[str, str] = {}
    for name in touched:
        path = souvenir_path(name)
        if not path.exists():
            skipped[name] = "souvenir absente"
            continue
        with memory_file_lock(path):
            current = path.read_text(encoding="utf-8")
            frontmatter, body = parse_souvenir_frontmatter(current)
            metadata = SouvenirMetadata.from_raw(frontmatter)
            new_facets = list(dict.fromkeys(mapping.get(f, f) for f in metadata.facets))
            if new_facets == metadata.facets:
                skipped[name] = "aucune facette concernée"
                continue
            updated = with_souvenir_metadata(
                current,
                SouvenirMetadata(subjects=metadata.subjects, facets=new_facets),
            )
            _frontmatter, updated_body = parse_souvenir_frontmatter(updated)
            if updated_body != body:
                skipped[name] = "le corps Markdown aurait changé"
                continue
            atomic_write_text(backup_root / f"{name}.md", current)
            atomic_write_text(path, updated)
            applied.append(name)
    return applied, skipped, backup_root if applied else None


def _maintainable_facet_projection() -> dict[str, list[str]]:
    projection = {}
    for label, names in souvenir_facet_projection().items():
        eligible = [name for name in names if not is_system_managed_souvenir(name)]
        if eligible:
            projection[label] = eligible
    return projection


def _build_merge_messages(catalog: str) -> list[dict]:
    system = """You are the gardener of Daemon Harness's facet-label vocabulary.

Memory souvenirs carry facet labels that serve as discovery paths. Search compares labels word for word: two synonymous labels are two paths unaware of one another.

Propose a merge map: each merge replaces a label with a canonical label of the same meaning.

Judgment rules:
- Merge true synonyms, wording variants and specializations so fine that they would never guide a distinct search.
- Judge each label from the searcher's perspective: a label exists only to be anticipated by a search plan. If a search targeting these souvenirs would naturally use a more established label in the catalog, merge into it.
- Preserve genuine distinctions: two facets leading to different souvenirs for different questions remain separate.
- Choose as canonical the most established (most widely used) label or, when tied, the clearest and most generic.
- Abstention is the default: when in doubt, do not merge. An empty list is a good answer when the vocabulary is healthy.
- Do not rename an isolated label if no synonym exists: legitimate novelty has a right to stand alone.
- Respond in strict JSON, without Markdown.

Schema:
{"merges":[{"from":"label to replace","into":"canonical label","reason":"short reason"}]}
"""
    user = f"""Current facet catalog (label, number of souvenirs carrying it, examples):

{catalog}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _last_run_at() -> datetime | None:
    path = maintenance_journal_path()
    if not path.exists():
        return None
    last = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            last = json.loads(line).get("ts", last)
        except json.JSONDecodeError:
            continue
    return datetime.fromisoformat(last) if last else None


def _append_journal(entry: dict) -> None:
    path = maintenance_journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with memory_file_lock(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _strip_json_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text
