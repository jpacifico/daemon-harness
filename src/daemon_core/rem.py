"""REM narrative consolidation of past episodes.

Nightly Dreaming invokes REM, which reads exact dialogue accumulated since
its last successful watermark. Density or elapsed time makes an episode
eligible for a narrative note. The full span is kept in its metadata.

Episodes use the normal memory indexing and recall path. A stable name based
on the span's start prevents duplicates when replaying a failed write.
The watermark advances only after success and can be rebuilt from the latest
episode when its state file is missing."""

import argparse
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from daemon_core.memory_dates import MEMORY_DATE_RULES, normalize_memory_dates
from daemon_core.dreaming import load_l0_window
from daemon_core.memory.integrity import (
    atomic_write_text,
    memory_file_lock,
)
from daemon_core.memory.reader import souvenir_index_path, load_souvenir, load_trajectory
from daemon_core.narrator import run_narrator
from daemon_core.memory.writer import safe_memory_user_id
from daemon_core.paths import MEMORY_ROOT, PROJECT_ROOT
from daemon_core.prompt_composer import current_time_context
from daemon_core.prompt_logging import log_llm_prompt
from daemon_core.providers.base import LLMProvider

logger = logging.getLogger("daemon_core.rem")

MAX_L0_CHARS_REM = 400_000  # REM reads the full period offline; a dense week is roughly 150k characters.
CHRONIQUES_DOMAIN = "theme/chroniques"

# Episode triggers: initial values, adjustable from observed behavior.
REM_DENSITY_THRESHOLD_CHARS = 50_000   # A dense episode matures before the time-based fallback.
REM_MAX_INTERVAL_HOURS = 10 * 24       # Time fallback: summarize even a quiet period beyond this interval.
REM_MAX_SPAN_HOURS = 30 * 24           # Read bound: content beyond max_chars is intentionally excluded.
REM_MIN_CONTENT_CHARS = 200            # Below this limit, there is too little material for an episode.
DEFAULT_BOOTSTRAP_HOURS = 7 * 24       # Bootstrap from one week when neither an episode nor a watermark exists.


def chronique_name(span_start: datetime) -> str:
    return f"{CHRONIQUES_DOMAIN}/{span_start.date().isoformat()}"


def span_label(span_start: datetime, span_end: datetime) -> str:
    return f"{span_start:%d/%m} → {span_end:%d/%m/%Y}"


def _state_path(user_id: str) -> Path:
    return MEMORY_ROOT / "levels" / f"rem_state_{safe_memory_user_id(user_id)}.json"


def _latest_chronique_path() -> Path | None:
    from daemon_core.memory.library_layout import unique_note_locations
    files = [item.path for item in sorted(unique_note_locations(MEMORY_ROOT), key=lambda item: item.name)
             if item.name.startswith(CHRONIQUES_DOMAIN + "/")]
    return files[-1] if files else None


def _chronique_span_end(text: str) -> datetime | None:
    for line in text.splitlines()[:12]:
        stripped = line.strip()
        if stripped.startswith("span_end:"):
            try:
                return datetime.fromisoformat(stripped.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def read_watermark(user_id: str, now: datetime | None = None) -> datetime:
    """Read the episode watermark, rebuild it from the latest note, or initialize it."""
    now = now or datetime.now()
    path = _state_path(user_id)
    if path.exists():
        try:
            return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["watermark"])
        except (ValueError, KeyError, json.JSONDecodeError):
            logger.warning("REM : watermark illisible (%s) — reconstruction", path.name)
    latest = _latest_chronique_path()
    if latest is not None:
        rebuilt = _chronique_span_end(latest.read_text(encoding="utf-8"))
        if rebuilt is not None:
            logger.info("REM : watermark reconstruit depuis %s → %s", latest.name, rebuilt.isoformat(timespec="minutes"))
            return rebuilt
    return now - timedelta(hours=DEFAULT_BOOTSTRAP_HOURS)


