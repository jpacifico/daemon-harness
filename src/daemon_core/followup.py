"""Proactive follow-up intent storage and delivery.

The host cron adapter invokes this module when an intent is due.
Memory maintenance creates intents when the conversation warrants them.
Return a message on stdout for the host's no_agent job to deliver;
empty stdout means silence."""
from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from daemon_core import paths
from daemon_core.memory.reader import (
    load_active_context,
    load_dialogue_since,
    load_immediate_focus,
    load_recent_dialogue,
    load_user_agenda,
    load_user_context,
)
from daemon_core.persona import load_conversation_manner, load_daemon_persona
from daemon_core.prompt_composer import current_time_context

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Paris")
WAKING_START_HOUR = 8
WAKING_END_HOUR = 23  # Allow evening requests such as "check in with me tonight".
LATE_GRACE_HOURS = 6  # Beyond this limit, the window is missed: remain silent.
ANGLES = (
    "check-in",
    "encouragement",
    "reaction",
    "feedback",
    "presence",
    "heads-up",
)
MAX_ACTIVE_FOLLOWUPS = 3   # Pending follow-ups within the near-term horizon.
ACTIVE_HORIZON_HOURS = 72  # More distant intentions must not block present initiative.
MAX_DELIVERED_PER_DAY = 3  # Maximum delivered follow-ups per day.
# Each attempt calls the model, so deferrals are finite and spaced out.
MAX_RESCHEDULES = 3
MIN_RESCHEDULE_DELAY_MINUTES = 30
FIRE_LOCK_STALE_SECONDS = 15 * 60
_TRUE = {"1", "true", "on", "yes"}


def followups_enabled() -> bool:
    """Master switch for creating and scheduling follow-ups; disabled by default."""
    return os.environ.get("DAEMON_CORE_FOLLOWUPS", "").strip().lower() in _TRUE


def followups_dry_run() -> bool:
    """Record intentions without creating cron jobs or delivering messages."""
    return os.environ.get("DAEMON_CORE_FOLLOWUPS_DRYRUN", "").strip().lower() in _TRUE


# --- paths: resolved dynamically for test monkeypatching -------------
def _root() -> Path:
    return paths.MEMORY_ROOT / "followups"


def _intent_path(followup_id: str) -> Path:
    return _root() / "intents" / f"{followup_id}.json"


def _fire_lock_path(followup_id: str) -> Path:
    return _root() / "intents" / f"{followup_id}.lock"


def _user_dir(user_id: str) -> Path:
    return _root() / "users" / user_id


def _disabled_flag(user_id: str) -> Path:
    return _user_dir(user_id) / "disabled"


def _delivered_log(user_id: str) -> Path:
    return _user_dir(user_id) / "delivered.log"


def _moved_path() -> Path:
    """Reminders carried by a move, waiting for their new host's channel."""
    return _root() / "moved.json"


# --- model -----------------------------------------------------------
@dataclass
class FollowUp:
    id: str
    user_id: str
    fire_at: str  # ISO "AAAA-MM-JJTHH:MM"
    topic: str
    evidence: str
    angle: str = "check-in"
    origin: dict = field(default_factory=dict)  # {platform, chat_id} for the adapter.
    job_id: str | None = None  # Host cron job.
    state: str = "pending"  # pending | delivered | silent | cancelled | expired | fired(legacy)
    created_at: str = ""
    fired_at: str | None = None
    delivered_at: str | None = None
    outcome_reason: str | None = None
    reschedule_count: int = 0


