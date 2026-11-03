# Public Productivity Benchmark V1 Specification

[Version française](productivity-benchmark.fr.md)

Protocol version: `1.0.0`

Status: frozen for implementation; no scored public campaign has run. Three of
the ten scenarios are implemented; see
[`benchmarks/productivity_v1/`](../benchmarks/productivity_v1/README.md).

Authoritative language: English

This document defines the benchmark before its results are known. A campaign
may support a public adoption claim only if its protocol was frozen, versioned,
and published before the first scored turn.

## Question And Scope

For Hermes and OpenClaw, the benchmark compares the actual experience provided
by the native host with the experience provided by the same host after the
daemon is installed.

It answers four questions separately:

1. Does the daemon preserve the host's native productivity capabilities?
2. Does it improve task success when work depends on a past exchange, a
   revised decision, or personal context?
3. What is its effect on the time required to obtain a correct result?
4. How many tokens are consumed to obtain that result, including deferred
   maintenance when it contributes to the episode?

This is not a general model leaderboard or a contest between Hermes and
OpenClaw. Every conclusion remains tied to the host versions, model, provider,
and environment that were actually measured.

## Language Policy

The scored V1 corpus is English-only. This includes:

- user prompts and synthetic conversation histories;
- fixture content and expected answers;
- checker identifiers and failure messages;
- manifests, replay instructions, and public reports;
- chart labels and machine-readable result fields.

English is fixed before implementation so language does not become a hidden
variable in the A/C comparison and third-party reviewers can audit one common
corpus.

French is explicitly preserved for a later `fr-FR` track. That track will use
its own version, seeds, manifests, results, and report. Its results will never
be pooled into the English headline numbers. A translated prompt is not treated
as the same scored run because language can change model and tool behavior.

## Non-Negotiable Principles

- The protocol must be able to detect both improvement and degradation. A bad
  answer, high latency, or high token use cannot be removed because it conflicts
  with the expected outcome.
- The daemon receives no procedure tailored to a scenario, website, or tool.
  Each host remains responsible for its native tools.
- All data is synthetic. No personal conversation, calendar, file, memory, or
  production identifier participates in the benchmark.
- Arms use independent profiles, sessions, workspaces, memories, caches, and
  queues. No production channel or service is used.
- Primary success is computed by deterministic checkers. An LLM judge or human
  review may add qualitative analysis but cannot replace objective ground truth.
- There is no opaque aggregate score. Success, latency, and token consumption
  remain separately visible.
- Excluded, negative, and atypical raw results are retained with their reason.
  A new benchmark version never overwrites an earlier campaign.

## Compared Arms

### A — Native Host

An official host installation in a clean profile, with the reference native
memory and learning settings. The model, reasoning effort, external tools, and
permissions are recorded in the campaign manifest.

### C — Same Host With The Daemon

The same host version, model, reasoning effort, and external capabilities after
the canonical daemon plugin is installed. Documented changes to memory,
Dreaming, skills, and memory tools are part of the evaluated installation
impact. They are not hidden to make the catalogs artificially identical.

External tools must still be at parity for a native task that does not depend
on memory. For a longitudinal task, both arms receive the same history through
ordinary user and assistant messages. Directly writing the C arm's memory while
leaving A empty is forbidden.

Maintenance specific to each installation is triggered only through an
official, observable path. Its mode, duration, and token use are recorded. If
maintenance does not complete within its published bound, that is product
behavior, not an automatic infrastructure exclusion.

## V1 Scenario Matrix

V1 contains ten general scenarios. Each scenario has six independent synthetic
seeds of equivalent difficulty.

| Identifier | Family | Controlled outcome |
| --- | --- | --- |
| `native.files.aggregate` | files | transform multiple sources into an exact deliverable without modifying inputs |
| `native.tools.extract` | tools | inspect a controlled synthetic source and produce a sourced structured result |
| `continuity.decision` | continuity | recover the current work decision and the operation that must not be restarted |
| `continuity.project_state` | continuity | resume a project at the correct milestone, blocker, and next action |
| `temporal.present_future` | context | respect a current constraint and a near-future commitment |
| `temporal.recent_past` | context | understand that a recent event changed the current state or responsibility |
| `revision.corrected_fact` | correction | prefer a confirmed correction over an older fact that is no longer true |
| `uncertainty.missing_evidence` | evidence | avoid inventing a claim that neither memory nor available tools support |
| `longitudinal.delivery` | sequence | complete a small deliverable across sessions without losing decisions and corrections |
| `longitudinal.preference` | sequence | reuse a confirmed work preference without turning it into an absolute rule |

