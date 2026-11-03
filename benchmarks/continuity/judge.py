"""Blind judgment for the Continuity Benchmark: packets without arms, published instructions, quote checks.

Each judge is one isolated `claude -p` call (Claude Opus 5.5, Claude
subscription) per batch of packets: no tool, no MCP server, no project setting,
an empty working directory. It never sees an arm name; the label key stays in
the campaign's private directory until every verdict exists.
Protocol: docs/50_continuity_benchmark.md, "Who judges".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import re
import secrets
import unicodedata

from benchmarks.continuity.corpus import LIVES
from benchmarks.continuity.run import sessions
from benchmarks.productivity_v1.runner.common import read_jsonl, write_json

CRITERIA = ("must_use", "must_not", "no_repeat", "unknown")
JUDGES = ("j1", "j2")
ORDERS = ("XY", "YX")
BATCH = 8

INSTRUCTIONS = """You are judging replies from a personal assistant. You do not know which system
wrote which reply, and you must not guess. Judge each reply only against the
criteria given, literally and separately. Use only the packet files you are
given; do not open any other file.

Each packet holds one person's messages to their assistant over several
separate conversations, the criteria for the final message, and two candidate
replies to that final message, labelled X and Y. The assistant's earlier
replies are not shown: judge only the two final replies.

For each reply, for each criterion item, give a verdict and, when the verdict
says the reply did something, an exact quotation copied character for
character from that reply (a short phrase is enough). A quotation that does not
occur in the reply invalidates your verdict.

- must_use[i]: "met" if the reply clearly relies on that earlier fact (quote
  where it does); otherwise "not_met" (no quote).
- must_not[i]: "violated" if the reply does what the item forbids (quote it);
  otherwise "clear" (no quote).
- no_repeat[i]: "asked" if the reply asks the person for that information again
  (quote the question); otherwise "clear" (no quote).
- unknown[i]: "invented" if the reply states that never-given fact as known
  (quote it); otherwise "clear" (no quote). Saying it does not know is clear.
- next_step: "met" if the reply gives what the packet describes as a useful
  next step (quote it). A question in its place is also "met" (quote it) when
  the reply could not reasonably act without the answer: what it asks is found
  nowhere in the person's messages and cannot be worked out from them, and the
  answer would change what the assistant does. A question about something the
  person already said, or that their messages already answer, is "not_met".
  Otherwise "not_met".

Then the preference: which reply sounds more like an assistant that knows this
person and actually helps them now? Answer "X", "Y" or "tie", with one
sentence of reason. Length alone is not a reason.

Answer with exactly one JSON object and nothing else:
{"judge": "<your judge id>", "order": "<order>", "verdicts": {
  "<packet id>": {"X": {"must_use": [{"verdict": "...", "quote": "..."}], "must_not": [...],
                        "no_repeat": [...], "unknown": [...],
                        "next_step": {"verdict": "...", "quote": "..."}},
                  "Y": {...},
                  "preference": {"choice": "X|Y|tie", "reason": "..."}}}}
