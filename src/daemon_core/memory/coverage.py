from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from daemon_core.memory.integrity import memory_file_lock
from daemon_core.memory.planning import MemoryNeed, MemoryPlan
from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.paths import MEMORY_ROOT
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider


logger = logging.getLogger("daemon_core.memory_coverage")

MAX_SOURCE_CHARS = 4_000
MAX_TOTAL_SOURCE_CHARS = 18_000
MAX_UNPLANNED_NEEDS = 6
MAX_ATTRIBUTION_ISSUES = 6
MAX_RECOVERY_NEEDS = 6
MAX_RECOVERY_SOUVENIRS = 3
_shadow_slot = threading.BoundedSemaphore(value=1)


@dataclass
class NeedCoverage:
    need_index: int
    status: str
    sources: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class MemoryCoverage:
    overall_status: str
    plan_status: str
    needs: list[NeedCoverage] = field(default_factory=list)
    unplanned_needs: list[MemoryNeed] = field(default_factory=list)
    attribution_issues: list[str] = field(default_factory=list)
    recovery_needs: list[RecoveryNeed] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0


@dataclass
class RecoveryNeed:
    """A semantic target for one complementary recall pass."""

    description: str
    subjects: list[str] = field(default_factory=list)
    facets: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: object) -> RecoveryNeed | None:
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            description = _clean_text(raw, 500)
            return cls(description=description) if description else None
        if not isinstance(raw, dict):
            return None
        description = _clean_text(raw.get("description"), 500)
        if not description:
            return None
        return cls(
            description=description,
            subjects=_clean_text_list(raw.get("subjects"), 6, 120),
            facets=_clean_text_list(raw.get("facets"), 8, 120),
        )


@dataclass
class MemoryRecovery:
    """One bounded second recall pass, guided by the exact missing pieces."""

    attempted: bool = False
    guided_by: list[RecoveryNeed] = field(default_factory=list)
    souvenirs_found: list[str] = field(default_factory=list)
    status_before: str = ""
    status_after: str = ""
    reason: str = ""
    coverage_after: MemoryCoverage | None = None


def memory_coverage_shadow_enabled() -> bool:
    value = os.getenv("DAEMON_CORE_MEMORY_COVERAGE_SHADOW", "false")
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def memory_recovery_shadow_enabled() -> bool:
    value = os.getenv("DAEMON_CORE_MEMORY_RECOVERY_SHADOW", "false")
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def memory_forensic_shadow_enabled() -> bool:
    value = os.getenv("DAEMON_CORE_MEMORY_FORENSIC_SHADOW", "false")
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def assess_memory_coverage(
    message: str,
    recent_dialogue: str,
    plan: MemoryPlan,
    sources: dict[str, str],
    provider: LLMProvider,
) -> MemoryCoverage:
    # The current turn reaches the voice directly. Treating a fact introduced
    # now as missing memory creates pointless recovery attempts for information
    # that is already available to answer this message.
    normalized_sources = _normalized_sources(
        {"current_user_message": message, **sources}
    )
    messages = _build_coverage_messages(
        message=message,
        recent_dialogue=recent_dialogue,
        plan=plan,
        sources=normalized_sources,
    )
    log_llm_prompt(logger, "COUVERTURE MÉMOIRE (OMBRE)", messages)
    raw = provider.complete(messages=messages, temperature=0.0)
    return _parse_coverage(raw, plan=plan, available_sources=set(normalized_sources))


