"""Post-turn curation for short-lived, decision-relevant temporal memory."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from daemon_core.memory_dates import MEMORY_DATE_RULES, normalize_memory_dates
from daemon_core.agenda_governance import invalid_new_agenda_lines
from daemon_core.memory.integrity import atomic_write_text, memory_file_lock
from daemon_core.memory.reader import load_recent_dialogue, load_user_agenda
from daemon_core.memory.writer import user_agenda_path
from daemon_core.prompt_composer import current_time_context
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider
from daemon_core.source_posture import SOURCE_POSTURE_PROMPT

logger = logging.getLogger("daemon_core.temporal_memory")


class AgendaRefusal(ValueError):
    """A governance decision to keep the agenda unchanged, not a failed update.

    Durable work records it as a rejected path and continues; provider,
    parsing and concurrent-write errors remain retryable failures.
    """


@dataclass(frozen=True)
class TemporalMemoryResult:
    updated: bool = False
    reason: str = ""
    source_posture: str = ""
    changed_paths: tuple[str, ...] = ()
    unchanged_paths: tuple[str, ...] = ()
    rejected_paths: tuple[str, ...] = ()


def temporal_curator_enabled(user_id: str) -> bool:
    enabled = os.getenv("DAEMON_CORE_TEMPORAL_CURATOR", "").strip().lower()
    if enabled not in {"1", "on", "true"}:
        return False
    configured = os.getenv(
        "DAEMON_CORE_TEMPORAL_CURATOR_USER_IDS",
        "",
    )
    allowed = {item.strip() for item in configured.split(",") if item.strip()}
    return bool(user_id and user_id in allowed)


def run_temporal_curator(
    user_id: str,
    user_message: str,
    provider: LLMProvider,
    recent_dialogue: str | None = None,
) -> TemporalMemoryResult:
    """Merge a temporal user fact into the agenda outside the response path."""

    current = load_user_agenda(user_id).strip()
    recent = (
        load_recent_dialogue(user_id, max_turns=24)
        if recent_dialogue is None
        else recent_dialogue
    )
    messages = _build_temporal_curator_messages(
        current_agenda=current,
        recent_dialogue=recent,
        user_message=user_message,
    )
    log_llm_prompt(logger, "CURATION TEMPORELLE", messages)
    try:
        raw = provider.complete(messages=messages, temperature=0.0)
        data = json.loads(_strip_json_fence(raw))
        if not isinstance(data, dict) or data.get("should_update") is not True:
            return TemporalMemoryResult(
                reason=_clean_reason(data),
                source_posture=_source_posture(data),
            )
        if data.get("source_posture") != "direct_fact":
            return TemporalMemoryResult(
                reason=(
                    _clean_reason(data)
                    or "Une demande de vérification n'établit pas un fait temporel."
                ),
                source_posture=_source_posture(data),
            )
        proposed = data.get("agenda_md")
        if not isinstance(proposed, str) or not proposed.strip():
            return TemporalMemoryResult(reason="Proposition d'agenda vide.")
        changed = _write_agenda_safely(
            user_id=user_id,
            current=current,
            proposed=proposed,
            removed_existing_lines=data.get("removed_existing_lines"),
            user_message=user_message,
            provider=provider,
        )
        path = _agenda_memory_path(user_id)
        return TemporalMemoryResult(
            updated=changed,
            reason=_clean_reason(data),
            source_posture=_source_posture(data),
            changed_paths=(path,) if changed else (),
            unchanged_paths=() if changed else (path,),
        )
    except Exception as exc:
        from daemon_core.maintenance_errors import reraise_memory_update_failure

        if not isinstance(exc, AgendaRefusal):
            reraise_memory_update_failure(exc)
        logger.warning(
            "Curation temporelle refusée | user_id=%s | error=%s",
            user_id,
            exc,
        )
        return TemporalMemoryResult(
            reason=str(exc),
            rejected_paths=(_agenda_memory_path(user_id),),
        )


def _build_temporal_curator_messages(
    current_agenda: str,
    recent_dialogue: str,
    user_message: str,
) -> list[dict]:
    system = """You maintain the short agenda of a personal memory after the response, without making the conversation wait.

The latest user message is the only authorized source for adding, correcting or removing a fact. Recent dialogue serves only to resolve references, people and distinct stages of the same scene.