Lists follow the criteria order; use "" when no quote applies. Every packet in
the batch gets a verdict."""

JUDGE_MODEL = "claude-opus-5-5"
JUDGE_EFFORT = "high"
SYSTEM = "You judge a personal assistant's replies against written criteria. Answer only with the requested JSON."


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().casefold()


def quoted(quote: str, reply: str) -> bool:
    quote = normalized(quote).strip(" .\"'")
    return bool(quote) and quote in normalized(reply)


def scored_reply(episode: dict) -> str | None:
    scored = [t for t in episode["turns"] if t["scored"]]
    return scored[-1]["response"] if episode["status"] == "completed" and scored else None


def prepare(campaign: Path, stage: str = "judging") -> None:
    """Write blinded packets and keep the label key private."""
    runs = {r["run_id"]: r for r in read_jsonl(campaign / "runs.jsonl") if not r["dry_run"]}
    rng = random.Random(secrets.randbits(64))
    lives = [life for life in LIVES if f"{life.id}-A" in runs and f"{life.id}-C" in runs]
    rng.shuffle(lives)
    key, packets = {}, []
    for index, life in enumerate(lives, 1):
        packet_id = f"P{index:02d}"
        x_arm = rng.choice("AC")
        arms = {"X": x_arm, "Y": "C" if x_arm == "A" else "A"}
        key[packet_id] = {"life": life.id, **arms}
        packets.append({
            "packet": packet_id,
            "conversations": [list(messages) for messages in sessions(life)],
            "criteria": {name: list(getattr(life, name)) for name in CRITERIA} | {"next_step": life.next_step},
            "replies": {label: scored_reply(runs[f"{life.id}-{arm}"]) or "(no reply: the system failed)"
                        for label, arm in arms.items()},
        })
    root = campaign / stage
    for order in ORDERS:
        for start in range(0, len(packets), BATCH):
            batch = []
            for packet in packets[start:start + BATCH]:
                labels = list(order)
                batch.append({**packet, "replies": {label: packet["replies"][label] for label in labels}})
            write_json(root / "packets" / order / f"batch-{start // BATCH + 1}.json", batch)
    (root / "INSTRUCTIONS.md").write_text(INSTRUCTIONS + "\n")
    write_json(campaign / "private" / f"{stage}-blind-key.json", key)


def judge_command(claude: Path, mcp_config: Path) -> list[str]:
    return [str(claude), "-p", "--model", JUDGE_MODEL, "--effort", JUDGE_EFFORT, "--tools", "",
            "--setting-sources", "project", "--strict-mcp-config", "--mcp-config", str(mcp_config),
            "--system-prompt", SYSTEM, "--no-session-persistence", "--output-format", "json"]


def _verdict_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def run_judges(campaign: Path, claude: Path, *, attempts: int = 2, stage: str = "judging") -> None:
    """Every judge, order and batch in its own isolated call; raw answers are kept."""
    import subprocess
    import tempfile
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
                    with tempfile.TemporaryDirectory() as empty:
                        mcp = Path(empty) / "mcp.json"
                        mcp.write_text('{"mcpServers": {}}')
                        work = Path(empty) / "work"
                        work.mkdir()
                        done = subprocess.run(judge_command(claude, mcp), input=prompt, text=True,
                                              capture_output=True, cwd=work, timeout=1800)
                    raw = root / "raw" / f"{judge}-{order}-{batch_path.stem}-attempt-{attempt}.json"
                    raw.parent.mkdir(parents=True, exist_ok=True)
                    raw.write_text(done.stdout)
                    try:
                        envelope = json.loads(done.stdout)
                        verdict = _verdict_json(envelope["result"])
                        if envelope.get("is_error") or set(verdict["verdicts"]) != {p["packet"] for p in batch}:
                            raise ValueError("incomplete verdict")
                    except (ValueError, KeyError, TypeError):
                        continue  # a malformed answer is retried once and kept as evidence
                    write_json(out, verdict)
                    print(json.dumps({"judge": judge, "order": order, "batch": batch_path.name,
                                      "attempt": attempt, "models": list(envelope.get("modelUsage", {}))}), flush=True)
                    break
                else:
                    raise RuntimeError(f"No valid verdict for {judge} {order} {batch_path.name}")


def _status(kind: str, entry: dict, reply: str) -> str:
    verdict, quote = entry.get("verdict"), entry.get("quote", "")
    good = {"must_use": "met", "next_step": "met"}.get(kind, "clear")
    if verdict == good:
        return "pass" if kind not in ("must_use", "next_step") or quoted(quote, reply) else "unverified"
    if verdict in {"not_met"} and kind in ("must_use", "next_step"):
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
    lives = {}
    for packet_id, packet in packets.items():
        life = next(item for item in LIVES if item.id == key[packet_id]["life"])
        result = {"life": life.id, "family": life.family, "counter": life.counter, "arms": {}}
        for label in ("X", "Y"):
            arm = key[packet_id][label]
            reply = packet["replies"][label]
            items = {}
            for kind in (*CRITERIA, "next_step"):
                count = 1 if kind == "next_step" else len(getattr(life, kind))
                for i in range(count):
                    statuses = []
                    for judge in JUDGES:
                        entry = files[judge, "XY"].get(packet_id, {}).get(label, {}).get(kind, {} if kind == "next_step" else [])
                        entry = entry if kind == "next_step" else (entry[i] if i < len(entry) else {})
                        statuses.append(_status(kind, entry, reply))
                    agreed = statuses[0] if len(set(statuses)) == 1 and statuses[0] != "unverified" else "disagreement"
                    items[f"{kind}[{i}]" if kind != "next_step" else kind] = {"judges": statuses, "result": agreed}
            result["arms"][arm] = items
        choices = []
        for judge in JUDGES:
            per_order = [files[judge, order].get(packet_id, {}).get("preference", {}).get("choice") for order in ORDERS]
            arms = [key[packet_id].get(choice) if choice in ("X", "Y") else "tie" for choice in per_order]
            choices.append(arms[0] if len(set(arms)) == 1 else "tie")
        result["preference"] = {"judges": choices, "result": choices[0] if len(set(choices)) == 1 else "tie"}
        lives[life.id] = result
    write_json(root / "merged.json", lives)
    return lives


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "judge", "merge"))
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--claude", type=Path, help="Claude Code 2.1.280 or newer, signed in")
    parser.add_argument("--preview", action="store_true",
                        help="judge the lives finished so far, apart from the final judgment")
    args = parser.parse_args()
    stage = "preview-judging" if args.preview else "judging"
    if args.action == "judge":
        run_judges(args.campaign.resolve(), args.claude, stage=stage)
    else:
        {"prepare": prepare, "merge": merge}[args.action](args.campaign.resolve(), stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
