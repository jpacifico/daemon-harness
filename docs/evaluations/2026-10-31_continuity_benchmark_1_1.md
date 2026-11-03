# Continuity Benchmark 1.1.0

Date: 31 October 2026. Protocol: [docs/50_continuity_benchmark.md](../50_continuity_benchmark.md),
section "Version 1.1". Branch `codex/continuum`,
merged to `main` on 1 November 2026.
Previous version: [Continuity Benchmark 1.0.0](2026-10-31_continuity_benchmark_1_0.md).

The unit of the benchmark is a **scenario**. The code that ran and judged this
campaign, kept at the tag `continuity-1.1.0`, still called it a "life"; the
rename that followed changed no scenario, message, criterion or instruction.

## Question and outcome

Version 1.1 replays only the continuity part of 1.0, on the same 24 scenarios,
with the same messages and criteria. Only the final conversation changes: it
runs over several turns, with a simulated person (a separate Claude Opus 5.5)
who answers the assistant's questions, up to three person messages in all.

- **"Improves Hermes as a personal assistant": not allowed.** Scenarios passed
  12/24 for C against 10/24 for A, a difference of +2 with a 95% paired
  bootstrap interval of [−5, +9]. The threshold, unchanged from 1.0, was at
  least +6 with an interval that excludes zero.
- The other gates hold: C invents no more than A (0 and 0), uses outdated
  context no more than A (1 against 5), and is preferred more often (13 against
  8, 3 ties).
- **"Keeps Hermes's capabilities"** was not rerun; the 1.0 result stands (12/12
  on both sides).
- **No efficiency wording.** The full turn, after-turn work included on both
  sides, used 3.42 times A's tokens over the campaign.

## What ran

| | |
| --- | --- |
| Plan | created 2026-10-31 13:31 UTC at `50459ad2`, clean checkout, seed 20261031 |
| Corpus | `corpus.py` sha256 `91bd0e02…d342`; the 24 scenarios of 1.0 field for field, plus a `person` sheet each (a test checks this against a fingerprint of the 1.0 scenarios taken at the tag `continuity-1.0.0`) |
| Host, model, daemon | as in 1.0: Hermes Agent v0.21.5, `gpt-6-astra` through Codex CLI 0.159.2, fresh Companion daemon, empty vault, identity Aster |
| Simulated person | Claude Opus 5.5, effort medium, Claude Code 2.1.309, instructions sha256 `764ed97a…b196`; 86 calls, median 7.7 s, none failed |
| Judges | Claude Opus 5.5 (effort high), two judges × two presentation orders, blinded packets, instructions of 1.1 written before any episode |
| Episodes | 48 of 48 scored episodes completed, 0 API errors, the canonical voice observed in all 24 daemon episodes; wall time 58 min (A) and 185 min (C) |

Engineering attempt 1 was interrupted by the operator before its end (a
start that was not logged); attempt 2 passed its gates.

The 36-scenario draft started earlier the same day (one scored episode) was
abandoned when the owner restated the scope; it is archived as
`continuity-1.1.0-20261031-abandoned-36` and none of it counts.

## Results

| | Hermes alone | Hermes + daemon |
| --- | ---: | ---: |
| **Personal assistant, 24 scenarios** |  |  |
| Scenarios passed | 10 / 24 | 12 / 24 |
| Remembers what was said earlier | 12 / 24 | 16 / 24 |
| Questions about something already said | 2 | 0 |
| Uses outdated or unrelated context | 5 | 1 |
| Invents a fact | 0 | 0 |
| Concrete help by the end of the conversation | 18 / 24 | 17 / 24 |
| Reply that first gives that help, median | 2 | 1 |
| Assistant replies in the final conversation, median | 2 | 2 |
| Preferred in blind comparison | 8 | 13 (ties: 3) |
| Judge disagreements (not counted as passed) | 2 | 2 |
| **Time and cost** |  |  |
| Reply time per message, median (90th percentile) | 15.2 s (29.1 s) | 13.1 s (29.1 s) |
| After-turn work per message, median | 0.0 s | 38.8 s |
| Full turn per message, median (90th percentile) | 16.8 s (33.0 s) | 60.6 s (87.4 s) |
| Tokens per scenario, median: reply / after-turn | 149,564 / 0 | 126,489 / 390,452 |
| Tokens per scenario, median: full turn | 149,564 | 518,290 |
| Tokens, whole campaign: ratio C / A | 1 | 3.42 |

| Family | Hermes alone | Hermes + daemon |
| --- | ---: | ---: |
| people | 2 / 4 | 1 / 4 |
| work | 2 / 4 | 3 / 4 |
| time | 2 / 4 | 1 / 4 |
| correction | 2 / 4 | 2 / 4 |
| unknown | 2 / 4 | 3 / 4 |
| relationship | 0 / 4 | 2 / 4 |

