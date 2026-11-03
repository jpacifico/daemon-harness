import argparse
import difflib
import hashlib
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
import yaml

from daemon_core.memory_dates import MEMORY_DATE_RULES, normalize_memory_dates
from daemon_core.continuity import (
    CONTINUITY_EVENT_SCHEMA,
    continuity_receipt_path,
    technical_ref,
)
from daemon_core.memory.souvenir_index import (
    souvenir_index_entry,
    existing_souvenir_index_line,
    merge_souvenir_index,
    validated_souvenir_index_line,
)
from daemon_core.memory.reader import is_system_managed_souvenir
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.library_layout import (
    LibraryLayoutError, resolve_note, vault_root, with_identity, unique_note_locations,
    memory_reference, checked_note_path, name_for_path,
)
from daemon_core.memory.note_relocation import (
    NoteSnapshot, Relocation, publish_relocation, recover_relocation,
)
from daemon_core.memory.obsidian import obsidian_format_enabled, prepare_souvenir_format
from daemon_core.memory.metadata import preserve_existing_frontmatter
from daemon_core.memory.context_pages import (
    context_page_prompt, refresh_navigation_safely, structure_enabled,
    validate_context_page,
)
from daemon_core.memory.writer import (
    GroundZeroReference,
    read_ground_zero_record,
    safe_memory_user_id,
)
from daemon_core.paths import MEMORY_ROOT, PROJECT_ROOT
from daemon_core.providers.base import LLMProvider
from daemon_core.providers.factory import build_memory_provider

logger = logging.getLogger("daemon_core.dreaming")

if TYPE_CHECKING:
    from daemon_core.memory.relation_gardening import RelationGardeningResult

MAX_DREAMING_UPDATES = 5
MAX_SOUVENIRS_IN_PROMPT = 80
MAX_SOUVENIR_CHARS_IN_PROMPT = 2200
MAX_MUTATION_HISTORY_PER_PATH = 4
MAX_SHRINK_RATIO = 0.60
MIN_SHRINK_GUARD_CHARS = 400
MAX_PROVIDER_JSON_ATTEMPTS = 2

_JSON_RETRY_MESSAGE = """The previous output was not a complete, readable JSON object.
Regenerate the entire response from the same sources, without continuing or
repairing the previous text. Return exactly one strict JSON object matching
the requested schema, with no Markdown fence or text before or after it."""


@dataclass
class DreamingUpdate:
    path: str
    action: str
    reason: str
    content_md: str
    index_line: str = ""
    base_sha256: str = ""
    base_location: str = ""
    storage_path: str = ""
    applied: bool = False
    changed: bool = False
    diff: str = ""


@dataclass
class RejectedDreamingUpdate:
    path: str
    reason: str


@dataclass
class DreamingResult:
    dry_run: bool
    reason: str = ""
    confidence: float = 0.0
    report_md: str = ""
    updates: list[DreamingUpdate] = field(default_factory=list)
    rejected_updates: list[RejectedDreamingUpdate] = field(default_factory=list)
    unresolved_conflicts: list[str] = field(default_factory=list)
    review_notes: list[str] = field(default_factory=list)
    relation_gardening_results: list["RelationGardeningResult"] = field(
        default_factory=list
    )
    input_summary: dict[str, int] = field(default_factory=dict)
    report_json_path: str = ""
    report_markdown_path: str = ""
    latest_json_path: str = ""
    latest_markdown_path: str = ""


@dataclass(frozen=True)
class MemoryMutationEvidence:
    trace_id: str
    at: str
    source_turn_started_at: float
    changed_paths: tuple[str, ...]
    context_paths: tuple[str, ...]
    user_l0_ref: GroundZeroReference
    assistant_l0_ref: GroundZeroReference


def run_dreaming(
    user_id: str,
    provider: LLMProvider,
    since_hours: int = 24,
    dry_run: bool = True,
    now: datetime | None = None,
) -> DreamingResult:
    if not dry_run:
        recover_relocation(MEMORY_ROOT)
    memory_locations: dict[str, Path] = {}
    memory_revisions = _capture_dreaming_revisions(user_id, locations=memory_locations)
    l0_recent = load_l0_window(user_id=user_id, since_hours=since_hours, now=now)
    receipt_paths = _memory_mutation_receipt_paths()
    mutation_evidence = load_memory_mutation_evidence(
        user_id=user_id,
        since_hours=since_hours,
        now=now,
    )
    targeted_paths = {
        path
        for evidence in mutation_evidence
        for path in (*evidence.changed_paths, *evidence.context_paths)
    }
    memory_snapshot = load_memory_snapshot(
        user_id,
        souvenir_paths=targeted_paths if mutation_evidence else None,
    )
    messages = build_dreaming_messages(
        user_id=user_id,
        since_hours=since_hours,
        l0_recent=l0_recent,
        memory_snapshot=memory_snapshot,
        write_provenance=render_memory_mutation_evidence(mutation_evidence),
    )
    input_summary = {
        "l0_chars": len(l0_recent),
        "continuity_journals": sum(path.is_file() for path in receipt_paths),
        "mutation_evidence_events": len(mutation_evidence),
        "targeted_memory_paths": len(targeted_paths),
        "memory_snapshot_chars": len(memory_snapshot),
        "prompt_chars": sum(
            len(str(message.get("content", ""))) for message in messages
        ),
    }
    data, raw, parse_error, attempts = _complete_dreaming_json(
        provider=provider,
        messages=messages,
    )
    if data is None:
        diagnostic = _json_failure_diagnostic(
            raw=raw,
            error=parse_error,
            attempts=attempts,
        )
        return DreamingResult(
            dry_run=dry_run,
            reason="Dreaming indisponible: le provider n'a pas retourné de JSON strict.",
            report_md=(
                "Le DreamingAgent attend un JSON strict et a épuisé son "
                "second essai borné. Vérifie le provider mémoire et le "
                "diagnostic ci-dessous.\n\n"
                f"{diagnostic}"
            ),
            rejected_updates=[RejectedDreamingUpdate(path="", reason="Réponse provider non JSON.")],
            input_summary=input_summary,
        )
    relation_gardening_results = _run_relation_gardening_if_enabled(
        user_id=user_id,
        provider=provider,
        publish=not dry_run,
    )
    result = apply_dreaming_result(
        user_id=user_id,
        data=data,
        dry_run=dry_run,
        expected_revisions=memory_revisions,
        expected_locations=memory_locations,
    )
    result.input_summary = input_summary
    result.relation_gardening_results.extend(relation_gardening_results)
    if relation_gardening_results:
        result.report_md += _relation_gardening_report(
            relation_gardening_results
        )
    if attempts > 1:
        result.review_notes.append(
            "Sortie JSON du provider récupérée au second essai ; aucune sortie "
            "partielle du premier essai n'a été appliquée."
        )
    if not dry_run:
        # Weekly facet-vocabulary maintenance runs within the nightly cycle,
        # like REM. It is autonomous; a failure is recorded
        # without failing the rest of Dreaming.
        try:
            from daemon_core.memory.metadata_maintenance import maybe_run_facet_maintenance

            maintenance = maybe_run_facet_maintenance(provider=provider)
            if maintenance and maintenance.get("applied"):
                result.report_md += (
                    f"\n\nEntretien des facettes : {len(maintenance['applied'])} souvenir(s) "
                    f"ré-étiquetée(s), sauvegarde {maintenance['backup_root']}."
                )
        except Exception as exc:
            logger.warning("Entretien des facettes échoué | error=%s", exc.__class__.__name__)
    return result