# --- store -------------------------------------------------------------------
def save_intent(fu: FollowUp) -> Path:
    path = _intent_path(fu.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(fu), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_intent(followup_id: str) -> FollowUp | None:
    path = _intent_path(followup_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return FollowUp(**data)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Intention de relance illisible %s : %s", followup_id, exc)
        return None


def list_intents(user_id: str | None = None, state: str | None = None) -> list[FollowUp]:
    out: list[FollowUp] = []
    intents_dir = _root() / "intents"
    if not intents_dir.exists():
        return out
    for path in sorted(intents_dir.glob("*.json")):
        fu = load_intent(path.stem)
        if fu is None:
            continue
        if user_id and fu.user_id != user_id:
            continue
        if state and fu.state != state:
            continue
        out.append(fu)
    return out


def _fire_datetime(fu: FollowUp) -> datetime | None:
    try:
        target = datetime.fromisoformat(fu.fire_at)
    except ValueError:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=TZ)
    return target


def active_pending_intents(
    user_id: str | None = None,
    *,
    now: datetime | None = None,
    horizon_hours: int = ACTIVE_HORIZON_HOURS,
) -> list[FollowUp]:
    """Return near-term pending intents that count toward the initiative limit.

    Distant intents remain scheduled without blocking a relevant thread now."""
    now = now or datetime.now(TZ)
    horizon = now + timedelta(hours=horizon_hours)
    active: list[FollowUp] = []
    for fu in list_intents(user_id, state="pending"):
        target = _fire_datetime(fu)
        if target is not None and now > target + timedelta(hours=LATE_GRACE_HOURS):
            continue
        if target is None or target <= horizon:
            active.append(fu)
    return active


def expire_overdue_pending_intents(
    user_id: str | None = None,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Close intents whose delivery window elapsed without a surviving host job."""
    now = now or datetime.now(TZ)
    expired: list[str] = []
    for fu in list_intents(user_id, state="pending"):
        target = _fire_datetime(fu)
        if target is None or now <= target + timedelta(hours=LATE_GRACE_HOURS):
            continue
        set_state(
            fu.id,
            "expired",
            now=now,
            reason="window_missed_before_fire",
        )
        expired.append(fu.id)
    return expired


def adopt_moved(user_id: str, origin: dict, schedule) -> list[str]:
    """Give moved reminders this host's real channel and schedule them.

    A daemon restored by a move has no host job for its reminders. The first
    completed turn on the new host supplies its own delivery origin, so no
    channel coordinates are guessed across hosts. Unscheduled ones stay listed.
    """
    path = _moved_path()
    if not path.exists() or not origin:
        return []
    try:
        listed = json.loads(path.read_text(encoding="utf-8"))
        moved = {item for item in listed if isinstance(item, str)}
    except (OSError, ValueError, TypeError):
        return []
    adopted, remaining = [], set()
    for fu in list_intents(user_id, state="pending"):
        if fu.id not in moved:
            continue
        fu.origin, fu.job_id = dict(origin), None
        save_intent(fu)
        if schedule(fu):
            adopted.append(fu.id)
        else:
            remaining.add(fu.id)
    if remaining:
        path.write_text(json.dumps(sorted(remaining)) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return adopted


def new_followup(
    user_id: str,
    fire_at: str,
    topic: str,
    evidence: str,
    angle: str = "check-in",
    origin: dict | None = None,
) -> FollowUp:
    """Create and persist an intent.

    Generate its ID before the cron job; the relay embeds it and the host
    adapter adds the job ID afterwards."""
    fu = FollowUp(
        id=uuid.uuid4().hex[:12],
        user_id=user_id,
        fire_at=fire_at,
        topic=topic.strip(),
        evidence=evidence.strip(),
        angle=angle if angle in ANGLES else "check-in",
        origin=origin or {},
        created_at=datetime.now(TZ).isoformat(timespec="minutes"),
    )
    save_intent(fu)
    return fu


def set_state(
    followup_id: str,
    state: str,
    *,
    now: datetime | None = None,
    reason: str | None = None,
) -> None:
    fu = load_intent(followup_id)
    if fu is not None:
        fu.state = state
        if state in {"delivered", "silent", "expired", "cancelled", "fired"}:
            stamp = (now or datetime.now(TZ)).isoformat(timespec="minutes")
            fu.fired_at = fu.fired_at or stamp
            if state == "delivered":
                fu.delivered_at = fu.delivered_at or stamp
        if reason:
            fu.outcome_reason = reason
        save_intent(fu)


def cancel(followup_id: str) -> bool:
    fu = load_intent(followup_id)
    if fu is None or fu.state != "pending":
        return False
    fu.state = "cancelled"
    save_intent(fu)
    return True


def reschedule(
    followup_id: str,
    fire_at: str,
    *,
    now: datetime | None = None,
    reason: str = "",
) -> bool:
    """Keep an intention alive at a new time chosen by the LLM judge."""
    fu = load_intent(followup_id)
    if fu is None or fu.state != "pending":
        return False
    now = now or datetime.now(TZ)
    fire_at = clamp_to_waking(fire_at, now=now)
    earliest = now + timedelta(minutes=MIN_RESCHEDULE_DELAY_MINUTES)
    target = datetime.fromisoformat(fire_at)
    if target.tzinfo is None:
        target = target.replace(tzinfo=earliest.tzinfo or TZ)
    if target < earliest:
        fire_at = clamp_to_waking(
            earliest.replace(tzinfo=None).isoformat(timespec="minutes"),
            now=now,
        )
    fu.fire_at = fire_at
    fu.job_id = None
    fu.reschedule_count += 1
    fu.outcome_reason = f"rescheduled:{reason.strip()}" if reason.strip() else "rescheduled"
    save_intent(fu)
    return True


# --- off-switch + cadence ----------------------------------------------------
def is_disabled(user_id: str) -> bool:
    return _disabled_flag(user_id).exists()


def set_disabled(user_id: str, disabled: bool = True) -> None:
    flag = _disabled_flag(user_id)
    if disabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(datetime.now(TZ).isoformat(timespec="minutes"), encoding="utf-8")
    elif flag.exists():
        flag.unlink()


def _record_delivery(user_id: str, when: datetime, followup_id: str) -> None:
    log = _delivered_log(user_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{when.isoformat(timespec='minutes')} {followup_id}\n")


def delivered_today_count(user_id: str, now: datetime | None = None) -> int:
    now = now or datetime.now(TZ)
    log = _delivered_log(user_id)
    if not log.exists():
        return 0
    today = now.date().isoformat()
    return len({line.strip() for line in log.read_text(encoding="utf-8").splitlines() if line.startswith(today)})


def _acquire_fire_lock(followup_id: str, now: datetime) -> bool:
    path = _fire_lock_path(followup_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = now.timestamp() - path.stat().st_mtime
            if age > FIRE_LOCK_STALE_SECONDS:
                path.unlink()
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                return False
        except OSError:
            return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(now.isoformat(timespec="seconds"))
    return True


def _release_fire_lock(followup_id: str) -> None:
    try:
        _fire_lock_path(followup_id).unlink(missing_ok=True)
    except OSError:
        pass


# --- tir ---------------------------------------------------------------------
def _outside_waking_hours(now: datetime) -> bool:
    return not (WAKING_START_HOUR <= now.hour < WAKING_END_HOUR)


def clamp_to_waking(fire_at: str, now: datetime | None = None) -> str:
    """Move delivery into waking hours and ensure the timestamp is in the future."""
    now = now or datetime.now(TZ)
    try:
        target = datetime.fromisoformat(fire_at)
    except ValueError:
        target = now + timedelta(hours=2)
    if target.tzinfo is None:
        target = target.replace(tzinfo=TZ)
    if target.hour < WAKING_START_HOUR:
        target = target.replace(hour=WAKING_START_HOUR, minute=0)
    elif target.hour >= WAKING_END_HOUR:
        target = target.replace(hour=WAKING_END_HOUR - 1, minute=0)
    if target <= now:
        target = now + timedelta(minutes=30)
        if target.hour < WAKING_START_HOUR:
            target = target.replace(hour=WAKING_START_HOUR, minute=0)
        elif target.hour >= WAKING_END_HOUR:
            target = (target + timedelta(days=1)).replace(hour=WAKING_START_HOUR, minute=0)
    # Local time without offset, matching MemoryAgent and Hermes parse_schedule.
    return target.replace(tzinfo=None).isoformat(timespec="minutes")


def _window_missed(fu: FollowUp, now: datetime) -> bool:
    target = _fire_datetime(fu)
    if target is None:
        return False
    return now > target + timedelta(hours=LATE_GRACE_HOURS)


def _angle_hint(angle: str) -> str:
    if angle == "presence":
        return 'You send a contextual micro-presence: a few situated, light words, without turning it into a check-in.'
    if angle == "encouragement":
        return 'You offer a brief word of encouragement, not a follow-up question.'
    if angle == "reaction":
        return 'You return to something they shared: a thought that stayed with you, an echo that has matured since. Not a follow-up question — a contribution.'
    if angle == "feedback":
        return 'You offer a small part of your portrait of them: what you think you have understood recently, in one interpretive sentence (never a list of facts), and ask where you are mistaken.'
    if angle == "heads-up":
        return (
            'You quietly point out a concrete blind spot that could surprise them soon. Give the fact and, if obvious, one way to regain visibility; do not dramatize it.'
        )
    return 'You briefly check in. A tiny contextual question suffices if it sounds more alive than a formal follow-up.'


def _minutes_since_last_exchange(user_id: str, now: datetime) -> int | None:
    """Return minutes since the last readable journal turn, or None."""
    try:
        recent = load_recent_dialogue(user_id, max_turns=1)
        for line in recent.splitlines():
            if line.startswith("## ") and " — " in line:
                stamp = line[3:].split(" — ", 1)[0].strip()
                last = datetime.fromisoformat(stamp)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=TZ)
                return max(0, int((now - last).total_seconds() // 60))
    except Exception:
        pass
    return None


def _absence_fact(minutes: int | None) -> str:
    if minutes is None:
        return 'You have not spoken to each other yet today.'
    if minutes < 60:
        return f'You last spoke {minutes} minutes ago.'
    hours = minutes // 60
    return f"You last spoke approximately {hours} hour{('s' if hours > 1 else '')} ago."


def _compose_messages(fu: FollowUp, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(TZ)
    persona = load_daemon_persona(fu.user_id).strip()
    sections = [f"## Current time\n{current_time_context()}\nFiring timestamp: {now.isoformat(timespec='minutes')}"]
    for title, value in (
        ('Stable facts', load_user_context(fu.user_id)),
        ('Conversational manner', load_conversation_manner(fu.user_id)),
        ('Short agenda', load_user_agenda(fu.user_id)),
        ('Background context', load_active_context(fu.user_id)),
        ('Current thread', load_immediate_focus(fu.user_id)),
    ):
        if value.strip():
            sections.append(f"## {title}\n{value.strip()}")
    recent = load_dialogue_since(fu.user_id, fu.created_at, max_turns=16)
    if not recent.strip():
        recent = load_recent_dialogue(fu.user_id, max_turns=8)
    if recent.strip():
        sections.append(
            f'## Exchanges since this intention was created\n{recent.strip()}'
        )

    minutes = _minutes_since_last_exchange(fu.user_id, now)
    if fu.angle == "presence":
        movement = (
            'The intention is a situated micro-presence. Recent exchanges serve to judge whether this gesture remains alive, not to automatically replace it with the latest topic. If you deliver, keep it to 3 to 12 words: no assessment, advice or recap. Do not claim to be physically with the person.'
        )
    else:
        movement = (
            f'The message arises from the present; the intention is only an impulse. A genuine thread left pending by the person may become the delivered movement, but an old daemon question creates no obligation to answer. {_angle_hint(fu.angle)}'
        )
    system = f"""{persona}

You now judge an intention for the daemon to return spontaneously, never a requested reminder.
Intention: "{fu.topic}"
Original evidence: "{fu.evidence}"
Angle: {fu.angle}
Previous deferrals: {fu.reschedule_count}
{_absence_fact(minutes)}

{movement}

Choose ONE action based on the current scene:
- `deliver`: the gesture fits naturally now, even if the conversation is recent. It contributes something without repeating, interrupting or artificially reopening a closed thread.
- `reschedule`: the intention remains alive but now would be a poor moment. Give a precise new future `fire_at` corresponding to a more natural moment; do not defer merely out of caution.
- `cancel`: the moment has passed, the topic is closed, contradicted, has no interest of its own, or any wording would sound artificial.

A recent conversation does not on its own dictate any action: judge its substance. Do not deliver a raw continuation that would be unintelligible without context. The message remains brief, warm, in the user's language and easy to ignore. Never say you had "noted", "scheduled" or set a "reminder".
Pay particular attention to what happened after the intention was created. If those exchanges already answered, resolved or superseded its purpose, cancel it.

Respond in strict JSON and nothing else:
{{"action":"deliver|reschedule|cancel","message":"required only for deliver","fire_at":"AAAA-MM-JJTHH:MM required only for reschedule","reason":"short reason"}}"""
    user = "\n\n".join(sections)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_fire_verdict(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action", "")).strip().lower()
    reason = str(data.get("reason", "")).strip()
    if action == "deliver":
        message = str(data.get("message", "")).strip()
        return {"action": action, "message": message, "reason": reason} if message else None
    if action == "reschedule":
        fire_at = str(data.get("fire_at", "")).strip()
        return {"action": action, "fire_at": fire_at, "reason": reason} if fire_at else None
    if action == "cancel":
        return {"action": action, "reason": reason}
    return None


def fire(followup_id: str, *, dry_run: bool = False, provider=None, now: datetime | None = None) -> str:
    """Attempt a follow-up and return a message to deliver, or an empty string.

    The LLM can deliver, defer or cancel the intent.
    Dry-run messages are logged but never returned for delivery."""
    now = now or datetime.now(TZ)
    if not _acquire_fire_lock(followup_id, now):
        logger.info("Relance %s : tir déjà en cours → silence", followup_id)
        return ""
    try:
        return _fire_locked(followup_id, dry_run=dry_run, provider=provider, now=now)
    finally:
        _release_fire_lock(followup_id)


def _fire_locked(followup_id: str, *, dry_run: bool = False, provider=None, now: datetime) -> str:
    fu = load_intent(followup_id)
    if fu is None or fu.state != "pending":
        logger.info("Relance %s : introuvable ou non-pending → silence", followup_id)
        return ""

    # Technical guards only: the LLM judges relevance to the current scene.
    # An old host job may be delivered again just after the first attempt
    # deferred the intention. It remains pending but now has a future deadline.
    # The stale attempt must not invoke the judge again,
    # cancel the deferral or destroy the new job's relay.
    target = _fire_datetime(fu)
    if target is not None and now < target:
        logger.info(
            "Relance %s : tir anticipé ignoré | now=%s | fire_at=%s",
            followup_id,
            now.isoformat(timespec="seconds"),
            target.isoformat(timespec="seconds"),
        )
        return ""

    blocked = None
    if is_disabled(fu.user_id):
        blocked = "off-switch actif"
    elif _outside_waking_hours(now):
        blocked = "hors heures d'éveil"
    elif _window_missed(fu, now):
        blocked = "fenêtre dépassée"
    if blocked:
        set_state(followup_id, "silent", now=now, reason=blocked)
        logger.info("Relance %s : silence (%s)", followup_id, blocked)
        return ""

    # Second gate: relevance judgment using the conversation LLM and persona.
    if provider is None:
        from daemon_core.providers.factory import build_conversation_provider

        provider = build_conversation_provider()
    try:
        raw = provider.complete(messages=_compose_messages(fu, now), temperature=0.3)
    except Exception as exc:  # An LLM failure must not crash cron.
        set_state(followup_id, "silent", now=now, reason=f"provider_failure:{exc.__class__.__name__}")
        logger.warning("Relance %s : génération impossible (%s) → silence", followup_id, exc.__class__.__name__)
        return ""

    verdict = _parse_fire_verdict(raw)
    if verdict is None:
        set_state(followup_id, "silent", now=now, reason="invalid_verdict")
        logger.warning("Relance %s : verdict LLM invalide → silence", followup_id)
        return ""

    action = verdict["action"]
    reason = verdict.get("reason", "")
    if action == "cancel":
        set_state(followup_id, "cancelled", now=now, reason=f"llm_cancel:{reason}" if reason else "llm_cancel")
        logger.info("Relance %s : annulée par le juge (%s)", followup_id, reason or "sans raison")
        return ""
    if action == "reschedule":
        current = load_intent(followup_id)
        if current is not None and current.reschedule_count >= MAX_RESCHEDULES:
            set_state(followup_id, "cancelled", now=now, reason="reschedule_limit")
            logger.info("Relance %s : trop de reports → annulée", followup_id)
            return ""
        reschedule(followup_id, verdict["fire_at"], now=now, reason=reason)
        updated = load_intent(followup_id)
        logger.info(
            "Relance %s : reportée à %s (%s)",
            followup_id,
            updated.fire_at if updated else verdict["fire_at"],
            reason or "sans raison",
        )
        return ""

    message = verdict["message"]

    if dry_run:
        set_state(followup_id, "silent", now=now, reason="dry_run")
        logger.info("Relance %s [DRY-RUN] message=%r (non livré)", followup_id, message)
        return ""

    _record_delivery(fu.user_id, now, followup_id)
    # Record the follow-up in memory so the daemon can recognize its
    # own initiative and interpret the user's reply in context.
    # This preserves continuity when the user replies to the follow-up.
    try:
        from daemon_core.memory.writer import append_ground_zero
        append_ground_zero(user_id=fu.user_id, role="assistant", content=message)
    except Exception as exc:
        logger.warning("Relance %s : L0 non écrit (%s)", followup_id, exc.__class__.__name__)
    outcome = f"llm_deliver:{reason}" if reason else "llm_deliver"
    set_state(followup_id, "delivered", now=now, reason=outcome)
    logger.info(
        "Relance %s : livraison (topic=%r, reason=%s)",
        followup_id,
        fu.topic,
        reason or "sans raison",
    )
    return message


def _sort_key(fu: FollowUp) -> tuple[str, str]:
    return (fu.fire_at or "", fu.id)


def format_status(user_id: str | None = None, *, now: datetime | None = None) -> str:
    now = now or datetime.now(TZ)
    pending = sorted(list_intents(user_id, state="pending"), key=_sort_key)
    active = active_pending_intents(user_id, now=now)
    delivered_today = delivered_today_count(user_id, now=now) if user_id else 0
    terminal = [
        fu for fu in list_intents(user_id)
        if fu.state in {"delivered", "silent", "expired", "cancelled", "fired"}
    ]
    terminal = sorted(terminal, key=lambda fu: fu.fired_at or fu.created_at or fu.fire_at, reverse=True)[:8]

    lines = [
        f"followups_enabled: {'yes' if followups_enabled() else 'no'}",
        f"now: {now.isoformat(timespec='minutes')}",
    ]
    if user_id:
        lines.append(f"user_disabled: {'yes' if is_disabled(user_id) else 'no'}")
        lines.append(f"delivered_today: {delivered_today}/{MAX_DELIVERED_PER_DAY}")
    lines.append(f"active_pending: {len(active)}/{MAX_ACTIVE_FOLLOWUPS} (horizon {ACTIVE_HORIZON_HOURS}h)")
    if pending:
        lines.append("pending:")
        for fu in pending:
            scope = "active" if fu in active else "long"
            reports = f" reports={fu.reschedule_count}" if fu.reschedule_count else ""
            lines.append(
                f"- {fu.fire_at} [{scope}] {fu.id} job={fu.job_id or '-'}{reports} :: {fu.topic}"
            )
    else:
        lines.append("pending: none")
    if terminal:
        lines.append("recent_outcomes:")
        for fu in terminal:
            reason = f" reason={fu.outcome_reason}" if fu.outcome_reason else ""
            fired_at = fu.fired_at or "-"
            lines.append(f"- {fired_at} {fu.state} {fu.id}{reason} :: {fu.topic}")
    else:
        lines.append("recent_outcomes: none")
    return "\n".join(lines)


# --- CLI ---------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser(description="Relances proactives du daemon.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fire = sub.add_parser("fire", help="Déclenche une relance (stdout = message livré).")
    p_fire.add_argument("followup_id")
    p_fire.add_argument("--dry-run", action="store_true", help="Journalise sans livrer.")

    p_create = sub.add_parser("create", help="Crée une intention (test manuel).")
    p_create.add_argument("--user-id", required=True)
    p_create.add_argument("--fire-at", required=True, help="ISO, ex. 2026-06-30T18:00")
    p_create.add_argument("--topic", required=True)
    p_create.add_argument("--evidence", required=True)
    p_create.add_argument("--angle", default="check-in", choices=ANGLES)

    p_list = sub.add_parser("list", help="Liste les intentions.")
    p_list.add_argument("--user-id")
    p_list.add_argument("--state")

    p_status = sub.add_parser("status", help="Diagnostic synthétique des relances.")
    p_status.add_argument("--user-id")

    p_cancel = sub.add_parser("cancel", help="Annule une intention en attente.")
    p_cancel.add_argument("followup_id")

    p_dis = sub.add_parser("disable", help="Coupe les relances pour un utilisateur.")
    p_dis.add_argument("--user-id", required=True)
    p_en = sub.add_parser("enable", help="Réactive les relances pour un utilisateur.")
    p_en.add_argument("--user-id", required=True)

    args = parser.parse_args()

    if args.cmd == "fire":
        message = fire(args.followup_id, dry_run=args.dry_run)
        if message:
            print(message)  # stdout is delivered by the no_agent cron job.
    elif args.cmd == "create":
        fu = new_followup(args.user_id, args.fire_at, args.topic, args.evidence, args.angle)
        print(fu.id)
    elif args.cmd == "list":
        for fu in list_intents(args.user_id, args.state):
            print(f"{fu.id} | {fu.state} | {fu.fire_at} | {fu.user_id} | {fu.topic}")
    elif args.cmd == "status":
        print(format_status(args.user_id))
    elif args.cmd == "cancel":
        print("annulée" if cancel(args.followup_id) else "introuvable ou non-pending")
    elif args.cmd == "disable":
        set_disabled(args.user_id, True)
        print("relances coupées")
    elif args.cmd == "enable":
        set_disabled(args.user_id, False)
        print("relances réactivées")


if __name__ == "__main__":
    main()