Principles:
""" + SOURCE_POSTURE_PROMPT + """
- with `reported_memory_claim`, always respond with `should_update=false`, even if the wording seems to correct the agenda;
- the current agenda is the source of truth; always return its complete Markdown content;
- preserve each existing bullet verbatim, except for a clarification, correction or cancellation explicitly established by the latest user message;
- when a message explicitly corrects one component of an existing bullet, or provides a clarification about the same person, object or event, change only that component and preserve all its independent, uncorrected clauses verbatim;
- if an existing bullet and the new fact describe two independent elements that can be true at the same time, never rephrase the bullet: preserve it verbatim and, if needed, add the new fact separately. If the message explicitly qualifies the same element, you may enrich its bullet without removing any earlier fact;
- several nearby events, journeys or deadlines can coexist: a new stage never silently replaces another dated stage;
- when an explicit fact covers a period or several dates, preserve the entire period or each relevant date; never reduce it to the first day. Facts that can change independently stay in separate bullets;
- preserve attributions, dates, times, conditions and uncertainties;
- add no deduction drawn from the assistant's response;
- if the latest message brings no temporal fact with lasting usefulness, respond with `should_update=false`.
- each added bullet must carry an absolute date, a bounded period or an explicit recurrence; resolve “today”, “tomorrow” and relative days using the current time instead of writing them as is;
- `En cours`, `En ce moment`, `Récemment`, `Déjà`, `Cette année` and `À prévoir` are never sufficient horizons on their own. A taste, a relationship, an appreciated place, a general state, a past episode or an intention without a deadline belongs in souvenirs, active context or focus, not in the agenda.

Any rephrased existing bullet counts as a removed bullet, even if the new version retains almost all of its content. For each changed or removed bullet, therefore provide its exact old line and an exact, continuous quotation from the latest message that establishes the new value, boundary or cancellation. The user does not need to repeat the entire old bullet: the quotation must prove the corrected component, while the new bullet must preserve the rest. Without this evidence, copy the bullet identically. Respond in strict JSON, without Markdown.

Schema:
{"source_posture":"direct_fact|reported_memory_claim|none","should_update":false,"agenda_md":null,"removed_existing_lines":[{"line":"- exact removed line","evidence":"exact quotation"}],"reason":"short reason"}"""
    system = system.replace("\nSchema:", "\n" + MEMORY_DATE_RULES + "\nSchema:")
    agenda_text = current_agenda or "# Agenda\n\n- No explicit event."
    user = f"""Current time:
{current_time_context()}

Current agenda:
{agenda_text}

Recent dialogue:
{recent_dialogue[-8_000:] or "None."}

Latest user message to consolidate:
{user_message}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _write_agenda_safely(
    user_id: str,
    current: str,
    proposed: str,
    removed_existing_lines: object,
    user_message: str,
    provider: LLMProvider,
) -> bool:
    path = user_agenda_path(user_id)
    normalized = normalize_memory_dates(proposed.strip(), current=current)
    if current == normalized:
        return False
    invalid_additions = invalid_new_agenda_lines(current, normalized)
    if invalid_additions:
        raise AgendaRefusal(
            "horizon d'agenda non opérationnel ("
            + " | ".join(item.line for item in invalid_additions[:3])
            + ")"
        )
    protected = set(_agenda_bullets(current))
    resulting = set(_agenda_bullets(normalized))
    removed = protected - resulting
    declared = _authorized_removals(removed_existing_lines, user_message)
    unauthorized = sorted(removed - declared)
    if unauthorized:
        raise AgendaRefusal(
            "perte d'agenda non autorisée (" + " | ".join(unauthorized[:3]) + ")"
        )
    if removed:
        semantically_authorized = _judge_agenda_removals(
            provider=provider,
            current=current,
            proposed=normalized,
            user_message=user_message,
            removed=sorted(removed),
        )
        unauthorized = sorted(removed - semantically_authorized)
        if unauthorized:
            raise AgendaRefusal(
                "correction d'agenda non explicitement établie ("
                + " | ".join(unauthorized[:3])
                + ")"
            )
    with memory_file_lock(path):
        latest = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        if latest != current:
            raise ValueError("agenda modifié concurremment ; nouvelle curation requise")
        atomic_write_text(path, normalized + "\n")
        return True


