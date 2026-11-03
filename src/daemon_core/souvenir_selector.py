import json
import logging
from dataclasses import asdict, dataclass

from daemon_core.memory.planning import MemoryPlan
from daemon_core.memory.reader import souvenir_domain_names, list_available_souvenirs, list_souvenirs_for_metadata, list_zoom_souvenirs, load_domain_souvenir_index, load_named_souvenir_index, load_root_souvenir_index
from daemon_core.prompt_composer import current_time_context
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider

logger = logging.getLogger("daemon_core.prompts")
MAX_MEMORY_PLAN_CANDIDATES = 36


class SouvenirSelectionUnavailable(RuntimeError):
    """A maintenance source could not be resolved; retry before writing."""


@dataclass
class SouvenirSelection:
    selected: list[str]
    reason: str
    confidence: float = 0.0
    source: str = "llm"
    ambiguity: str = "none"


def select_souvenirs_minimal() -> SouvenirSelection:
    return SouvenirSelection(
        selected=[],
        reason="LLM selection unavailable. No thematic souvenir selected.",
        confidence=0.0,
        source="minimal",
    )


def select_souvenirs(
    message: str,
    provider: LLMProvider,
    recent_dialogue: str = "",
    user_context: str = "",
    user_preferences: str = "",
    user_agenda: str = "",
    moment_memory: str = "",
    current_discussion: str = "",
    max_souvenirs: int = 4,
    *,
    require_success: bool = False,
) -> SouvenirSelection:
    available = list_available_souvenirs()
    if not available:
        return SouvenirSelection(selected=[], reason="No souvenir available.", confidence=0.0, source="none")

    selectable = sorted(set(available) | souvenir_domain_names())
    try:
        messages = _build_selection_messages(message, load_root_souvenir_index(), recent_dialogue, user_context, user_preferences, user_agenda, moment_memory, current_discussion, max_souvenirs)
        log_llm_prompt(logger, "SÉLECTION SOUVENIRS", messages)
        raw = provider.complete(messages=messages, temperature=0.0)
        selection = _parse_llm_selection(raw, selectable, max_souvenirs=max_souvenirs, require_success=require_success)
    except Exception as exc:
        if require_success:
            raise SouvenirSelectionUnavailable("Souvenir selection unavailable") from exc
        fallback = select_souvenirs_minimal()
        fallback.reason = f"{fallback.reason} Error: {exc.__class__.__name__}."
        return fallback
    return expand_domain_selection(
        selection,
        message=message,
        provider=provider,
        recent_dialogue=recent_dialogue,
        user_context=user_context,
        user_preferences=user_preferences,
        user_agenda=user_agenda,
        moment_memory=moment_memory,
        current_discussion=current_discussion,
        max_souvenirs=max_souvenirs,
        require_success=require_success,
    )


def expand_domain_selection(
    selection: SouvenirSelection,
    message: str,
    provider: LLMProvider,
    recent_dialogue: str = "",
    user_context: str = "",
    user_preferences: str = "",
    user_agenda: str = "",
    moment_memory: str = "",
    current_discussion: str = "",
    max_souvenirs: int = 4,
    *,
    require_success: bool = False,
) -> SouvenirSelection:
    """Expand selected domains once, then select their notes from the detailed view."""
    domains = [name for name in selection.selected if name in souvenir_domain_names()]
    if not domains:
        return selection
    selectable = list_zoom_souvenirs(selection.selected)
    try:
        messages = _build_selection_messages(
            message, load_domain_souvenir_index(selection.selected), recent_dialogue,
            user_context, user_preferences, user_agenda, moment_memory,
            current_discussion, max_souvenirs, final_zoom=True,
        )
        log_llm_prompt(logger, "ZOOM DOMAINES", messages)
        raw = provider.complete(messages=messages, temperature=0.0)
        zoomed = _parse_llm_selection(raw, selectable, max_souvenirs=max_souvenirs, require_success=require_success)
    except Exception as exc:
        if require_success:
            raise SouvenirSelectionUnavailable("Souvenir domain zoom unavailable") from exc
        # Historical voice fallback; an aggregate domain need not have a body.
        selection.reason = f"{selection.reason} Zoom unavailable ({exc.__class__.__name__}): domain maps retained as they are."
        return selection
    return SouvenirSelection(
        selected=zoomed.selected,
        reason=f"{selection.reason} Zoom {', '.join(domains)}: {zoomed.reason}",
        confidence=zoomed.confidence,
        source=f"{selection.source}+zoom",
        ambiguity="material" if "material" in (selection.ambiguity, zoomed.ambiguity) else "none",
    )


