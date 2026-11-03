"""Follow-up judgment as a dedicated prospective-memory faculty.

One LLM judgment uses the recent dialogue to decide whether to return to a
topic later, when to do so and from which angle. Its output goes through the
intent store; the host remains responsible for scheduling and delivery.
No keyword rules substitute for that semantic judgment."""

import json
import logging
from datetime import datetime, timedelta

from daemon_core import followup as _followup
from daemon_core.memory.reader import load_recent_dialogue, load_trajectory, load_user_agenda, without_trailing_user_message
from daemon_core.memory_agent import _quoted_evidence_appears_in_message
from daemon_core.narrator import run_narrator
from daemon_core.prompt_composer import current_time_context
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider

logger = logging.getLogger("daemon_core.relanceur")


def run_relanceur(
    user_id: str,
    provider: LLMProvider,
    dry_run: bool = False,
    *,
    recent_dialogue: str | None = None,
) -> dict:
    """Judge whether to create a follow-up.

    Return {"followup": dict|None, "followups_pref": dict|None}.
    The host adapter schedules delivery."""
    empty = {"followup": None, "followups_pref": None}
    if not _followup.followups_enabled():
        return empty
    try:
        expired = _followup.expire_overdue_pending_intents(user_id)
        if expired:
            logger.info(
                "Relanceur : %s intention(s) hors fenêtre classée(s) expired",
                len(expired),
            )
        pending = _followup.list_intents(user_id, state="pending")
        active_pending = _followup.active_pending_intents(user_id)
        if len(active_pending) >= _followup.MAX_ACTIVE_FOLLOWUPS:
            logger.info("Relanceur : %s intentions proches déjà en attente — pas de nouvelle", len(active_pending))
            return empty
        recent_dialogue = (
            load_recent_dialogue(user_id, max_turns=12)
            if recent_dialogue is None
            else recent_dialogue
        )
        if not recent_dialogue.strip():
            return empty
        trajectory = run_narrator(
            user_id=user_id,
            provider=provider,
            recent_dialogue=recent_dialogue,
        )
        messages = _build_messages(
            recent_dialogue=recent_dialogue,
            trajectory=trajectory or load_trajectory(user_id),
            agenda=load_user_agenda(user_id),
            pending="\n".join(f"- {p.fire_at} — {p.topic}" for p in pending),
            delivered_today=_followup.delivered_today_count(user_id),
        )
        log_llm_prompt(logger, "RELANCEUR", messages)
        raw = provider.complete(messages=messages, temperature=0.2)
        data = json.loads(_strip_json_fence(raw))
        result = {
            "followup": _validate_followup(data, recent_dialogue),
            "followups_pref": _validate_pref(data, recent_dialogue),
        }
        if dry_run:
            logger.info("Relanceur [DRY-RUN] %s", result)
            return {"followup": None, "followups_pref": None, "dry_run_judgment": result}
        if result["followup"]:
            logger.info("Relanceur : relance plantée | angle=%s | fire_at=%s | topic=%r",
                        result["followup"]["angle"], result["followup"]["fire_at"], result["followup"]["topic"])
        return result
    except Exception as exc:
        logger.info("Relanceur ignoré | error=%s", exc.__class__.__name__)
        return empty


