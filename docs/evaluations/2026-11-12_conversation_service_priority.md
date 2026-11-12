# Conversation service priority on Mac

Date: 12 November 2026. Branch `companion/conversation-priority`, from `main`
at `d59c8f3` (Companion 0.11.15).

## Terms

- **Gateway**: the host process (Hermes Agent or OpenClaw) that receives the
  owner's Telegram messages and sends the replies.
- **Composition**: the work the daemon does before the model call: it reads
  memory, runs the semantic Atlas search and builds the context.
- **Atlas search**: the selection of the twelve most relevant memory notes. It
  embeds the message with the local embedding service.
- **launchd class** (`ProcessType`): the scheduling class macOS gives to a
  service. `Background` limits a service to throttled CPU and disk access.
  `Standard` is the class launchd applies when no class is set.

## Question and outcome

A daemon installed by Companion 0.11.15 answered more slowly than the reference
daemon on the same Mac, which runs a source checkout. Companion rendered every
macOS service in the `Background` class, including the gateway and the
embedding service, which work while the owner waits. The reference daemon's
gateway sets no class, so it runs as `Standard`.

**Decision: conversational services now use `Standard`; the after-turn worker
and the nightly maintenance stay `Background`.** On a real Companion daemon, the
daemon's work before the model call fell from about 3.4 s to 1.0 s, the
reference daemon's level. The model call remains the largest part of the wait.

## Measurements before the change (read-only, from host logs)

Both daemons ran on the same Mac (Apple M4, 16 GB), with the same model
(`gpt-6-astra`, medium reasoning) and the same embedding model file. Only log
timestamps and counters were read; no message text was kept.

Medians of the phases before the first model call, from 5 to 11 November for
the reference daemon and up to the change on 12 November for the Companion
daemon:

| Phase | Companion daemon (11 turns) | Reference daemon (60 turns) |
| --- | --- | --- |
| prefetch to Atlas start | 1.14 s | 0.32 s |
| Atlas search | 1.81 s | 0.66 s |
| Atlas end to context ready | 0.40 s | 0.10 s |
| First model call | 12.5 s | 6.6 s |
| Message received to reply ready | 15.4 s | 7.7 s |

A first count kept only turns whose first model call was the first call of its
agent session (5 and 23 turns). The table uses the corrected count; the
conclusions did not change.

Each daemon phase is about three times slower. The process priority reported by
`ps` was 4 for the Companion gateway and its embedding server, and 20 for the
reference gateway.

The first model call is also slower. The two daemons hold different
conversations and memories, so their model calls are not directly comparable.
**The cause of that gap is not established.** No measurement links it to the
launchd class.

The Mac was using 13 of its 14 GB of swap during these reads. That affects both
daemons.

## Isolated launchd check

A throwaway LaunchAgent ran the same Python script three times in each class,
alternating the classes. It was removed after the run.

| Class | Priority | CPU loop |
| --- | --- | --- |
| Standard | 20 | 0.24 to 0.29 s |
| Background | 4 | 0.78 to 1.04 s |

File reads varied too much between runs to compare (0.17 to 2.86 s in
`Standard`, 0.30 to 0.58 s in `Background`), because of the cache state.

## Change

- `BackgroundJob` gains `conversational`, left out of `public_dict`. Plan and
  receipt digests are unchanged, so transactions recorded by 0.11.15 still
  recover.
- The macOS rendering uses `Standard` for conversational jobs: the Hermes
  gateway, the OpenClaw gateway, the OpenClaw API, the embedding service, and
  the API and gateway of an attached host. Other jobs stay `Background`.
- Windows definitions are byte-identical to before.
- The runtime upgrade recognises an installation made before the change. It
  accepts the former `Background` definition of a conversational job, installs
  the current one, and restores the former one exactly on rollback. Any other
  definition is still refused.

## Canary on the Companion daemon

With the owner's agreement, the existing Companion daemon's gateway and
embedding service definitions were changed on 12 November at 10:16, and only in
their class (`Background` to `Standard`). Both processes then ran at priority
20. The after-turn worker and nightly maintenance were left unchanged. The
original definitions were kept byte for byte for rollback.

Restarting the gateway took two attempts: launchd refused the first restart
because the gateway had not finished stopping. The daemon was offline for about
one minute.

The owner then used the daemon normally for 11 turns. Medians:

| Phase | Before (11 turns) | After (11 turns) | Reference daemon (60 turns) |
| --- | --- | --- | --- |
| prefetch to Atlas start | 1.14 s | 0.27 s | 0.32 s |
| Atlas search | 1.81 s | 0.63 s | 0.66 s |
| Atlas end to context ready | 0.40 s | 0.10 s | 0.10 s |
| First model call | 12.5 s | 8.8 s (4.2 to 17.0 s) | 6.6 s |
| Message received to reply ready | 15.4 s | 13.4 s | 7.7 s |

The first turn after the restart is included. Its Atlas search took 2.21 s and
its intake 4.38 s.

**The daemon's own work before the model call now matches the reference daemon
(about 1.0 s against 3.4 s before).** The total time is still dominated by the
model call. Across the 13 model calls after the change, call time follows the
length of the reply (Spearman rank correlation 0.77 with output tokens). Prompt
cache hits on a turn's first call stayed at 6,656 tokens, about 35 % of the
input.

## Exit criteria and results

Fixed before the change.

| Criterion | Result |
| --- | --- |
| 1. Conversational jobs render `Standard`; worker and nightly render `Background` | pass (unit test) |
| 2. Windows definitions unchanged | pass (unit test) |
| 3. Plan and receipt digests unchanged | pass (unit test) |
| 4. Upgrade accepts pre-change definitions, refuses others, restores them on rollback | pass (unit tests, Mac and Windows fixtures) |
| 5. Targeted and full Companion suites pass on Python 3.12 | pass: targeted 122 passed, 1 skipped; full suite 3,180 passed, 25 skipped |
| 6. A launchd job in `Standard` runs unthrottled, one in `Background` does not | pass (priority and CPU); file reads not conclusive |

## Not established

- Any effect on model-call latency. The medians moved from 12.5 s to 8.8 s, but
  calls vary from 4 to 17 s with reply length, and 11 turns cannot separate
  that from chance.
- Why the Companion daemon's model calls stay slower than the reference
  daemon's.
- Windows, where Companion sets no task priority and Task Scheduler applies its
  default. Not measured.
- Daemons created by 0.11.15 or earlier keep the `Background` class. Updating
  the app does not rewrite their services.