def _reconcile_watermark(user_id: str, chronique: Path, watermark: datetime) -> bool:
    """Align a stale watermark with an already written episode's span_end.

    Return True when recovery changed the watermark."""
    end = _chronique_span_end(chronique.read_text(encoding="utf-8"))
    if end is not None and end > watermark:
        write_watermark(user_id, end)
        return True
    return False


def write_watermark(user_id: str, ts: datetime) -> None:
    path = _state_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"watermark": ts.isoformat(timespec="seconds")}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def run_rem(user_id: str, provider: LLMProvider, now: datetime | None = None, force: bool = False, dry_run: bool = False) -> dict:
    """Write the current episode when it is mature.

    Return {"status": ..., "chronique": name}; legacy status identifiers are
    ecrite, deja-ecrite, pas-mur, vide and dry-run."""
    now = now or datetime.now()
    watermark = read_watermark(user_id, now=now)
    span_start = watermark
    name = chronique_name(span_start)
    from daemon_core.memory.library_layout import resolve_note, vault_root, with_identity
    target = resolve_note(MEMORY_ROOT, name, create=True,
                          storage_path="Archives/Chronicles/" + name.rsplit("/", 1)[-1] + ".md")
    if target.exists() and not force:
        # Recovery: if the episode file exists but the watermark still points
        # to the span's beginning, a crash may have interrupted its update.
        # Align it with the written episode's span_end;
        # otherwise further episode generation would remain blocked.
        _repair_chronique_discoverability(name)
        healed = _reconcile_watermark(user_id, target, watermark)
        logger.info("REM : chronique %s déjà écrite — %s", name, "watermark réaligné" if healed else "rien à faire")
        return {"status": "deja-ecrite", "chronique": name}

    gap_hours = max(0.0, (now - watermark).total_seconds() / 3600)
    read_hours = max(1.0, min(gap_hours, REM_MAX_SPAN_HOURS))
    l0_recent = load_l0_window(user_id=user_id, since_hours=read_hours, now=now, max_chars=MAX_L0_CHARS_REM)
    chars = len(l0_recent.strip())

    if chars < REM_MIN_CONTENT_CHARS:
        logger.info("REM : rien à chroniquer depuis %s (%s chars) — silence", span_start.date(), chars)
        return {"status": "vide", "chronique": name}
    if not force and chars < REM_DENSITY_THRESHOLD_CHARS and gap_hours < REM_MAX_INTERVAL_HOURS:
        logger.info("REM : épisode en cours (%s chars, %.0fh) — pas encore mûr", chars, gap_hours)
        return {"status": "pas-mur", "chronique": name}

    span = span_label(span_start, now)
    trajectory = run_narrator(user_id=user_id, provider=provider)
    messages = _build_messages(
        user_id=user_id,
        l0_recent=l0_recent,
        trajectory=trajectory or load_trajectory(user_id),
        previous_chronique=_latest_chronique_text(),
        name=name,
        span=span,
    )
    log_llm_prompt(logger, "REM", messages)
    raw = provider.complete(messages=messages, temperature=0.4)
    data = json.loads(_strip_json_fence(raw))
    chronique_md = normalize_memory_dates(str(data.get("chronique_md", "") or "").strip())
    index_line = str(data.get("index_line", "") or "").strip()
    if not chronique_md:
        logger.info("REM : rien produit — silence")
        return {"status": "vide", "chronique": name}

    if dry_run:
        logger.info("REM [DRY-RUN] chronique=%s span=%s\n%s", name, span, chronique_md)
        return {"status": "dry-run", "chronique": name}

    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = _with_chronique_frontmatter(chronique_md, span_start, now).strip() + "\n"
    if vault_root(MEMORY_ROOT) is not None:
        rendered = with_identity(rendered, name)
    atomic_write_text(
        target,
        rendered,
    )
    _append_index_line(name, index_line)
    _refresh_domain_line()
    write_watermark(user_id, now)  # Advance LAST: if an earlier step fails, the same span can be replayed.
    logger.info("REM : chronique %s écrite (%s chars, span %s)", name, len(chronique_md), span)
    return {"status": "ecrite", "chronique": name}