def _build_messages(
    recent_dialogue: str,
    trajectory: str,
    agenda: str,
    pending: str,
    delivered_today: int,
) -> list[dict]:
    system = f"""You are the daemon's prospective memory — the faculty that decides to return to the user later, on its own initiative.

Your sole judgment: given the latest exchanges, is there a real reason to return to them in the coming hours or days — and if so, when, from what angle, about what?

Angles:
- check-in: ask how they are after a moment that matters, or send a light, contextual touch of presence when the scene invites it.
- encouragement: a brief word before or during something that calls for courage or momentum.
- reaction: return to something they shared — a thought that stayed with you, an idea that matured.
- feedback: offer a small part of your portrait of them — what you think you have recently understood, in one sentence — and ask where you are wrong.
- presence: a situated touch of presence during an ordinary but live moment (place, heat, journey, waiting, outing), just a few words.
- heads-up: quietly point out a concrete blind spot on which the near-term scene depends, before it becomes an unpleasant surprise.

Rules:
- Aim for the rhythm of an attentive friend: a few follow-ups per week when life provides material — an experienced event, an expressed emotion, an unresolved idea, a place they are in, a silence following an intense moment. Abstain only if there is genuinely nothing to support it.
- Follow up only on what MATTERS to them: a stake, an emotion, a milestone, a live moment. Never on speculation or a social micro-prediction (who will get along with whom, whether an activity among loved ones will “take”) — these details are lived, not tracked.
- In a quiet period (holidays, rest), a touch of presence sometimes suffices: a short, situated sentence, easy to ignore, without trying to be useful. Presence is not limited to major stakes.
- A fleeting moment of complicity or a polite laugh does not create a conversational debt. Delayed presence must still have its own resonance when it fires; do not schedule the mere reminder of a joke that has already served its purpose.
- The exact state of a connected source can justify a `heads-up` only if an already present situation materially depends on its coverage and action remains possible before the horizon. Its age alone is not enough. Schedule nothing if the response has just made this blind spot visible.
- For a same-day touch of presence (`presence`), be responsive: aim for the next live window, often within 30 to 90 minutes. Do not wait until the whole scene has subsided.
- SPONTANEOUS initiative only: if they explicitly asked for a reminder or follow-up (“remind me…”, “check in on me tonight”), emit nothing — those requests are handled elsewhere.
- `evidence`: an exact, continuous quotation from recent dialogue (any user message, not just the latest). Without an exact quotation, do not emit.
- `fire_at`: a future timestamp “YYYY-MM-DDTHH:MM”, between 08:00 and 23:00, placed when the follow-up will make most sense (during a live place/moment, after the event, the day after an evening out, before a moment that calls for momentum). For a daytime scene, prefer the same day if the moment is known; do not postpone until tomorrow out of caution.
- Only one follow-up per pass, and no duplicate of pending intentions or what has already been delivered today ({delivered_today} delivered).
- `followups_pref`: only if the user says in recent dialogue that they want or no longer want follow-ups ({{"action":"disable"|"enable","evidence":"exact quotation"}}). Otherwise null.

Respond in strict JSON:
{{"followup": {{"should_schedule": true, "topic": "…", "evidence": "exact quotation", "angle": "check-in|encouragement|reaction|feedback|presence|heads-up", "fire_at": "YYYY-MM-DDTHH:MM"}} | null, "followups_pref": null, "reason": "short reason"}}"""
    user = f"""Current time:
{current_time_context()}

User's trajectory:
{trajectory.strip() or "None."}

Short agenda:
{agenda.strip() or "None."}

Follow-ups already pending:
{pending or "None."}

Recent dialogue:
{recent_dialogue.strip()[-4500:]}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _validate_followup(data: dict, recent_dialogue: str) -> dict | None:
    raw = data.get("followup")
    if not isinstance(raw, dict) or raw.get("should_schedule") is not True:
        return None
    evidence = str(raw.get("evidence", "")).strip()
    if not _quoted_evidence_appears_in_message(evidence, recent_dialogue):
        return None
    topic = str(raw.get("topic", "")).strip()
    fire_at = str(raw.get("fire_at", "")).strip()
    if not topic or not fire_at:
        return None
    angle = str(raw.get("angle", "check-in")).strip()
    if angle not in _followup.ANGLES:
        angle = "check-in"
    fire_at = _tighten_presence_fire_at(fire_at, angle)
    return {"topic": topic, "fire_at": fire_at, "angle": angle, "evidence": evidence}


def _tighten_presence_fire_at(fire_at: str, angle: str, now: datetime | None = None) -> str:
    """Keep brief proactive messages within the current moment.

    The LLM judges relevance through presence; code only bounds the latest
    delivery time for a same-day scene."""
    if angle != "presence":
        return fire_at
    now = now or datetime.now(_followup.TZ)
    try:
        target = datetime.fromisoformat(fire_at)
    except ValueError:
        return _followup.clamp_to_waking((now + timedelta(minutes=45)).isoformat(timespec="minutes"), now=now)
    if target.tzinfo is None:
        target = target.replace(tzinfo=_followup.TZ)
    if target.date() != now.date():
        return fire_at
    latest = now + timedelta(minutes=90)
    if target > latest:
        return _followup.clamp_to_waking((now + timedelta(minutes=60)).isoformat(timespec="minutes"), now=now)
    return _followup.clamp_to_waking(target.isoformat(timespec="minutes"), now=now)


def _validate_pref(data: dict, recent_dialogue: str) -> dict | None:
    raw = data.get("followups_pref")
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action", "")).strip().lower()
    if action not in ("disable", "enable"):
        return None
    evidence = str(raw.get("evidence", "")).strip()
    if not _quoted_evidence_appears_in_message(evidence, recent_dialogue):
        return None
    return {"action": action, "evidence": evidence}


def _strip_json_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("```")).strip()
    return text


# ---------------------------------------------------------------------------
# The moment judge belongs to the initiative faculty:
# one judgment per faculty, not scene coaching.
# Historical latency came from transport, not the faculty itself.
# daemon_voice invokes it for judged closures and explicit
# initiative requests, keeping these cases distinct.
# ---------------------------------------------------------------------------
SILENCE_TOKEN = "NO_REPLY"


def run_moment_judge(
    user_id: str,
    provider: LLMProvider,
    message: str = "",
    free_initiative_requested: bool = False,
    selected_souvenirs: list[str] | None = None,
) -> str:
    """Judge the next conversational movement and return the final message."""
    try:
        messages = compose_moment_judge_messages(
            user_id, message, free_initiative_requested, selected_souvenirs=selected_souvenirs
        )
        log_llm_prompt(logger, "JUGE DU MOMENT", messages)
        text = (provider.complete(messages=messages, temperature=0.4) or "").strip()
        if not text or (SILENCE_TOKEN in text and len(text) <= len(SILENCE_TOKEN) + 20):
            logger.warning("Juge du moment : sortie vide — retour à la conversation normale")
            return ""
        logger.info("Juge du moment : reprise d'initiative | chars=%s", len(text))
        return text
    except Exception as exc:
        logger.info("Juge du moment ignoré (%s) — retour à la conversation normale", exc.__class__.__name__)
        return ""


def compose_moment_judge_messages(
    user_id: str,
    message: str,
    free_initiative_requested: bool = False,
    selected_souvenirs: list[str] | None = None,
) -> list[dict]:
    from daemon_core.memory.reader import load_active_context, load_souvenir, load_conversation_state, load_immediate_focus, load_root_souvenir_index, load_user_agenda
    from daemon_core.persona import load_conversation_manner, load_daemon_persona

    relevant_memory = "\n\n".join(
        content
        for name in (selected_souvenirs or [])[:4]
        for content in [load_souvenir(name).strip()[:4000]]
        if content
    )

    return build_moment_judge_messages(
        message=message,
        free_initiative_requested=free_initiative_requested,
        recent_dialogue=without_trailing_user_message(
            load_recent_dialogue(user_id, max_turns=32), message
        ),
        conversation_state=load_conversation_state(user_id),
        immediate_focus=load_immediate_focus(user_id),
        # Include the topic map and active context, including rejected topics.
        # Without them, the judge can propose unrelated curiosities;
        # this occurred with an empty memory_map on 2026-07-10.
        memory_map=load_root_souvenir_index(),
        active_context=load_active_context(user_id),
        relevant_memory=relevant_memory,
        # Supply the full identity context. Initiative turns previously lacked
        # persona and conversational style, producing generic,
        # disconnected openings (observed 2026-07-11).
        daemon_persona=load_daemon_persona(user_id),
        conversation_manner=load_conversation_manner(user_id),
        # Include recent personal context so the judge has more
        # than the general topics in the map.
        user_agenda=load_user_agenda(user_id),
    )


def build_moment_judge_messages(
    message: str,
    free_initiative_requested: bool,
    recent_dialogue: str,
    conversation_state: str = "",
    immediate_focus: str = "",
    memory_map: str = "",
    active_context: str = "",
    relevant_memory: str = "",
    daemon_persona: str = "",
    conversation_manner: str = "",
    user_agenda: str = "",
) -> list[dict]:
    gesture = (
        "The person explicitly entrusts you with the initiative. Taking the initiative does not oblige you to hand out a topic: read their actual moment — the time, their current period, their energy, what has just come alive or died in the exchange — and choose the right move: a concrete new topic, a step into what is actually happening in their life, or handing the initiative back by asking what they would feel like. A series of declined offers signals a need to change posture, not just topic. When offering a topic, offer it as an invitation they remain free to accept: ask whether they want to enter it instead of presuming so, hold your material until they have entered, and vary the form from one time to the next."
        if free_initiative_requested
        else
        "The current thread seems to be subsiding. Judge freely whether to extend what is still alive, open a new door or conclude warmly."
    )
    identity = "\n\n".join(
        section
        for title, content in (
            ("# Who you are", daemon_persona),
            ("# Your way of conversing", conversation_manner),
        )
        if content.strip()
        for section in [f"{title}\n\n{content.strip()}"]
    )
    system = f"""You are the daemon's faculty of conversational initiative. Your sole judgment is the next natural move in this exchange.

