# Public Productivity Benchmark V1

This directory implements the first reproducibility milestone from the
[frozen English protocol](../../docs/44_public_productivity_benchmark_v1.md).
Its independent-copy validation is recorded in the
[canonical evaluation report](../../docs/evaluations/2026-08-29_reproductibilite_banc_productivite_v1.md).

Implemented now:

- frozen manifest and run JSON Schemas;
- the English `native.files.aggregate`, `native.tools.extract`, and
  `continuity.decision` fixtures, each with six public pair seeds;
- deterministic `left` and `right` twin fixtures for independent A/C sessions;
- deterministic checkers that verify artifacts, source integrity, file scope,
  and actual access to every controlled HTTP source;
- a standard-library-only offline recalculator;
- disposable Hermes and OpenClaw A/C launchers shared by both scenarios;
- reproduction of the daemon's installed OpenClaw `workspace/AGENTS.md` as a
  pre-existing runtime file in arm C, excluded from agent-created side effects
  but still present in the model context;
- content-safe host telemetry for tool, usage, and latency boundaries (including
  OpenClaw's native Codex rollout inside the disposable profile, whose final
  cumulative usage snapshot covers every model call in the episode);
- separate OpenClaw host and daemon-server environments: provider credentials
  required only by the daemon never make a host skill eligible in arm C;
- a four-run engineering campaign with a machine-readable go/no-go verdict;
- one explicitly non-reportable schema example with a known summary hash.

Not implemented yet:

- the OpenClaw first-fragment boundary, which its local JSON runner does not
  currently expose and which therefore remains explicitly unavailable;
- the other seven scenarios;
- any scored V1 campaign.

The files under `examples/` are schema examples, not benchmark evidence. Their
manifest has `report_eligible: false` and uses placeholder host metadata.

## Requirements

- Python 3.11;
- no Python package beyond the standard library for fixture generation,
  checking, or recalculation;
- provider credentials are not needed for this milestone.

Provider credentials and installed Hermes/OpenClaw hosts are required only for
the live dry-run below. The runner copies credentials into short-lived profiles
but never copies personal transcripts, memory, skills, or messaging settings.

Run commands from the repository root with `PYTHONPATH=.`.

## Materialize The English File Scenario

The control record must remain outside the workspace shown to an agent.

```bash
BENCH_TMP="$(mktemp -d)"
PYTHONPATH=. python3 -m \
  benchmarks.productivity_v1.runner.prepare_fixture \
  --seed 1101 \
  --variant left \
  --workspace "$BENCH_TMP/workspace" \
  --control "$BENCH_TMP/control.json"
```

The command prints the English prompt as JSON. After an agent completes the
task in the disposable workspace, check the actual artifact rather than its
prose response:

```bash
PYTHONPATH=. python3 -m \
  benchmarks.productivity_v1.runner.check_fixture \
  --workspace "$BENCH_TMP/workspace" \
  --control "$BENCH_TMP/control.json" \
  --output "$BENCH_TMP/outcome.json"
```

The checker exits with `0` only when every authoritative check passes.

## Recalculate The Versioned Example Offline

```bash
PYTHONPATH=. python3 -m \
  benchmarks.productivity_v1.report.recalculate \
  --manifest \
    benchmarks/productivity_v1/examples/native_files_aggregate/manifest.json \
  --runs \
    benchmarks/productivity_v1/examples/native_files_aggregate/runs.jsonl \
  --output /tmp/productivity-v1-summary.json
shasum -a 256 /tmp/productivity-v1-summary.json
```

The expected digest is recorded in
`examples/native_files_aggregate/expected-summary.sha256`. The recalculator
uses no network or LLM call, emits no current timestamp, sorts every group, and
uses the frozen nearest-rank percentile method.

On Linux, `sha256sum` may be used instead of `shasum -a 256`.

## Run A Live A/C Dry-Run

Commit the implementation first: the launcher refuses a dirty checkout so the
daemon revision is unambiguous. It then executes exactly four independent
episodes: Hermes A/C and OpenClaw A/C. It uses no gateway or real channel.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src .venv/bin/python -m \
  benchmarks.productivity_v1.runner.run_dry_run
```

Select the controlled local-source scenario explicitly with:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src .venv/bin/python -m \
  benchmarks.productivity_v1.runner.run_dry_run \
  --scenario-id native.tools.extract
```

Run the first longitudinal scenario with:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src .venv/bin/python -m \
  benchmarks.productivity_v1.runner.run_dry_run \
  --scenario-id continuity.decision
```

This episode sends two ordinary history turns in one native session, then
opens a distinct scored session. No arm is seeded through a private memory
write. All three turns count toward latency and tokens.

The first longitudinal dry-run and its two rejected instrumentation attempts
are documented in the
[canonical evaluation report](../../docs/evaluations/2026-08-30_productivity_v1_continuity_decision.md).
The report's original OpenClaw comparison was later invalidated: arm A had no
native memory provider and the Codex process was not isolated from the host
home. Its raw bundle is deliberately not versioned because one failed native
output contained state outside the synthetic fixture and potentially private.
The report retains only content-safe measurements and the artifact digest.

Replay the six corrected, preregistered OpenClaw continuity pairs with:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src .venv/bin/python -m \
  benchmarks.productivity_v1.runner.run_openclaw_continuity_replay
```

The corrected runner enables native `memory-core` only in A, invokes its public
transcript-index synchronization between history and resumption, and counts
that time. Both arms receive a disposable home and an attested outer macOS
sandbox; the real host home is denied while OpenClaw's native memory remains
unsandboxed from the host's own point of view. The exact protocol is frozen in
[`2026-08-30_protocole_rejeu_isole_continuity_openclaw.md`](../../docs/evaluations/2026-08-30_protocole_rejeu_isole_continuity_openclaw.md).

