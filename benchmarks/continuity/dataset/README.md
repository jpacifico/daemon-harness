---
license: mit
language:
- en
pretty_name: Continuity Benchmark
size_categories:
- n<1K
task_categories:
- text-generation
tags:
- personal-assistant
- long-term-memory
- multi-session
- multi-turn
- user-simulation
- conversational-continuity
- llm-as-a-judge
configs:
- config_name: lives
  data_files: lives.jsonl
---

# Continuity Benchmark — scenarios

Version `1.1.0`. Synthetic English scenarios for one question: does an
assistant carry what a person told it across separate conversations, and use it
well, without being told again? Every person, fact and message is invented.

The full protocol, how the scenarios are run and scored, and what results may
claim is in [docs/50_continuity_benchmark.md](../../../docs/50_continuity_benchmark.md).
Version 1.1.0 replays the 24 lives of 1.0.0 with a final conversation over
several turns. Version 1.0.0 (a single scored reply, and 12 native tool tasks)
is kept at the repository tag `continuity-1.0.0`.

## `lives` — 24 multi-session conversations

Each line is one invented person talking to an assistant over three or four
**separate conversations** (each one a new session, with no shared context
other than what the assistant itself kept):

| Stage | What it holds |
| --- | --- |
| `telling` | ordinary messages that give facts: people, constraints, plans, worries |
| `elsewhere` | unrelated everyday questions, long enough to push the facts out of a short context window |
| `change` | optional: a correction, a cancellation, a task done, a new date |
| `final` | the opening message of the final conversation, written without restating anything |

The **final conversation** continues over several turns. After each assistant
reply, a simulated person answers, up to three person messages in all, or ends
the conversation when they have what they wanted. The simulated person follows
`simulator_instructions.md` and sees only what the person told the assistant
before, the `person` sheet and the final conversation so far; never the
criteria.

`person` says what the person wants from the final conversation and what they
can say if asked: details never given before, so asking for them is legitimate.

Six families of four: `people`, `work`, `time`, `correction`, `unknown`
(not inventing what was never said) and `relationship`. In each family one
life is a **counter-case** (`counter_case: true`), where earlier context must
*not* be applied.

`criteria` holds what the final conversation is judged against, as a whole:

- `must_use`: earlier facts the assistant must rely on without being told again;
- `must_not`: outdated or unrelated context the assistant must not apply;
- `no_repeat`: what the assistant must not ask again;
- `unknown`: facts never given, which the assistant must not state as known;
- `outcome`: the concrete help the person should have by the end.

The judge instructions used by the reference campaign are in
`judge_instructions.md`.

## Integrity

`manifest.json` records the checksum of the preregistered corpus these files
are generated from, and of each file. `python -m benchmarks.continuity.export_dataset --check`
fails if they ever differ.
