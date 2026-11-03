# Developer installation guide

Use [Companion](getting-started.md) for a new guided Hermes installation.
This page is for developers integrating an existing host or studying how the
installed components fit together.

## Choose the deployment path

| Path | What it does | Detailed procedure |
| --- | --- | --- |
| Companion-managed Hermes | Creates a private compatible host, runtime, memory and services | [User setup](getting-started.md), [exact effects](installation_impact.md) |
| Existing Hermes profile | Links memory and voice plugins into the selected profile | [Hermes manual integration](../integrations/hermes_plugin/INSTALL.md) |
| Existing OpenClaw profile | Adds a context-engine plugin and local Python bridge | [OpenClaw manual integration](../integrations/openclaw_plugin/INSTALL.md) |

Do not combine a managed installation with the manual scripts indiscriminately.
The scripts can manage shared service names, whereas Companion owns
installation-specific services and receipts.

## Prepare a development checkout

Choose a durable checkout. Use a separate host profile, user identity, memory,
channel and provider configuration for tests. A Git worktree alone does not
isolate any of those resources.

~~~sh
git clone https://github.com/jpacifico/daemon-harness.git
cd daemon-harness
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=.:src .venv/bin/python -m pytest -q
node --test integrations/openclaw_plugin/index.test.js
~~~

These commands prepare source checks, not personal background services.
For Windows test-shell equivalents, see [contributing](../CONTRIBUTING.md).
The current desktop source is identified separately in
[Companion source and distribution](../apps/companion/README.md).

## Initialize before activating

A usable integration needs all of the following:

1. A host that can already reach its model and intended channel.
2. An initialized daemon identity and conversation profile.
3. A private, explicit memory root with the appropriate layout.
4. Working credentials for maintenance as well as conversation.
5. A durable after-turn consumer and scheduled maintenance configured for
   the same daemon.
6. Loaded plugin paths and versions matching the validated deployment.

The legacy terminal entry point `scripts/onboard.sh` can initialize or import
memory. Inspect its options with `bash scripts/onboard.sh --help` and supply a
private environment file. It is not the distributed graphical setup.
Do not point its import options at an existing personal vault as a test.

Use [configuration](configuration.md) to understand provider inheritance,
identity, paths, optional activation and the distinction between raw defaults
and managed installation settings.

## Platform scope

The manual API/worker managers described here use macOS launchd. Some older
scripts include Linux cron or systemd paths; their presence does not establish
a currently qualified complete Linux installer.
Windows Companion has a separate private-service implementation. Running
the macOS shell guide on Windows is not an equivalent deployment.

## Verify and recover

Follow the host-specific checks, then [operations](operations.md).
A process or plugin listing is insufficient: verify exact memory access,
a complete test response and completion of after-turn work in an isolated
profile before promoting a runtime change.

Keep rollback state and backups, including hidden original journals.
Manual removal, app removal and deleting a vault are distinct operations.

The [architecture](architecture.md), [bridge API](bridge-api.md) and
[memory schema](memory_structure_developer_guide.md) explain implementation
contracts.