Counter-cases, where earlier context must not be applied: C passed 3 of 6
(one judge disagreement), A 4 of 6.

The simulated person ended the conversation before its limit in 18 of A's
scenarios and 16 of C's.

## Where the difference comes from

Scenario by scenario: C passes 8 scenarios that A fails, A passes 6 that C
fails, and 10 have the same result. The counts are the same as in 1.0, which
is why the interval is the same; the scenarios are not: 14 of the 24 changed
result on at least one side between 1.0 and 1.1.

A mostly fails on memory: an earlier fact is missing 15 times across its 14
failed scenarios (for example a Thursday morning planned around a swim the
person had said was cancelled, or a side for the soup with no word of the
sourdough loaf the person was baking that day). C misses one 6 times.

C fails mostly by asking one question per reply until the conversation ends.
In 6 scenarios every daemon reply is a single question and no help comes
within the three person messages, although the person answered each question
and the daemon's replies show it remembered the earlier facts:

- the trip to the wedding: the year of 21 June, then the departure city, then
  the hotel budget;
- Noor's Saturday: steps inside the flat, then which Saturday, then steps again;
- the celebration: who with, then how much cooking, then allergies;
- the afternoon run: how long, then how flexible the 4 p.m. finish is, then
  when to be back;
- yoga with day shifts at 8: class times, then bedtime, then hours of sleep;
- Priya's bakery: its name, then her Instagram handle, then which town.

The other C failures:

- 2 scenarios where the help is given but an earlier fact is not visible in
  it: the weekly update for Danielle written as a short message, without
  saying it is for Teams; the Sunday with Teo, by train, with no word of his
  carsickness;
- 1 scenario where the film fits the driving test tomorrow morning, but there
  is no good-luck word, which the expected outcome includes;
- 1 scenario with outdated items (the clean-up half hour also lists the
  litter pickers and flyers already settled);
- 2 judge disagreements (the chips tonight, the dentist's breakfast and lunch).

The final conversations ran on Saturday 31 October. In two scenarios that say
"Saturday" (Noor's visit, on both sides), the assistants asked whether it
meant that day or the next Saturday; this follows from the campaign date, not
from the scenario.

## Comparison with 1.0

| | 1.0, A | 1.0, C | 1.1, A | 1.1, C |
| --- | ---: | ---: | ---: | ---: |
| Scenarios passed | 8 | 10 | 10 | 12 |
| Remembers | 9 | 16 | 12 | 16 |
| Questions about something already said | 4 | 0 | 2 | 0 |
| Uses outdated context | 2 | 1 | 5 | 1 |
| Help (1.0: next step in the one reply; 1.1: by the end) | 14 | 14 | 18 | 17 |
| Preferred | 6 | 15 | 8 | 13 |

The extra turns raised both sides by two scenarios. They did not remove the
daemon's main failure from 1.0, the lone question: with the answer in hand,
the daemon asked another question instead of acting, in 6 scenarios.

## Limits

- Blinding is imperfect: a reply can reveal the daemon's name or tone.
- The judgment and the simulated person are entirely automatic (Claude Opus
  5.5); no person scores a reply. The scenarios were also written with Claude
  Opus 5.5.
- Three person messages are a fixed limit: a daemon that would have acted on a
  fourth turn is scored as not having helped.
- The campaign talks through the Hermes CLI, not Telegram; follow-ups,
  Dreaming and the embeddings server are off, as in 1.0.
- Capabilities were not remeasured against this build.

## Records

The campaign directory (`continuity-1.1.0-20261031b`) holds the plan,
`runs.jsonl`, per-episode records with every simulated-person answer, blinded
packets, raw judge answers, verdicts, the blind key and the report. An
anonymized copy, without runtime directories or the bench daemon's vault, is in
[`benchmarks/continuity/records/`](../../benchmarks/continuity/records/README.md).

`python -m benchmarks.continuity.report --campaign <campaign>` at the tag
`continuity-1.1.0` recomputes the tables offline.

## Decision

No improvement claim from 1.1. The daemon remembers more, asks nothing again,
applies outdated context less, and is preferred more often, but passes only
two more scenarios than Hermes alone. Its main failure is a chain of single
questions in place of help, observed in 1.0 and again here over several turns.

The daemon's questions are deliberate: an earlier line of work (the
"conversational dynamics" experiments of July and August 2026, in the former
repository) brought a voice that only commented and informed to ask genuine
questions, and reached a balance the owner considers fragile. This benchmark
scores mostly practical help; how the two interact was not measured. The owner
decided on 31 October 2026 not to change the voice for this result: no
diagnostic or correction follows from it, and the result stands as reported.
