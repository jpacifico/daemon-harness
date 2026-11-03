import json
import os
from dataclasses import dataclass, field

from daemon_core.memory.planning import MemoryPlan
from daemon_core.memory.reader import souvenir_domain_names, list_available_souvenirs, load_souvenir_index, load_souvenir_metadata_label_catalog, load_root_souvenir_index
from daemon_core.prompt_composer import current_time_context
from daemon_core.providers.base import LLMProvider


RADAR_FINAL_SELECTION_ENV = "DAEMON_CORE_RADAR_FINAL_SELECTION"
SITUATED_CONTEXT_ENV = "DAEMON_CORE_SITUATED_CONTEXT"


@dataclass
class SceneFrame:
    """Decision-relevant scene state extracted by the radar."""

    established: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)
    decision_effect: str = ""
    resolution_mode: str = "direct"

    @classmethod
    def from_raw(cls, raw: object) -> "SceneFrame":
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            return cls()
        return cls(
            established=_clean_scene_items(raw.get("established")),
            uncertain=_clean_scene_items(raw.get("uncertain")),
            decision_effect=_clean_scene_text(raw.get("decision_effect")),
            resolution_mode=_clean_resolution_mode(raw.get("resolution_mode")),
        )


@dataclass
class ContextRoute:
    mode: str
    reason: str
    confidence: float = 0.0
    context_depth: str = ""
    interaction_mode: str = "conversation"
    scene_register: str = "pratique"
    candidate_souvenirs: list[str] = field(default_factory=list)
    active_request: str = ""
    ambiguity: str = "none"
    needs_host_tools: bool = False
    needs_temporal_memory: bool = False
    # Legacy trace compatibility only. The canonical runtime no longer asks
    # for or consumes the Stance Register.
    needs_stance_register: bool = False
    needs_carnet: bool = False
    needs_trajectory: bool = False
    fil_clos: bool = False
    free_initiative_requested: bool = False
    radar_final_souvenirs: list[str] = field(default_factory=list)
    radar_final_complete: bool = False
    radar_final_reason: str = ""
    scene_frame: SceneFrame = field(default_factory=SceneFrame)
    memory_plan: MemoryPlan = field(default_factory=MemoryPlan)

    def __post_init__(self) -> None:
        self.scene_frame = SceneFrame.from_raw(self.scene_frame)
        self.memory_plan = MemoryPlan.from_raw(self.memory_plan)
        if self.needs_carnet and not self.needs_stance_register:
            self.needs_stance_register = True
        self.needs_carnet = self.needs_stance_register


def route_context_depth(
    message: str,
    provider: LLMProvider,
    user_context: str = "",
    user_preferences: str = "",
    user_agenda: str = "",
    moment_memory: str = "",
    current_discussion: str = "",
    recent_dialogue: str = "",
    situated_context: bool | None = None,
) -> ContextRoute:
    available = list_available_souvenirs()
    souvenir_catalog = load_root_souvenir_index() if available else ""
    metadata_catalog = load_souvenir_metadata_label_catalog() if available else ""
    final_selection_mode = os.environ.get(RADAR_FINAL_SELECTION_ENV, "off").strip().lower()
    flat_souvenir_catalog = (
        load_souvenir_index()
        if available and final_selection_mode in {"shadow", "on"}
        else ""
    )
    if situated_context is None:
        situated_context = (
            os.environ.get(SITUATED_CONTEXT_ENV, "off").strip().lower()
            in {"1", "on", "true"}
        )
    messages = _build_radar_route_messages(
        message=message,
        souvenir_catalog=souvenir_catalog,
        user_context=user_context,
        user_preferences=user_preferences,
        user_agenda=user_agenda,
        moment_memory=moment_memory,
        current_discussion=current_discussion,
        recent_dialogue=recent_dialogue,
        metadata_catalog=metadata_catalog,
        flat_souvenir_catalog=flat_souvenir_catalog,
        situated_context=situated_context,
    )
    try:
        raw = provider.complete(messages=messages, temperature=0.0)
        return _parse_radar_route(raw, sorted(set(available) | souvenir_domain_names()))
    except Exception as exc:
        from daemon_core.maintenance_errors import reraise_memory_update_failure

        reraise_memory_update_failure(exc)
        return ContextRoute(
            mode="contextual",
            context_depth="contextual",
            interaction_mode="conversation",
            reason=f"Memory radar unavailable: {exc.__class__.__name__}.",
            confidence=0.0,
        )