def attempt_memory_recovery(
    message: str,
    recent_dialogue: str,
    plan: MemoryPlan,
    assessment: MemoryCoverage,
    sources: dict[str, str],
    already_selected: list[str],
    provider: LLMProvider,
    souvenir_limit: int = 1_200,
) -> tuple[MemoryRecovery, MemoryCoverage | None]:
    """Run one additional recall pass guided by the exact information gap.

    Read-only: select, load and judge again without writing personal memory.
    The final coverage verdict reports whether the missing material was found."""
    from daemon_core.souvenir_selector import select_souvenirs, select_souvenirs_for_memory_plan
    from daemon_core.memory.planning import MemoryNeed
    from daemon_core.memory.reader import load_souvenir

    recovery_needs = _parse_recovery_needs(assessment.recovery_needs)
    recovery = MemoryRecovery(
        attempted=True,
        guided_by=recovery_needs,
        status_before=assessment.overall_status,
    )
    recovery_subjects = list(
        dict.fromkeys(
            subject
            for need in recovery_needs
            for subject in need.subjects
        )
    )
    recovery_facets = list(
        dict.fromkeys(
            facet
            for need in recovery_needs
            for facet in need.facets
        )
    )
    recovery_plan = MemoryPlan(
        subjects=recovery_subjects or plan.subjects,
        facets=recovery_facets or plan.facets,
        needs=[
            MemoryNeed(description=item.description, subjects=item.subjects)
            for item in recovery_needs
        ],
        intent=plan.intent or "recover the missing memory evidence",
    )
    selection = select_souvenirs_for_memory_plan(
        plan=recovery_plan,
        seed_candidates=[],
        message=message,
        provider=provider,
        recent_dialogue=recent_dialogue,
        user_context=sources.get("user_context", ""),
        user_preferences=sources.get("user_preferences", ""),
        user_agenda=sources.get("user_agenda", ""),
        moment_memory=sources.get("active_context", ""),
        current_discussion=sources.get("current_discussion", ""),
        max_souvenirs=MAX_RECOVERY_SOUVENIRS,
    )
    if selection is None:
        selection = select_souvenirs(
            message=message,
            provider=provider,
            recent_dialogue=recent_dialogue,
            user_context=sources.get("user_context", ""),
            user_preferences=sources.get("user_preferences", ""),
            user_agenda=sources.get("user_agenda", ""),
            moment_memory=sources.get("active_context", ""),
            current_discussion="Memory evidence needed for an accurate response: "
            + " ; ".join(item.description for item in recovery_needs),
            max_souvenirs=MAX_RECOVERY_SOUVENIRS,
        )
    found = [
        name
        for name in (selection.selected if selection else [])
        if name not in set(already_selected)
    ]
    extended = dict(sources)
    for name in found:
        content = load_souvenir(name).strip()
        if content:
            extended[f"capsule:{name}"] = content[:souvenir_limit]
            recovery.souvenirs_found.append(name)
    if not recovery.souvenirs_found:
        recovery.reason = "No new souvenir found by the second pass."
        recovery.status_after = assessment.overall_status
        return recovery, None

    after = assess_memory_coverage(
        message=message,
        recent_dialogue=recent_dialogue,
        plan=plan,
        sources=extended,
        provider=provider,
    )
    recovery.status_after = after.overall_status
    recovery.reason = after.reason
    recovery.coverage_after = after
    return recovery, after


