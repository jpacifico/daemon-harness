"""Canary REM: sourced, single-episode Markdown memories.

The legacy REM remains available.  This module is activated only for an
allow-listed canary user or by the explicit CLI switch in :mod:`rem`. It uses
one offline model call for a bounded L0 window, may write zero to three scenes,
and advances its own cursor even when the correct decision is to abstain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from daemon_core.memory_dates import MEMORY_DATE_RULES, normalize_memory_dates
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.metadata import SouvenirMetadata
from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.paths import MEMORY_ROOT
from daemon_core.prompt_composer import current_time_context
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider


logger = logging.getLogger("daemon_core.episodic_rem")

EPISODIC_DOMAIN = "theme/scenes_episodiques"
EPISODIC_REM_ENV = "DAEMON_CORE_REM_EPISODIC_SCENES"
EPISODIC_REM_USER_IDS_ENV = "DAEMON_CORE_REM_EPISODIC_SCENE_USER_IDS"
EPISODIC_REM_PLATFORMS_ENV = "DAEMON_CORE_REM_EPISODIC_SCENE_PLATFORMS"
EPISODIC_STATE_SCHEMA = "daemon.rem-episodic-state.v1"
EPISODIC_STATE_PREFIX = "rem_episodic_state_"
DEFAULT_BOOTSTRAP_HOURS = 72
MIN_INTERVAL_HOURS = 6
MIN_CONTENT_CHARS = 400
MAX_WINDOW_CHARS = 300_000
MAX_SCENES_PER_PASS = 3
MAX_REVISIT_HOURS = 24
MAX_SCENE_BODY_CHARS = 12_000

TURN_RE = re.compile(
    r"^## (?P<timestamp>[^\n]+) — (?P<role>user|assistant)\n\n"
    r"(?P<content>.*?)(?=\n\n## [^\n]+ — (?:user|assistant)\n\n|\Z)",
    re.MULTILINE | re.DOTALL,
)


@dataclass(frozen=True)
class L0Turn:
    timestamp: str
    instant: datetime
    role: str
    content: str
    source_path: Path

    @property
    def rendered(self) -> str:
        return f"## {self.timestamp} — {self.role}\n\n{self.content}"

    @property
    def source_ref(self) -> str:
        journal = (
            "levels/L0_ground_zero/conversations/"
            + self.source_path.name
        )
        return f"{journal}#{self.timestamp} — {self.role}"


@dataclass(frozen=True)
class SceneClaim:
    text: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class SceneReading:
    text: str
    status: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class EpisodicScene:
    episode_id: str
    name: str
    title: str
    span_start: str
    span_end: str
    subjects: tuple[str, ...]
    facets: tuple[str, ...]
    anchors: tuple[str, ...]
    established: tuple[SceneClaim, ...]
    open_readings: tuple[SceneReading, ...]
    future_value: str
    uncertainties: tuple[str, ...]
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class EpisodicState:
    assessed_through: datetime
    revisit_from: datetime | None = None


def episodic_rem_enabled(user_id: str) -> bool:
    if os.getenv(EPISODIC_REM_ENV, "off").strip().casefold() != "canary":
        return False
    allowed = {
        value.strip()
        for value in os.getenv(EPISODIC_REM_USER_IDS_ENV, "").split(",")
        if value.strip()
    }
    return bool(user_id and user_id in allowed)


def episodic_scene_voice_enabled(user_id: str, platform: str) -> bool:
    if not episodic_rem_enabled(user_id):
        return False
    platforms = {
        value.strip()
        for value in os.getenv(
            EPISODIC_REM_PLATFORMS_ENV,
            "telegram,openclaw",
        ).split(",")
        if value.strip()
    }
    return platform in platforms


def episode_state_path(user_id: str, memory_root: Path = MEMORY_ROOT) -> Path:
    safe = safe_memory_user_id(user_id)
    return Path(memory_root) / "levels" / f"{EPISODIC_STATE_PREFIX}{safe}.json"


def read_episode_state(
    user_id: str,
    *,
    memory_root: Path = MEMORY_ROOT,
    now: datetime | None = None,
) -> EpisodicState:
    current = now or datetime.now()
    path = episode_state_path(user_id, memory_root)
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema") != EPISODIC_STATE_SCHEMA:
                raise ValueError("unknown episodic REM state schema")
            assessed = datetime.fromisoformat(str(payload["assessed_through"]))
            raw_revisit = payload.get("revisit_from")
            revisit = datetime.fromisoformat(str(raw_revisit)) if raw_revisit else None
            return EpisodicState(assessed, revisit)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning(
                "REM épisodique : état illisible (%s) — amorçage borné",
                path.name,
            )
    return EpisodicState(
        assessed_through=current - timedelta(hours=DEFAULT_BOOTSTRAP_HOURS)
    )


def write_episode_state(
    user_id: str,
    state: EpisodicState,
    *,
    memory_root: Path = MEMORY_ROOT,
) -> None:
    path = episode_state_path(user_id, memory_root)
    payload = {
        "schema": EPISODIC_STATE_SCHEMA,
        "assessed_through": state.assessed_through.isoformat(timespec="seconds"),
        "revisit_from": (
            state.revisit_from.isoformat(timespec="seconds")
            if state.revisit_from is not None
            else None
        ),
    }
    with memory_file_lock(path):
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )


def run_episodic_rem(
    user_id: str,
    provider: LLMProvider,
    *,
    memory_root: Path = MEMORY_ROOT,
    now: datetime | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Assess one bounded L0 window and optionally commit its scenes."""

    current = now or datetime.now()
    root = Path(memory_root)
    state = read_episode_state(user_id, memory_root=root, now=current)
    window_start = state.revisit_from or state.assessed_through
    gap_hours = max(0.0, _elapsed_seconds(window_start, current) / 3600)
    if not force and gap_hours < MIN_INTERVAL_HOURS:
        return _result("pas-mur", (), state, window_start, current)

    turns = _load_l0_turns(root, user_id, window_start, current)
    if not turns:
        return _result("vide", (), state, window_start, current)
    turns = _bounded_turns(turns, MAX_WINDOW_CHARS)
    effective_end = turns[-1].instant
    excerpt = "\n\n".join(turn.rendered for turn in turns)
    if not force and len(excerpt.strip()) < MIN_CONTENT_CHARS:
        return _result("pas-mur", (), state, window_start, effective_end)

    messages = build_episodic_messages(excerpt, now=current)
    log_llm_prompt(logger, "REM ÉPISODIQUE", messages)
    raw = provider.complete(messages=messages, temperature=0.0)
    payload = json.loads(_strip_json_fence(raw))
    scenes, revisit_from = parse_episodic_output(
        payload,
        turns,
        window_start=window_start,
        window_end=effective_end,
    )
    assessed_through = effective_end
    if len(scenes) == MAX_SCENES_PER_PASS:
        assessed_through = _latest_scene_end(scenes)
    next_state = EpisodicState(
        assessed_through=assessed_through,
        revisit_from=revisit_from,
    )
    previews = {scene.name: render_episode(scene) for scene in scenes}
    if dry_run:
        result = _result(
            "dry-run",
            scenes,
            next_state,
            window_start,
            effective_end,
        )
        result["previews"] = previews
        return result

    written: list[str] = []
    for scene in scenes:
        target = _episode_path(scene.name, root)
        if target.is_file():
            _validate_existing_episode(target, scene)
        else:
            from daemon_core.memory.library_layout import vault_root, with_identity, render_vault_links
            content = previews[scene.name].rstrip() + "\n"
            if vault_root(root) is not None:
                content = render_vault_links(root, with_identity(content, scene.name))
            atomic_write_text(target, content)
            written.append(scene.name)
    _repair_episode_index(scenes, root)
    write_episode_state(user_id, next_state, memory_root=root)
    if written:
        _refresh_semantic_cache(user_id, root, written)

    status = "ecrite" if written else "deja-evaluee" if scenes else "abstention"
    result = _result(status, scenes, next_state, window_start, effective_end)
    result["written"] = written
    return result