def _build_radar_route_messages(
    message: str,
    souvenir_catalog: str,
    user_context: str = "",
    user_preferences: str = "",
    user_agenda: str = "",
    moment_memory: str = "",
    current_discussion: str = "",
    recent_dialogue: str = "",
    metadata_catalog: str = "",
    flat_souvenir_catalog: str = "",
    situated_context: bool = False,
) -> list[dict]:
    final_selection_enabled = bool(flat_souvenir_catalog.strip())
    final_selection_mission = (
        f'\n{12 if situated_context else 11}. Propose in `final_souvenirs` the precise files that would entirely cover the `memory_plan`, as if no second selector would follow you.'
        if final_selection_enabled
        else ""
    )
    situated_mission = (
        '\n11. Compose in `scene_frame` the situational elements that genuinely change '
        'the decision, separating what is established from what remains '
        'uncertain.'
        if situated_context
        else ""
    )
    final_selection_rules = (
        """
- `final_souvenirs` is a final shadow proposal: choose at most 6 names present in the flat index. Gather the necessary complementary sources rather than the first resemblance, check every required need and its subject, and prefer a precise leaf over its parent domain. Keep a parent only if its own general facts are themselves necessary. A souvenir about the wrong person never covers a need. Leave empty if no souvenir is useful.
- `final_selection_complete` is your sufficiency judgment, not a confidence score. Set `true` only if you would accept `final_souvenirs` feeding the voice directly without a second reading. Set `false` if an independent reading of the entire scene and index could reasonably discover personal information that would materially change the response. Unknown information that no indexed souvenir seems able to provide is not a reason for a second reading. Souvenir count never decides. Briefly explain this judgment in `final_selection_reason`.
__SITUATED_FINAL_RULE__
- Chronicles remain cold episodic memory: choose them only to retrieve a past episode, origin, period or evolution, never as current state.
"""
        if final_selection_enabled
        else ""
    )
    situated_final_rule = (
        '- Before declaring the selection complete, check that the plan covers the '
        'decision in `scene_frame`. If the plan itself narrowed the scene to the '
        'latest message, correct it in this same judgment instead of validating '
        'artificially complete coverage.'
        if situated_context
        else ""
    )
    final_selection_schema = (
        ',"final_capsules":["theme/domaine/feuille"],"final_selection_complete":false,"final_selection_reason":"brief judgment"'
        if final_selection_enabled
        else ""
    )
    situated_schema = (
        ',"scene_frame":{"established":["useful established item"],"uncertain":["useful uncertain item"],"decision_effect":"what these items change in the response","resolution_mode":"direct"}'
        if situated_context
        else ""
    )
    final_selection_catalog = (
        '\n\nFlat index of souvenirs available for the final shadow proposal:\n'
        + flat_souvenir_catalog
        if final_selection_enabled
        else ""
    )
    active_request_rule = (
        '- `active_request` composes the decision in its actual situation, not a '
        'paraphrase of the latest message. Rely on `scene_frame` to connect '
        'the request to the circumstances and people that genuinely change the '
        'response. The grammatical recipient of the request is not always the '
        'only subject affected by its result: the immediate context determines '
        'this scope. An unknown becomes `material` only if it leads to a '
        'different response; otherwise decide without turning the conversation into '
        'an investigation.'
        if situated_context
        else '- `active_request` describes what the user is actually seeking to '
        'obtain now. If the user asks you to answer their question, '
        'find the latest still-unresolved request in the scene.'
    )
    memory_plan_scope_rule = (
        '- Build the plan from this composed scene, never from only the '
        "request's words. For each established subject affecting the decision, or "
        'already in the immediate situation and plausibly affected by it, '
        'seek only knowledge that could change a robust '
        'response and keep its attribution separate. This does not establish '
        'participation. A merely adjacent subject whose involvement is '
        'supported by no immediate element remains an unknown, and their profile '
        'does not become a certain need.'
        if situated_context
        else "- Do not limit the plan to the latest message's words: for a personal "
        'choice, comparison or allusion, also make the necessary indirect '
        'knowledge explicit. However, create no decorative '
        'need: if the scene is satisfied by the short context already supplied and calls for '
        'no deeper personal memory, an empty plan is correct.'
    )
    situated_rules = (
        """- `scene_frame` is compact decision state, not a prompt summary. Keep only what can concretely modify the present response.
- `established` rephrases only elements asserted by the supplied sources or current message. Do not enter a conclusion about the decision's scope as if it were a fact. Proximity, a habit, a shared place or a possibility never alone proves that someone participates in the specific action.
- Distinguish presence in the immediate situation from participation in the specific action: the former can justify preparing an answer compatible with that person without inventing the latter. Before fixing the decision's scope, counterfactually test the plausible compositions supported by the scene: any person or group whose participation would genuinely change the right choice must remain visible in `uncertain` until established.
- `uncertain` separately preserves every unknown genuinely suggested by the scene whose several plausible values would lead to different responses. Do not merge people or hypotheses with different evidence levels, resolve them by assumption or turn them into artificial memory needs. If a person belongs to the situation but participation in the action is not established, name that participation as unknown instead of replacing it with a vague need to adapt the choice for them.
- `decision_effect` briefly explains what the frame changes in how to respond: a choice to adapt, a possible robust response, or a single useful clarification. It does not resolve the request.
- `resolution_mode` makes this judgment operative. `direct` only if unknowns do not materially change the response. `conditional` if a useful, honest response can proceed under an explicitly named hypothesis. `clarify` if choosing a precise solution or launching an action first depends on missing information: the response must then provide at most brief orientation and ask one question, before any external research or action.
- An option is not robust merely because it suits the already known people. For a potentially shared choice, if several human compositions supported by the scene would reasonably lead to different choices, use `clarify`: `conditional` must never serve to choose participants arbitrarily. Reserve `conditional` for a reversible hypothesis that allows honest progress without changing the decision's human scope.
- A `material` ambiguity normally calls for `conditional` or `clarify`, never `direct` from a mere desire to answer. Choose `clarify` when plausible branches would lead to genuinely different searches, recommendations or actions; choose `conditional` only if the stated hypothesis leaves an immediately useful response.
- If the scene is already fully determined or its context does not affect the response, the lists may remain empty. This mechanism must not create an interrogation."""
        if situated_context
        else ""
    )
    system = """You are Daemon Harness's memory radar.

You never respond to the user.
You read the complete conversational scene, not only the latest message.
You have a short root map of existing souvenirs, but not their complete content.
Entries marked `[domain: N souvenirs]` group a whole souvenir subtree whose details are not visible here.

Your mission:
1. Choose the useful context depth.
2. Choose the interaction mode.
3. Choose the useful scene register for the response.
4. Flag candidate souvenirs that could help the next response.
5. Rephrase the active request very briefly.
6. Flag a material ambiguity if several plausible interpretations lead to different responses.
7. Say whether the turn needs the host's capabilities (`needs_host_tools`).
8. Say whether the scene calls for the current arc (`needs_trajectory`).
9. Distinguish a closing thread (`fil_clos`) from initiative freely entrusted to the daemon (`free_initiative_requested`).
10. Formulate in `memory_plan` what the response needs to know from personal memory before choosing files.
11. Flag whether the latest turn brings or corrects a temporal fact to preserve in the agenda (`needs_temporal_memory`).
__SITUATED_MISSION__
__FINAL_SELECTION_MISSION__

Depths:
- light: the floor — simple message, continuity, assent, light resumption of a thread. Short context without complete souvenirs.
- contextual: a souvenir, precise memory, preference, implicit reference or personal fact may help.
- deep: work sequence, synthesis, correction, contradiction, memory maintenance or need for sustained reasoning.

Interaction modes:
- conversation: natural, fluid, restrained exchange.
- work: work sequence; the assistant must keep the thread, structure more and not lighten too much.
- memory: memory correction, recall, organization or analysis; the assistant must be precise, cautious and avoid extrapolation.

Scene registers:
- pratique: concrete request, time, action, direct response.
- relationnel: mood, fatigue, slight tension, need to be met.
- creatif: idea, writing, imagination, open exploration.
- taquin: playful or ironic exchange, only if the user clearly opens this register.
- serieux: sensitive subject, precision, caution, important stakes.
- tendu: annoyance, firm correction, frustration or risk of misunderstanding.

Rules:
- Do not resolve the request.
- Never classify the latest message as if it were isolated.
- Read between the lines using history, moment memory, the ongoing discussion and the souvenir map.
- The latest message governs the scene: history and the current thread serve to INTERPRET it (allusions, ellipses, continuities), never to replace it. When the message clearly pivots to another topic, look for candidates for the new topic — conversational continuity is not a reason for candidacy.
- Do not rely on isolated keywords: judge the scene, references and implicit continuities.
- An allusion may pass through a concrete object, purchase, garment, brand, gift, work or cultural reference; then seek candidate souvenirs across all plausible domains of the map.
- Candidate souvenirs are not a taxonomy of the user: propose them only if they can genuinely help the response.
- A personal response requires the person's memory. When response quality depends on its fit to them — advice, a suggestion, a choice for them or a loved one, or a topic embedded in their own life — their lasting memories in the domain are constitutive of the response: choose `contextual` and propose the necessary coverage (lasting preferences, current context, genuinely useful experiences) instead of making them repeat what they already told you. Simplicity of the expected response governs its length, never this coverage.
- Rank sources: a fact confirmed by the person grounds the response; an old daemon proposal or nonpersonal content never substitutes for it when confirmed memory of the domain exists.
- For an open creative request (“surprise me”, “tell me something”, “take my mind off things”), memory is a palette, not a response: propose the user's domains of tastes and passions as material, and choose contextual — a surprise made from their world is better than generic improvisation.
- Weekly chronicles (`theme/chroniques`) are episodic archives of the past, never a source of current state. Propose them only for an explicit or implicit retrospective request: “remember when”, “the week when…”, a decision's origin, resumption after absence, evolution of a project or period. Do not propose them for agenda, schedules, tasks, current state, current preferences or everyday responses.
- Requests about times, planning, temporal reminders, “tonight”, “today”, “tomorrow”, “Friday” or picking up a loved one must use the short agenda if available.
- If the short agenda contains information useful to the response, choose at least `light`. If a souvenir must supplement the agenda, choose `contextual`.
- If the short agenda SUFFICES to answer (a simple planning question whose answer is already in its lines), stay at `light` without a candidate souvenir. Move to `contextual` only if a souvenir brings what the agenda lacks: a regular activity's regime (continuation during an unusual period, condition, season), its history, or a preference related to the request.
- When several souvenirs represent plausible competing events, routines or facts, propose them together rather than choosing prematurely.
__ACTIVE_REQUEST_RULE__
- `ambiguity` equals `material` only if plausible interpretations lead to different responses and the scene does not yet distinguish them. Otherwise use `none`.
- To judge ambiguity, consider the day, time, event concerned, history and immediate focus. Matching words is not enough.
- If a candidate souvenir is proposed, choose contextual or deep.
- If `ambiguity` equals `material`, choose contextual or deep.
- If the user explicitly asks to change how you respond (format, length, tone, detail level, follow-ups, extrapolation, behavior) or your voice, choose at least `contextual`: this adjustment must be able to be remembered even if the message is short.
- Use only names present in the souvenir map.
- A domain marked `[domain: N souvenirs]` may be proposed as a candidate souvenir: propose the domain when useful memory lies in its scope; its souvenirs will be distinguished later by a zoom. Propose a precise souvenir only if it appears verbatim in the map.
- When reasonably uncertain about useful memory, move up one level.
- `needs_host_tools` is `true` only if responding requires ACTION beyond conversation and supplied memory: searching the Web or fresh world information (news, prices, a place's opening hours, requested fact checking), scheduling/modifying/canceling a reminder or alarm, reading or changing an external resource (calendar, files, service), executing code or a command. A conversational opening may also require it: reacting substantively to the exact content of a named work, document, page or event absent from the supplied sources requires verification before asserting its meaning or details. A souvenir index may signal the personal relationship to that object; because it does not show the exact external content, it never replaces this verification. Talking, remembering, reasoning or giving an opinion based on genuinely supplied elements does not need it: `false`. When in doubt about an action necessary for an accurate response, `true`.
- `needs_temporal_memory` is `true` if the latest user message brings, refines or corrects an event, deadline, time, period, routine or travel stage that must remain useful after this dialogue. Judge the scene's meaning, never keywords. A simple question about an already known agenda or an assistant response creates no fact: `false`.
- `needs_trajectory` is `true` only if the scene calls for the user's current arc: return after absence, allusion to the current period (“these days”, “at the moment”), life transition, or a need to situate the present within its arc. Not for an exchange satisfied by recent history.
- `fil_clos` is `true` only when the latest message lets the exchange subside without entrusting a new task: brief assent, final thanks, an ending signal, nothing new. A request, even to open another topic, is not a silent closure.
- `free_initiative_requested` is `true` only when the person leaves the choice of material itself to the daemon: freely proposing a topic, surprising without an imposed terrain, telling something of its choice. A simple question, assent or invitation to start in an already named domain → `false`.
- If the person chose the topic or domain to discuss, they already supplied the guiding material: `free_initiative_requested=false`, even if they invite the daemon to start the exchange on it. `true` means the daemon must still invent or choose the terrain, not merely speak within a named terrain.
- `memory_plan` is a semantic contract, not a second souvenir selection. `subjects` names the people, groups, projects or other genuinely concerned subjects. `facets` chooses useful discovery angles in the view map available below. `needs` describes in natural language each piece of personal context necessary for an accurate response; each need may specify its own subjects and whether it is required. `intent` describes this memory's use in the response.
- For `subjects` and `facets`, reuse view-map labels exactly when they exist. This map contains no fact and proves no relevance: it offers only several paths to memory. Leave `facets` empty if no proposed view genuinely matches the scene.
__MEMORY_PLAN_SCOPE_RULE__
__SITUATED_RULES__
- `required=true` means the response would be materially less accurate without this knowledge. If lasting memory suffices for a personal, honest response, a detail that would only refine it remains optional; an abstract possibility not suggested by the scene is not a need.
- When several people are concerned, distinguish their needs instead of blending them into one profile. A preference, experience or constraint must remain attributed to its subject.
__FINAL_SELECTION_RULES__
- Respond in strict JSON, without Markdown.

JSON schema:
{"context_depth":"light","interaction_mode":"conversation","scene_register":"pratique","candidate_capsules":[],"active_request":"short active request","ambiguity":"none","needs_host_tools":false,"needs_temporal_memory":false,"needs_trajectory":false,"fil_clos":false,"free_initiative_requested":false__SITUATED_SCHEMA____FINAL_SELECTION_SCHEMA__,"memory_plan":{"subjects":["subject concerned"],"facets":["available facet"],"needs":[{"description":"required personal knowledge","subjects":["subject concerned"],"required":true}],"intent":"use of this memory"},"reason":"short reason","confidence":0.0}
"""
    system = (
        system.replace("__FINAL_SELECTION_MISSION__", final_selection_mission)
        .replace("__SITUATED_MISSION__", situated_mission)
        .replace("__FINAL_SELECTION_RULES__", final_selection_rules)
        .replace("__FINAL_SELECTION_SCHEMA__", final_selection_schema)
        .replace("__SITUATED_SCHEMA__", situated_schema)
        .replace("__SITUATED_FINAL_RULE__", situated_final_rule)
        .replace("__ACTIVE_REQUEST_RULE__", active_request_rule)
        .replace("__MEMORY_PLAN_SCOPE_RULE__", memory_plan_scope_rule)
        .replace("__SITUATED_RULES__", situated_rules)
    )
    if situated_context:
        scene_inputs = f"""Present situation to consider before the stable profile:

Active context:
{moment_memory or "No explicit active context."}

Short agenda:
{user_agenda or "No explicit short agenda."}

Immediate focus:
{current_discussion or "No explicit immediate focus."}

Stable facts about the user:
{user_context or "No explicit user context."}

Recent history:
{recent_dialogue[-3000:] or "No recent history."}

Discussion-format preferences:
{user_preferences or "No explicit preference."}"""
    else:
        scene_inputs = f"""Stable facts about the user:
{user_context or "No explicit user context."}

Discussion-format preferences:
{user_preferences or "No explicit preference."}

Short agenda:
{user_agenda or "No explicit short agenda."}

Active context:
{moment_memory or "No explicit active context."}

Immediate focus:
{current_discussion or "No explicit immediate focus."}

Recent history:
{recent_dialogue[-3000:] or "No recent history."}"""
    user = f"""Current time:
{current_time_context()}

Latest user message:
{message}

{scene_inputs}

Root map of available souvenirs:
{souvenir_catalog or "No souvenir available."}

Compact map of views by subject and facet:
{metadata_catalog or "No discovery metadata available."}
__FINAL_SELECTION_CATALOG__
"""
    user = user.replace("__FINAL_SELECTION_CATALOG__", final_selection_catalog)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_radar_route(raw: str, available: list[str]) -> ContextRoute:
    data = json.loads(_strip_json_fence(raw))
    context_depth = data.get("context_depth", "contextual")
    if context_depth == "minimal":  # Compatibility: the previous depth is handled by light (2026-07-09).
        context_depth = "light"
    if context_depth not in {"light", "contextual", "deep"}:
        context_depth = "contextual"
    interaction_mode = data.get("interaction_mode", "conversation")
    if interaction_mode not in {"conversation", "work", "memory"}:
        interaction_mode = "conversation"
    scene_register = data.get("scene_register", "pratique")
    if scene_register not in {"pratique", "relationnel", "creatif", "taquin", "serieux", "tendu"}:
        scene_register = "pratique"
    candidates = _clean_candidate_souvenirs(data.get("candidate_capsules", []), available)
    active_request = data.get("active_request", "")
    if not isinstance(active_request, str):
        active_request = ""
    active_request = active_request.strip()
    ambiguity = data.get("ambiguity", "none")
    if ambiguity not in {"none", "material"}:
        ambiguity = "none"
    needs_host_tools = data.get("needs_host_tools", False) is True
    needs_temporal_memory = data.get("needs_temporal_memory", False) is True
    needs_trajectory = data.get("needs_trajectory", False) is True
    fil_clos = data.get("fil_clos", False) is True
    free_initiative_requested = data.get("free_initiative_requested", False) is True
    radar_final_souvenirs = _clean_candidate_souvenirs(
        data.get("final_capsules", []),
        list_available_souvenirs(),
    )
    radar_final_complete = data.get("final_selection_complete", False) is True
    radar_final_reason = data.get("final_selection_reason", "")
    if not isinstance(radar_final_reason, str):
        radar_final_reason = ""
    memory_plan = MemoryPlan.from_raw(data.get("memory_plan"))
    scene_frame = SceneFrame.from_raw(data.get("scene_frame"))
    if (candidates or ambiguity == "material") and context_depth == "light":
        context_depth = "contextual"
    reason = data.get("reason", "Memory radar.")
    if not isinstance(reason, str):
        reason = "Memory radar."
    confidence = data.get("confidence", 0.0)
    if not isinstance(confidence, int | float):
        confidence = 0.0
    mode = "contextual" if context_depth in {"contextual", "deep"} else "fast"
    return ContextRoute(
        mode=mode,
        context_depth=context_depth,
        interaction_mode=interaction_mode,
        scene_register=scene_register,
        candidate_souvenirs=candidates,
        active_request=active_request,
        ambiguity=ambiguity,
        needs_host_tools=needs_host_tools,
        needs_temporal_memory=needs_temporal_memory,
        needs_trajectory=needs_trajectory,
        fil_clos=fil_clos,
        free_initiative_requested=free_initiative_requested,
        radar_final_souvenirs=radar_final_souvenirs,
        radar_final_complete=radar_final_complete,
        radar_final_reason=radar_final_reason.strip(),
        scene_frame=scene_frame,
        memory_plan=memory_plan,
        reason=reason,
        confidence=max(0.0, min(float(confidence), 1.0)),
    )


def _clean_candidate_souvenirs(raw_candidates: object, available: list[str]) -> list[str]:
    if not isinstance(raw_candidates, list):
        return []
    available_set = set(available)
    cleaned = []
    for name in raw_candidates:
        if isinstance(name, str) and name in available_set and name not in cleaned:
            cleaned.append(name)
    return cleaned[:6]


def _clean_scene_items(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    cleaned = []
    for item in raw:
        text = _clean_scene_text(item)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:6]


def _clean_scene_text(raw: object) -> str:
    return raw.strip()[:500] if isinstance(raw, str) else ""


def _clean_resolution_mode(raw: object) -> str:
    return raw if raw in {"direct", "conditional", "clarify"} else "direct"


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