The source server binds only to loopback, lives only for one episode, records
requested paths without query contents, and never places its source documents
inside the agent workspace.

The command exits with `0` only if all engineering gates pass: four completed
and objectively checkable outcomes, host token and tool telemetry, Hermes
first-fragment telemetry, explicit OpenClaw boundary unavailability, daemon
composition timing, external tool parity, context attestations, and exact
offline recalculation. An objectively measured agent failure is a valid
benchmark outcome; it does not make the measurement infrastructure
non-conclusive.

Each campaign is written below `.cache/benchmarks/productivity-v1/` and
contains:

- `plan.json`, written before the first provider turn;
- `manifest.json` and `runs.jsonl` using the public schemas;
- deterministic output artifacts and content-safe diagnostics;
- `summary.json`, `dry-run-verdict.json`, and `SHA256SUMS`.

`timing.wall_ms` is the turn wall time excluding disposable setup;
`timing.startup_ms` records that setup separately, so their sum is the full
wall time including startup. A failed episode receives the registered timeout
in `correct_result_ms`, while its actual elapsed time remains in `wall_ms`.

The three first live engineering campaigns are retained under `results/`.
The first two are intentionally marked non-conclusive because their OpenClaw
instrumentation was incomplete. The third passes every engineering gate. None
of them contributes to the scored V1 corpus (`report_eligible: false`).

## Run One Preregistered OpenClaw Validation Window

The OpenClaw-only validation campaign has two fixed windows of six A/C pairs.
Its allocation, temporal separation, and decision gates are frozen in
`docs/evaluations/2026-08-30_protocole_validation_native_openclaw_deux_fenetres.md`.
Run exactly one window from a clean checkout with:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src .venv/bin/python -m \
  benchmarks.productivity_v1.runner.run_openclaw_window \
  --window 1
```

Use `--window 2` only in the later preregistered time window. The launcher
writes the complete plan before the first provider turn, runs only disposable
OpenClaw profiles, checks stable context and tool-catalog attestations, then
recalculates the consolidated summary offline. These engineering campaigns
remain excluded from the scored public corpus (`report_eligible: false`).

## Attribute Native After-Turn Work

The corrective native-only campaign separates response tokens from immediate
native background tokens on both hosts. Its protocol is frozen in
[`2026-08-30_protocole_attribution_apres_tour_natif_hotes.md`](../../docs/evaluations/2026-08-30_protocole_attribution_apres_tour_natif_hotes.md).
Run it from its clean experimental checkout with:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src .venv/bin/python -m \
  benchmarks.productivity_v1.runner.run_native_afterturn_attribution
```

Hermes keeps a measured native review thread alive until its terminal hook.
OpenClaw keeps the disposable Gateway alive, closes attributable tasks between
turns, and observes at least 35 seconds after the final response. Missing
embedding usage remains unavailable; it is never inferred from LLM counters.

## Run The Full-Lifecycle Continuity Comparison

The full-lifecycle runner compares all six `continuity.decision` pairs on both
hosts, with two persistent conversations per episode. It waits for the three
daemon receipts, scene projection, native after-turn barrier, and every known
LLM usage record before producing a verdict:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src .venv/bin/python -m \
  benchmarks.productivity_v1.runner.run_full_lifecycle_continuity \
  --campaign-id <unique-campaign-id>
```

To replay the same lifecycle and gates on OpenClaw only, without spending an
unrelated Hermes window, select the host explicitly:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src .venv/bin/python -m \
  benchmarks.productivity_v1.runner.run_full_lifecycle_continuity \
  --campaign-id <unique-campaign-id> \
  --host-id openclaw \
  --scenario-id native.files.aggregate \
  --scenario-id native.tools.extract \
  --scenario-id continuity.decision
```

By default the command keeps the historical continuity-only behavior. Repeat
`--scenario-id` to run any subset of the three implemented scenarios. For
example, the two native scenarios use:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src .venv/bin/python -m \
  benchmarks.productivity_v1.runner.run_full_lifecycle_continuity \
  --campaign-id <unique-campaign-id> \
  --scenario-id native.files.aggregate \
  --scenario-id native.tools.extract
```

Each native episode has one conversation and one durable daemon receipt; the
continuity episode keeps two conversations and three receipts. The plan fixes
the selected scenarios, seeds, turns, history size, host order, and arm order
before the first provider call. Gates derive their expected counts from that
plan rather than assuming the continuity shape.

Hermes 0.21 arm A keeps its native `memory` tool and background review
defaults. Arm C mirrors the canonical Daemon Harness installation: native
review and skill creation nudges are disabled, and the native memory tool is
replaced by `daemon_read_souvenirs` and `daemon_recall_l0`. The gate compares
external tools separately from these mutually exclusive memory installations
and attests the expected memory tools in both arms.

OpenClaw authentication is imported into each disposable profile through the
public migration command. Only a 30-second timeout is retried, at most three
times; explicit command failures remain terminal. Attempts, timeouts, and total
duration are recorded and remain part of startup time.

The command is conclusive only after 24 completed records, stable attestations,
external tool parity, exact memory-installation gates, closed durable cycles,
native after-turn closure, complete token attribution, and exact offline
recalculation. Its engineering bundles remain non-reportable for public V1.

## Safety Boundary

- Never point a benchmark workspace, control record, cache, or host profile at
  production state.
- Never put provider credentials, full host system prompts, or personal paths
  in a manifest or result bundle.
- A bad model answer, timeout, tool loop, high latency, or high token use after
  turn receipt is a result, not an exclusion.
- English V1 and a future French track remain separate campaigns.
