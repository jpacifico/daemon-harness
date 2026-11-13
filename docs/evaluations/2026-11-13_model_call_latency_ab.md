# Model-call latency: Companion and reference configurations side by side

Date: 13 November 2026. Branch `companion/conversation-priority`. Follows
[Conversation service priority on Mac](2026-11-12_conversation_service_priority.md),
which left open why the Companion daemon's model calls stay slower than the
reference daemon's.

## Terms

- **Reference daemon**: the owner's first daemon, run from a source checkout of
  the core at `c22c44bc` by its own Hermes gateway.
- **Companion daemon**: a daemon installed by Companion 0.11.15 (core release
  `a9074739`), with the Hermes profile Companion writes.
- **Gateway**: the Hermes process that receives Telegram messages and sends the
  replies.
- **Model call**: one request from Hermes to the model (`gpt-6-astra`, medium
  reasoning, through a ChatGPT sign-in). Hermes logs its latency and its input,
  output and cached input tokens.
- **Composition**: the daemon's work before the model call (memory reading,
  context building).
- **Prefetch**: the memory provider's read of the daemon's memory before each
  reply.
- **After-turn handoff**: the reply's memory work, queued for the after-turn
  worker once the reply is sent.

## Question and outcome

On the same Mac, the Companion daemon's first model call took a median 8.8 s
against 6.6 s for the reference daemon (previous report). The two daemons hold
different memories and conversations, so those figures do not say whether the
configuration Companion sets up is the cause.

The test ran both configurations at the same time of day, with the same
synthetic memory and the same twelve messages.

**Outcome: the Companion configuration did not make model calls slower. Its
first model calls were faster: median 4.75 s against 6.75 s.** Under the
decision rule fixed before the test, this is classed as *inconclusive*. The rule
had only two conclusive outcomes: Companion slower by at least 1.5 s, or no
difference beyond 1.0 s. The rule did not provide for Companion being faster.
The gap between the real daemons is therefore not explained by what Companion
configures. Its cause is still not established.

## Protocol

Fixed before any model call (scratch file `criteria.md`, with two amendments
written before the runs they govern).

- **Two copies.** R copies the reference daemon's configuration: core tree
  identical to the files its gateway loads, its Hermes `config.yaml`, and its
  daemon switches. C copies the Companion daemon's configuration: a byte copy of
  the installed release, its Hermes `config.yaml`, and the environment the
  Companion service wrapper composes. Paths and the owner's id were remapped to
  throwaway ones.
- **Real transport.** Each copy ran the real `hermes gateway run` (Hermes
  0.21.5 `f97608f`) on the Telegram platform. The Telegram adapter pointed to a
  local stand-in for the Telegram Bot API, so nothing reached Telegram. The
  stand-in recorded when each reply was sent.
- **Same model, same sign-in.** `gpt-6-astra`, medium reasoning, through one
  ChatGPT sign-in reserved for benchmarks.
- **Same memory.** A fresh synthetic Companion vault, daemon name "Aster", for
  each run. The semantic Atlas search was off in both copies (embedding service
  unreachable), and the after-turn worker did not run: handoffs were queued
  only.
- **Isolation.** A macOS sandbox per gateway made the personal home, the real
  daemons' files and the credential store unreadable.
- **Order.** One gateway at a time, because a ChatGPT refresh token works once.
  Two repetitions of 12 messages per copy, in the order R, C, C, R.
  48 turns, 13 November 09:28 to 09:36 CET.

### Fidelity checks

Checked before the results were trusted:

| Check | R | C | Real daemon |
| --- | --- | --- | --- |
| Tools visible to the model, plus deferred | 35 + 4 | 15 + 4 | 35 + 4 (reference), 15 + 4 (Companion) |
| Voice variant | `voice_brief_authority_hierarchy_v3` | same | same on both |
| Platform, model, provider | telegram, `gpt-6-astra`, `openai-codex` | same | same |
| Turns with canonical delegation, prefetch and after-turn handoff | 24 of 24 | 24 of 24 | 22 of 22 checked on the Companion daemon |
| Memory provider start failures | 0 | 0 | 0 |

Stable cached prefix of the first call: 12,928 tokens (R) and 6,656 tokens (C).
The real daemons show 12,800 and 6,656.

### A first run that does not count

The first run of 48 turns (same protocol) is invalid. In C the memory provider
refused to start because the runner had copied its configuration file
readable by all users; the real file is private. C therefore answered without
prefetch and without after-turn handoff. The dry runs already showed this
failure, but the checks did not look for it. The runner was fixed (the file
keeps its private mode) and the fidelity check now covers prefetch, handoff and
start failures for every turn. None of the first run's figures are used here.

## Results

First model call of each turn (the decision measure), 24 turns per copy:

| Measure | R | C |
| --- | --- | --- |
| Median latency | 6.75 s | 4.75 s |
| Range | 3.0 to 15.2 s | 2.1 to 16.0 s |
| Median by repetition (1, 2) | 8.2 s, 5.4 s | 4.5 s, 5.25 s |
| Median input tokens | 17,371 | 11,224 |
| Median cached input tokens | 12,928 | 6,656 |
| Median output tokens | 62 | 63 |
| Linear fit, latency against output tokens | 4.38 s + 3.69 s per 100 tokens | 1.77 s + 5.07 s per 100 tokens |

Paired by message and repetition, C minus R: median −1.4 s; C was slower in
6 of 24 pairs (25 %).

Every turn made exactly one model call and no tool call. Totals over 24 turns:
input 416,938 tokens (R) and 269,310 (C), output 2,087 and 2,082. All token
counts are as reported by Hermes.

Time seen through the stand-in, medians:

| Measure | R | C |
| --- | --- | --- |
| Message received to reply ready (Hermes log) | 6.85 s | 5.1 s |
| Message delivered to first visible text | 3.88 s | 2.65 s |
| Message delivered to last edit | 7.03 s | 5.87 s |

No turn failed and no error was logged.

## Decision

Rule fixed before the test (primary measure: median first-call latency):

| Outcome | Condition | Met |
| --- | --- | --- |
| Companion configuration explains the gap | C − R ≥ +1.5 s and C slower in ≥ 70 % of pairs | no (−2.0 s, 25 %) |
| Configuration does not explain the gap | \|C − R\| ≤ 1.0 s | no (2.0 s) |
| Inconclusive | otherwise | **yes** |

No product change follows from this test. Companion's configuration is not a
reason to slow the voice; in this test it was the faster one.

## Not established

- **Why C was faster.** C sends about 6,000 fewer input tokens per call (a
  smaller tool list among other differences). No measurement isolates that
  cause.
- **Why the real Companion daemon is slower than the reference daemon.** The
  test excludes the configuration. It leaves the real memory (far larger than
  the synthetic vault), the conversation history, the Atlas search, the
  after-turn worker running alongside and the time of day. None was measured
  here.
- Variance is large: R's medians differ by 2.8 s between its two repetitions,
  a few minutes apart.
- R lacks one file the reference gateway has: a diagnostic capture file. It is
  written after composition and logged a warning on each R turn.
- Windows was not tested.

## Data

Raw results, gateway logs and the runner scripts stay outside Git: the scripts
read the real daemons' configuration to copy it, and the replies are kept with
the logs. No personal memory, message or credential was read by the gateways.
The benchmark ChatGPT sign-in was renewed by device code on 13 November, before
the runs.
