# Continuity Benchmark 1.0.0

Date: 31 October 2026. Protocol: [docs/continuity-benchmark.md](../continuity-benchmark.md)
(version 1.0.0 at the tag `continuity-1.0.0`). Branch `codex/continuum`,
merged to `main` on 1 November 2026.

The unit is called a **scenario** here; the code at the tag and its tables
called it a "life". Only the word changed.

## Question and outcome

Does the daemon make Hermes a better personal assistant across conversations,
without making it a worse agent? Same Hermes, model and tools, with (C) and
without (A) the daemon.

- **"Improves Hermes as a personal assistant": not allowed.** Scenarios passed
  10/24 for C against 8/24 for A, a difference of +2 with a 95% paired
  bootstrap interval of [−5, +9]. The preregistered threshold was at least +6
  with an interval that excludes zero.
- **"Keeps Hermes's capabilities": allowed.** 12/12 native tasks on both
  sides, no task lost, native reply time ratio C/A 0.93 (limit 1.15).
- **No efficiency wording.** The full turn, after-turn work included on both
  sides, used 3.46 times A's tokens over the campaign.

## What ran

| | |
| --- | --- |
| Plan | created 2026-10-31 08:22 UTC at `ebefedd5`, clean checkout, seed 20261031 |
| Corpus | `corpus.py` sha256 `e54c640b…0a61`, 24 scenarios, 6 families × 4, 1 counter-case each |
| Host | Hermes Agent v0.21.5 (2026.9.24), Companion's installer, binary sha256 `b82e2bae…f1d5` |
| Model | `gpt-6-astra` through Codex CLI 0.159.2 (sha256 `16593cc2…0704`), ChatGPT subscription, medium effort for the voice |
| Daemon | fresh Companion daemon (`parga-v1`), empty vault, identity Aster, CLI added to the canonical platforms, follow-ups off, no Dreaming, no embeddings server |
| Native tasks | `native.files.aggregate` 1101–1106, `native.tools.extract` 1201–1206, V1 deterministic checkers |
| Judges | Claude Opus 5.5 (effort high) through Claude Code 2.1.309 on the Claude subscription, two judges × two presentation orders, blinded packets |
| Episodes | 72 of 72 scored episodes completed, 0 API errors, the canonical voice observed in all 36 daemon episodes; wall time 53 min (A) and 162 min (C) |

Engineering attempts 1 and 2 failed their gates and are kept: the worker could
not execute the interpreter in the sandbox, after-turn jobs were filed under
the profile name instead of the owner's id, and the CLI platform was missing
from the canonical voice's platforms (about 90k daemon tokens before the
reply). Attempt 3 passed with no daemon token before the reply.

## Results

| | Hermes alone | Hermes + daemon |
| --- | ---: | ---: |
| **Personal assistant, 24 scenarios** |  |  |
| Scenarios passed | 8 / 24 | 10 / 24 |
| Remembers what was said earlier (every earlier fact used) | 9 / 24 | 16 / 24 |
| Questions about something already said | 4 | 0 |
| Uses outdated or unrelated context | 2 | 1 |
| Invents a fact | 0 | 0 |
| Gives a useful next step | 14 / 24 | 14 / 24 |
| Preferred in blind comparison | 6 | 15 (ties: 3) |
| Judge disagreements (not counted as passed) | 0 | 3 |
| **Agent capabilities, 12 tasks** |  |  |
| Native tasks passed | 12 / 12 | 12 / 12 |
| Tool errors | 25 | 24 |
| **Time and cost** |  |  |
| Reply time per message, median (90th percentile) | 13.0 s (19.9 s) | 11.8 s (20.3 s) |
| After-turn work per message, median | 0.0 s | 35.2 s |
| Full turn per message, median (90th percentile) | 16.2 s (32.2 s) | 57.9 s (80.9 s) |
| Tokens per scenario, median: reply / after-turn | 109,474 / 0 | 102,302 / 297,602 |
| Tokens per scenario, median: full turn | 109,474 | 416,144 |
| Tokens, whole campaign: ratio C / A | 1 | 3.46 |

Token counts are reported by the host (Hermes) and, for the daemon's own
calls, by the provider through Codex CLI.

| Family | Hermes alone | Hermes + daemon |
| --- | ---: | ---: |
| people | 0 / 4 | 2 / 4 |
| work | 1 / 4 | 1 / 4 |
| time | 2 / 4 | 1 / 4 |
| correction | 3 / 4 | 2 / 4 |
| unknown | 2 / 4 | 2 / 4 |
| relationship | 0 / 4 | 2 / 4 |

