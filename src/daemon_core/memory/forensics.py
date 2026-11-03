"""Bounded, read-only forensic lookup in the append-only L0 journal."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

import yaml

from daemon_core.memory.coverage import RecoveryNeed
from daemon_core.memory.metadata import parse_souvenir_frontmatter
from daemon_core.memory.l0_search import L0TextQuery, parse_text_query, search_l0_text
from daemon_core.memory.reader import parse_souvenir_index, read_markdown
from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.paths import MEMORY_ROOT
from daemon_core.prompt_composer import current_time_context
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider


logger = logging.getLogger("daemon_core.memory_forensics")

MAX_FORENSIC_FILES = 2
MAX_FORENSIC_FILE_CHARS = 24_000
MAX_FORENSIC_TOTAL_CHARS = 40_000
MAX_EVIDENCE_CHARS = 1_000
MAX_EVIDENCE_CONTEXT_CHARS = 6_000
MAX_EPISODIC_ANCHOR_COUNT = 64
MAX_EPISODIC_ANCHOR_CHARS = 24_000
MAX_EPISODIC_ANCHOR_SUMMARY_CHARS = 360
MAX_EPISODIC_L0_FILES = 8
MAX_CHRONOLOGY_SOURCE_CHARS = 2_000_000
MAX_CHRONOLOGY_CATALOG_TURNS = 240
MAX_CHRONOLOGY_CATALOG_CHARS = 48_000
MAX_CHRONOLOGY_EVIDENCE_TURNS = 10
MAX_CHRONOLOGY_EVIDENCE_CHARS = 8_000
_TURN_HEADER = re.compile(
    r"^##\s+(.+?)\s+[—-]\s+(user|assistant)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass
class ForensicPlan:
    should_search: bool = False
    files: list[str] = field(default_factory=list)
    archive: str = ""
    reason: str = ""
    confidence: float = 0.0
    text_query: L0TextQuery | None = None


@dataclass
class ForensicRecovery:
    attempted: bool = False
    plan: ForensicPlan = field(default_factory=ForensicPlan)
    status: str = "not_attempted"
    source_file: str = ""
    evidence: str = ""
    reason: str = ""
    confidence: float = 0.0
    relation_status: str = ""
    conclusion: str = ""
    search_stats: dict = field(default_factory=dict)
    evidence_context: str = ""


@dataclass(frozen=True)
class ChronologyTurn:
    turn_id: str
    locator: str
    role: str
    body: str
    start: int
    end: int


def failed_recall_tool_payload(*, evidence_kind: str) -> dict:
    """Render a material lookup failure as a fail-closed tool result."""

    if evidence_kind not in {"personal_fact", "conversation_chronology"}:
        raise ValueError(f"Unsupported forensic evidence kind: {evidence_kind}")
    return {
        "status": "error",
        "response_constraint": (
            'The required exact evidence could not be obtained. Do not assert, infer, complete or suggest the fact or relationship depending on this search.'
        ),
        "relation_status": "not_established",
        "conclusion": "",
        "evidence_status": "error",
        "evidence": "",
        "confidence": 0.0,
        "evidence_kind": evidence_kind,
        "source_file": "",
        "error_code": "forensic_lookup_failed",
        "boundary": (
            'The error status is a material boundary of the turn, not the absence of a fact. No dependent response may be composed as if the search had succeeded.'
        ),
    }


def chronology_tool_payload(recovery: ForensicRecovery) -> dict:
    """Render chronology evidence without conflating retrieval and verdict."""

    exact = recovery.status == "found"
    relation_status = recovery.relation_status if exact else "not_established"
    conclusion = recovery.conclusion if exact else ""
    if not exact:
        response_constraint = (
            'No usable exact evidence: neither confirm nor complete the requested relationship.'
        )
    elif relation_status == "established":
        response_constraint = (
            f'The requested relationship is established. Respond only within the bounds of this conclusion: {conclusion}'
        )
    elif relation_status == "not_established":
        response_constraint = (
            f'Do not confirm the requested relationship. Explicitly distinguish the stages according to this conclusion: {conclusion}'
        )
    else:
        response_constraint = (
            f'Choose none of the possible readings. Convey the ambiguity according to this conclusion: {conclusion}'
        )
    return {
        "status": recovery.status,
        "response_constraint": response_constraint,
        "relation_status": relation_status,
        "conclusion": conclusion,
        "evidence_status": "exact_evidence_found" if exact else recovery.status,
        "evidence": recovery.evidence if exact else "",
        "confidence": recovery.confidence,
        "evidence_kind": "conversation_chronology",
        "source_file": recovery.source_file if exact else "",
        "boundary": (
            'Evidence status indicates only whether an exact range was retrieved. Only relation_status, conclusion and the quotation bound the response; an assistant statement never establishes a personal fact.'
        ),
    }


def personal_fact_tool_payload(
    recovery: ForensicRecovery,
    *,
    status: str | None = None,
    confidence: float | None = None,
) -> dict:
    """Render personal evidence without exposing a judge's free-form rationale."""

    effective_status = status or recovery.status
    effective_confidence = recovery.confidence if confidence is None else confidence
    exact = effective_status == "found" and recovery.status == "found"
    partial = effective_status == "partial" and recovery.status == "found"
    if exact:
        relation_status = recovery.relation_status
    elif partial:
        relation_status = "ambiguous"
    else:
        relation_status = "not_established"
    conclusion = recovery.conclusion if exact else ""

    if not exact and not partial:
        response_constraint = (
            'No usable exact evidence: neither confirm nor complete the requested personal fact.'
        )
    elif partial:
        response_constraint = (
            'The quotation is exact but incomplete. Use only what it explicitly says and do not complete the requested item.'
        )
    elif relation_status == "established":
        response_constraint = (
            f'The requested fact is established. Respond only within the bounds of this conclusion: {conclusion}'
        )
    elif relation_status == "not_established":
        response_constraint = (
            f'Do not confirm the requested fact. Explicitly convey the distinction established by this conclusion: {conclusion}'
        )
    else:
        response_constraint = (
            f'Choose none of the possible readings. Convey the ambiguity according to this conclusion: {conclusion}'
        )

    if exact:
        evidence_status = "exact_evidence_found"
    elif partial:
        evidence_status = "partial_exact_evidence"
    else:
        evidence_status = effective_status
    return {
        "status": effective_status,
        "response_constraint": response_constraint,
        "relation_status": relation_status,
        "conclusion": conclusion,
        "evidence_status": evidence_status,
        "evidence": recovery.evidence if exact or partial else "",
        "evidence_context": getattr(recovery, "evidence_context", "") if exact or partial else "",
        "confidence": effective_confidence,
        "evidence_kind": "personal_fact",
        "source_file": recovery.source_file if exact or partial else "",
        "boundary": (
            'The quotation must come exactly from a user turn. evidence_context is an exact surrounding passage with dates and speaker labels, not an additional verdict. Assistant statements never establish personal facts. Only relation_status, conclusion and the exact sources bound complete evidence; a partial status authorizes only the words of the quotation.'
        ),
    }


