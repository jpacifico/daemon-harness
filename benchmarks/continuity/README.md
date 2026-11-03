# Continuity Benchmark — reproduction

Protocol: [docs/50_continuity_benchmark.md](../../docs/50_continuity_benchmark.md).
Scenarios as a reusable dataset: [dataset/](dataset/README.md).

Requirements: macOS on Apple silicon (the runner uses `sandbox-exec`), Python
3.11 with this repository's dependencies, a ChatGPT subscription with access to
`gpt-6-astra`, and Claude Code 2.1.280 or newer signed in to a Claude
subscription with access to Claude Opus 5.5. Run every command from the
repository root.

## 1. Prepare a benchmark home

```bash
CONTINUITY_HOME="$HOME/continuity-bench" PYTHONPATH=src:. python -m benchmarks.continuity.setup_host
```

It installs, under `CONTINUITY_HOME` only, Hermes `0.21.5` with Companion's own
installer (pinned source archive and checksum), the official Codex CLI
`0.159.2` (Companion's pinned package and checksum) and an empty credential
store. It prints two sign-in commands. Run both and sign in with the same
ChatGPT account; they are separate sessions on purpose, because ChatGPT refresh
tokens rotate and must not be shared between programs.

## 2. Run the campaign

```bash
CONTINUITY_HOME="$HOME/continuity-bench" PYTHONPATH=src:. python -m benchmarks.continuity.run \
  --output "$HOME/continuity-bench/campaigns/<name>"
```

It refuses to start on a modified checkout, records the plan (revision, corpus
checksum, Hermes and Codex checksums, the public random order), runs the
engineering episodes, stops if any engineering gate fails, then runs every
scored episode once. `--resume` continues an interrupted campaign without
overwriting an episode that reached a model. After a failed engineering gate,
fix the cause and rerun with `--resume --engineering-attempt <n>`.

## 3. Judge

```bash
PYTHONPATH=src:. python -m benchmarks.continuity.judge prepare --campaign <campaign>
PYTHONPATH=src:. python -m benchmarks.continuity.judge judge --campaign <campaign> --claude <path to claude>
PYTHONPATH=src:. python -m benchmarks.continuity.judge merge --campaign <campaign>
```

`prepare` writes the blinded packets and keeps the label key in the campaign's
`private/` directory. `judge` makes one isolated call per judge, order and
batch:

```text
claude -p --model claude-opus-5-5 --effort high --tools "" --setting-sources project \
  --strict-mcp-config --mcp-config <empty> --system-prompt "<SYSTEM>" \
  --no-session-persistence --output-format json
```

from an empty working directory, with `judge_instructions.md` and the batch on
standard input. Every raw answer is kept under `judging/raw/`. A malformed
answer is retried once; both attempts stay in the records. `merge` opens the
label key and applies the two-judge rule.

## 4. Report

```bash
PYTHONPATH=src:. python -m benchmarks.continuity.report --campaign <campaign>
```

No model call. It recomputes `report/summary.json` and `report/table.md`, the
results table and the claim gates, from `runs.jsonl` and
`judging/merged.json`. Anyone with a published campaign can run it offline.