When a task needs a web-like or remote source, a deterministic local fixture
server replaces the unstable public source. The scenario never dictates the
order of tool calls or the browsing method.

The two `native.*` scenarios and `uncertainty.missing_evidence` contain one
scored turn without prior history. The other seven receive two to four history
messages followed by a scored resumption turn. All tokens and durations from
the beginning of the episode remain in scope.

A separate extension may later measure a complete overnight maintenance cycle.
It will not be mixed into V1 until both hosts expose a comparable observation
boundary.

## Sample Size And Ordering

The main corpus contains:

```text
10 scenarios × 6 seeds × 2 arms × 2 hosts = 240 scored episodes
```

An episode may contain multiple turns, so the manifest also publishes the
total number of turns.

- Every episode uses a fresh provider session.
- The two arms never share an opaque identifier.
- A pair receives isomorphic twin fixtures with the same structure and
  difficulty; fixture assignment is reversed across blocks.
- Arm and host order is alternated from a public random seed.
- The six repetitions are spread across at least two time windows so a
  transient provider condition is not mistaken for a daemon effect.
- One engineering run per scenario occurs before the scored corpus is released.
  It is labeled `dry-run`, remains public, and never contributes to V1 results.

## Success Measurement

Every scenario has computable ground truth and an allowed effect boundary. The
report publishes at least:

- `success_first_attempt`: correct result without a repair request;
- `success_within_episode`: correct result before the episode turn limit;
- every individual check, not only its aggregate;
- produced files or actions and any out-of-scope effect;
- hidden fixture claims invented by a control arm;
- tool calls, errors, and retries.

An eloquent answer with an incorrect deliverable is a failure. A correct answer
obtained after a repair is an eventual success, but not a first-attempt success.

Qualitative observations cover clarity, usefulness, and naturalness. They are
reviewed blind on a published sample and cannot reverse a deterministic check.

## Latency Measurement

Clocks use a monotonic source and raw events retain timestamps. Every episode
publishes the following values when their boundary exists:

- full wall time, both including and excluding disposable startup;
- time to the first fragment actually visible to a user;
- time to the final correct result;
- host-reported internal duration;
- daemon composition time;
- number and duration of tool cells;
- after-turn or deferred maintenance time, outside the visible path.

The current OpenClaw local runner does not expose the first streamed fragment.
V1 must attempt to observe it at the streaming boundary in the disposable
environment, without modifying OpenClaw source code. If the boundary remains
unavailable, the field is `unavailable`; a final-message hook cannot be used as
a substitute.

The report includes individual values, median, 90th percentile, and a 95%
bootstrap interval. Reporting only a mean or removing a slow result from a
chart is forbidden.

A fast wrong answer must not appear efficient. For time to correct result, an
episode without success receives the pre-published timeout bound. Its actual
elapsed duration remains available separately.

## Token Accounting

Tokens are separated by owner and phase:

| Scope | Contents |
| --- | --- |
| `host.visible` | response-model calls, including tool-loop calls |
| `daemon.critical` | any internal call before the visible result |
| `host.background` | native maintenance attributable to the episode |
| `daemon.background` | after-turn, Dreaming, or REM attributable to the episode |

For every scope, the schema keeps these categories separate:

- uncached input;
- cache reads and writes;
- output;
- reasoning, when exposed;
- provider- or host-reported total.

Every token value has a mandatory measurement quality:

- `provider_reported`: exact per-call usage returned by the provider;
- `host_reported`: aggregate usage returned by the host;
- `tokenizer_estimated`: estimate from a versioned tokenizer;
- `unavailable`: information not exposed, never silently replaced by zero.

Hermes currently exposes per-call usage, including reasoning and cache. The
OpenClaw runner reads the final cumulative usage snapshot from the native Codex
rollout and counts the distinct cumulative snapshots as model calls; it must
never substitute the last-call usage from the final response envelope for the
whole episode. Input is split into uncached, cache-read and cache-write values
from that native snapshot. A missing field must remain visible. Internal daemon
calls use provider usage when available and an explicitly labeled estimate
otherwise.

Primary indicators are total tokens per episode, the `C − A` delta, tokens per
successful episode, and cumulative tokens until the first correct result. Cache
categories are not flattened into a misleading cost total. Any monetary
estimate uses a dated price manifest and remains secondary to raw token counts.

The report publishes measurement coverage for each scope. If a known provider
call remains `unavailable`, no total-consumption or token-efficiency claim is
allowed for that scope. Only observed counters may be compared.

## Analysis And Communication Gates

