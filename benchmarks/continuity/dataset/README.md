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
- conversational-continuity
- llm-as-a-judge
configs:
- config_name: lives
  data_files: lives.jsonl
- config_name: native_tasks
  data_files: native_tasks.jsonl
---

# Continuity Benchmark — scenarios

Version `1.0.0`. Synthetic English scenarios for one question: does an
assistant carry what a person told it across separate conversations, and use it
well, without being told again? Every person, fact and message is invented.

The full protocol, how the scenarios are run and scored, and what results may
claim is in [docs/50_continuity_benchmark.md](../../../docs/50_continuity_benchmark.md).

## `lives` — 24 multi-session conversations

Each line is one invented person talking to an assistant over three or four
**separate conversations** (each one a new session, with no shared context
other than what the assistant itself kept):

| Stage | What it holds |
| --- | --- |
| `telling` | ordinary messages that give facts: people, constraints, plans, worries |
| `elsewhere` | unrelated everyday questions, long enough to push the facts out of a short context window |
| `change` | optional: a correction, a cancellation, a task done, a new date |
| `asking` | the one scored message, written without restating anything |

Six families of four: `people`, `work`, `time`, `correction`, `unknown`
(not inventing what was never said) and `relationship`. In each family one
life is a **counter-case** (`counter_case: true`), where earlier context must
*not* be applied.

`criteria` holds what a reply to the `asking` message is judged against:

- `must_use`: earlier facts the reply must rely on;
- `must_not`: outdated or unrelated context the reply must not apply;
- `no_repeat`: what the reply must not ask again;
- `unknown`: facts never given, which the reply must not state as known;
- `next_step`: what counts as a useful next step here.

Send only the user messages; the assistant's own replies are its own. Only the
assistant's reply to the `asking` message is scored. The judge instructions
used by the reference campaign are in `judge_instructions.md`.

## `native_tasks` — 12 tool tasks

Two deterministic agent tasks (assemble files into an exact deliverable;
extract sourced data from a controlled local source), six seeds each. Each line
names the generator and the checker in this repository that build the task and
verify its result. They test that an assistant keeps its agent capabilities.

## Integrity

`manifest.json` records the checksum of the preregistered corpus these files
are generated from, and of each file. `python -m benchmarks.continuity.export_dataset --check`
fails if they ever differ.