## Where the difference comes from

Scenario by scenario: C passes 8 scenarios that A fails, A passes 6 scenarios that C fails,
and 10 scenarios have the same result.

The two systems fail differently. A misses an earlier fact 21 times (for
example, a Saturday plan with no word of the friend who cannot use stairs).
C misses one 10 times, and in the 8 failed daemon scenarios whose memory was
checked, the fact was in the daemon's vault (for example
`Areas/Family/Teo.md`, `Areas/Work/Daniel.md` with the correction to
Danielle). C's failures are mostly in the reply:

- 7 scenarios: the reply is a single question although the judges found the
  messages already held enough to act (the dentist's breakfast and lunch, the
  yoga time after a return to day shifts at 8, Teo's Sunday, the celebration
  plan, Noor's afternoon, the afternoon run, the laptop choice);
- 3 scenarios: the reply acts but lacks part of the expected next step (a warm word
  for the father's check-up, how to redirect the post, how to tell colleagues
  without the bakery's name);
- 2 scenarios: the reply does not show the earlier fact (Danielle on Teams, the
  blood pressure tablets);
- 1 scenario: an outdated item (distributing flyers that were already printed);
- 1 scenario: noting that the 2nd of August is past and asking which year, without
  a day for the spare room.

Counter-cases, where earlier context must not be applied: C passed 2 of 6,
A 3 of 6.

## Analyses not fixed in advance

These are reported for understanding and support no claim.

**Sensitivity to the next step.** Scenarios that fail only on the useful next step:
5 for C (dentist, afternoon run, the cousin's room, moving list, bakery), 1 for
A (bakery). If every such scenario passed, C would have 15 and A 9, a difference
of +6. The criterion as written rewards acting at once, while the daemon is
designed to ask a context question first; a single scored reply cannot show
what it does with the answer. This finding led to version 1.1.

**Judge variance.** The preview (below) was judged twice on the same 12 pairs
of replies. Outside the next-step criterion, 3 of 88 merged verdicts changed
between the two judgments (one "invents" verdict on "November" for "next
month" said on 31 October, one "asks again", one "remembers").

## Preview and the correction of the judge instructions

At the owner's request, the 12 scenarios finished at mid-campaign were judged
apart as a preview that never counts. The preview showed that the judge
instructions did not carry a rule the protocol fixed before any episode: a
useful next step holds "unless the case requires a question first". The owner
asked for the nuance after reading the preview. The instructions were corrected
(`16c449e9`), the correction is disclosed in the protocol with its timing, the
first preview is kept as `preview-1-judging`, and the final judgment used the
corrected instructions with new blind labels. No criterion of any scenario changed.

| Scenarios passed, same 12 scenarios | Hermes alone | Hermes + daemon |
| --- | ---: | ---: |
| Preview 1 (without the rule) | 4 | 2 |
| Preview 2 (with the rule) | 5 | 6 |

## Limits

- Blinding is imperfect: a reply can reveal the daemon's name or tone.
- The campaign talks through the Hermes CLI, not Telegram; follow-ups, Dreaming
  and the embeddings server are off.
- The judgment is entirely automatic (two Claude Opus 5.5 judges); no person
  scores a reply. The scenarios were also written with Claude Opus 5.5.
- One scored reply per scenario: a context question can never be followed by what
  the assistant does with the answer.
- The corpus docstring still carries the benchmark's earlier name; the
  checksum covers those bytes and they were kept unchanged.

## Records

The campaign directory (`continuity-1.0.0-20261031`) holds the plan,
`runs.jsonl`, per-episode records and evidence, blinded packets, raw judge
answers, verdicts, the blind key and the report. An anonymized copy, without
runtime directories or the bench daemon's vault, is in
[`benchmarks/continuity/records/`](../../benchmarks/continuity/records/README.md);
the vault check above cannot be audited from it.

`python -m benchmarks.continuity.report --campaign <campaign>` at the tag
`continuity-1.0.0` recomputes the tables offline.

## Decision

No improvement claim from 1.0. Its capability result stands. Version 1.1
judges the final conversation over several turns with a simulated person, on
the same 24 scenarios, and reruns only the personal-assistant part
([report](2026-10-31_continuity_benchmark_1_1.md)).