def select_souvenirs_for_memory_plan(
    plan: MemoryPlan,
    seed_candidates: list[str],
    message: str,
    provider: LLMProvider,
    recent_dialogue: str = "",
    user_context: str = "",
    user_preferences: str = "",
    user_agenda: str = "",
    moment_memory: str = "",
    current_discussion: str = "",
    max_souvenirs: int = 4,
) -> SouvenirSelection | None:
    """Compose tree and metadata views, then judge all memory needs together."""
    if not plan.facets:
        return None
    projected = list_souvenirs_for_metadata(
        subjects=plan.subjects,
        facets=plan.facets,
    )
    if not projected:
        return None

    # The tree takes precedence: the full subtree of radar candidates remains
    # selectable. Topic/facet projection adds cross-links but never removes
    # paths. Different facet labels must not hide a leaf from the correct
    # domain (regression observed 2026-07-10). The mismatch
    # in person or facet is resolved by the final judgment using the plan.
    tree_candidates = list_zoom_souvenirs(seed_candidates) if seed_candidates else []
    selectable = list(dict.fromkeys([*tree_candidates, *projected]))
    if len(selectable) > MAX_MEMORY_PLAN_CANDIDATES:
        logger.info(
            "Projection MemoryPlan trop large, repli arbre | candidates=%s | subjects=%s | facets=%s",
            len(selectable),
            plan.subjects,
            plan.facets,
        )
        return None
    try:
        messages = _build_selection_messages(
            message=message,
            souvenir_catalog=load_named_souvenir_index(selectable),
            recent_dialogue=recent_dialogue,
            user_context=user_context,
            user_preferences=user_preferences,
            user_agenda=user_agenda,
            moment_memory=moment_memory,
            current_discussion=current_discussion,
            max_souvenirs=max_souvenirs,
            final_zoom=True,
            memory_plan=plan,
        )
        log_llm_prompt(logger, "SÉLECTION MULTI-BESOINS", messages)
        raw = provider.complete(messages=messages, temperature=0.0)
        selection = _parse_llm_selection(
            raw,
            selectable,
            max_souvenirs=max_souvenirs,
        )
    except Exception as exc:
        logger.info(
            "Sélection MemoryPlan indisponible, repli arbre | error=%s",
            exc.__class__.__name__,
        )
        return None
    selection.source = "memory-plan"
    selection.reason = f"Plan multidimensionnel: {selection.reason}"
    return selection


