# Documentation

Daemon Harness has user guides, implementation references and reproducible evaluation
records. Technical detail belongs here; terms are introduced before use.
[Project overview and download](../README.md) · [Présentation française](README.fr.md)

## Use Daemon Harness

| Goal | Guide |
| --- | --- |
| Install the app and send a first message | [Getting started](getting-started.md), [installation en français](companion_installation.md) |
| Understand daemon, host and background work | [How it works](how-it-works.md), [the daemon at a glance](overview.md) |
| Open and manage memory in Obsidian | [Your memory](memory.md) |
| Understand how memory is organized | [PARGA: the five folders](parga.md) |
| Know what an installation changes | [Exact installation impact](installation_impact.md) |
| Try the research preview and report a failure | [Research preview checklist and limits](alpha-testing.md) |

## Develop and operate Daemon Harness

Read these in order when joining the project:

1. [Architecture](architecture.md): components, each step of a turn, what the
   model reads, after-turn and nightly work, write ownership and failures.
2. [Configuration reference](configuration.md): paths, identity, model roles,
   queue settings, nightly modes and activation dependencies.
3. [Developer installation](installation_technique.md), then the complete
   [Hermes](../integrations/hermes_plugin/INSTALL.md) or
   [OpenClaw](../integrations/openclaw_plugin/INSTALL.md) procedure.
4. [Memory developer reference](memory_structure_developer_guide.md):
   ontology, formats, provenance, retrieval, edits, links and lifecycle.
5. [Local HTTP API](bridge-api.md): contracts, mutation boundaries, idempotence
   and recovery.
6. [Operations and troubleshooting](operations.md): health, readiness, queues,
   maintenance, backups, upgrades and rollback.
7. [Contributing](../CONTRIBUTING.md): isolated development and local checks.

[Glossary](glossary.md) defines internal names including L0, Atlas, Scene Model,
Dreaming, REM, receipts and canary.

## Component references

- [After-turn worker contract](../integrations/after_turn_service/README.md).
- [Companion source, branches and distribution](../apps/companion/README.md).
- [Daemon health and installation diagnostics](companion-health.md): installed-state evidence and safe report export.
- [Health actions and opening memory in Obsidian](companion-health-actions.md).
- [Run the local HTTP API in Docker](docker.md): an isolated container for the OpenClaw bridge.

## Evidence

The [Continuity Benchmark](continuity-benchmark.md) compares Hermes with and
without the daemon as a personal assistant; its
[reproduction guide](../benchmarks/continuity/README.md),
[scenario dataset](../benchmarks/continuity/dataset/README.md) and
[campaign records](../benchmarks/continuity/records/README.md) let anyone
recompute or rerun the 1.0 and 1.1 results.
The [public benchmark protocol](productivity-benchmark.md)
and [benchmark implementation](../benchmarks/productivity_v1/README.md)
describe reproducible measurement; unexecuted cases are not results.