def schedule_memory_coverage_shadow(
    user_id: str,
    message: str,
    recent_dialogue: str,
    plan: MemoryPlan,
    selected_souvenirs: list[str],
    sources: dict[str, str],
    provider: LLMProvider,
    scene_register: str = "",
    interaction_mode: str = "",
) -> bool:
    """Run an observational audit without delaying or changing the reply path."""
    if not memory_coverage_shadow_enabled():
        return False
    if not _shadow_slot.acquire(blocking=False):
        logger.info("Couverture mémoire ombre ignorée | reason=audit déjà en cours")
        return False

    def _run() -> None:
        try:
            assessment = assess_memory_coverage(
                message=message,
                recent_dialogue=recent_dialogue,
                plan=plan,
                sources=sources,
                provider=provider,
            )
            recovery = None
            forensic = None
            if (
                memory_recovery_shadow_enabled()
                and assessment.recovery_needs
                and assessment.overall_status in {"missing", "partial"}
            ):
                recovery, _after = attempt_memory_recovery(
                    message=message,
                    recent_dialogue=recent_dialogue,
                    plan=plan,
                    assessment=assessment,
                    sources=sources,
                    already_selected=selected_souvenirs,
                    provider=provider,
                )
            if (
                memory_forensic_shadow_enabled()
                and recovery is not None
                and recovery.status_after in {"missing", "partial"}
                and recovery.guided_by
            ):
                from daemon_core.memory.forensics import attempt_l0_forensic_recovery

                forensic_result = attempt_l0_forensic_recovery(
                    user_id=user_id,
                    message=message,
                    recent_dialogue=recent_dialogue,
                    recovery_needs=recovery.guided_by,
                    provider=provider,
                )
                forensic = asdict(forensic_result)
            append_memory_coverage_trace(
                user_id=user_id,
                message=message,
                plan=plan,
                selected_souvenirs=selected_souvenirs,
                assessment=assessment,
                recovery=recovery,
                forensic=forensic,
                scene_register=scene_register,
                interaction_mode=interaction_mode,
            )
            logger.info(
                "Couverture mémoire ombre | user_id=%s | plan=%s | overall=%s | missing=%s | attribution=%s | recovery=%s | forensic=%s",
                user_id,
                assessment.plan_status,
                assessment.overall_status,
                [item.description for item in assessment.recovery_needs],
                assessment.attribution_issues,
                f"{recovery.status_before}->{recovery.status_after} via {recovery.souvenirs_found}" if recovery else "off",
                forensic.get("status") if forensic else "off",
            )
        except Exception as exc:
            try:
                append_memory_coverage_trace(
                    user_id=user_id,
                    message=message,
                    plan=plan,
                    selected_souvenirs=selected_souvenirs,
                    assessment=MemoryCoverage(
                        overall_status="error",
                        plan_status="unassessed",
                        reason=f"Audit indisponible: {exc.__class__.__name__}.",
                    ),
                )
            except Exception:
                pass
            logger.warning(
                "Couverture mémoire ombre échouée | user_id=%s | error=%s",
                user_id,
                exc.__class__.__name__,
            )
        finally:
            _shadow_slot.release()

    threading.Thread(
        target=_run,
        daemon=True,
        name="daemon-memory-coverage",
    ).start()
    return True


def memory_coverage_trace_path(user_id: str) -> Path:
    return (
        MEMORY_ROOT
        / "levels"
        / "memory_coverage"
        / f"{safe_memory_user_id(user_id)}.jsonl"
    )