def _run_relation_gardening_if_enabled(
    *,
    user_id: str,
    provider: LLMProvider,
    publish: bool,
):
    from daemon_core.memory.relation_gardening import (
        relation_gardening_enabled,
        run_pending_relation_gardening_dry_run,
    )
    from daemon_core.memory.relation_gardening_publication import (
        relation_gardening_personal_publication_enabled,
        run_pending_personal_relation_gardening,
    )

    if not relation_gardening_enabled(user_id):
        return ()
    if publish and relation_gardening_personal_publication_enabled(user_id):
        return run_pending_personal_relation_gardening(
            user_id=user_id,
            provider=provider,
        )
    return run_pending_relation_gardening_dry_run(
        user_id=user_id,
        provider=provider,
    )


def _relation_gardening_report(results) -> str:
    planned = sum(result.planned_count for result in results)
    blocked = sum(result.blocked_count for result in results)
    provider_calls = sum(result.provider_calls for result in results)
    published = sum(result.published_count for result in results)
    active = any(result.publication_status for result in results)
    statuses = ", ".join(result.status for result in results)
    return (
        f"\n\nJardinage relationnel ({'active' if active else 'dry-run'}) : "
        f"{len(results)} événement(s), {planned} relation(s) planifiée(s), "
        f"{published} publiée(s), {blocked} bloquée(s), "
        f"{provider_calls} appel(s) provider. "
        f"Statuts : {statuses}."
    )


def _complete_dreaming_json(
    provider: LLMProvider,
    messages: list[dict],
) -> tuple[dict | None, str, json.JSONDecodeError | ValueError | None, int]:
    """Retry only a malformed provider envelope, never a rejected update."""

    raw = ""
    error: json.JSONDecodeError | ValueError | None = None
    for attempt in range(1, MAX_PROVIDER_JSON_ATTEMPTS + 1):
        attempt_messages = messages
        if attempt > 1:
            attempt_messages = [
                *messages,
                {"role": "user", "content": _JSON_RETRY_MESSAGE},
            ]
        raw = provider.complete(messages=attempt_messages, temperature=0.0)
        try:
            data = json.loads(_strip_json_fence(raw))
            if not isinstance(data, dict):
                raise ValueError("la racine JSON n'est pas un objet")
            return data, raw, None, attempt
        except (json.JSONDecodeError, ValueError) as exc:
            error = exc
            logger.warning(
                "Sortie Dreaming non JSON | attempt=%s/%s | raw_chars=%s | error=%s",
                attempt,
                MAX_PROVIDER_JSON_ATTEMPTS,
                len(raw),
                _json_error_summary(exc),
            )
    return None, raw, error, MAX_PROVIDER_JSON_ATTEMPTS


def _json_failure_diagnostic(
    *,
    raw: str,
    error: json.JSONDecodeError | ValueError | None,
    attempts: int,
) -> str:
    stripped = raw.strip()
    if not stripped:
        preview = "[vide]"
    elif len(stripped) <= 800:
        preview = stripped
    else:
        preview = (
            stripped[:380]
            + "\n...[aperçu intermédiaire retiré]...\n"
            + stripped[-380:]
        )
    return (
        f"Tentatives : {attempts}\n"
        f"Taille de la dernière sortie : {len(raw)} caractères\n"
        f"Erreur : {_json_error_summary(error)}\n\n"
        f"Aperçu de la dernière sortie :\n{preview}"
    )


def _json_error_summary(
    error: json.JSONDecodeError | ValueError | None,
) -> str:
    if isinstance(error, json.JSONDecodeError):
        return (
            f"{error.msg} (ligne {error.lineno}, colonne {error.colno}, "
            f"position {error.pos})"
        )
    return str(error or "erreur de décodage inconnue")


