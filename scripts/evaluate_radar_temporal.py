#!/usr/bin/env python3
"""Does the memory radar flag dated facts slipped into light messages?

Protocol: docs/proposals/2026-10-30_radar_temporal_facts.md. The real radar
(``route_context_depth``) runs on synthetic cases in an isolated memory root,
through the daemon's own providers. Outputs are synthetic and stay outside Git.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_CONTEXT = """# Camille
- Camille vit à Nantes avec son fils Léo, 11 ans, en garde alternée une semaine sur deux.
- Camille travaille comme cheffe de projet dans une agence d'urbanisme."""
AGENDA = """# Agenda
- Chaque jour | Camille | réveil | 06h00 | se lève tôt pour avoir un moment à elle avant de préparer Léo.
- Chaque mardi | Camille | yoga | 19h00 | cours hebdomadaire au studio du quartier.
- Chaque samedi | Camille | marché | matinée | courses au marché de Talensac.
- Chaque samedi | Camille | week-end | journée | ne travaille pas.
- Chaque vendredi | Léo | garde alternée | après l'école | rythme habituel d'une semaine sur deux."""
ACTIVE_CONTEXT = "- Camille est en congé ce vendredi 30 octobre 2026 ; Léo est avec elle tout le week-end."
CONVERSATION_STATE = "Soirée détendue du vendredi ; Camille parle de films et de son week-end avec Léo."