def _refresh_domain_line() -> None:
    """Expose recent episode anchors in the domain's root entry.

    Without those places, events and user terms at the root,
    the radar may never request the expansion that contains them."""
    index = souvenir_index_path()
    if not index.exists():
        return
    with memory_file_lock(index):
        lines = index.read_text(encoding="utf-8").splitlines()
        entries = sorted(
            line.strip()
            for line in lines
            if line.strip().startswith(
                f"- {CHRONIQUES_DOMAIN}/"
            )
        )
        if not entries:
            return
        recents = []
        for entry in entries[-3:]:
            name, _, payload = entry[2:].partition(": ")
            debut = name.rsplit("/", 1)[-1]
            recents.append(
                f"{debut} ({payload.strip()[:110].rstrip()})"
            )
        domain_line = (
            f"- {CHRONIQUES_DOMAIN}: Past episodic archives, to recall only for retrospective requests "
            f"(“do you remember the week when…”, the origin of a decision, resuming after an absence). Recent: "
            + " ; ".join(recents)
        )
        kept = [
            line
            for line in lines
            if not line.strip().startswith(
                f"- {CHRONIQUES_DOMAIN}:"
            )
        ]
        kept.append(domain_line)
        atomic_write_text(
            index,
            "\n".join(kept).strip() + "\n",
        )


def _repair_chronique_discoverability(name: str) -> None:
    """Repair every derived index before accepting an existing chronicle."""

    _append_index_line(name, "")
    _refresh_domain_line()


def _build_messages(user_id: str, l0_recent: str, trajectory: str, previous_chronique: str, name: str, span: str) -> list[dict]:
    system = f"""You are Daemon Harness REM — memory, not the archive.

Slow-wave sleep organizes facts; you write what will be REMEMBERED of this period ({span}). A chronicle is not a report of everything said — it is the SELECTIVE memory of what mattered. Like a human: retain what left a mark, how things began, the turning points; forget the rest, and forgetting is work, not a malfunction.

YOUR sole CRITERION: "What from this period will still matter in a month?"
- WHAT STAYS: a significant event, a turning point, the beginning of a thread or decision, a strong relational moment, what was built or settled together. Also retain the small detail THAT CARRIES MEANING — a song, an object, a word — when it resonates: its value lies in what it conveys, not its size (the first song on a playlist, a pair of sneakers that conveys something: that stays).
- WHAT GOES: the HYPOTHETICAL. A memory retains what HAPPENED, was DECIDED or BUILT — not what was merely discussed. An idea, an avenue, an outing or scheduling option explored but not yet realized is NOT a memory: discard it, including its name, place and time (a tournament "we could do", a search for shops). An idea enters only when it becomes real. Likewise discard logistics, lists, searches and checks, small talk. ALSO discard pure facts: a preference, a list of tastes, a stable fact about the person or someone close (films Sam likes, a list of projects) — a memory is not a fact sheet. A fact enters ONLY if its emergence was an event: the moment they discovered or decided it, or when it mattered between you; never as an inventory.

Rules:
- Selective AND compact: one movement of a few sentences, not sections or successive paragraphs. Even a rich period fits in 5 to 8 sentences — a memory, not a report. No padding, no shortcut that loses a genuine memory. Do not tell the period — tell what will be retained from it.
- Still an arc: where they were coming from, what unfolded, where they arrive. Each sentence carries something that matters, never padding.
- Connect distant things when the connection is true (recombination) — that is your own work, what the ongoing stream never does.
- Faithful to L0: nothing invented, no heavy psychologizing, no decorative lyricism. A restrained chronicler who knows the subject. Do not begin a sentence you cannot finish.
- `index_line`: exact format `- {name}: <short description> — <concrete anchors: decisions, turning points, people, proper names, the user's words>`. Anchors are recall handles: what the person will use to evoke this period later.
- Episodic archive of the past: avoid times, tasks and current states as anchors unless they help identify the episode.
- Fabricate no daemon inner life, write nothing for the Stance Register: REM recounts episodes.

Respond in strict JSON: {{"reason": "…", "chronique_md": "# Chronique {span}\\n\\n…", "index_line": "- {name}: …"}}"""
    system += "\n\n" + MEMORY_DATE_RULES
    user = f"""Current time:
{current_time_context()}

User: {safe_memory_user_id(user_id)}

Current trajectory:
{trajectory.strip() or "None."}

Previous chronicle:
{previous_chronique.strip()[:1500] or "None — this is the first."}

L0 for the period (read in full):
{l0_recent}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _latest_chronique_text() -> str:
    latest = _latest_chronique_path()
    return latest.read_text(encoding="utf-8") if latest else ""


def _with_chronique_frontmatter(chronique_md: str, span_start: datetime, span_end: datetime) -> str:
    text = chronique_md.strip()
    if text.startswith("---"):
        return text
    return f"""---