def attempt_l0_forensic_recovery(
    user_id: str,
    message: str,
    recent_dialogue: str,
    recovery_needs: list[RecoveryNeed],
    provider: LLMProvider,
) -> ForensicRecovery:
    """Open justified dates or bounded text matches, then verify exact evidence."""
    catalog = l0_file_catalog(user_id)
    result = ForensicRecovery(attempted=True)
    if not catalog or not recovery_needs:
        result.status = "abstained"
        result.reason = "No usable L0 journal or forensic recall need."
        return result

    episodic_anchors = episodic_archive_catalog()
    plan_messages = _build_forensic_plan_messages(
        message=message,
        recent_dialogue=recent_dialogue,
        recovery_needs=recovery_needs,
        catalog=catalog,
        episodic_anchors=episodic_anchors,
    )
    log_llm_prompt(logger, "PLAN FORENSIQUE L0 (OMBRE)", plan_messages)
    raw_plan = provider.complete(messages=plan_messages, temperature=0.0)
    result.plan = _parse_forensic_plan(
        raw_plan,
        available_files=set(catalog),
        available_archives={item["archive"] for item in episodic_anchors},
        allow_text_query=True,
    )
    if result.plan.archive:
        result.plan.files = episodic_archive_l0_files(
            result.plan.archive,
            episodic_anchors=episodic_anchors,
            available_files=set(catalog),
        )
    if not result.plan.should_search:
        result.status = "abstained"
        result.reason = result.plan.reason or "No defensible time window."
        return result

    if result.plan.text_query is not None:
        matches = search_l0_text(MEMORY_ROOT, user_id, result.plan.text_query, catalog)
        sources = matches.sources
        verification_sources = matches.exact_sources
        result.search_stats = {
            "scanned_files": matches.scanned_files,
            "scanned_bytes": matches.scanned_bytes,
            "matched_files": len(sources),
            "truncated": matches.truncated,
        }
    else:
        sources = load_bounded_l0_sources(
            user_id,
            result.plan.files,
            max_files=(
                MAX_EPISODIC_L0_FILES if result.plan.archive else MAX_FORENSIC_FILES
            ),
        )
        verification_sources = sources
    if not sources:
        result.status = "abstained"
        result.reason = "The selected files provide no readable content."
        return result

    evidence_messages = _build_forensic_evidence_messages(
        message=message,
        recent_dialogue=recent_dialogue,
        recovery_needs=recovery_needs,
        sources=sources,
    )
    log_llm_prompt(logger, "PREUVE FORENSIQUE L0 (OMBRE)", evidence_messages)
    raw_evidence = provider.complete(messages=evidence_messages, temperature=0.0)
    evidence = _parse_forensic_evidence(raw_evidence, sources=sources)
    # The displayed match must also exist verbatim in an original user turn.
    # This second local check prevents a discontinuous excerpt from becoming
    # an invented continuous quotation. It does not add a model call.
    if evidence["status"] == "found":
        evidence = _parse_forensic_evidence(raw_evidence, sources=verification_sources)
    result.status = evidence["status"]
    result.source_file = evidence["source_file"]
    result.evidence = evidence["evidence"]
    result.reason = evidence["reason"]
    result.confidence = evidence["confidence"]
    result.relation_status = evidence["relation_status"]
    result.conclusion = evidence["conclusion"]
    if result.status == "found":
        result.evidence_context = _exact_quote_context(
            sources[result.source_file], verification_sources[result.source_file], result.evidence
        )
    return result