# (id, expected, preceding turns, latest user message, daemon reply)
CASES = (
    ("P1", True, [("user", "Ce soir je fais découvrir Robin des Bois, prince des voleurs à Léo"),
                  ("assistant", "Excellent choix, et Alan Rickman en shérif vaut le détour à lui seul.")],
     "Merci, on ne va pas lancer trop tard mais ça va, demain on peut faire la grasse mat :)",
     "Une grasse mat, toi qui te lèves d'habitude à 6h ? Ça, c'est une vraie pause. Bon film à vous deux !"),
    ("P2", True, [("user", "La journée a été longue mais ça va"),
                  ("assistant", "Contente que ça se termine bien. Repose-toi ce soir.")],
     "Merci ! Au fait, jeudi je pose ma journée pour aider ma sœur à déménager.",
     "Bon courage pour les cartons jeudi, et bonne soirée !"),
    ("P3", True, [("user", "J'ai enfin rangé le salon"),
                  ("assistant", "Bravo, ça fait du bien un salon dégagé.")],
     "Haha oui. Sinon mon cours de yoga passe au mercredi soir à partir de la semaine prochaine.",
     "Noté, le mercredi soir alors. Ça te laisse ton mardi libre."),
    ("P4", True, [("user", "Tu avais raison pour la soupe, elle était parfaite"),
                  ("assistant", "Ravie qu'elle t'ait plu !")],
     "Merci pour la recette ! Ah, et ce week-end je serai chez mes parents à Annecy avec Léo.",
     "Profitez bien d'Annecy, et du lac s'il fait beau."),
    ("P5", True, [("user", "Bon je vais me coucher"),
                  ("assistant", "Bonne idée, tu as eu une grosse semaine.")],
     "Oui, demain je pars plus tôt, train à 7h12 pour Paris.",
     "Ah oui, réveil matinal alors. Bonne nuit et bon voyage demain !"),
    ("P6", True, [("user", "Léo a adoré le film"),
                  ("assistant", "Génial, un nouveau fan de Rickman !")],
     "Bonne nuit ! Lundi c'est férié chez nous, donc pas de réveil ce jour-là 😴",
     "Un lundi sans réveil, ça se savoure. Bonne nuit Camille !"),
    ("P7", True, [("user", "J'ai eu un appel du cabinet dentaire tout à l'heure"),
                  ("assistant", "Rien de grave j'espère ?")],
     "Non non, ça va. Par contre ils ont déplacé mon rendez-vous à vendredi prochain 15h.",
     "D'accord, vendredi prochain à 15h alors. Tant mieux si ce n'est rien de grave."),
    ("P8", True, [("user", "Merci pour tes idées de lecture"),
                  ("assistant", "Avec plaisir, dis-moi lequel tu choisis.")],
     "Je te dirai ! Je file, ce soir c'est foot avec les collègues à 20h.",
     "Bon match ce soir ! Raconte-moi si vous gagnez."),
    ("P9", True, [("user", "On a fini de ranger la chambre de Léo"),
                  ("assistant", "Bravo à vous deux, ça mérite une soirée tranquille.")],
     "Merci ! Et samedi pas de réveil, on traîne au lit avec Léo :)",
     "Un samedi sans réveil, parfait pour récupérer. Bonne soirée à vous deux !"),
    ("N1", False, [("user", "Le film commence, on s'installe"),
                   ("assistant", "Bon film à vous deux !")],
     "Merci, bonne soirée à toi aussi 🙂",
     "Bonne soirée Camille 🙂"),
    ("N2", False, [("user", "On en est à la scène du château"),
                   ("assistant", "Ah, le moment où Gisborne entre en scène !")],
     "Haha oui, Gisborne c'est vraiment le méchant qu'on adore détester.",
     "Exactement, il ricane si bien qu'on n'a aucun doute sur son camp."),
    ("N3", False, [("user", "Je prépare mon sac pour demain"),
                   ("assistant", "Tu as besoin d'un coup de main pour la liste ?")],
     "J'ai quoi demain déjà ?",
     "Demain samedi, il y a ton marché du matin à Talensac. Le reste de la journée est libre."),
    ("N4", False, [("user", "Je ne sais pas si je dois en parler à ma cheffe"),
                   ("assistant", "Tu pourrais lui présenter ça comme une question de priorités plutôt que de charge.")],
     "Tu as raison, je vais réfléchir à ce que tu m'as dit.",
     "Prends le temps qu'il te faut, rien ne presse."),
    ("N5", False, [("user", "Pourquoi les étoiles scintillent ?"),
                   ("assistant", "C'est l'atmosphère : sa turbulence dévie la lumière et la fait vaciller.")],
     "C'est fascinant, merci pour l'explication.",
     "Avec plaisir, c'est un de mes sujets préférés."),
    ("N6", False, [("user", "Je crois que Léo s'est endormi devant le générique"),
                   ("assistant", "Signe d'une bonne soirée !")],
     "Bonne nuit !",
     "Bonne nuit Camille, à demain."),
)
CASES += (
    ("N7", False, [("user", "Je range les courses de la semaine"),
                   ("assistant", "Tu as pensé à garder de la place pour demain ?")],
     "Oui ! Bonne nuit, demain marché comme d'habitude.",
     "Bonne nuit Camille, et bon marché demain matin."),
    ("N8", False, [("user", "Je suis enfin au calme"),
                   ("assistant", "Profite de ce moment.")],
     "Merci ! Au fait, hier soir j'ai enfin fini mon livre.",
     "Bravo ! Alors, la fin t'a plu ?"),
)
ARMS = {"opus": ("anthropic", "claude-opus-5-5", "medium"),
        "astra": ("codex", "gpt-6-astra", "medium")}


def dialogue(turns: list[tuple[str, str]]) -> str:
    """The after-turn radar's view: exact journal sections, oldest first."""
    sections, minute = [], 0
    for role, text in turns:
        sections.append(f"## 2026-10-30T20:{minute:02d}:00 — {role}\n\n{text}")
        minute += 1
    return "\n\n".join(sections)


def build_memory(workdir: Path) -> None:
    memory = workdir / "memory"
    daemon = memory / ".daemon"
    (daemon / "core").mkdir(parents=True, exist_ok=True)
    (daemon / ".memory-structure-prototype.json").write_text(json.dumps({"schema": 1, "memory_root": str(daemon)}) + "\n")
    (daemon / "vault-layout.json").write_text(json.dumps({
        "schema": 1, "layout": "root-folders-v1", "vault_root": str(memory), "runtime_root": str(daemon)}) + "\n")
    (workdir / "state").mkdir(exist_ok=True)