Effects are published per scenario, per family, and per host. A combined host
view is supplementary only.

For one host, the campaign may support the statement “the daemon improves
productivity” only when all of these conditions hold:

1. no critical safety, scope, or truth failure;
2. on native tasks, C loses no more than one success out of twelve relative to
   A, and no scenario shows a repeated regression;
3. on continuity-dependent tasks, C improves success by at least 20 percentage
   points;
4. on native tasks, the median C/A latency ratio is at most `1.15` and the 90th
   percentile ratio is at most `1.25`;
5. the median native tokens-per-success ratio is at most `1.25`.

The daemon may be described as **more token-efficient** only if its
tokens-per-success ratio is at most `1.00`. Between `1.00` and `1.25`, the
report describes a bounded overhead and shows the corresponding success gain.
Above `1.25`, no token-efficiency language is allowed even if continuity
improves.

These thresholds are communication gates, not universal laws. The report keeps
effect sizes and uncertainty intervals. A favorable result for one version is
not a promise about all future versions.

## Pre-Registered Exclusions

A run may be excluded only for one of these reasons:

- wrong model, wrong host version, or tool catalog inconsistent with the
  manifest;
- corrupted fixture or a checker unable to compute ground truth;
- proven cross-session contamination or access to data outside the fixture;
- runner failure before the host receives the turn;
- independent provider outage confirmed by the shared A/C health probe.

A model timeout, tool loop, incorrect answer, high latency, or high token use
after the host receives the turn is a **result**, not an exclusion reason.

Every exclusion remains in `runs.jsonl` with its reason, detecting checks, and
replacement seed. The decision is made without considering whether it favors A
or C.

## Public Reproducibility

V1 is not considered published until a third party can perform two distinct
operations.

### Offline Recalculation

Without a model or secret, a reviewer must be able to start from anonymized raw
results and regenerate exactly:

- success outcomes and individual checks;
- latency and token aggregates;
- exclusions;
- report tables and charts;
- checksums for every artifact.

The offline report generator is deterministic and makes no LLM call.

### Full Rerun

With their own provider credentials, a reviewer must be able to rerun the same
episodes in disposable profiles. Identical wording and millisecond latency are
not expected from a remote model; the protocol, ground truth, and calculations
must be identical, and rerun results are compared with uncertainty intervals.

If a host or model version is no longer available, offline recalculation
remains exact, but the new live run is a different-version campaign. It does
not claim to reproduce the unavailable environment.

The `benchmarks/productivity_v1/` directory must contain:

```text
README.md                 exact offline and live commands
manifest.schema.json      version and measurement contract
manifests/                public campaign manifests
fixtures/                 synthetic generators and seeds
checkers/                 deterministic ground truth and checks
runner/                   Hermes and OpenClaw launchers
report/                   offline aggregator and public report template
```

Every manifest records:

- daemon commit and clean-worktree state;
- host versions and binary checksums;
- model, provider, reasoning effort, and exposed parameters;
- operating system, architecture, and runtime versions;
- hashes of tool catalogs and compared context blocks;
- random seed, ordering, timeout bounds, and maintenance mode;
- provenance for every token counter;
- allowed non-secret environment variables and explicit secret redaction.

Public traces contain no secret, personal path, or complete proprietary host
system prompt. When content cannot be published, its size and hash are retained
to verify parity without disclosing it.

Synthetic data and reproduction code live in Git. Larger anonymized raw results
are published as a release archive; its manifest and SHA-256 checksums remain
versioned in the repository.

## Freeze, Versioning, And Publication

This protocol is frozen as version `1.0.0`. Any normative change to scenarios,
metrics, thresholds, exclusion rules, or language policy requires a new
protocol version and remains visible in Git history.

Implementation proceeds in this order:

1. implement fixtures, checkers, schemas, and offline recalculation;
2. run only `dry-run` episodes to correct infrastructure and verify that every
   ground truth value is computable;
3. verify offline recalculation from a second machine or by a third party;
4. freeze the implemented corpus under tag `productivity-v1.0.0`;
5. execute the 240 episodes without changing gates or exclusions;
6. publish the report, anonymized data, manifests, exclusions, and reproduction
   commands together.

A later correction produces a new protocol and corpus version such as `1.0.1`
or `1.1.0`, plus a distinct campaign. The previous report stays available.

None of the 240 scored V1 episodes may run until every scenario is implemented
and its offline recalculation is identical from a second repository copy. What
is implemented today is listed in
[`benchmarks/productivity_v1/README.md`](../benchmarks/productivity_v1/README.md).