def attempt_l0_chronology_recovery(
    user_id: str,
    message: str,
    recent_dialogue: str,
    provider: LLMProvider,
) -> ForensicRecovery:
    """Recover one exact, contiguous conversation boundary from bounded L0."""

    catalog = l0_file_catalog(user_id)
    result = ForensicRecovery(attempted=True)
    if not catalog:
        result.status = "abstained"
        result.reason = "No usable L0 journal."
        return result

    plan_messages = _build_chronology_plan_messages(
        message=message,
        recent_dialogue=recent_dialogue,
        catalog=catalog,
    )
    log_llm_prompt(logger, "PLAN CHRONOLOGIQUE L0", plan_messages)
    raw_plan = provider.complete(messages=plan_messages, temperature=0.0)
    result.plan = _parse_forensic_plan(raw_plan, available_files=set(catalog))
    if not result.plan.should_search or not result.plan.files:
        result.status = "abstained"
        result.reason = result.plan.reason or "No defensible time window."
        return result

    sources = load_chronology_l0_sources(user_id, result.plan.files)
    timelines = {
        name: _parse_chronology_turns(content)
        for name, content in sources.items()
    }
    timelines = {name: turns for name, turns in timelines.items() if turns}
    if not timelines:
        result.status = "abstained"
        result.reason = "The selected files provide no readable L0 turn."
        return result

    evidence_messages = _build_chronology_evidence_messages(
        message=message,
        recent_dialogue=recent_dialogue,
        timelines=timelines,
    )
    log_llm_prompt(logger, "PASSAGE CHRONOLOGIQUE L0", evidence_messages)
    raw_evidence = provider.complete(messages=evidence_messages, temperature=0.0)
    evidence = _parse_chronology_evidence(
        raw_evidence,
        sources=sources,
        timelines=timelines,
    )
    result.status = evidence["status"]
    result.source_file = evidence["source_file"]
    result.evidence = evidence["evidence"]
    result.reason = evidence["reason"]
    result.confidence = evidence["confidence"]
    result.relation_status = evidence["relation_status"]
    result.conclusion = evidence["conclusion"]
    return result


def l0_file_catalog(user_id: str) -> dict[str, int]:
    root = MEMORY_ROOT / "levels" / "L0_ground_zero" / "conversations"
    prefix = f"{safe_memory_user_id(user_id)}_"
    return {
        path.name: path.stat().st_size
        for path in sorted(root.glob(f"{prefix}*.md"), reverse=True)
        if path.is_file()
    }