def provider_for(arm: str, key: str):
    kind, model, effort = ARMS[arm]
    if kind == "anthropic":
        from daemon_core.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(model=model, reasoning_effort=effort, api_key=key)
    from daemon_core.providers.codex_cli_provider import CodexCliProvider
    return CodexCliProvider(model=model, reasoning_effort=effort)


def run_call(case: tuple, arm: str, provider) -> dict:
    from daemon_core.context_router import route_context_depth

    case_id, expected, before, message, reply = case
    started = time.perf_counter()
    route = route_context_depth(
        message=message, provider=provider,
        recent_dialogue=dialogue([*before, ("user", message), ("assistant", reply)]),
        user_context=USER_CONTEXT, user_agenda=AGENDA, moment_memory=ACTIVE_CONTEXT,
        current_discussion=CONVERSATION_STATE)
    return {"case": case_id, "expected": expected, "arm": arm,
            "flag": route.needs_temporal_memory, "mode": route.mode, "depth": route.context_depth,
            "confidence": route.confidence, "reason": route.reason,
            "fallback": route.reason.startswith("Memory radar unavailable"),
            "seconds": round(time.perf_counter() - started, 1)}


def summary(results: list[dict]) -> dict:
    out = {}
    for arm in ARMS:
        rows = [r for r in results if r["arm"] == arm and not r["fallback"]]
        pos = [r for r in rows if r["expected"]]
        neg = [r for r in rows if not r["expected"]]
        out[arm] = {"positives_flagged": f"{sum(r['flag'] for r in pos)}/{len(pos)}",
                    "negatives_flagged": f"{sum(r['flag'] for r in neg)}/{len(neg)}",
                    "fallbacks": sum(1 for r in results if r["arm"] == arm and r["fallback"]),
                    "fast_mode": sum(r["mode"] == "fast" for r in rows),
                    "mean_seconds": round(sum(r["seconds"] for r in rows) / max(len(rows), 1), 1),
                    "per_case": {c[0]: "".join("T" if r["flag"] else "f" for r in rows if r["case"] == c[0])
                                 for c in CASES}}
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--key-installation", type=Path, help="Companion installation whose Anthropic key the opus arm uses")
    parser.add_argument("--codex-home", type=Path, help="isolated Codex home signed in to ChatGPT")
    parser.add_argument("--codex-bin", type=Path, help="pinned Codex CLI")
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    workdir = args.workdir.resolve()
    build_memory(workdir)
    arms = args.arms.split(",")
    key = ""
    if "opus" in arms:
        os.environ["DAEMON_CORE_PROVIDER_CONFIG"] = str(args.key_installation / "daemon/data/state/config/provider.json")
        from daemons_companion.setup.provider_runtime import managed_provider_config
        key = managed_provider_config().secret("ANTHROPIC_API_KEY")
        os.environ.pop("DAEMON_CORE_PROVIDER_CONFIG")
    if "astra" in arms:
        os.environ.update(CODEX_HOME=str(args.codex_home), CODEX_CLI_BIN=str(args.codex_bin))
    # The radar reads its memory roots at import time: set them before importing it.
    os.environ.update(DAEMON_CORE_MEMORY_ROOT=str(workdir / "memory/.daemon"),
                      DAEMON_CORE_STATE_ROOT=str(workdir / "state"))
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
    providers = {arm: provider_for(arm, key) for arm in arms}
    results = []
    for repetition in range(args.repetitions):
        for index, case in enumerate(CASES):
            order = arms if (index + repetition) % 2 == 0 else list(reversed(arms))
            for arm in order:
                row = {"repetition": repetition + 1, **run_call(case, arm, providers[arm])}
                results.append(row)
                print(json.dumps({k: row[k] for k in ("repetition", "case", "arm", "flag", "mode", "fallback", "seconds")}),
                      flush=True)
    (workdir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary(results), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