def _judge_agenda_removals(
    *,
    provider: LLMProvider,
    current: str,
    proposed: str,
    user_message: str,
    removed: list[str],
) -> set[str]:
    system = """You are an integrity judge for temporal memory.

Decide separately for each old line whether the LATEST user message, on its own:
- makes it false or obsolete as written;
- explicitly resolves a value that this line left unknown;
- or provides a directly established qualification about the same person, object or event, while the proposal preserves every fact already carried by the old line.

This third case is monotonic enrichment: the old line remains entirely true and the new version only adds a proven clarification. It can be authorized without the old fact being contradicted.

The user does not need to repeat the entire old line: an explicitly corrected date, boundary, time, attribution, condition or uncertainty can authorize replacing the entire line when the format requires rewriting it. In all cases, the proposal must apply only the correction or enrichment while preserving every independent, uncorrected clause. The proposal may be compared with the old line to check this preservation, but it is never evidence of the correction.

Apply this decisive test: if the old line and the latest message can still be true together as two independent facts, refuse the replacement; the new fact must then be added separately or ignored if it is not lasting. Do not confuse this coexistence with a clarification explicitly attached to the same element. An immediate priority, an ambiguous rephrasing, a new stage or a local remark that does not qualify the earlier scope is never enough. Also refuse if the proposal drops an independent clause that the latest message does not correct. Earlier dialogue is not evidence. When in doubt, refuse the removal.

Respond in strict JSON:
{"judgments":[{"line":"exact old line","authorized":false,"evidence":"exact quotation from the latest message or empty string","reason":"short reason"}]}"""
    user = f"""Current agenda:
{current}

Proposal:
{proposed}

Old lines that would disappear:
{json.dumps(removed, ensure_ascii=False)}

Latest user message, the only authorized evidence:
{user_message}"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    log_llm_prompt(logger, "GARDE D'INTÉGRITÉ AGENDA", messages)
    raw = provider.complete(messages=messages, temperature=0.0)
    data = json.loads(_strip_json_fence(raw))
    judgments = data.get("judgments") if isinstance(data, dict) else None
    if not isinstance(judgments, list):
        return set()
    authorized: set[str] = set()
    normalized_message = " ".join(user_message.split()).casefold()
    removed_set = set(removed)
    for item in judgments:
        if not isinstance(item, dict) or item.get("authorized") is not True:
            continue
        line = item.get("line")
        evidence = item.get("evidence")
        if not isinstance(line, str) or line not in removed_set:
            continue
        if not isinstance(evidence, str):
            continue
        normalized_evidence = _normalize_quoted_evidence(evidence)
        if normalized_evidence and normalized_evidence in normalized_message:
            authorized.add(line)
    return authorized


def _agenda_bullets(content: str) -> list[str]:
    return [line.strip() for line in content.splitlines() if line.strip().startswith("- ")]


def _authorized_removals(raw: object, user_message: str) -> set[str]:
    if not isinstance(raw, list):
        return set()
    authorized: set[str] = set()
    normalized_message = " ".join(user_message.split()).casefold()
    for item in raw:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        evidence = item.get("evidence")
        if not isinstance(line, str) or not isinstance(evidence, str):
            continue
        normalized_evidence = _normalize_quoted_evidence(evidence)
        if normalized_evidence and normalized_evidence in normalized_message:
            authorized.add(line.strip())
    return authorized


def _normalize_quoted_evidence(evidence: str) -> str:
    normalized = " ".join(evidence.split()).casefold()
    quote_pairs = (
        ("\"", "\""),
        ("'", "'"),
        ("«", "»"),
        ("“", "”"),
        ("‘", "’"),
    )
    while len(normalized) >= 2:
        for opening, closing in quote_pairs:
            if normalized.startswith(opening) and normalized.endswith(closing):
                normalized = normalized[len(opening) : -len(closing)].strip()
                break
        else:
            break
    return normalized


def _agenda_memory_path(user_id: str) -> str:
    path = user_agenda_path(user_id)
    return f"users/{path.parent.name}/{path.name}"


def _clean_reason(data: object) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("reason"), str):
        return ""
    return data["reason"].strip()[:500]


def _source_posture(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    value = data.get("source_posture")
    return value if value in {"direct_fact", "reported_memory_claim", "none"} else ""


def _strip_json_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text