def episodic_archive_catalog() -> list[dict[str, str]]:
    """Return a bounded narrative map that may orient, but never prove, L0 recall."""

    from daemon_core.memory.library_layout import unique_note_locations
    indexed = parse_souvenir_index(
        read_markdown(MEMORY_ROOT / "capsules" / "index.md")
    )
    anchors: list[dict[str, str]] = []
    rendered_chars = 0
    for item in sorted(unique_note_locations(MEMORY_ROOT), key=lambda item: item.name, reverse=True):
        name, path = item.name, item.path
        if not name.startswith("theme/chroniques/"):
            continue
        if len(anchors) >= MAX_EPISODIC_ANCHOR_COUNT:
            break
        try:
            frontmatter, body = parse_souvenir_frontmatter(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            continue
        if frontmatter.get("memory_type") != "episodic_archive":
            continue
        span_start = _temporal_scalar(frontmatter.get("span_start"))
        span_end = _temporal_scalar(frontmatter.get("span_end"))
        if not span_start or not span_end:
            continue
        summary = _episodic_anchor_summary(indexed.get(name, ""), body)
        if not summary:
            continue
        anchor = {
            "archive": name,
            "span_start": span_start,
            "span_end": span_end,
            "summary": summary,
        }
        anchor_chars = len(json.dumps(anchor, ensure_ascii=False))
        if rendered_chars + anchor_chars > MAX_EPISODIC_ANCHOR_CHARS:
            break
        anchors.append(anchor)
        rendered_chars += anchor_chars
    return anchors


def episodic_archive_l0_files(
    archive: str,
    *,
    episodic_anchors: list[dict[str, str]],
    available_files: set[str],
) -> list[str]:
    """Map one selected episode to every overlapping daily L0 journal."""

    anchor = next(
        (item for item in episodic_anchors if item.get("archive") == archive),
        None,
    )
    if anchor is None:
        return []
    try:
        span_start = datetime.fromisoformat(anchor["span_start"])
        span_end = datetime.fromisoformat(anchor["span_end"])
    except (KeyError, TypeError, ValueError):
        return []
    if span_end <= span_start:
        return []

    selected: list[tuple[datetime, str]] = []
    for name in available_files:
        match = re.search(r"_(\d{4}-\d{2}-\d{2})\.md$", name)
        if match is None:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        day_start = datetime.combine(day, time.min, tzinfo=span_start.tzinfo)
        day_end = day_start + timedelta(days=1)
        if day_start < span_end and day_end > span_start:
            selected.append((day_start, name))
    if len(selected) > MAX_EPISODIC_L0_FILES:
        return []
    return [name for _day, name in sorted(selected)]


def _temporal_scalar(value: object) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    rendered = isoformat() if callable(isoformat) else str(value)
    return rendered.strip()


def _episodic_anchor_summary(index_line: str, body: str) -> str:
    summary = index_line.partition(":")[2].strip() if index_line else ""
    if not summary:
        paragraphs = [
            " ".join(part.split())
            for part in body.split("\n\n")
            if part.strip() and not part.lstrip().startswith("#")
        ]
        summary = paragraphs[0] if paragraphs else ""
    return summary[:MAX_EPISODIC_ANCHOR_SUMMARY_CHARS].strip()


def load_bounded_l0_sources(
    user_id: str,
    names: list[str],
    *,
    max_files: int = MAX_FORENSIC_FILES,
) -> dict[str, str]:
    root = MEMORY_ROOT / "levels" / "L0_ground_zero" / "conversations"
    prefix = f"{safe_memory_user_id(user_id)}_"
    remaining = MAX_FORENSIC_TOTAL_CHARS
    sources: dict[str, str] = {}
    selected_names = names[:max_files]
    for index, name in enumerate(selected_names):
        if not name.startswith(prefix) or Path(name).name != name:
            continue
        path = root / name
        if not path.is_file() or remaining <= 0:
            continue
        content = _user_l0_turns(path.read_text(encoding="utf-8"))
        files_left = len(selected_names) - index
        limit = min(MAX_FORENSIC_FILE_CHARS, max(1, remaining // files_left))
        bounded = _bounded_file_content(content, limit)
        if bounded.strip():
            sources[name] = bounded
            remaining -= len(bounded)
    return sources


def _user_l0_turns(content: str) -> str:
    return "\n\n".join(
        content[turn.start : turn.end].strip()
        for turn in _parse_chronology_turns(content)
        if turn.role == "user"
    )


def load_chronology_l0_sources(user_id: str, names: list[str]) -> dict[str, str]:
    """Load at most two complete daily journals under an explicit local bound."""

    root = MEMORY_ROOT / "levels" / "L0_ground_zero" / "conversations"
    prefix = f"{safe_memory_user_id(user_id)}_"
    sources: dict[str, str] = {}
    for name in names[:MAX_FORENSIC_FILES]:
        if not name.startswith(prefix) or Path(name).name != name:
            continue
        path = root / name
        if (
            not path.is_file()
            or path.stat().st_size > MAX_CHRONOLOGY_SOURCE_CHARS
        ):
            continue
        content = path.read_text(encoding="utf-8")
        if content.strip():
            sources[name] = content
    return sources


def _build_forensic_plan_messages(
    message: str,
    recent_dialogue: str,
    recovery_needs: list[RecoveryNeed],
    catalog: dict[str, int],
    episodic_anchors: list[dict[str, str]] | None = None,
) -> list[dict]:
    system = f"""You plan an exceptional forensic search in a person's raw L0 journal.

Normal recall and a second navigation through souvenirs have already failed. Choose an episodic archive, at most {MAX_FORENSIC_FILES} justified daily files, or one bounded text search when the period is known but the day of conversation is not. A file is one day of conversation, not a semantic category.

Principles:
- L0 is a safety net, never a diffuse search through an entire life.
- An explicit date, a datable event or a recent episode can justify a small window.
- The reference suggested by the voice is a navigation hypothesis, never a reliable bound. It may be wrong and must not exclude contradictory clues.
- Episodic archives are a narrative table of contents leading to L0, never evidence of the requested fact. Use their bounds and summaries only to choose which journals to open; only an exact user quotation in L0 can subsequently establish or refute the fact.
- When the sought fact is attached to a situation or an episode boundary, choose the archive describing that situation. An archive sharing only the fact's theme does not suffice to locate it.
- If an archive reasonably identifies the episode but the exact day remains uncertain, select its path in `archive` and leave `files` empty. The runtime will open its bounded range. Do not invent a day from the archive's name.
- If an exact day is already established independently of the archives, leave `archive` empty and select the file or files directly.
- When a bounded period is established but no exact day or archive is available, set `search` to an inclusive ISO date range of at most 366 days and 1–3 short literal terms locating the topic. The terms are ANDed within a few adjacent turns; use names or phrases already supplied, never guess the missing answer. Search terms may locate an assistant mention, but only a subsequent exact user quotation can establish the fact.
- A text search returns at most 12 matched passages from 8 files, within the existing 40,000-character evidence budget. It is not exhaustive. Choose either search, files or archive, never combine them.
- Never choose arbitrary dates for an old preference whose period is unknown.
- When in doubt, abstain: `should_search=false`.
- Propose only names appearing exactly in the catalog.
- Respond in strict JSON, without Markdown.

Schema:
{{"should_search":false,"archive":"","files":[],"search":null,"reason":"short reason","confidence":0.0}}
`search`, when used: {{"start":"YYYY-MM-DD","end":"YYYY-MM-DD","terms":["known literal topic"]}}"""
    user = f"""Current time:
{current_time_context()}

Current message:
{message}

Recent dialogue:
{recent_dialogue[-4_000:] or "None."}

Pieces still missing after consulting souvenirs:
{json.dumps([asdict(item) for item in recovery_needs], ensure_ascii=False, indent=2)}

Available L0 catalog (file name and size in bytes):
{json.dumps(catalog, ensure_ascii=False, indent=2)}

Episodic archives available to guide the window (index, not evidence):
{json.dumps(episodic_anchors or [], ensure_ascii=False, indent=2)}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _build_chronology_plan_messages(
    message: str,
    recent_dialogue: str,
    catalog: dict[str, int],
) -> list[dict]:
    system = f"""You bound an exceptional chronological retrieval in a person's raw L0 journal.

The request concerns the actual order of conversation: the topic immediately
before or after an episode, a resumption, a branching point or an identifiable
period. Thematic souvenirs cannot prove this adjacency. Choose at most
{MAX_FORENSIC_FILES} daily files only if the message provides a defensible
temporal or conversational window.

Principles:
- A file represents one day of conversation, not a theme.
- Prefer the narrowest window consistent with the request.
- Invent no date and do not turn thematic resemblance into chronological
  proximity.
- When in doubt, abstain: `should_search=false`.
- Propose only names appearing exactly in the catalog.
- Respond in strict JSON, without Markdown.

Schema:
{{"should_search":false,"files":[],"reason":"short reason","confidence":0.0}}"""
    user = f"""Current time:
{current_time_context()}

Sought chronological relationship:
{message}

Recent dialogue:
{recent_dialogue[-6_000:] or "None."}

Available L0 catalog (file name and size in bytes):
{json.dumps(catalog, ensure_ascii=False, indent=2)}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _build_forensic_evidence_messages(
    message: str,
    recent_dialogue: str,
    recovery_needs: list[RecoveryNeed],
    sources: dict[str, str],
) -> list[dict]:
    system = """You verify a memory item in a bounded excerpt of the raw L0 journal.

Valid evidence is an exact, continuous quotation from a `user` turn that explicitly covers the sought knowledge with correct attribution. An assistant response, an inference, thematic proximity or mere absence of contradiction is never evidence.

Excerpts may be non-contiguous text matches. Adjacent assistant turns can clarify what a pronoun refers to, but cannot establish a personal fact or supply a missing value. A bounded search is not exhaustive; no match never establishes absence. Respect both the conversation date and any explicitly stated period of validity.

You must separate finding the quotation from the verdict on the requested relationship:
- `status=found` means an exact, directly relevant user quotation was retrieved, even if it corrects or refutes the submitted wording;
- `relation_status=established` if the quotation directly establishes the entire requested item;
- `relation_status=not_established` if the relevant quotation establishes a different reality and therefore cannot confirm the submitted wording;
- `relation_status=ambiguous` if the relevant quotation leaves several readings possible.

`conclusion` states only what the quotation actually allows you to say. A mention of the same event with a different place, time, person or action must be returned as `found` with `not_established`, never hidden in the reason for a `not_found`.

If no directly relevant quotation exists, respond `not_found`. Never assemble several fragments or complete a quotation. Respond in strict JSON, without Markdown.

Schema:
{"status":"found|not_found","source_file":"exact name or empty","evidence":"exact quotation or empty","relation_status":"established|not_established|ambiguous","conclusion":"bounded conclusion or empty","reason":"short internal diagnostic","confidence":0.0}"""
    source_text = "\n\n".join(
        f"<l0 source={json.dumps(name, ensure_ascii=False)}>\n{content}\n</l0>"
        for name, content in sources.items()
    )
    user = f"""Current message:
{message}

Recent dialogue:
{recent_dialogue[-3_000:] or "None."}

Sought items:
{json.dumps([asdict(item) for item in recovery_needs], ensure_ascii=False, indent=2)}

L0 excerpts:
{source_text}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _build_chronology_evidence_messages(
    message: str,
    recent_dialogue: str,
    timelines: dict[str, list[ChronologyTurn]],
) -> list[dict]:
    system = f"""You locate a chronological relationship in a bounded catalog of L0 turns.

The catalog is a discovery index, not the final evidence. Identify a single
continuous range of at most {MAX_CHRONOLOGY_EVIDENCE_TURNS} turns that precisely
answers the requested relationship. The range must include the sought topic
and, for "immediately before" or "immediately after", the boundary actually
establishing that order.

If the same topic or reference appears in several episodes, attach the request
to the nearest pivot continuing the recent dialogue. An older mention does
not constitute the beginning of the current thread if other topics intervened
and a new pivot then reopened that reference. If the recent dialogue does not
allow an honest choice between several episodes, respond `not_found` instead
of selecting one arbitrarily.

You must separate finding the range from answering the relationship:
- `established` if the range directly establishes the requested relationship;
- `not_established` if the relevant range does not prove the assertion submitted
  for verification;
- `ambiguous` if several readings remain possible.
`conclusion` states in one sentence what the range actually allows you to say,
without filling gaps or turning one stage into another. For a question such
as "did I say X?", mentioning X in relation to a different action does not
confirm the assertion.

Do not summarize or complete excerpts, and do not choose merely thematic
proximity. If the catalog is insufficient, respond `not_found`. Copy the
displayed file name and turn identifiers exactly. Respond in strict JSON,
without Markdown.

Schema:
{{"status":"found|not_found","source_file":"exact name or empty","start_turn":"turn-0001 or empty","end_turn":"turn-0002 or empty","relation_status":"established|not_established|ambiguous","conclusion":"bounded conclusion or empty","reason":"short reason","confidence":0.0}}"""
    user = f"""Sought chronological relationship:
{message}

Recent dialogue:
{recent_dialogue[-4_000:] or "None."}

Chronological catalog:
{_render_chronology_catalog(timelines)}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_forensic_plan(
    raw: str,
    available_files: set[str],
    available_archives: set[str] | None = None,
    *,
    allow_text_query: bool = False,
) -> ForensicPlan:
    data = json.loads(_strip_json_fence(raw))
    if not isinstance(data, dict):
        raise ValueError("The forensic plan must be a JSON object.")
    files = data.get("files", [])
    selected = []
    if isinstance(files, list):
        selected = [
            item
            for item in files
            if isinstance(item, str) and item in available_files
        ][:MAX_FORENSIC_FILES]
    archive = data.get("archive") if isinstance(data.get("archive"), str) else ""
    if archive not in (available_archives or set()):
        archive = ""
    if archive:
        selected = []
    text_query = (parse_text_query(data.get("search"))
                  if allow_text_query and not selected and not archive else None)
    confidence = data.get("confidence", 0.0)
    return ForensicPlan(
        should_search=(
            data.get("should_search") is True and bool(selected or archive or text_query)
        ),
        files=list(dict.fromkeys(selected)),
        archive=archive,
        text_query=text_query,
        reason=_clean_text(data.get("reason"), 500),
        confidence=_confidence(confidence),
    )


def _parse_forensic_evidence(raw: str, sources: dict[str, str]) -> dict:
    data = json.loads(_strip_json_fence(raw))
    if not isinstance(data, dict):
        raise ValueError("Forensic evidence must be a JSON object.")
    source = data.get("source_file") if isinstance(data.get("source_file"), str) else ""
    evidence = _clean_text(data.get("evidence"), MAX_EVIDENCE_CHARS)
    if data.get("status") != "found":
        return {
            "status": "not_found",
            "source_file": "",
            "evidence": "",
            "reason": _clean_text(data.get("reason"), 500),
            "confidence": _confidence(data.get("confidence")),
            "relation_status": "not_established",
            "conclusion": "",
        }
    if source not in sources or not evidence or not _quote_in_user_turns(sources[source], evidence):
        return {
            "status": "invalid_evidence",
            "source_file": source if source in sources else "",
            "evidence": "",
            "reason": "The proposed quotation is not an exact quotation from a visible user turn.",
            "confidence": 0.0,
            "relation_status": "ambiguous",
            "conclusion": "",
        }
    relation_status = data.get("relation_status")
    conclusion = _clean_text(data.get("conclusion"), 500)
    if (
        relation_status not in {"established", "not_established", "ambiguous"}
        or not conclusion
    ):
        return {
            "status": "invalid_evidence",
            "source_file": source,
            "evidence": "",
            "reason": "The verdict associated with the exact quotation is invalid.",
            "confidence": 0.0,
            "relation_status": "ambiguous",
            "conclusion": "",
        }
    return {
        "status": "found",
        "source_file": source,
        "evidence": evidence,
        "reason": _clean_text(data.get("reason"), 500),
        "confidence": _confidence(data.get("confidence")),
        "relation_status": relation_status,
        "conclusion": conclusion,
    }


def _parse_chronology_evidence(
    raw: str,
    *,
    sources: dict[str, str],
    timelines: dict[str, list[ChronologyTurn]],
) -> dict:
    data = json.loads(_strip_json_fence(raw))
    if not isinstance(data, dict):
        raise ValueError("Chronological evidence must be a JSON object.")
    if data.get("status") != "found":
        return {
            "status": "not_found",
            "source_file": "",
            "evidence": "",
            "reason": _clean_text(data.get("reason"), 500),
            "confidence": _confidence(data.get("confidence")),
            "relation_status": "not_established",
            "conclusion": "",
        }

    source = data.get("source_file")
    start_turn = data.get("start_turn")
    end_turn = data.get("end_turn")
    if (
        not isinstance(source, str)
        or source not in sources
        or not isinstance(start_turn, str)
        or not isinstance(end_turn, str)
    ):
        return _invalid_chronology_evidence()

    turns = timelines.get(source, [])
    positions = {turn.turn_id: index for index, turn in enumerate(turns)}
    start_index = positions.get(start_turn)
    end_index = positions.get(end_turn)
    if (
        start_index is None
        or end_index is None
        or start_index > end_index
        or end_index - start_index + 1 > MAX_CHRONOLOGY_EVIDENCE_TURNS
    ):
        return _invalid_chronology_evidence(source)

    first = turns[start_index]
    last = turns[end_index]
    evidence = sources[source][first.start:last.end].strip()
    if not evidence or len(evidence) > MAX_CHRONOLOGY_EVIDENCE_CHARS:
        return _invalid_chronology_evidence(source)
    relation_status = data.get("relation_status")
    conclusion = _clean_text(data.get("conclusion"), 500)
    if (
        relation_status not in {"established", "not_established", "ambiguous"}
        or not conclusion
    ):
        return _invalid_chronology_evidence(source)
    return {
        "status": "found",
        "source_file": source,
        "evidence": evidence,
        "reason": _clean_text(data.get("reason"), 500),
        "confidence": _confidence(data.get("confidence")),
        "relation_status": relation_status,
        "conclusion": conclusion,
    }


def _invalid_chronology_evidence(source: str = "") -> dict:
    return {
        "status": "invalid_evidence",
        "source_file": source,
        "evidence": "",
        "reason": (
            "The proposed span is not a continuous, exact, bounded L0 sequence."
        ),
        "confidence": 0.0,
        "relation_status": "ambiguous",
        "conclusion": "",
    }


def _exact_quote_context(visible: str, original: str, quote: str) -> str:
    """Keep dated speaker context without splicing disjoint matches or judging it."""
    turns = _parse_chronology_turns(visible)
    matches = [i for i, turn in enumerate(turns) if turn.role == "user" and quote in turn.body]
    if len(matches) != 1:
        return ""
    i = matches[0]
    first, last = max(0, i - 2), min(len(turns) - 1, i + 1)
    context = visible[turns[first].start:turns[last].end].strip()
    if len(context) > MAX_EVIDENCE_CONTEXT_CHARS or context not in original:
        return ""
    return context


def _quote_in_user_turns(content: str, quote: str) -> bool:
    role = ""
    lines: list[str] = []
    for line in content.splitlines():
        if line.startswith("## ") and " — " in line:
            if role == "user" and quote in "\n".join(lines).strip():
                return True
            role = line.rsplit(" — ", 1)[-1].strip().casefold()
            lines = []
        elif role:
            lines.append(line)
    return role == "user" and quote in "\n".join(lines).strip()


def _parse_chronology_turns(content: str) -> list[ChronologyTurn]:
    matches = list(_TURN_HEADER.finditer(content))
    turns: list[ChronologyTurn] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        timestamp = match.group(1).strip()
        role = match.group(2).strip().casefold()
        turns.append(
            ChronologyTurn(
                turn_id=f"turn-{index + 1:04d}",
                locator=f"{timestamp} — {role}",
                role=role,
                body=content[match.end():end].strip(),
                start=match.start(),
                end=end,
            )
        )
    return turns


def _render_chronology_catalog(
    timelines: dict[str, list[ChronologyTurn]],
) -> str:
    per_file = max(1, MAX_CHRONOLOGY_CATALOG_TURNS // max(1, len(timelines)))
    sections: list[str] = []
    for name, turns in timelines.items():
        lines = [f"## {name}"]
        for turn in turns[-per_file:]:
            excerpt = " ".join(turn.body.split())[:100]
            lines.append(
                f"- {turn.turn_id} | {turn.locator} | {excerpt or '[empty turn]'}"
            )
        sections.append("\n".join(lines))
    rendered = "\n\n".join(sections)
    if len(rendered) > MAX_CHRONOLOGY_CATALOG_CHARS:
        raise ValueError(
            "L0 chronology catalog exceeds its explicit bound "
            f"({len(rendered)} > {MAX_CHRONOLOGY_CATALOG_CHARS} chars)"
        )
    return rendered or "No turn."


def _bounded_file_content(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    half = max(1, (limit - 80) // 2)
    return content[:half] + "\n\n[... middle omitted: forensic bound ...]\n\n" + content[-half:]


def _confidence(value: object) -> float:
    return max(0.0, min(float(value), 1.0)) if isinstance(value, int | float) else 0.0


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip()


def _strip_json_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()
    return text