def _build_selection_messages(
    message: str,
    souvenir_catalog: str,
    recent_dialogue: str = "",
    user_context: str = "",
    user_preferences: str = "",
    user_agenda: str = "",
    moment_memory: str = "",
    current_discussion: str = "",
    max_souvenirs: int = 4,
    final_zoom: bool = False,
    memory_plan: MemoryPlan | None = None,
) -> list[dict]:
    zoom_instruction = (
        'You are at the final zoom: no further level will refine your selection. When a specific descendant souvenir directly matches the scene, choose it rather than its parent domain. Retain the parent only if its own general facts genuinely are the best source. If the parent provides the general landscape but a descendant contains a confirmed fact directly addressing the quality requested by the user, select both.'
        if final_zoom
        else
        'You are at the first level: a domain may be selected when useful memory lies in its subtree; a subsequent zoom will refine it.'
    )
    system = """You are the Daemon Harness souvenir micro-selector.

A souvenir is not a user-profile category.
It is a small contextual tool activated to better understand the present scene.

Your mission:
1. Read the latest message, user context and recent thread.
2. Understand what is unfolding.
3. Establish the memory coverage required for an accurate, personal response.
4. Choose only souvenirs providing that coverage.

Selection level: __ZOOM_INSTRUCTION__

Rules:
- Do not build a general taxonomy of the user.
- Do not select a souvenir "just in case".
- Select for coverage, not resemblance: when the response must be accurate for the person, gather its complementary supporting sources — durable domain preferences, current context, experiences that genuinely change the choice — even if the final response will be brief.
- Rank sources: confirmed personal memory always takes precedence over an old daemon suggestion or non-personal content.
- At the final zoom, cover all relevant facets of the requested domain within the available limit; do not stop at the first souvenir resembling the message.
- When a memory needs plan is supplied, check each required need and its subject before finalizing. A souvenir about the wrong person does not cover the need, even if its theme is close.
- For an open creative request ("surprise me", "tell me something", "take my mind off things"), memory is a palette, not a response: select taste and passion souvenirs providing personal material for the response.
- Chronicles (`theme/chroniques`) are cold episodic memory: they retrieve a past episode, the origin of a decision, a period or a development. Never select them to answer a question about agenda, time, task, current state, current preference or current operational fact.
- For a reference to the lived past ("last week", "remember when", "the week when…", a past episode), select the corresponding chronicle in `theme/chroniques` — it is a past archive, not a source of current truth.
- Do not choose more than {max_souvenirs} souvenirs unless strongly necessary.
- Use only names present in the catalog.
- Entries marked `[domain: N souvenirs]` group an entire souvenir subtree: select the domain when useful memory lies in its scope; a subsequent zoom on that domain will distinguish its souvenirs.
- An allusion may pass through a concrete object, purchase, garment, brand, gift, work or cultural reference; then select the souvenir carrying the primary scope, even if the link is indirect.
- Requests about times, schedules, temporal reminders, "tonight", "today", "tomorrow", "Friday" or picking up a loved one must account for current time and the short agenda.
- If several souvenirs represent plausible competing events, routines or facts leading to different responses, select them together and indicate `material`.
- To distinguish interpretations, consider the day, time, event, history and immediate focus. Matching words is insufficient.
- `ambiguity` is `material` only if the scene does not yet allow choosing between several plausible responses. Otherwise use `none`.
- If no thematic souvenir is useful, choose an empty list.
- Do not select a style or communication-preference souvenir: this information is already injected outside souvenirs.
- Never choose "index".
- Respond in strict JSON, without Markdown.

JSON schema:
{"selected":["souvenir_name"],"ambiguity":"none","reason":"short reason","confidence":0.0}
"""
    system = system.replace("__ZOOM_INSTRUCTION__", zoom_instruction).replace(
        "{max_souvenirs}", str(max_souvenirs)
    )
    plan_text = (
        json.dumps(asdict(memory_plan), ensure_ascii=False, indent=2)
        if memory_plan is not None
        else 'No explicit memory plan.'
    )
    user = f"""Current time:
{current_time_context()}

User message:
{message}

Recent L0 Ground Zero:
{recent_dialogue[-2200:]}

User Context:
{user_context or "No explicit user context."}

Preferences:
{user_preferences or "No explicit preference."}

Short agenda:
{user_agenda or "No explicit short agenda."}

Active context:
{moment_memory or "No explicit active context."}

Immediate focus:
{current_discussion or "No explicit immediate focus."}

Memory needs plan:
{plan_text}

Available souvenir index:
{souvenir_catalog}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_llm_selection(raw: str, available: list[str], max_souvenirs: int = 4, *, require_success: bool = False) -> SouvenirSelection:
    data = json.loads(_strip_json_fence(raw))
    available_set = set(available)
    if require_success and "selected" not in data:
        raise ValueError("Selection is missing")
    selected = data.get("selected", [])
    if not isinstance(selected, list):
        raise ValueError("selected must be a list")

    cleaned = []
    for name in selected:
        if require_success and (not isinstance(name, str) or name not in available_set):
            raise ValueError("Selected souvenir is not available")
        if isinstance(name, str) and name in available_set and name not in cleaned:
            cleaned.append(name)

    reason = data.get("reason", "Structured LLM selection.")
    if not isinstance(reason, str):
        reason = "Structured LLM selection."

    confidence = data.get("confidence", 0.0)
    if not isinstance(confidence, int | float):
        confidence = 0.0
    ambiguity = data.get("ambiguity", "none")
    if ambiguity not in {"none", "material"}:
        ambiguity = "none"

    return SouvenirSelection(
        selected=cleaned[:max_souvenirs],
        reason=reason,
        confidence=max(0.0, min(float(confidence), 1.0)),
        source="llm",
        ambiguity=ambiguity,
    )


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