def apply_dreaming_report_file(user_id: str, report_path: Path | str) -> DreamingResult:
    path = Path(report_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    result = apply_dreaming_result(user_id=user_id, data=data, dry_run=False)
    raw_summary = data.get("input_summary")
    if isinstance(raw_summary, dict):
        result.input_summary = {
            str(key): value
            for key, value in raw_summary.items()
            if isinstance(value, int) and value >= 0
        }
    return result


def latest_dreaming_report(user_id: str, mode: str = "dry-run", report_root: Path | None = None) -> Path | None:
    safe_user = safe_memory_user_id(user_id)
    root = report_root or MEMORY_ROOT / "levels" / "dreaming" / "reports"
    reports = sorted(root.glob(f"{safe_user}_*_{mode}_*.json"))
    return reports[-1] if reports else None


def load_l0_window(
    user_id: str,
    since_hours: int = 24,
    now: datetime | None = None,
    max_chars: int | None = None,
) -> str:
    now = now or datetime.now()
    threshold = now - timedelta(hours=since_hours)
    root = MEMORY_ROOT / "levels" / "L0_ground_zero" / "conversations"
    safe_user = safe_memory_user_id(user_id)
    entries = []
    for path in sorted(root.glob(f"{safe_user}_*.md")):
        entries.extend(_read_l0_entries(path, threshold=threshold, now=now))
    text = "\n\n".join(entries).strip()
    # Dreaming reads its entire time window. Silent truncation would make
    # a partial read appear complete. Consumers that explicitly accept
    # another bound, such as REM, can
    # always supply their own cap.
    if max_chars is not None and max_chars > 0 and len(text) > max_chars:
        return text[-max_chars:]
    return text


def load_memory_mutation_evidence(
    user_id: str,
    since_hours: int = 24,
    now: datetime | None = None,
    receipt_dir: Path | None = None,
) -> list[MemoryMutationEvidence]:
    """Return recent mutations plus bounded prior evidence for the same files."""

    paths = _memory_mutation_receipt_paths(receipt_dir=receipt_dir)
    strict_receipts = receipt_dir is None and bool(
        os.getenv("DREAMING_CONTINUITY_RECEIPT_DIRS", "").strip()
    )
    expected_user = technical_ref(user_id)
    sourced_events: list[tuple[dict, dict[str, str]]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            if strict_receipts:
                raise RuntimeError(
                    "Configured Dreaming continuity journal is unavailable."
                ) from exc
            continue
        journal_events: list[dict] = []
        identity_by_turn: dict[str, str] = {}
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, ValueError) as exc:
                if strict_receipts:
                    raise RuntimeError(
                        "Configured Dreaming continuity journal contains invalid JSON."
                    ) from exc
                continue
            if (
                not isinstance(event, dict)
                or event.get("schema") != CONTINUITY_EVENT_SCHEMA
            ):
                continue
            turn_id = str(event.get("turn_id", ""))
            identity = event.get("identity")
            if isinstance(identity, dict) and isinstance(
                identity.get("user_ref"), str
            ):
                identity_by_turn[turn_id] = identity["user_ref"]
            journal_events.append(event)
        sourced_events.extend(
            (event, identity_by_turn) for event in journal_events
        )

    candidates_by_trace: dict[
        str,
        tuple[float, MemoryMutationEvidence],
    ] = {}
    for event, identity_by_turn in sourced_events:
        if event.get("stage") != "memory_mutation" or event.get("status") != "completed":
            continue
        details = event.get("details")
        if not isinstance(details, dict):
            continue
        turn_id = str(event.get("turn_id", ""))
        user_ref = str(details.get("user_ref", "")) or identity_by_turn.get(turn_id, "")
        if user_ref != expected_user:
            continue
        changed_paths = tuple(
            path
            for path in (
                _normalize_memory_evidence_path(value)
                for value in details.get("changed_paths", [])
            )
            if path
        )
        if not changed_paths:
            continue
        context_paths = tuple(
            path
            for path in (
                _normalize_memory_evidence_path(value)
                for value in details.get("context_paths", [])
            )
            if path
        )
        try:
            user_l0_ref = GroundZeroReference.from_dict(details.get("user_l0_ref"))
            assistant_l0_ref = GroundZeroReference.from_dict(details.get("assistant_l0_ref"))
        except (TypeError, ValueError):
            continue
        source_started = _safe_float(details.get("source_turn_started_at"))
        event_timestamp = source_started or _event_timestamp(str(event.get("at", "")))
        if event_timestamp <= 0:
            continue
        evidence = MemoryMutationEvidence(
            trace_id=turn_id,
            at=str(event.get("at", "")),
            source_turn_started_at=source_started,
            changed_paths=changed_paths,
            context_paths=context_paths,
            user_l0_ref=user_l0_ref,
            assistant_l0_ref=assistant_l0_ref,
        )
        candidate = (
            event_timestamp,
            evidence,
        )
        previous = candidates_by_trace.get(turn_id)
        if (
            previous is not None
            and _memory_mutation_fingerprint(previous[1])
            != _memory_mutation_fingerprint(evidence)
        ):
            raise RuntimeError(
                "Conflicting Dreaming continuity events share one turn id."
            )
        if previous is None or candidate[0] >= previous[0]:
            candidates_by_trace[turn_id] = candidate
    candidates = list(candidates_by_trace.values())

    current = now or datetime.now().astimezone()
    current_timestamp = current.timestamp()
    threshold = current_timestamp - max(1, int(since_hours)) * 3600
    recent = [
        item
        for item in candidates
        if threshold <= item[0] <= current_timestamp + 300
    ]
    if not recent:
        return []
    targeted_paths = {
        path
        for _timestamp, evidence in recent
        for path in (*evidence.changed_paths, *evidence.context_paths)
    }
    selected: dict[str, tuple[float, MemoryMutationEvidence]] = {}
    for target in sorted(targeted_paths):
        history = sorted(
            (
                item
                for item in candidates
                if target in item[1].changed_paths
                and item[0] <= current_timestamp + 300
            ),
            key=lambda item: item[0],
        )[-MAX_MUTATION_HISTORY_PER_PATH:]
        for timestamp, evidence in history:
            selected[evidence.trace_id] = (timestamp, evidence)
    ordered = sorted(selected.values(), key=lambda item: item[0])
    return [
        evidence
        for _timestamp, evidence in ordered
    ]


def render_memory_mutation_evidence(
    evidence: list[MemoryMutationEvidence],
) -> str:
    if not evidence:
        return ""
    sections = []
    for item in evidence:
        paths = ", ".join(f"memory/{path}" for path in item.changed_paths)
        context_paths = ", ".join(
            f"memory/{path}" for path in item.context_paths
        )
        try:
            user_record = read_ground_zero_record(item.user_l0_ref)
            assistant_record = read_ground_zero_record(item.assistant_l0_ref)
        except Exception as exc:
            sections.append(
                f"## Turn {item.trace_id}\nModified files: {paths}\nConsulted memories: {context_paths or 'none'}\nL0 evidence inaccessible: {exc.__class__.__name__}. Infer nothing from this turn."
            )
            continue
        sections.append(
            f"## Turn {item.trace_id}\nModified files: {paths}\nConsulted memories: {context_paths or 'none'}\nExact user L0 message ({user_record.timestamp}) :\n{user_record.body[:4000]}\n\nAssistant response in the same turn (processing context, never factual evidence):\n{assistant_record.body[:4000]}"
        )
    return "\n\n".join(sections)


def load_memory_snapshot(
    user_id: str,
    souvenir_paths: set[str] | None = None,
) -> str:
    safe_user = safe_memory_user_id(user_id)
    users_root = MEMORY_ROOT / "users" / safe_user
    sections = [
        _snapshot_file("context.md", users_root / "context.md"),
        _snapshot_file("agenda.md", users_root / "agenda.md"),
        _snapshot_file("contexte_actif.md", users_root / "contexte_actif.md"),
        _snapshot_file("focus_immediat.md", users_root / "focus_immediat.md"),
        _snapshot_file("capsules/index.md", MEMORY_ROOT / "capsules" / "index.md"),
        _snapshot_file('Souvenir recalls (levels/capsule_recalls.md)', MEMORY_ROOT / "levels" / "capsule_recalls.md"),
        _snapshot_theme_souvenirs(souvenir_paths),
    ]
    return "\n\n".join(section for section in sections if section).strip()


def build_dreaming_messages(
    user_id: str,
    since_hours: int,
    l0_recent: str,
    memory_snapshot: str,
    write_provenance: str = "",
) -> list[dict]:
    system = """You are the Daemon Harness DreamingAgent.

You work outside the response path, in deferred maintenance. You never respond to the user.
Your role is to reread recent L0 and cautiously check what MemoryAgent recorded during the conversation.

Posture:
- Act as a gardener, not a bulldozer.
- Souvenirs under `memory/capsules/theme/scenes_episodiques/**` belong exclusively to episodic REM: never propose creating, modifying, archiving or linking them.
- Preserve existing information by default.
- Correct only obvious omissions, explicit contradictions or misclassifications.
- Merge, move or split only when the gain is clear.
- Do not reorganize memory on a large scale.
- Do not create a CRM, an exhaustive taxonomy or document RAG.
- When in doubt, prefer a `to be confirmed` point or a report without changes.
- Never remove a fact except upon a request to forget it or an explicit contradiction in L0.
- Never compact at the expense of information.
- Source hierarchy: an exact user L0 message is authoritative about what the user established; an assistant response helps explain how it was handled but proves no user fact; memory files and the scene are revisable derivatives.
- When write provenance is supplied, first check that the scope of the recorded fact remains within the scope of its source user message. A local clarification does not silently replace a broader rule.
- If accessible evidence is insufficient to settle a contradiction, do not resolve it by intuition: retain the information, record the conflict in `unresolved_conflicts` and explain what is missing.
- The L0 window, provenance and targeted memory supplied to this pass are bounded excerpts, not the complete history. The absence of original evidence in these excerpts is never evidence that a stored fact is false, misattributed or too precise.
- Do not remove, weaken, anonymize or make any existing fact uncertain merely because its exact source does not appear in this pass. Such a revision requires an exact, more recent user correction or contradiction in accessible evidence; otherwise retain the fact without rewriting it.
- Read L0 chronologically through to the latest expressed state. A more recent explicit correction, renunciation, impossibility or closure replaces the earlier intention: a project later declared infeasible, abandoned or completed does not remain "under consideration" or open.
- A dated, one-off desire or plan belongs in the agenda or focus while current; it does not on its own become a durable fact to copy into a souvenir. If the latest message invalidates it, correct the relevant temporal projection instead of duplicating the old state in a souvenir.
- The operational agenda belongs exclusively to Temporal Curator. Never propose creating or modifying `memory/users/<user_id>/agenda.md`. You may flag a missing reclassification in `report_md`, but do not perform it yourself.
- Before each update, identify the most recent exact user evidence justifying it. To weaken an existing fact, require its exact contradiction; to update a current state, retain the latest expressed position. If this check fails, abandon the update.
- Expiry: a souvenir may carry a `valid_until: YYYY-MM-DD` field (expiry date). If that date has passed relative to the supplied current date, propose `action: "archive"` for that souvenir: it will be moved out of active memory (preserved, not deleted). Never archive a souvenir whose `valid_until` is absent or still valid, except for the merged-duplicate case below.
- Recurrence scope: a regular activity or appointment may be temporary, seasonal or suspended without the souvenir saying so. When the present context makes its continuation uncertain (for example, holidays versus an activity observed during the school term), qualify the souvenir — "continuation during X unconfirmed" — instead of leaving it asserting certainty. Do not invent the exception: capture the uncertainty, and remove the qualification when L0 settles it (observed continuation or stated cessation).
- Duplicate consolidation: when two souvenirs cover the same scope and say the same thing, propose their merger in the same response: `update` the retained souvenir with all information from both, `archive` the duplicate souvenir, and `update` memory/capsules/index.md if the retained souvenir's line needs to recover anchors from the duplicate (the duplicate's index entry is removed automatically on archiving). Merge only what is clearly redundant; at the slightest nuance unique to either, retain both.
- To choose which souvenir to retain in a merger, use the "Souvenir recalls" section (last scene-recall date and count) and the path most consistent with the existing tree. Do not yet use these recalls to archive merely old souvenirs: dormancy will be a separate judgment.

Technical constraints:
- Propose at most 5 updates.
- Allowed paths:
  - memory/users/<user_id>/context.md
  - memory/users/<user_id>/contexte_actif.md
  - memory/users/<user_id>/focus_immediat.md
  - memory/capsules/index.md
  - memory/capsules/theme/**/*.md
- Always return the complete modified file content in `content_md` (except for `archive`, which has no content).
- For each created or modified souvenir, supply `index_line`: a complete, compact line in the format `- theme/<chemin>: description — anchors`. Judge anchors over the whole souvenir and its previous index line, not only the facts added tonight. Preserve still-discriminating recall handles, condense lists of names/works/places/expressions instead of copying sentences, and aim for fewer than 300 characters.
- Do not propose a separate update to `memory/capsules/index.md` when the `index_line` fields suffice. Archiving already removes the corresponding entry automatically.
- Possible actions: `create`, `update`, `archive`. No `delete` action.
- Archiving applies only to souvenirs under `memory/capsules/theme/**`.
- If nothing should change, return `updates: []` and explain in `report_md`.

Respond in strict JSON, with no Markdown outside Markdown content.

Schema:
{
  "reason": "short reason",
  "confidence": 0.0,
  "report_md": "short report",
  "unresolved_conflicts": ["unresolved conflict and missing evidence"],
  "updates": [
    {"path": "memory/capsules/theme/domaine/sujet.md", "action": "update", "reason": "reason for the change", "content_md": "# ...", "index_line": "- theme/domaine/sujet: description — compact anchors"},
    {"path": "memory/capsules/theme/quotidien/vacances_ete.md", "action": "archive", "reason": "valid_until 2026-07-24 has passed relative to the current date"},
    {"path": "memory/capsules/theme/domaine/sujet_bis.md", "action": "archive", "reason": "duplicate merged into theme/domaine/sujet"}
  ]
}
"""
    system = context_page_prompt(system, merge=True) + "\n\n" + MEMORY_DATE_RULES
    if vault_root(MEMORY_ROOT) is not None:
        system += "\nFor a NEW Dreaming update include storage_path inside its updates item. The path field remains the stable memory/capsules/<identity>.md address. Existing notes stay at their user-chosen location.\n"
    user = f"""# Parameters

User: {safe_memory_user_id(user_id)}
Current date: {datetime.now().date().isoformat()}
L0 window: last {since_hours}h

# Recent L0

{l0_recent or "No recent L0."}

# Exact provenance of targeted memory writes

{write_provenance or "No recent technical provenance available; cautious general maintenance."}

# Current memory

{memory_snapshot or "Empty memory."}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def apply_dreaming_result(
    user_id: str,
    data: dict,
    dry_run: bool = True,
    expected_revisions: dict[str, str] | None = None,
    expected_locations: dict[str, Path] | None = None,
) -> DreamingResult:
    if not dry_run:
        recover_relocation(MEMORY_ROOT)
    result = DreamingResult(
        dry_run=dry_run,
        reason=str(data.get("reason", "") or ""),
        confidence=_safe_float(data.get("confidence")),
        report_md=str(data.get("report_md", "") or ""),
        unresolved_conflicts=_bounded_string_list(
            data.get("unresolved_conflicts"),
            limit=10,
        ),
    )
    updates = data.get("updates", [])
    if not isinstance(updates, list):
        result.rejected_updates.append(RejectedDreamingUpdate(path="", reason="updates doit être une liste."))
        return result

    for index, raw_update in enumerate(updates):
        if index >= MAX_DREAMING_UPDATES:
            path = str(raw_update.get("path", "")) if isinstance(raw_update, dict) else ""
            result.rejected_updates.append(RejectedDreamingUpdate(path=path, reason="Limite de 5 mises à jour dépassée."))
            continue
        update, rejected = _parse_update(raw_update)
        if rejected:
            result.rejected_updates.append(rejected)
            continue
        target, rejected = _validate_update_target(user_id, update)
        if rejected:
            result.rejected_updates.append(rejected)
            continue
        if not update.base_sha256:
            update.base_sha256 = (
                expected_revisions.get(update.path, "missing")
                if expected_revisions is not None
                else _file_revision(target)
            )
        if update.action == "archive":
            rejected = _validate_archive_target(target, update)
            if rejected:
                result.rejected_updates.append(rejected)
                continue
            vault = vault_root(MEMORY_ROOT)
            relative = target.relative_to(vault) if vault is not None else None
            update.changed = relative is None or relative.parts[0] != "Archives"
            destination = (
                (Path("Archives") / relative).as_posix()
                if update.changed and relative else "memory/archive/capsules/…"
            )
            update.diff = f"archive: {update.path} → {destination}" if update.changed else ""
            # Persist the observed location in dry-run reports too, so applying
            # a saved report cannot undo a human move made after that report.
            try:
                if expected_revisions is not None:
                    update.base_sha256 = expected_revisions.get(update.path, "missing")
                location = (
                    expected_locations.get(update.path) if expected_locations is not None
                    else checked_note_path(vault, update.base_location)
                    if vault is not None and update.base_location else target
                )
                if location is None:
                    raise LibraryLayoutError("Note absent from the Dreaming snapshot")
                if vault is not None:
                    update.base_location = location.relative_to(vault).as_posix()
            except ValueError as exc:
                result.rejected_updates.append(RejectedDreamingUpdate(path=update.path, reason=str(exc)))
                continue
            if not dry_run:
                try:
                    # The captured revision, not a model-supplied hash, owns the
                    # decision's source. The publisher owns the lock ordering.
                    if _validate_update_target(user_id, update)[0] != target:
                        raise LibraryLayoutError("Note absent from the Dreaming snapshot")
                    update.applied = _archive_souvenir(
                        target, expected_revision=update.base_sha256,
                        expected_location=location, reason=update.reason,
                    )
                except (LibraryLayoutError, FileNotFoundError) as exc:
                    result.rejected_updates.append(RejectedDreamingUpdate(
                        path=update.path, reason=f"Archive refused; reconciliation must be replayed: {exc}",
                    ))
                    continue
            result.updates.append(update)
            continue
        rejected = _validate_update_content(target, update)
        if rejected:
            result.rejected_updates.append(rejected)
            continue
        relative_path = update.path.removeprefix("memory/")
        if obsidian_format_enabled() and relative_path.startswith("capsules/"):
            try:
                existing = target.read_text(encoding="utf-8") if target.exists() else ""
                update.content_md = prepare_souvenir_format(
                    preserve_existing_frontmatter(existing, update.content_md),
                    relative_path[len("capsules/"):].removesuffix(".md"),
                )
                if vault_root(MEMORY_ROOT) is not None and relative_path != "capsules/index.md":
                    update.content_md = with_identity(update.content_md,
                        relative_path[len("capsules/"):].removesuffix(".md"), current=existing)
                if structure_enabled():
                    validate_context_page(update.content_md)
            except (ValueError, yaml.YAMLError) as exc:
                result.rejected_updates.append(RejectedDreamingUpdate(
                    path=update.path, reason=f"Obsidian format refused: {exc.__class__.__name__}",
                ))
                continue
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            update.content_md = normalize_memory_dates(update.content_md, current=existing)
        except ValueError as exc:
            result.rejected_updates.append(RejectedDreamingUpdate(
                path=update.path, reason=f"Invalid generated date: {exc}",
            ))
            continue
        update.diff = _build_update_diff(target=target, update=update)
        update.changed = not _same_markdown_content(target, update.content_md)
        if not dry_run and update.changed:
            with memory_file_lock(target):
                if (_file_revision(target) != update.base_sha256
                        or _validate_update_target(user_id, update)[0] != target):
                    result.rejected_updates.append(
                        RejectedDreamingUpdate(
                            path=update.path,
                            reason="Fichier modifié depuis la lecture Dreaming ; réconciliation à rejouer.",
                        )
                    )
                    continue
                atomic_write_text(target, update.content_md.strip() + "\n")
            update.applied = True
        result.updates.append(update)

    index_auto_checked = _refresh_souvenir_index_entries(result.updates, dry_run=dry_run)
    result.review_notes.extend(_build_review_notes(result.updates, index_auto_checked=index_auto_checked))
    if not dry_run and any(update.applied for update in result.updates):
        try:
            from daemon_core.scene_model import invalidate_scene_models

            invalidate_scene_models(
                user_id,
                reason="dreaming_memory_repair",
            )
        except Exception as exc:
            logger.warning(
                "Invalidation de scène après dreaming échouée | error=%s",
                exc.__class__.__name__,
            )
        _refresh_semantic_atlas_after_dreaming(user_id, result.updates)
        refresh_navigation_safely(MEMORY_ROOT)
    return result


def _refresh_semantic_atlas_after_dreaming(
    user_id: str,
    updates: list[DreamingUpdate],
) -> None:
    souvenir_names = tuple(
        name
        for update in updates
        if update.applied
        and (
            name := _souvenir_name_from_memory_path(update.path)
        ).startswith("theme/")
    )
    if not souvenir_names:
        return
    try:
        from daemon_core.semantic_atlas import (
            coalesced_semantic_atlas_prewarmer,
        )

        prewarmer = coalesced_semantic_atlas_prewarmer(
            user_id,
            MEMORY_ROOT,
        )
        if prewarmer is None:
            return
        prewarmer.request(souvenir_names)
        prewarmer.flush_if_pending(force=True)
    except Exception as exc:
        # The vectors are a disposable routing index.  A refresh failure must
        # never roll back or invalidate an already committed Markdown repair.
        logger.warning(
            "Préchauffage Atlas après dreaming différé | error=%s",
            exc.__class__.__name__,
        )


def save_dreaming_report(
    result: DreamingResult,
    user_id: str,
    since_hours: int,
    now: datetime | None = None,
    report_root: Path | None = None,
    latest_root: Path | None = None,
) -> tuple[Path, Path]:
    now = now or datetime.now()
    safe_user = safe_memory_user_id(user_id)
    mode = "dry-run" if result.dry_run else "apply"
    root = report_root or MEMORY_ROOT / "levels" / "dreaming" / "reports"
    root.mkdir(parents=True, exist_ok=True)
    timestamp = now.strftime("%Y%m%dT%H%M%S")
    base = f"{safe_user}_{timestamp}_{mode}_{since_hours}h"
    json_path = root / f"{base}.json"
    markdown_path = root / f"{base}.md"
    latest = latest_root or root.parent / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    latest_json_path = latest / f"{safe_user}.json"
    latest_markdown_path = latest / f"{safe_user}.md"
    result.report_json_path = _memory_relative_path(json_path)
    result.report_markdown_path = _memory_relative_path(markdown_path)
    result.latest_json_path = _memory_relative_path(latest_json_path)
    result.latest_markdown_path = _memory_relative_path(latest_markdown_path)
    json_content = json.dumps(_result_to_dict(result), ensure_ascii=False, indent=2) + "\n"
    markdown_content = _render_dreaming_report_markdown(result, user_id=user_id, since_hours=since_hours)
    json_path.write_text(json_content, encoding="utf-8")
    markdown_path.write_text(markdown_content, encoding="utf-8")
    latest_json_path.write_text(json_content, encoding="utf-8")
    latest_markdown_path.write_text(markdown_content, encoding="utf-8")
    return json_path, markdown_path


def _read_l0_entries(path: Path, threshold: datetime, now: datetime) -> list[str]:
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    entries = []
    current_heading = ""
    current_lines = []
    current_timestamp = None
    for line in content.splitlines():
        if line.startswith("## ") and " — " in line:
            if current_heading:
                _append_l0_entry(entries, current_heading, current_lines, current_timestamp, threshold, now)
            current_heading = line
            current_lines = []
            current_timestamp = _parse_l0_timestamp(line)
            continue
        if current_heading:
            current_lines.append(line)
    if current_heading:
        _append_l0_entry(entries, current_heading, current_lines, current_timestamp, threshold, now)
    return entries


def _append_l0_entry(
    entries: list[str],
    heading: str,
    lines: list[str],
    timestamp: datetime | None,
    threshold: datetime,
    now: datetime,
) -> None:
    if timestamp is None or not _timestamp_in_window(timestamp, threshold, now):
        return
    body = "\n".join(lines).strip()
    entries.append(f"{heading}\n\n{body}".strip())


def _timestamp_in_window(
    timestamp: datetime,
    threshold: datetime,
    now: datetime,
) -> bool:
    """Compare legacy local L0 headings with newer offset-aware headings."""

    if timestamp.tzinfo is not None and now.tzinfo is None:
        timestamp = timestamp.astimezone().replace(tzinfo=None)
    elif timestamp.tzinfo is None and now.tzinfo is not None:
        timestamp = timestamp.replace(tzinfo=now.tzinfo)
    elif timestamp.tzinfo is not None and now.tzinfo is not None:
        timestamp = timestamp.astimezone(now.tzinfo)
    return threshold <= timestamp <= now


def _parse_l0_timestamp(heading: str) -> datetime | None:
    raw = heading.removeprefix("## ").split(" — ", 1)[0].strip()
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _snapshot_file(label: str, path: Path) -> str:
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    return f"## {label}\n{content.strip() or 'Empty file.'}"


def _capture_dreaming_revisions(user_id: str, *, locations: dict[str, Path] | None = None) -> dict[str, str]:
    safe_user = safe_memory_user_id(user_id)
    user_root = MEMORY_ROOT / "users" / safe_user
    paths = [
        user_root / name
        for name in (
            "context.md",
            "agenda.md",
            "contexte_actif.md",
            "focus_immediat.md",
        )
    ]
    paths.append(MEMORY_ROOT / "capsules" / "index.md")
    paths.extend(item.path for item in unique_note_locations(MEMORY_ROOT)
                 if (item.name.startswith("theme/") or vault_root(MEMORY_ROOT) is not None)
                 and not is_system_managed_souvenir(item.name))
    revisions = {}
    for path in paths:
        if not path.exists():
            continue
        relative = memory_reference(MEMORY_ROOT, path)
        revisions[f"memory/{relative}"] = _file_revision(path)
        if locations is not None:
            locations[f"memory/{relative}"] = path
    return revisions


def _file_revision(path: Path) -> str:
    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_theme_souvenirs(souvenir_paths: set[str] | None = None) -> str:
    targeted_snapshot = souvenir_paths is not None
    selected = None
    if targeted_snapshot:
        selected = {
            normalized
            for normalized in (
                _normalize_memory_evidence_path(path) for path in souvenir_paths
            )
            if normalized.startswith("capsules/theme/") and normalized.endswith(".md")
        }
    sections = []
    paths = [item.path for item in unique_note_locations(MEMORY_ROOT)
             if (item.name.startswith("theme/") or vault_root(MEMORY_ROOT) is not None)
             and not is_system_managed_souvenir(item.name)]
    if selected is not None:
        paths = [
            path
            for path in paths
            if memory_reference(MEMORY_ROOT, path) in selected
        ]
    else:
        paths = paths[:MAX_SOUVENIRS_IN_PROMPT]
    for path in paths:
        relative = memory_reference(MEMORY_ROOT, path)
        content = path.read_text(encoding="utf-8").strip()
        if not targeted_snapshot and len(content) > MAX_SOUVENIR_CHARS_IN_PROMPT:
            content = content[:MAX_SOUVENIR_CHARS_IN_PROMPT] + "\n...[truncated]"
        sections.append(f"### memory/{relative}\n{content or 'Empty file.'}")
    return "## Thematic souvenirs\n" + ("\n\n".join(sections) if sections else "No thematic souvenir.")


def _memory_mutation_receipt_paths(
    *,
    receipt_dir: Path | None = None,
) -> tuple[Path, ...]:
    if receipt_dir is not None:
        directories = [Path(receipt_dir).expanduser()]
    else:
        configured = os.getenv("DREAMING_CONTINUITY_RECEIPT_DIRS", "").strip()
        if configured:
            directories = [
                Path(value.strip()).expanduser()
                for value in configured.split(os.pathsep)
                if value.strip()
            ]
            if not directories:
                raise RuntimeError(
                    "DREAMING_CONTINUITY_RECEIPT_DIRS contains no directory."
                )
        else:
            return (continuity_receipt_path(""),)
    paths = {
        continuity_receipt_path("", directory=directory).resolve()
        for directory in directories
    }
    return tuple(sorted(paths))


def _memory_mutation_fingerprint(
    evidence: MemoryMutationEvidence,
) -> tuple[object, ...]:
    return (
        evidence.source_turn_started_at,
        evidence.changed_paths,
        evidence.context_paths,
        evidence.user_l0_ref,
        evidence.assistant_l0_ref,
    )


def _normalize_memory_evidence_path(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().replace("\\", "/").removeprefix("memory/")
    path = Path(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return ""
    return path.as_posix()


def _event_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _parse_update(raw_update: object) -> tuple[DreamingUpdate | None, RejectedDreamingUpdate | None]:
    if not isinstance(raw_update, dict):
        return None, RejectedDreamingUpdate(path="", reason="Mise à jour invalide.")
    path = str(raw_update.get("path", "") or "")
    action = str(raw_update.get("action", "") or "").lower()
    reason = str(raw_update.get("reason", "") or "")
    content = raw_update.get("content_md")
    if action not in {"create", "update", "archive"}:
        return None, RejectedDreamingUpdate(path=path, reason="Action non autorisée.")
    if action == "archive":
        # Archiving moves the note out of active memory; no new content is required.
        base_sha256 = raw_update.get("base_sha256")
        return DreamingUpdate(
            path=path,
            action="archive",
            reason=reason,
            content_md="",
            base_sha256=base_sha256 if isinstance(base_sha256, str) else "",
            base_location=str(raw_update.get("base_location", "") or ""),
        ), None
    if not isinstance(content, str) or not content.strip():
        return None, RejectedDreamingUpdate(path=path, reason="content_md vide ou invalide.")
    index_line = raw_update.get("index_line")
    base_sha256 = raw_update.get("base_sha256")
    return DreamingUpdate(
        path=path,
        action=action,
        reason=reason,
        content_md=content,
        index_line=index_line if isinstance(index_line, str) else "",
        storage_path=str(raw_update.get("storage_path", "") or ""),
        base_sha256=base_sha256 if isinstance(base_sha256, str) else "",
    ), None


def _validate_update_target(user_id: str, update: DreamingUpdate) -> tuple[Path | None, RejectedDreamingUpdate | None]:
    raw_path = Path(update.path)
    if raw_path.is_absolute() or update.path.startswith("~"):
        return None, RejectedDreamingUpdate(path=update.path, reason="Chemin absolu interdit.")
    normalized = raw_path.as_posix()
    if not normalized.startswith("memory/"):
        return None, RejectedDreamingUpdate(path=update.path, reason="Chemin hors mémoire interdit.")
    if vault_root(MEMORY_ROOT) is not None and normalized.startswith("memory/capsules/") and normalized != "memory/capsules/index.md":
        name = normalized[len("memory/capsules/"):].removesuffix(".md")
        if (not normalized.endswith(".md") or is_system_managed_souvenir(name)
                or not (name.startswith("theme/") or name.startswith("local/"))):
            return None, RejectedDreamingUpdate(path=update.path, reason="Protected or invalid note identity.")
        try:
            return resolve_note(MEMORY_ROOT, name, create=update.action == "create",
                                storage_path=update.storage_path or None), None
        except (OSError, ValueError) as exc:
            return None, RejectedDreamingUpdate(path=update.path, reason=str(exc))
    target = (MEMORY_ROOT / normalized.removeprefix("memory/")).resolve()
    memory_root = MEMORY_ROOT.resolve()
    try:
        target.relative_to(memory_root)
    except ValueError:
        return None, RejectedDreamingUpdate(path=update.path, reason="Chemin sort de memory/.")
    if target.suffix != ".md":
        return None, RejectedDreamingUpdate(path=update.path, reason="Seuls les fichiers Markdown sont autorisés.")
    safe_user = safe_memory_user_id(user_id)
    allowed_user_files = {
        (MEMORY_ROOT / "users" / safe_user / "context.md").resolve(),
        (MEMORY_ROOT / "users" / safe_user / "contexte_actif.md").resolve(),
        (MEMORY_ROOT / "users" / safe_user / "focus_immediat.md").resolve(),
    }
    if target in allowed_user_files:
        return target, None
    if target == (MEMORY_ROOT / "capsules" / "index.md").resolve():
        return target, None
    theme_root = (MEMORY_ROOT / "capsules" / "theme").resolve()
    try:
        relative = target.relative_to((MEMORY_ROOT / "capsules").resolve())
        if is_system_managed_souvenir(relative.with_suffix("").as_posix()):
            return None, RejectedDreamingUpdate(
                path=update.path,
                reason="Souvenir administrée exclusivement par le REM épisodique.",
            )
        target.relative_to(theme_root)
        return target, None
    except ValueError:
        return None, RejectedDreamingUpdate(path=update.path, reason="Chemin mémoire non autorisé pour le dreaming.")


def _validate_archive_target(target: Path, update: DreamingUpdate) -> RejectedDreamingUpdate | None:
    if vault_root(MEMORY_ROOT) is not None:
        name = name_for_path(MEMORY_ROOT, target)
        if not name or is_system_managed_souvenir(name) or not target.exists():
            return RejectedDreamingUpdate(path=update.path, reason="Missing or protected note.")
        return None
    theme_root = (MEMORY_ROOT / "capsules" / "theme").resolve()
    try:
        target.resolve().relative_to(theme_root)
    except ValueError:
        return RejectedDreamingUpdate(path=update.path, reason="Seules les souvenirs theme peuvent être archivées.")
    if not target.exists():
        return RejectedDreamingUpdate(path=update.path, reason="Souvenir introuvable à archiver.")
    return None


def _archive_souvenir(target: Path, *, expected_revision: str | None = None,
                      expected_location: Path | None = None, reason: str = "") -> bool:
    """Publish an already judged archive; PARGA keeps identity and local links.

    Dreaming retains its own eligibility rules. Its technical decision reference
    is not a user L0 quotation and does not bypass the after-turn evidence gate.
    The legacy layout retains its historical archive/index behaviour.
    """
    if expected_location is not None and target != expected_location:
        raise LibraryLayoutError("Note moved since the Dreaming snapshot")
    vault = vault_root(MEMORY_ROOT)
    if vault is not None:
        content = target.read_bytes()
        if expected_revision is not None and hashlib.sha256(content).hexdigest() != expected_revision:
            raise LibraryLayoutError("Note changed since the Dreaming snapshot")
        relative = target.relative_to(vault)
        if relative.parts[0] == "Archives":
            return False
        proposal = Relocation(
            NoteSnapshot(name_for_path(MEMORY_ROOT, target), relative.as_posix(), content),
            (Path("Archives") / relative).as_posix(),
            "dreaming:" + uuid.uuid4().hex, "", reason[:1200],
        )
        return publish_relocation(MEMORY_ROOT, proposal, content) == "applied"
    with memory_file_lock(target):
        if expected_revision is not None and _file_revision(target) != expected_revision:
            raise LibraryLayoutError("Note changed since the Dreaming snapshot")
        souvenirs_root = (MEMORY_ROOT / "capsules").resolve()
        relative = target.resolve().relative_to(souvenirs_root)
        dest = MEMORY_ROOT / "archive" / "capsules" / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(dest))
        _remove_index_entry(relative.with_suffix("").as_posix())
    return True


def _remove_index_entry(name: str) -> None:
    index = MEMORY_ROOT / "capsules" / "index.md"
    with memory_file_lock(index):
        if not index.exists():
            return
        kept = []
        for line in index.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                entry = stripped[2:].split(":", 1)[0].strip()
                if entry == name:
                    continue
            kept.append(line.rstrip())
        atomic_write_text(index, "\n".join(kept).strip() + "\n")


def _validate_update_content(target: Path, update: DreamingUpdate) -> RejectedDreamingUpdate | None:
    if not target.exists():
        return None
    existing = target.read_text(encoding="utf-8").strip()
    proposed = update.content_md.strip()
    if target == (MEMORY_ROOT / "capsules" / "index.md").resolve():
        current_managed = _system_managed_index_entries(existing)
        proposed_managed = _system_managed_index_entries(proposed)
        if current_managed != proposed_managed:
            return RejectedDreamingUpdate(
                path=update.path,
                reason="Entrées d'index administrées par le REM épisodique modifiées.",
            )
    if len(existing) >= MIN_SHRINK_GUARD_CHARS and len(proposed) < len(existing) * MAX_SHRINK_RATIO:
        return RejectedDreamingUpdate(path=update.path, reason="Réécriture trop appauvrissante refusée.")
    return None


def _system_managed_index_entries(content: str) -> dict[str, str]:
    entries = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        name = stripped[2:].split(":", 1)[0].strip()
        if is_system_managed_souvenir(name):
            entries[name] = stripped
    return entries


def _same_markdown_content(target: Path, proposed: str) -> bool:
    if not target.exists():
        return False
    return target.read_text(encoding="utf-8").strip() == proposed.strip()


def _build_update_diff(target: Path, update: DreamingUpdate) -> str:
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    proposed = update.content_md.strip() + "\n"
    return "".join(
        difflib.unified_diff(
            existing.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=f"current/{update.path}",
            tofile=f"proposed/{update.path}",
            n=3,
        )
    )


def _refresh_souvenir_index_entries(updates: list[DreamingUpdate], dry_run: bool) -> bool:
    if any(update.path == "memory/capsules/index.md" for update in updates):
        return False
    souvenir_updates = [
        update
        for update in updates
        if update.action in {"create", "update"}
        and update.changed
        and update.path.startswith("memory/capsules/theme/")
        and update.path.endswith(".md")
    ]
    if not souvenir_updates:
        return False

    index_path = MEMORY_ROOT / "capsules" / "index.md"
    existing_index = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    proposed_index = existing_index
    for update in souvenir_updates:
        name = _souvenir_name_from_memory_path(update.path)
        if name:
            preferred_anchors = _added_souvenir_bullets(update.diff)
            llm_entry = validated_souvenir_index_line(name, update.index_line)
            fallback = existing_souvenir_index_line(proposed_index, name)
            proposed_index = merge_souvenir_index(
                proposed_index,
                llm_entry or fallback or souvenir_index_entry(
                    name,
                    update.content_md,
                    preferred_anchor_lines=preferred_anchors,
                ),
            )

    if proposed_index == existing_index:
        return True

    index_update = DreamingUpdate(
        path="memory/capsules/index.md",
        action="update",
        reason="Rafraîchissement automatique des entrées d'index des souvenirs modifiées.",
        content_md=proposed_index,
        changed=True,
    )
    index_update.diff = _build_update_diff(target=index_path, update=index_update)
    if not dry_run:
        with memory_file_lock(index_path):
            latest_index = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
            merged_index = latest_index
            for update in souvenir_updates:
                name = _souvenir_name_from_memory_path(update.path)
                if name:
                    preferred_anchors = _added_souvenir_bullets(update.diff)
                    llm_entry = validated_souvenir_index_line(name, update.index_line)
                    fallback = existing_souvenir_index_line(merged_index, name)
                    merged_index = merge_souvenir_index(
                        merged_index,
                        llm_entry or fallback or souvenir_index_entry(
                            name,
                            update.content_md,
                            preferred_anchor_lines=preferred_anchors,
                        ),
                    )
            atomic_write_text(index_path, merged_index)
        index_update.applied = True
    updates.append(index_update)
    return True


def _souvenir_name_from_memory_path(path: str) -> str:
    prefix = "memory/capsules/"
    if not path.startswith(prefix) or not path.endswith(".md"):
        return ""
    return path.removeprefix(prefix).removesuffix(".md")


def _added_souvenir_bullets(diff: str) -> list[str]:
    bullets = []
    for line in diff.splitlines():
        if not line.startswith("+- "):
            continue
        bullet = line[3:].strip()
        if bullet:
            bullets.append(bullet)
    return bullets


def _build_review_notes(updates: list[DreamingUpdate], index_auto_checked: bool = False) -> list[str]:
    paths = {update.path for update in updates}
    theme_updates = [
        update.path
        for update in updates
        if update.action in {"create", "update"} and update.path.startswith("memory/capsules/theme/")
    ]
    notes = []
    if theme_updates and "memory/capsules/index.md" not in paths and not index_auto_checked:
        notes.append("Une ou plusieurs souvenirs theme sont modifiées sans mise à jour associée de memory/capsules/index.md.")
    return notes


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        stripped = stripped.removesuffix("```").strip()
    return stripped


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bounded_string_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:limit]:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:500])
    return result


def _memory_relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _render_dreaming_report_markdown(result: DreamingResult, user_id: str, since_hours: int) -> str:
    mode = "dry-run" if result.dry_run else "apply"
    applied_count = sum(1 for update in result.updates if update.applied)
    proposed_count = len(result.updates)
    title = "Rapport Dreaming Quotidien"
    update_section_title = "Mises à jour proposées" if result.dry_run else "Mises à jour appliquées"
    lines = [
        f"# {title}",
        "",
        "## Statut",
        "",
        f"- Utilisateur : `{safe_memory_user_id(user_id)}`",
        f"- Mode : `{mode}`",
        f"- Fenêtre relue : `{since_hours}h`",
        f"- Confiance : `{result.confidence:.2f}`",
        f"- Mises à jour proposées : `{proposed_count}`",
        f"- Mises à jour appliquées : `{applied_count}`",
        f"- Mises à jour rejetées : `{len(result.rejected_updates)}`",
        "",
    ]
    if result.input_summary:
        lines.extend(
            [
                "## Entrées Vérifiées",
                "",
                f"- L0 transmis : `{result.input_summary.get('l0_chars', 0)}` caractères",
                f"- Journaux de continuité lus : `{result.input_summary.get('continuity_journals', 0)}`",
                f"- Preuves de mutation : `{result.input_summary.get('mutation_evidence_events', 0)}`",
                f"- Chemins mémoire ciblés : `{result.input_summary.get('targeted_memory_paths', 0)}`",
                f"- Snapshot mémoire : `{result.input_summary.get('memory_snapshot_chars', 0)}` caractères",
                f"- Prompt Dreaming : `{result.input_summary.get('prompt_chars', 0)}` caractères",
                "",
            ]
        )
    lines.extend(
        [
            "## Résumé",
            "",
            result.reason or "Aucune raison fournie.",
            "",
            "## Rapport Humain",
            "",
            result.report_md or "Aucun rapport fourni.",
            "",
            "## Points De Vigilance",
            "",
        ]
    )
    if result.review_notes:
        lines.extend(f"- {note}" for note in result.review_notes)
        lines.append("")
    else:
        lines.extend(["Aucun point de vigilance automatique.", ""])
    lines.extend(["## Conflits Non Résolus", ""])
    if result.unresolved_conflicts:
        lines.extend(f"- {conflict}" for conflict in result.unresolved_conflicts)
        lines.append("")
    else:
        lines.extend(["Aucun conflit non résolu signalé.", ""])
    lines.extend(
        [
            f"## {update_section_title}",
            "",
        ]
    )
    if result.updates:
        for index, update in enumerate(result.updates, 1):
            applied = "oui" if update.applied else "non"
            diff = update.diff.strip()
            lines.extend(
                [
                    f"### {index}. `{update.path}`",
                    "",
                    f"- Action : `{update.action}`",
                    f"- Appliquée : `{applied}`",
                    f"- Raison : {update.reason or 'Aucune raison fournie.'}",
                    f"- Taille du contenu final : `{len(update.content_md)}` caractères",
                    "",
                    "```diff",
                    diff or "[aucun diff]",
                    "```",
                    "",
                ]
            )
    else:
        if result.dry_run:
            lines.extend(["Aucune mise à jour proposée.", ""])
        else:
            lines.extend(["Aucune mise à jour appliquée.", ""])
    lines.extend(["## Mises À Jour Rejetées", ""])
    if result.rejected_updates:
        for index, rejected in enumerate(result.rejected_updates, 1):
            lines.extend(
                [
                    f"### {index}. `{rejected.path or '[sans chemin]'}`",
                    "",
                    f"- Raison : {rejected.reason}",
                    "",
                ]
            )
    else:
        lines.extend(["Aucune mise à jour rejetée.", ""])
    lines.extend(
        [
            "## Notes",
            "",
            "Le JSON voisin contient les contenus Markdown complets proposés par le provider.",
            "Ce rapport Markdown est pensé pour une lecture quotidienne rapide : statut, résumé, changements appliqués ou proposés, puis rejets éventuels.",
            "",
        ]
    )
    return "\n".join(lines)


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.info("python-dotenv indisponible, .env non chargé automatiquement.")
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def main() -> None:
    _load_dotenv_if_available()
    parser = argparse.ArgumentParser(description="Run Daemon Harness dreaming maintenance manually.")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--since-hours", type=int, default=24)
    parser.add_argument("--apply", action="store_true", help="Apply accepted updates. Default is dry-run.")
    parser.add_argument("--apply-report", help="Apply exactly the updates from a previously reviewed dreaming JSON report.")
    parser.add_argument("--apply-latest", action="store_true", help="Apply exactly the latest dry-run JSON report for this user.")
    parser.add_argument("--latest-report", action="store_true", help="Print the latest dry-run JSON report path for this user and exit.")
    parser.add_argument("--no-save-report", action="store_true", help="Do not persist JSON/Markdown reports under memory/levels/dreaming/reports.")
    args = parser.parse_args()
    provider_failed = False

    if args.apply_report and args.apply_latest:
        parser.error("--apply-report et --apply-latest sont mutuellement exclusifs.")
    if args.latest_report:
        report = latest_dreaming_report(user_id=args.user_id)
        if report is None:
            parser.error("Aucun rapport dry-run trouvé pour cet utilisateur.")
        print(_memory_relative_path(report))
        return
    if args.apply_latest:
        report = latest_dreaming_report(user_id=args.user_id)
        if report is None:
            parser.error("Aucun rapport dry-run trouvé pour cet utilisateur.")
        result = apply_dreaming_report_file(user_id=args.user_id, report_path=report)
    elif args.apply_report:
        result = apply_dreaming_report_file(user_id=args.user_id, report_path=args.apply_report)
    else:
        from daemon_core.registre import catchup_window_hours, faculty_lock, record_pass

        # Recovery: extend the window back to the last successful pass,
        # within its cap, to cover missed nights automatically.
        since_hours = int(catchup_window_hours(args.since_hours, "dreaming", args.user_id))
        if since_hours != args.since_hours:
            logger.warning("Rattrapage dreaming : fenêtre étirée de %sh à %sh", args.since_hours, since_hours)
        args.since_hours = since_hours
        with faculty_lock("dreaming", args.user_id) as acquired:
            if not acquired:
                print(json.dumps({"status": "verrou-tenu"}, ensure_ascii=False))
                return
            try:
                result = run_dreaming(
                    user_id=args.user_id,
                    provider=build_memory_provider(),
                    since_hours=args.since_hours,
                    dry_run=not args.apply,
                )
            except Exception as exc:
                record_pass("dreaming", args.user_id, "echec", window_hours=args.since_hours, detail=exc.__class__.__name__)
                raise
            provider_failed = result.reason.startswith(
                "Dreaming indisponible"
            )
            record_pass(
                "dreaming",
                args.user_id,
                "echec" if provider_failed else "ok",
                window_hours=args.since_hours,
                detail=result.reason[:120] if provider_failed else f"{len(result.updates)} maj, {len(result.rejected_updates)} rejets",
            )
    if not args.no_save_report:
        save_dreaming_report(result=result, user_id=args.user_id, since_hours=args.since_hours)
    print(json.dumps(_result_to_dict(result), ensure_ascii=False, indent=2))
    if provider_failed:
        raise SystemExit(2)


def _result_to_dict(result: DreamingResult) -> dict:
    return {
        "dry_run": result.dry_run,
        "reason": result.reason,
        "confidence": result.confidence,
        "report_md": result.report_md,
        "unresolved_conflicts": result.unresolved_conflicts,
        "updates": [update.__dict__ for update in result.updates],
        "rejected_updates": [update.__dict__ for update in result.rejected_updates],
        "review_notes": result.review_notes,
        "input_summary": result.input_summary,
        "report_json_path": result.report_json_path,
        "report_markdown_path": result.report_markdown_path,
        "latest_json_path": result.latest_json_path,
        "latest_markdown_path": result.latest_markdown_path,
    }


if __name__ == "__main__":
    main()