memory_type: episodic_archive
truth_scope: past_only
span_start: {span_start.isoformat(timespec='seconds')}
span_end: {span_end.isoformat(timespec='seconds')}
---

{text}"""


def _append_index_line(name: str, index_line: str) -> None:
    index = souvenir_index_path()
    if not index_line.startswith(f"- {name}:"):
        title = _chronique_title(load_souvenir(name)) or name
        index_line = f"- {name}: {title}"
    with memory_file_lock(index):
        content = (
            index.read_text(encoding="utf-8")
            if index.exists()
            else ""
        )
        if f"- {name}:" in content:
            return
        atomic_write_text(
            index,
            content.rstrip()
            + ("\n" if content.strip() else "")
            + index_line
            + "\n",
        )


def _chronique_title(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        stripped = stripped.removesuffix("```").strip()
    return stripped


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    _load_dotenv_if_available()
    parser = argparse.ArgumentParser(description="REM : mémoire épisodique greffée sur le dreaming nocturne.")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--force", action="store_true", help="Écrit l'épisode courant même s'il n'est pas mûr (idempotent par span).")
    parser.add_argument("--dry-run", action="store_true", help="Compose sans écrire.")
    parser.add_argument(
        "--episodic-scenes",
        action="store_true",
        help=(
            "Utilise explicitement les scènes REM sourcées pour cette passe. "
            "Sans ce drapeau, seule la canary utilisateur peut les activer."
        ),
    )
    args = parser.parse_args()

    from daemon_core.providers.factory import build_memory_provider
    from daemon_core.registre import (
        faculty_lock,
        needs_retry,
        record_pass,
    )

    provider = build_memory_provider()
    retrying = needs_retry("rem", args.user_id)
    with faculty_lock("rem", args.user_id) as acquired:
        if not acquired:
            print(json.dumps({"status": "verrou-tenu"}, ensure_ascii=False))
            return
        try:
            from daemon_core.episodic_rem import (
                episodic_rem_enabled,
                run_episodic_rem,
            )

            if args.episodic_scenes or episodic_rem_enabled(args.user_id):
                result = run_episodic_rem(
                    args.user_id,
                    provider=provider,
                    force=args.force,
                    dry_run=args.dry_run,
                )
            else:
                result = run_rem(
                    args.user_id,
                    provider=provider,
                    force=args.force,
                    dry_run=args.dry_run,
                )
        except Exception as exc:
            record_pass("rem", args.user_id, "echec", detail=f"{exc.__class__.__name__}: {exc}")
            raise
        # A write, an explicit dry run or a judged abstention is a successful pass.
        # Immature or empty spans and replays remain silent.
        if result["status"] in ("ecrite", "dry-run", "abstention") or retrying:
            subject = result.get("chronique") or ",".join(result.get("episodes", ()))
            record_pass(
                "rem",
                args.user_id,
                "ok",
                detail=f"{subject or 'aucune-scene'}: {result['status']}",
            )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