def build_episodic_messages(
    excerpt: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    system = f"""You are the episodic memory curator of a personal daemon.

Examine a past L0 window and select zero to {MAX_SCENES_PER_PASS} independent scenes worth retrieving later. A scene is a specific episode whose movement or meaning would materially change how a later development is received. A scene is neither a period summary, a fact sheet nor a chronicle of a daemon weakness. Complete abstention is normal.

Each scene must separate two levels:
- `established` contains only assertions made by the user in the excerpt. Each assertion cites only the exact USER timestamps supporting it. An elliptical answer such as "yes" confirms no more than the precise proposition it answers.
- `open_readings` contains only assistant-proposed interpretations that may illuminate the episode without having been established by the user. Each reading cites only the exact ASSISTANT timestamps carrying it. If the user unambiguously confirms it, it may remain here with status `partly_confirmed`; never silently attribute it to the user.

Assistant statements can never support `established`. Never turn a possibility into an outcome, a project into a completed event, a suggested emotion into an experienced emotion, or a past state into a current state. Invent no reading to fill `open_readings`; the list may be empty.

Abstain for: a stable fact or preference with no episode of its own; logistics or agenda; web research; ordinary small talk; an assistant error or weakness; an operating rule; an unresolved hypothesis; a moment interesting only to the system analyzing it.

For each scene: concrete title; two to five established assertions; zero to three open readings; precise `future_value`; three to seven anchors; explicit uncertainties. `subjects` and `facets` are merely short discovery labels, never additional assertions. Scenes must not artificially overlap or merge independent episodes. If the window contains more than three scenes worth preserving, choose the first three chronologically: a later pass will handle the rest.

If the last turns seem to open a still-unfinished episode that you choose not to write now, put its first exact timestamp in `revisit_from`; otherwise use `null`. Never request revisiting mere small talk or a hypothetical project.

Respond only in strict JSON:
{{"reason":"...","scenes":[{{"title":"...","subjects":["..."],"facets":["..."],"established":[{{"claim":"...","source_refs":["exact USER timestamp"]}}],"open_readings":[{{"reading":"...","status":"open|partly_confirmed","source_refs":["exact ASSISTANT timestamp"]}}],"future_value":"...","anchors":["..."],"uncertainties":["..."]}}],"revisit_from":null}}"""
    system += "\n\n" + MEMORY_DATE_RULES
    user = (
        'Current time:\n'
        + current_time_context(now)
        + '\n\nHere is the bounded L0 window. Judge only what it contains.\n\n'
        + excerpt
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_episodic_output(
    payload: object,
    turns: Sequence[L0Turn],
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[tuple[EpisodicScene, ...], datetime | None]:
    if not isinstance(payload, dict):
        raise ValueError("REM episodic output must be an object")
    raw_scenes = payload.get("scenes")
    if not isinstance(raw_scenes, list) or len(raw_scenes) > MAX_SCENES_PER_PASS:
        raise ValueError("REM episodic scenes must be a bounded list")
    by_ref = {(turn.timestamp, turn.role): turn for turn in turns}
    scenes = tuple(_parse_scene(value, by_ref) for value in raw_scenes)
    if len({scene.episode_id for scene in scenes}) != len(scenes):
        raise ValueError("REM episodic output contains duplicate source spans")

    raw_revisit = payload.get("revisit_from")
    revisit = None
    if raw_revisit is not None:
        if not isinstance(raw_revisit, str):
            raise ValueError("REM revisit_from must be an exact timestamp or null")
        matching = [turn for turn in turns if turn.timestamp == raw_revisit]
        if not matching:
            raise ValueError("REM revisit_from does not match an exact L0 turn")
        revisit = matching[0].instant
        earliest = window_end - timedelta(hours=MAX_REVISIT_HOURS)
        if _is_before(revisit, earliest) or _is_before(revisit, window_start):
            raise ValueError("REM revisit_from exceeds its bounded overlap")
    return scenes, revisit


def render_episode(scene: EpisodicScene) -> str:
    frontmatter = {
        "memory_type": "episodic_scene",
        "truth_scope": "past_only",
        "episode_id": scene.episode_id,
        "span_start": scene.span_start,
        "span_end": scene.span_end,
        "subjects": list(scene.subjects),
        "facets": list(scene.facets),
        "anchors": list(scene.anchors),
        "source_refs": [f"[[{ref}]]" for ref in scene.source_refs],
    }
    yaml_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=10_000,
    ).rstrip()
    established = "\n".join(
        f"- {item.text} — "
        + ", ".join(f"[[{ref}]]" for ref in item.source_refs)
        for item in scene.established
    )
    readings = "\n".join(
        f"- {item.text} _({item.status})_ — "
        + ", ".join(f"[[{ref}]]" for ref in item.source_refs)
        for item in scene.open_readings
    ) or "- None."
    uncertainties = "\n".join(
        f"- {value}" for value in scene.uncertainties
    ) or "- None made explicit by this scene."
    body = f"""# {scene.title}

## Établi

{established}

## Lectures ouvertes

{readings}

## Valeur pour un rappel futur

{scene.future_value}

## Incertitudes

{uncertainties}"""
    rendered = normalize_memory_dates(f"---\n{yaml_text}\n---\n\n{body}")
    if len(rendered) > MAX_SCENE_BODY_CHARS:
        raise ValueError("REM episodic scene exceeds its explicit size bound")
    return rendered


def resolve_selected_episode(
    selected_paths: Sequence[str],
    *,
    memory_root: Path = MEMORY_ROOT,
) -> tuple[str, str]:
    """Return one exact past scene after semantic views agree on its lead."""

    if not selected_paths:
        return "", ""
    name = str(selected_paths[0]).strip()
    if not name.startswith(EPISODIC_DOMAIN + "/"):
        return "", ""
    try:
        path = _episode_path(name, memory_root)
    except (OSError, ValueError):
        return "", ""
    if not path.is_file():
        return "", ""
    text = path.read_text(encoding="utf-8").strip()
    frontmatter, body = _split_frontmatter(text)
    if (
        frontmatter.get("memory_type") != "episodic_scene"
        or frontmatter.get("truth_scope") != "past_only"
        or not body.strip()
    ):
        return "", ""
    return name, body.strip()


def _parse_scene(
    value: object,
    by_ref: dict[tuple[str, str], L0Turn],
) -> EpisodicScene:
    if not isinstance(value, dict):
        raise ValueError("REM episodic scene must be an object")
    title = _clean_text(value.get("title"), "scene title", 240)
    established_raw = value.get("established")
    readings_raw = value.get("open_readings")
    if not isinstance(established_raw, list) or not 2 <= len(established_raw) <= 5:
        raise ValueError("REM established claims must contain two to five items")
    if not isinstance(readings_raw, list) or len(readings_raw) > 3:
        raise ValueError("REM open readings must contain zero to three items")

    established: list[SceneClaim] = []
    readings: list[SceneReading] = []
    used_turns: list[L0Turn] = []
    for raw in established_raw:
        if not isinstance(raw, dict):
            raise ValueError("REM established claim must be an object")
        refs, source_turns = _validated_refs(raw.get("source_refs"), "user", by_ref)
        established.append(
            SceneClaim(_clean_text(raw.get("claim"), "established claim", 1200), refs)
        )
        used_turns.extend(source_turns)
    for raw in readings_raw:
        if not isinstance(raw, dict):
            raise ValueError("REM open reading must be an object")
        status = str(raw.get("status", "")).strip()
        if status not in {"open", "partly_confirmed"}:
            raise ValueError("REM open reading status is invalid")
        refs, source_turns = _validated_refs(raw.get("source_refs"), "assistant", by_ref)
        readings.append(
            SceneReading(
                _clean_text(raw.get("reading"), "open reading", 1200),
                status,
                refs,
            )
        )
        used_turns.extend(source_turns)
    if not used_turns:
        raise ValueError("REM episodic scene has no exact source")

    ordered = sorted(used_turns, key=lambda turn: turn.instant.timestamp())
    all_refs = tuple(dict.fromkeys(turn.source_ref for turn in ordered))
    established_refs = tuple(
        dict.fromkeys(
            ref
            for claim in established
            for ref in claim.source_refs
        )
    )
    identity_material = "\n".join(established_refs).encode("utf-8")
    episode_id = hashlib.sha256(identity_material).hexdigest()[:20]
    date_prefix = ordered[0].timestamp[:10]
    name = f"{EPISODIC_DOMAIN}/{date_prefix[:4]}/{date_prefix[5:7]}/{date_prefix}--{episode_id[:12]}"
    metadata = SouvenirMetadata.from_raw(
        {"subjects": value.get("subjects"), "facets": value.get("facets")}
    )
    anchors = _clean_text_list(value.get("anchors"), "anchors", 3, 7, 160)
    uncertainties = _clean_text_list(
        value.get("uncertainties"), "uncertainties", 0, 8, 500
    )
    return EpisodicScene(
        episode_id=episode_id,
        name=name,
        title=title,
        span_start=ordered[0].timestamp,
        span_end=ordered[-1].timestamp,
        subjects=tuple(metadata.subjects),
        facets=tuple(metadata.facets),
        anchors=anchors,
        established=tuple(established),
        open_readings=tuple(readings),
        future_value=_clean_text(value.get("future_value"), "future value", 1200),
        uncertainties=uncertainties,
        source_refs=all_refs,
    )


def _validated_refs(
    value: object,
    role: str,
    by_ref: dict[tuple[str, str], L0Turn],
) -> tuple[tuple[str, ...], list[L0Turn]]:
    if not isinstance(value, list) or not value:
        raise ValueError("REM source_refs must be a non-empty list")
    timestamps = tuple(dict.fromkeys(str(item).strip() for item in value))
    if not all(timestamps):
        raise ValueError("REM source_refs contain an empty timestamp")
    turns = []
    for timestamp in timestamps:
        turn = by_ref.get((timestamp, role))
        if turn is None:
            raise ValueError(f"REM source ref is not an exact {role} turn: {timestamp}")
        turns.append(turn)
    return tuple(turn.source_ref for turn in turns), turns


def _load_l0_turns(
    memory_root: Path,
    user_id: str,
    start: datetime,
    end: datetime,
) -> list[L0Turn]:
    root = memory_root / "levels" / "L0_ground_zero" / "conversations"
    safe_user = safe_memory_user_id(user_id)
    records: list[L0Turn] = []
    for path in sorted(root.glob(f"{safe_user}_*.md")):
        text = path.read_text(encoding="utf-8")
        for match in TURN_RE.finditer(text):
            raw_timestamp = match.group("timestamp").strip()
            try:
                instant = datetime.fromisoformat(raw_timestamp)
            except ValueError:
                continue
            if _in_window(instant, start, end):
                records.append(
                    L0Turn(
                        timestamp=raw_timestamp,
                        instant=instant,
                        role=match.group("role"),
                        content=match.group("content").strip(),
                        source_path=path,
                    )
                )
    records.sort(key=lambda turn: turn.instant.timestamp())
    return records


def _bounded_turns(turns: Sequence[L0Turn], max_chars: int) -> list[L0Turn]:
    selected: list[L0Turn] = []
    size = 0
    for turn in turns:
        addition = len(turn.rendered) + (2 if selected else 0)
        if selected and size + addition > max_chars:
            break
        if not selected and addition > max_chars:
            raise ValueError("One L0 turn exceeds the REM window bound")
        selected.append(turn)
        size += addition
    return selected


def _repair_episode_index(
    scenes: Iterable[EpisodicScene],
    memory_root: Path,
) -> None:
    index = memory_root / "capsules" / "index.md"
    scene_list = list(scenes)
    with memory_file_lock(index):
        lines = index.read_text(encoding="utf-8").splitlines() if index.is_file() else []
        known = {line[2:].partition(":")[0] for line in lines if line.startswith("- ")}
        for scene in scene_list:
            if scene.name not in known:
                lines.append(
                    f"- {scene.name}: {scene.title} — {', '.join(scene.anchors)}"
                )
                known.add(scene.name)
        episode_entries = sorted(
            line.strip()
            for line in lines
            if line.strip().startswith(f"- {EPISODIC_DOMAIN}/")
        )
        lines = [
            line
            for line in lines
            if not line.strip().startswith(f"- {EPISODIC_DOMAIN}:")
        ]
        if episode_entries:
            recent = []
            for entry in episode_entries[-3:]:
                name, _, description = entry[2:].partition(": ")
                recent.append(f"{name.rsplit('/', 1)[-1][:10]} ({description[:110].rstrip()})")
            lines.append(
                f"- {EPISODIC_DOMAIN}: Sourced past episodic scenes, "
                "never a current state. Recent: " + " ; ".join(recent)
            )
        atomic_write_text(index, "\n".join(lines).strip() + "\n")


def _refresh_semantic_cache(
    user_id: str,
    memory_root: Path,
    names: Sequence[str],
) -> None:
    try:
        from daemon_core.semantic_atlas import coalesced_semantic_atlas_prewarmer

        prewarmer = coalesced_semantic_atlas_prewarmer(user_id, memory_root)
        if prewarmer is not None:
            prewarmer.request(names)
            prewarmer.flush_if_pending(force=True)
    except Exception as exc:
        logger.warning(
            "Préchauffage Atlas après REM épisodique différé | error=%s",
            exc.__class__.__name__,
        )


def _validate_existing_episode(path: Path, scene: EpisodicScene) -> None:
    frontmatter, _body = _split_frontmatter(path.read_text(encoding="utf-8"))
    if (
        frontmatter.get("memory_type") != "episodic_scene"
        or frontmatter.get("episode_id") != scene.episode_id
    ):
        raise ValueError(f"Existing episodic scene conflicts with {scene.name}")


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    raw, separator, body = text[4:].partition("\n---\n")
    if not separator:
        return {}, ""
    payload = yaml.safe_load(raw) if raw.strip() else {}
    return (payload if isinstance(payload, dict) else {}), body


def _episode_path(name: str, memory_root: Path) -> Path:
    if not name.startswith(EPISODIC_DOMAIN + "/"):
        raise ValueError("Invalid episodic scene path")
    relative = Path(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Invalid episodic scene path")
    from daemon_core.memory.library_layout import resolve_note
    return resolve_note(memory_root, name, create=True,
                        storage_path="Archives/Episodes/" + relative.name + ".md")


def _result(
    status: str,
    scenes: Sequence[EpisodicScene],
    state: EpisodicState,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    return {
        "status": status,
        "episodes": [scene.name for scene in scenes],
        "scene_count": len(scenes),
        "window_start": window_start.isoformat(timespec="seconds"),
        "window_end": window_end.isoformat(timespec="seconds"),
        "assessed_through": state.assessed_through.isoformat(timespec="seconds"),
        "revisit_from": (
            state.revisit_from.isoformat(timespec="seconds")
            if state.revisit_from is not None
            else None
        ),
    }


def _latest_scene_end(scenes: Sequence[EpisodicScene]) -> datetime:
    latest = datetime.fromisoformat(scenes[0].span_end)
    for scene in scenes[1:]:
        candidate = datetime.fromisoformat(scene.span_end)
        if _is_before(latest, candidate):
            latest = candidate
    return latest


def _clean_text(value: object, label: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"REM {label} must be a string")
    text = " ".join(value.split()).strip()
    if not text or len(text) > max_chars:
        raise ValueError(f"REM {label} is empty or oversized")
    return text


def _clean_text_list(
    value: object,
    label: str,
    minimum: int,
    maximum: int,
    max_chars: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"REM {label} has an invalid item count")
    cleaned = tuple(_clean_text(item, label, max_chars) for item in value)
    if len(set(item.casefold() for item in cleaned)) != len(cleaned):
        raise ValueError(f"REM {label} contains duplicates")
    return cleaned


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        stripped = stripped.removesuffix("```").strip()
    return stripped


def _elapsed_seconds(start: datetime, end: datetime) -> float:
    left, right = _comparable(start, end)
    return (right - left).total_seconds()


def _is_before(left: datetime, right: datetime) -> bool:
    comparable_left, comparable_right = _comparable(left, right)
    return comparable_left < comparable_right


def _in_window(value: datetime, start: datetime, end: datetime) -> bool:
    comparable_value, comparable_start = _comparable(value, start)
    comparable_value, comparable_end = _comparable(comparable_value, end)
    return comparable_start < comparable_value <= comparable_end


def _comparable(left: datetime, right: datetime) -> tuple[datetime, datetime]:
    if left.tzinfo is not None and right.tzinfo is None:
        left = left.astimezone().replace(tzinfo=None)
    elif left.tzinfo is None and right.tzinfo is not None:
        left = left.replace(tzinfo=right.tzinfo)
    elif left.tzinfo is not None and right.tzinfo is not None:
        left = left.astimezone(right.tzinfo)
    return left, right


__all__ = [
    "EPISODIC_DOMAIN",
    "EpisodicScene",
    "episodic_rem_enabled",
    "episodic_scene_voice_enabled",
    "episode_state_path",
    "read_episode_state",
    "render_episode",
    "resolve_selected_episode",
    "run_episodic_rem",
    "write_episode_state",
]