{identity}

Scene's gesture: {gesture}

Read the current message first. If it brings material rather than entrusting you with the initiative, respond to that material without covering it with a new topic.

Rely on the conversational state to respect freshness: an important topic may be exhausted, recently declined or simply wrong for the present mood. The state describes what was alive; the latest message says what is alive — it takes precedence. Confirmation of what is done is not withdrawal: as long as the person is there, it is an outstretched hand — bounce toward something new, on the scale of their energy. If the current topic is unfinished and the person simply agrees, stay with it: continue or deepen it, do not change topics. The speed of closure follows the weight of the material: a light topic closes in a single move. When closing, add no new material — every addition calls for another response. Warm rest is right only when they truly withdraw. Never rephrase what is already done. Do not turn the person's best-documented traits into a recurring menu. A good opening can bring a work, a fact, an idea or a concrete external question that is new to the exchange. Lightness means absence of pressure and genuine play, not a generic question.

When opening a topic, draw first from the person's worlds (their map, below) — a named, precise element, never a general theme summarizing them — or from something true that you know if it resonates with those worlds. When opening a new topic, first ask whether they want to enter it instead of presuming so — especially if it is personal or sensitive. This applies in every scene.

Restraint is also a move. If your previous responses have already closed or repeated the same idea, do not add a new image to fill the silence. If the current message gives feedback on your way of conversing, acknowledge that feedback precisely and stop before steering elsewhere. Do not manufacture liveliness with a mini-game, a list, an artificial dilemma or an interview question. When opening a topic, enter the exchange yourself with an observation, idea, fact or position that genuinely gives material to respond to, on the same scale as the received message rather than as a mini-speech.

Make only one move. Do not explain your strategy, announce the topic change, give several options, repeat the previous response or invent an experience you have not lived. Your output is the final message addressed to the person."""
    relevant_memory_label = (
        # For unsolicited initiative, radar-selected memories are a hypothesis,
        # not a topic chosen by the user. Calling them "chosen"
        # previously produced forced topics (2026-07-11).
        "Material surfaced by the scene reading (the person did NOT request this topic — your choice remains entirely free)"
        if free_initiative_requested
        else "Memories targeted to the topic chosen in this scene"
    )
    user = f"""Current time:
{current_time_context()}

Their worlds (memory map — draw named elements):
{memory_map.strip() or "No map."}

Active context (including recently declined topics):
{active_context.strip() or "None."}

Their short agenda (their near-term reality):
{user_agenda.strip() or "None."}

{relevant_memory_label} :
{relevant_memory.strip() or "No targeted memory."}

Temporary conversational state:
{conversation_state.strip() or "No prior reading."}

Immediate focus:
{immediate_focus.strip() or "None."}

Recent dialogue:
{recent_dialogue.strip()[-9000:]}

Current message:
{message.strip() or "(no new material)"}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