def append_memory_coverage_trace(
    user_id: str,
    message: str,
    plan: MemoryPlan,
    selected_souvenirs: list[str],
    assessment: MemoryCoverage,
    recovery: MemoryRecovery | None = None,
    scene_register: str = "",
    interaction_mode: str = "",
    sync: bool = False,
    forensic: dict | None = None,
) -> Path:
    path = memory_coverage_trace_path(user_id)
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "user_id": user_id,
        "message": message,
        "memory_plan": asdict(plan),
        "selected_capsules": list(selected_souvenirs),
        "coverage": asdict(assessment),
    }
    if scene_register:
        payload["scene_register"] = scene_register
    if interaction_mode:
        payload["interaction_mode"] = interaction_mode
    if sync:
        payload["sync"] = True
    if recovery is not None:
        payload["recovery"] = asdict(recovery)
    if forensic is not None:
        payload["forensic"] = forensic
    path.parent.mkdir(parents=True, exist_ok=True)
    with memory_file_lock(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return path


def build_coverage_sources(
    *,
    user_context: str = "",
    user_preferences: str = "",
    user_agenda: str = "",
    active_context: str = "",
    current_discussion: str = "",
    trajectory: str = "",
    souvenirs: dict[str, str] | None = None,
    souvenir_limit: int = 1_200,
) -> dict[str, str]:
    sources = {
        "user_context": user_context,
        "user_preferences": user_preferences,
        "user_agenda": user_agenda,
        "active_context": active_context,
        "current_discussion": current_discussion,
        "trajectory": trajectory,
    }
    for name, content in (souvenirs or {}).items():
        sources[f"capsule:{name}"] = content[:souvenir_limit]
    return {name: content for name, content in sources.items() if content.strip()}


def _build_coverage_messages(
    message: str,
    recent_dialogue: str,
    plan: MemoryPlan,
    sources: dict[str, str],
) -> list[dict]:
    system = """You are the Daemon Harness memory-coverage judge.

You never respond to the user and do not yet select new souvenirs. You audit the context that actually reached the voice.

Make one judgment in two movements:
1. Check whether the needs plan describes all personal memory required for an accurate response in this scene. A plan may have omitted an essential need.
2. For each planned need, judge whether the supplied sources cover it explicitly and with correct attribution.

Need verdicts:
- `covered`: one or more sources provide the required information and attribute it to the correct subject;
- `partial`: nearby, incomplete, uncertain or only inferred information;
- `missing`: information absent or available only for another subject.

Rules:
- Plausibility is not coverage. Fill nothing in from general memory.
- One person's preference, experience or constraint never covers another's.
- `sources` contains only identifiers present in the supplied dossier.
- `unplanned_needs` contains essential needs omitted by the plan, in natural language. Leave it empty if the plan suffices.
- `current_user_message` is a direct source for the present response. Information the person has just given is covered for this turn and must never trigger retrieval, even if not yet consolidated into durable memory.
- `recovery_needs` describes what a second recall should seek, without suggesting a file or keyword. Each need preserves the subjects concerned and, when known, the plan's exact facets. Include no merely possible detail or optional need whose absence does not affect response accuracy.
- An absent optional need does not on its own require an overall `missing` verdict.
- Do not downgrade a covered need merely because other imaginable details might exist. Judge sufficiency for the actual scene, not a dossier's absolute completeness.
- Remain restrained: this is an audit, not a new response.
- Respond in strict JSON, without Markdown.

Schema:
{"plan_status":"adequate|incomplete","overall_status":"covered|partial|missing","needs":[{"need_index":0,"status":"covered|partial|missing","sources":["source_id"],"reason":"short reason"}],"unplanned_needs":[{"description":"omitted need","subjects":["subject"],"required":true}],"attribution_issues":["short issue"],"recovery_needs":[{"description":"knowledge to retrieve","subjects":["subject"],"facets":["exact facet from the plan"]}],"reason":"short overall verdict","confidence":0.0}
"""
    source_text = "\n\n".join(
        f"<source id={json.dumps(name, ensure_ascii=False)}>\n{content}\n</source>"
        for name, content in sources.items()
    )
    user = f"""Recent scene:
{recent_dialogue[-3_000:] or "No recent history."}

Current message:
{message}

Memory needs plan:
{json.dumps(asdict(plan), ensure_ascii=False, indent=2)}

Sources actually supplied to the voice:
{source_text or "No memory source."}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_coverage(
    raw: str,
    plan: MemoryPlan,
    available_sources: set[str],
) -> MemoryCoverage:
    data = json.loads(_strip_json_fence(raw))
    if not isinstance(data, dict):
        raise ValueError("The coverage verdict must be a JSON object.")

    by_index: dict[int, NeedCoverage] = {}
    raw_needs = data.get("needs", [])
    if isinstance(raw_needs, list):
        for item in raw_needs:
            if not isinstance(item, dict):
                continue
            index = item.get("need_index")
            if not isinstance(index, int) or not 0 <= index < len(plan.needs):
                continue
            status = item.get("status")
            if status not in {"covered", "partial", "missing"}:
                continue
            sources = item.get("sources", [])
            cleaned_sources = []
            if isinstance(sources, list):
                cleaned_sources = [
                    source
                    for source in sources
                    if isinstance(source, str) and source in available_sources
                ]
            by_index[index] = NeedCoverage(
                need_index=index,
                status=status,
                sources=list(dict.fromkeys(cleaned_sources)),
                reason=_clean_text(item.get("reason"), 500),
            )

    needs = [
        by_index.get(
            index,
            NeedCoverage(
                need_index=index,
                status="unassessed",
                reason="The judge returned no verdict for this need.",
            ),
        )
        for index in range(len(plan.needs))
    ]

    unplanned: list[MemoryNeed] = []
    raw_unplanned = data.get("unplanned_needs", [])
    if isinstance(raw_unplanned, list):
        for item in raw_unplanned:
            need = MemoryNeed.from_raw(item)
            if need is not None:
                unplanned.append(need)
            if len(unplanned) >= MAX_UNPLANNED_NEEDS:
                break

    overall_status = data.get("overall_status", "unassessed")
    if overall_status not in {"covered", "partial", "missing"}:
        overall_status = "unassessed"
    plan_status = data.get("plan_status", "unassessed")
    if plan_status not in {"adequate", "incomplete"}:
        plan_status = "unassessed"

    confidence = data.get("confidence", 0.0)
    if not isinstance(confidence, int | float):
        confidence = 0.0
    return MemoryCoverage(
        overall_status=overall_status,
        plan_status=plan_status,
        needs=needs,
        unplanned_needs=unplanned,
        attribution_issues=_clean_text_list(
            data.get("attribution_issues"), MAX_ATTRIBUTION_ISSUES, 500
        ),
        recovery_needs=_parse_recovery_needs(data.get("recovery_needs")),
        reason=_clean_text(data.get("reason"), 800),
        confidence=max(0.0, min(float(confidence), 1.0)),
    )


def _normalized_sources(sources: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    remaining = MAX_TOTAL_SOURCE_CHARS
    for name, content in sources.items():
        if not isinstance(name, str) or not isinstance(content, str):
            continue
        name = " ".join(name.split())[:200]
        content = content.strip()
        if not name or not content or remaining <= 0:
            continue
        clipped = content[: min(MAX_SOURCE_CHARS, remaining)]
        normalized[name] = clipped
        remaining -= len(clipped)
    return normalized


def _clean_text(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_chars].strip()


def _clean_text_list(value: object, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = _clean_text(item, max_chars)
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= max_items:
            break
    return cleaned


def _parse_recovery_needs(value: object) -> list[RecoveryNeed]:
    if not isinstance(value, (list, tuple)):
        return []
    cleaned: list[RecoveryNeed] = []
    for item in value:
        need = RecoveryNeed.from_raw(item)
        if need is None:
            continue
        if any(existing.description.casefold() == need.description.casefold() for existing in cleaned):
            continue
        cleaned.append(need)
        if len(cleaned) >= MAX_RECOVERY_NEEDS:
            break
    return cleaned


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


def coverage_to_voice_enabled() -> bool:
    value = os.getenv("DAEMON_CORE_COVERAGE_TO_VOICE", "false")
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def coverage_voice_gate(scene_register: str, plan: MemoryPlan) -> bool:
    """Gate synchronous coverage to factual-recall scenes.

    The gate limits cost; the dossier's wording independently protects the
    voice from treating incomplete coverage as an established fact.
    Shadow traces include the scene register for calibration."""
    if not plan.needs or not any(need.required for need in plan.needs):
        return False
    return scene_register.strip().casefold() == "pratique"


def build_coverage_voice_dossier(assessment: MemoryCoverage, plan: MemoryPlan) -> str:
    """Build a concise voice dossier; return nothing when coverage is complete."""
    if assessment.overall_status == "covered":
        return ""
    established = []
    uncertain = []
    for need_coverage in assessment.needs:
        if not 0 <= need_coverage.need_index < len(plan.needs):
            continue
        description = plan.needs[need_coverage.need_index].description
        if need_coverage.status == "covered":
            established.append(description)
        elif need_coverage.status in {"partial", "missing"}:
            uncertain.append(description)
    if not uncertain:
        return ""
    lines = []
    if established:
        lines.append("Established by your sources:")
        lines.extend(f"- {item}" for item in established)
        lines.append("")
    lines.append("Uncertain or not found in your sources:")
    lines.extend(f"- {item}" for item in uncertain)
    if assessment.attribution_issues:
        lines.append("")
        lines.append("Attribution to verify:")
        lines.extend(f"- {item}" for item in assessment.attribution_issues)
    lines.append("")
    lines.append(
        "Do not assert anything that relies on uncertain or missing evidence: "
        "simply say what you found and what you did not, without "
        "ceremony. Everything else — your positions, ideas and presence — "
        "is unaffected."
    )
    return "\n".join(lines)
