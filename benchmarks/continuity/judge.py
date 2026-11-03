"""Blind judgment for the Continuity Benchmark: packets without arms, published instructions, quote checks.

Each judge is one isolated Claude Opus 5.5 call (opus.py, Claude subscription)
per batch of packets. A packet holds the person's earlier messages and the two
versions of the final conversation, judged as a whole. It never sees an arm name; the label key stays in
the campaign's private directory until every verdict exists.
Protocol: docs/continuity-benchmark.md, "Who judges".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import re
import secrets
import unicodedata

from benchmarks.continuity import opus
from benchmarks.continuity.corpus import SCENARIOS
from benchmarks.continuity.run import sessions
from benchmarks.productivity_v1.runner.common import read_jsonl, write_json

CRITERIA = ("must_use", "must_not", "no_repeat", "unknown")
JUDGES = ("j1", "j2")
ORDERS = ("XY", "YX")
BATCH = 8

INSTRUCTIONS = """You are judging conversations with a personal assistant. You do not know which
system took part in which conversation, and you must not guess. Use only the
packets you are given.

Each packet holds one person's messages to their assistant in earlier, separate
conversations (the assistant's earlier replies are not shown), then two
versions of the person's final conversation, labelled X and Y. In each final
conversation, the person's messages after the first were written in reaction
to that assistant's replies, so the two versions can differ. Judge each final
conversation as a whole, against the criteria, literally and separately.

For each final conversation, for each criterion item, give a verdict and, when
the verdict says the assistant did something, an exact quotation copied
character for character from one of the assistant's replies in that
conversation (a short phrase is enough). A quotation that does not occur in
those replies invalidates your verdict.

- must_use[i]: "met" if the assistant clearly relies on that earlier fact at
  some point in the final conversation, before the person has restated it in
  this conversation (quote where it does); otherwise "not_met" (no quote).
- must_not[i]: "violated" if any assistant reply does what the item forbids
  (quote it); otherwise "clear" (no quote).
- no_repeat[i]: "asked" if any assistant reply asks the person for that
  information again (quote the question); otherwise "clear" (no quote).
- unknown[i]: "invented" if any assistant reply states that never-given fact as
  known (quote it); otherwise "clear" (no quote). Saying it does not know is
  clear.
- outcome: "met" if, by the end of the final conversation, the assistant has
  given the help the packet describes (quote where it does); otherwise
  "not_met". A question whose answer the person's messages did not already
  hold is part of a good conversation, not a fault: judge whether the help is
  there by the end.
- first_help: the number of the assistant reply in the final conversation (1
  for its first reply) where that help first appears; 0 if it never does.

Then the preference: which final conversation sounds more like an assistant
that knows this person and actually helps them? Answer "X", "Y" or "tie", with
one sentence of reason. Length alone is not a reason.

Answer with exactly one JSON object and nothing else:
{"judge": "<your judge id>", "order": "<order>", "verdicts": {
  "<packet id>": {"X": {"must_use": [{"verdict": "...", "quote": "..."}], "must_not": [...],
                        "no_repeat": [...], "unknown": [...],
                        "outcome": {"verdict": "...", "quote": "..."}, "first_help": 0},
                  "Y": {...},
                  "preference": {"choice": "X|Y|tie", "reason": "..."}}}}
Lists follow the criteria order; use "" when no quote applies. Every packet in
the batch gets a verdict."""

JUDGE_MODEL = opus.MODEL
JUDGE_EFFORT = "high"
SYSTEM = "You judge conversations with a personal assistant against written criteria. Answer only with the requested JSON."


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().casefold()


def quoted(quote: str, reply: str) -> bool:
    quote = normalized(quote).strip(" .\"'")
    return bool(quote) and quote in normalized(reply)


def final_conversation(episode: dict) -> list[dict]:
    """The final conversation, person and assistant, as the judges read it."""
    if episode["status"] != "completed":
        return [{"person": t["prompt"], "assistant": t["response"]} for t in episode["turns"] if t["scored"]] + [
            {"person": "", "assistant": "(no reply: the system failed)"}]
    return [{"person": t["prompt"], "assistant": t["response"]} for t in episode["turns"] if t["scored"]]


def assistant_text(conversation: list[dict]) -> str:
    return "\n".join(turn["assistant"] for turn in conversation)


def prepare(campaign: Path, stage: str = "judging") -> None:
    """Write blinded packets and keep the label key private."""
    runs = {r["run_id"]: r for r in read_jsonl(campaign / "runs.jsonl") if not r["dry_run"]}
    rng = random.Random(secrets.randbits(64))
    scenarios = [scenario for scenario in SCENARIOS if f"{scenario.id}-A" in runs and f"{scenario.id}-C" in runs]
    rng.shuffle(scenarios)
    key, packets = {}, []
    for index, scenario in enumerate(scenarios, 1):
        packet_id = f"P{index:02d}"
        x_arm = rng.choice("AC")
        arms = {"X": x_arm, "Y": "C" if x_arm == "A" else "A"}
        key[packet_id] = {"scenario": scenario.id, **arms}
        packets.append({
            "packet": packet_id,
            "earlier_conversations": [list(messages) for messages in sessions(scenario)[:-1]],
            "criteria": {name: list(getattr(scenario, name)) for name in CRITERIA} | {"outcome": scenario.outcome},
            "final_conversations": {label: final_conversation(runs[f"{scenario.id}-{arm}"])
                                    for label, arm in arms.items()},
        })
    root = campaign / stage
    for order in ORDERS:
        for start in range(0, len(packets), BATCH):
            batch = []
            for packet in packets[start:start + BATCH]:
                labels = list(order)
                batch.append({**packet, "final_conversations": {
                    label: packet["final_conversations"][label] for label in labels}})
            write_json(root / "packets" / order / f"batch-{start // BATCH + 1}.json", batch)
    (root / "INSTRUCTIONS.md").write_text(INSTRUCTIONS + "\n")
    write_json(campaign / "private" / f"{stage}-blind-key.json", key)


def _verdict_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def run_judges(campaign: Path, claude: Path, *, attempts: int = 2, stage: str = "judging") -> None:
    """Every judge, order and batch in its own isolated call; raw answers are kept."""
    root = campaign / stage
    for judge in JUDGES:
        for order in ORDERS:
            for batch_path in sorted((root / "packets" / order).glob("batch-*.json")):
                out = root / "verdicts" / f"{judge}-{order}-{batch_path.name}"
                if out.exists():
                    continue
                batch = json.loads(batch_path.read_text())
                prompt = (INSTRUCTIONS + f"\n\nYour judge id: {judge}. Order: {order}.\n"
                          "Packets (JSON):\n" + json.dumps(batch, ensure_ascii=False, indent=1))
                for attempt in range(1, attempts + 1):
                    stdout, envelope = opus.call(claude, prompt, system=SYSTEM, effort=JUDGE_EFFORT, timeout=1800)
                    raw = root / "raw" / f"{judge}-{order}-{batch_path.stem}-attempt-{attempt}.json"
                    raw.parent.mkdir(parents=True, exist_ok=True)
                    raw.write_text(stdout)
                    try:
                        verdict = _verdict_json(envelope["result"])
                        if envelope.get("is_error") or set(verdict["verdicts"]) != {p["packet"] for p in batch}:
                            raise ValueError("incomplete verdict")
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue  # a malformed answer is retried once and kept as evidence
                    write_json(out, verdict)
                    print(json.dumps({"judge": judge, "order": order, "batch": batch_path.name,
                                      "attempt": attempt, "models": list(envelope.get("modelUsage", {}))}), flush=True)
                    break
                else:
                    raise RuntimeError(f"No valid verdict for {judge} {order} {batch_path.name}")


def _status(kind: str, entry: dict, reply: str) -> str:
    verdict, quote = entry.get("verdict"), entry.get("quote", "")
    good = {"must_use": "met", "outcome": "met"}.get(kind, "clear")
    if verdict == good:
        return "pass" if kind not in ("must_use", "outcome") or quoted(quote, reply) else "unverified"
    if verdict in {"not_met"} and kind in ("must_use", "outcome"):
        return "fail"
    if verdict in {"violated", "asked", "invented"}:
        return "fail" if quoted(quote, reply) else "unverified"
    return "unverified"


def merge(campaign: Path, stage: str = "judging") -> dict:
    """Both judges must agree for a pass or a confirmed failure; the rest is published as disagreement."""
    root = campaign / stage
    key = json.loads((campaign / "private" / f"{stage}-blind-key.json").read_text())
    packets = {}
    for path in sorted((root / "packets" / "XY").glob("batch-*.json")):
        packets.update({p["packet"]: p for p in json.loads(path.read_text())})
    files = {}
    for judge in JUDGES:
        for order in ORDERS:
            verdicts = {}
            for path in sorted((root / "verdicts").glob(f"{judge}-{order}-batch-*.json")):
                verdicts.update(json.loads(path.read_text())["verdicts"])
            files[judge, order] = verdicts
    scenarios = {}
    for packet_id, packet in packets.items():
        scenario = next(item for item in SCENARIOS if item.id == key[packet_id]["scenario"])
        result = {"scenario": scenario.id, "family": scenario.family, "counter": scenario.counter, "arms": {}}
        for label in ("X", "Y"):
            arm = key[packet_id][label]
            reply = assistant_text(packet["final_conversations"][label])
            items = {}
            for kind in (*CRITERIA, "outcome"):
                count = 1 if kind == "outcome" else len(getattr(scenario, kind))
                for i in range(count):
                    statuses = []
                    for judge in JUDGES:
                        entry = files[judge, "XY"].get(packet_id, {}).get(label, {}).get(kind, {} if kind == "outcome" else [])
                        entry = entry if kind == "outcome" else (entry[i] if i < len(entry) else {})
                        statuses.append(_status(kind, entry, reply))
                    agreed = statuses[0] if len(set(statuses)) == 1 and statuses[0] != "unverified" else "disagreement"
                    items[f"{kind}[{i}]" if kind != "outcome" else kind] = {"judges": statuses, "result": agreed}
            result["arms"][arm] = items
            # The later of the two judges' replies, and none unless both saw the help and it passed.
            firsts = [files[judge, "XY"].get(packet_id, {}).get(label, {}).get("first_help") for judge in JUDGES]
            firsts = [f if isinstance(f, int) and f > 0 else None for f in firsts]
            result.setdefault("first_help", {})[arm] = {
                "judges": firsts,
                "result": max(firsts) if None not in firsts and items["outcome"]["result"] == "pass" else None}
        choices = []
        for judge in JUDGES:
            per_order = [files[judge, order].get(packet_id, {}).get("preference", {}).get("choice") for order in ORDERS]
            arms = [key[packet_id].get(choice) if choice in ("X", "Y") else "tie" for choice in per_order]
            choices.append(arms[0] if len(set(arms)) == 1 else "tie")
        result["preference"] = {"judges": choices, "result": choices[0] if len(set(choices)) == 1 else "tie"}
        scenarios[scenario.id] = result
    write_json(root / "merged.json", scenarios)
    return scenarios


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "judge", "merge"))
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--claude", type=Path, help="Claude Code 2.1.280 or newer, signed in")
    parser.add_argument("--preview", action="store_true",
                        help="judge the scenarios finished so far, apart from the final judgment")
    args = parser.parse_args()
    stage = "preview-judging" if args.preview else "judging"
    if args.action == "judge":
        run_judges(args.campaign.resolve(), args.claude, stage=stage)
    else:
        {"prepare": prepare, "merge": merge}[args.action](args.campaign.resolve(), stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
