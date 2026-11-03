# Daemons

**English** · [Français](docs/README.fr.md)

Your **daemon is your personal AI assistant**: an identity, persistent memory
and conversational continuity designed to last. Daemons is the open-source
cognitive system that lets it develop that continuity with you.

It runs on a **host agent**, such as Hermes Agent or OpenClaw, which provides
the model, tools and communication channels. Your history is kept outside the
model and host so it can remain yours as they change. Memory is local,
transparent and editable Markdown, also usable in **Obsidian**.

This is an **alpha**. See [available features and limits](docs/alpha-testing.md)
before trying it. Local storage does not mean local model inference:
relevant context is sent to your chosen model provider.

## Install the app

<a id="télécharger-lassistant"></a>
<a id="télécharger-manuellement"></a>
<a id="installer-depuis-terminal-ou-powershell"></a>

The Companion desktop app installs your daemon on Hermes Agent or OpenClaw and manages recognized
local host installations. It supports English and French, light and dark mode.

For now, use these commands on **Apple Silicon Macs** or **Windows x64**.
The repository currently requires an authorized GitHub account.
These permanent commands install the current published **alpha** on each run,
including releases labelled as previews on GitHub. No version needs to be edited.

### One-time prerequisite

Install GitHub CLI using its [macOS instructions](https://github.com/cli/cli/blob/trunk/docs/install_macos.md),
or run this in Windows PowerShell:

~~~powershell
winget install --id GitHub.cli --source winget
~~~

On Windows, close all PowerShell / Windows Terminal windows and reopen
PowerShell from Start. Check `gh --version`, then sign in on either platform:

~~~sh
gh auth login --hostname github.com --web --git-protocol https
~~~

### macOS — Terminal

~~~sh
(set -e; daemons_script=$(mktemp); trap 'rm -f "$daemons_script"' EXIT; gh api 'repos/jpacifico/daemon-harness/contents/apps/companion/distribution/install-macos.sh?ref=main' -H 'Accept: application/vnd.github.raw+json' > "$daemons_script"; sh "$daemons_script")
~~~

### Windows — PowerShell

~~~powershell
$daemonsScript = gh api 'repos/jpacifico/daemon-harness/contents/apps/companion/distribution/install-windows.ps1?ref=main' -H 'Accept: application/vnd.github.raw+json'; if ($LASTEXITCODE -eq 0) { & ([scriptblock]::Create(($daemonsScript -join "`n"))) }
~~~

Close the app before running the command again to replace it.
From r10, the app also updates itself: **Settings > Check for updates**.
Replacing or updating the app does not reinstall or upgrade your existing daemon.
The packages do not yet have recognized distribution signatures.
See the [installation guide](docs/companion_installation.md) for platform
prompts and troubleshooting, including an unrecognized `gh` command.

## Set up your daemon

1. Open the app, choose **Install a daemon**, then Hermes Agent (recommended) or OpenClaw.
2. Name your daemon, choose its grammatical gender and the conversation language,
   and connect a model account.
3. Follow the Telegram instructions to create a dedicated bot and send a first message.
   The daemon introduces itself and learns how to address you.

The guided path creates a **separate private installation of your chosen host**.
Both hosts recommend ChatGPT subscription sign-in with the Astra model, and also
accept an OpenAI or Anthropic API key. **Add a host** can later add OpenClaw
to a Hermes Agent daemon, sharing the same memory. Adopting an arbitrary
existing host is not a guided operation.
Which models are offered, and why, is listed in [Model providers](docs/model_providers.md).

[First setup](docs/getting-started.md) · [Memory and Obsidian](docs/memory.md) ·
[What Daemons does](docs/how-it-works.md)

## Exact installation impact

<a id="impact-exact-de-linstallation"></a>

After you confirm the setup plan, the app creates a private host installation
(Hermes Agent `0.21.5`, or OpenClaw `2026.10.7` with Node `24.22.1`), a memory
folder and background services: the host's gateway, an after-turn memory worker,
a nightly maintenance job and a local search model (about 640 MB). It does not
adopt or modify unrelated installations. API keys go to the operating system's
credential store.

- **Continuity:** new daemons receive the `parga-v1` profile and the reference
  daemon's voice (`persona.md` and `conversation.md`, without name or gender).
  No model call is added before the first text of a reply.
- **Host settings:** Companion writes the reference daemon's conversation and
  Telegram display settings into the new host's own configuration. With ChatGPT,
  OpenClaw also enables the subscription's hosted web search; API-key OpenClaw
  hosts have no web search until one is configured.
- **Host memory and tools:** the host keeps its native tools and permissions.
  Its competing native personal memory and automatic memory and skill rewriting
  are disabled.
- **Removal:** a Hermes Agent daemon can be removed while keeping Hermes, or
  uninstalled with its host; both keep the memory folder. A daemon created
  directly on OpenClaw is uninstalled with its private OpenClaw, keeping the
  memory folder.
- **Changing host:** a daemon created by the app can move from Hermes Agent to
  OpenClaw or back, with its memory, Telegram bot and model connection. The
  previous host is uninstalled only after the daemon has answered on the new
  one; its memory folder is kept as an archive.
- **Existing daemons:** updating the app changes none of them. A daemon keeps
  the profile it was created with.
- **Backups and background mode:** background mode, open at login, encrypted
  backups and Google Drive stay off until you turn them on. A backup restores
  into a new daemon, as a copy or a move; it never changes the original daemon.

Read the [complete installation-impact reference](docs/installation_impact.md)
for paths, credentials, settings, maintenance, removal and platform limits.
Manual plugin installation is in the [Hermes Agent](integrations/hermes_plugin/INSTALL.md)
and [OpenClaw](integrations/openclaw_plugin/INSTALL.md) guides.

## What has been measured

The [Continuity Benchmark](docs/50_continuity_benchmark.md) compares the same
Hermes Agent, model and tools with and without the daemon, on 24 synthetic
personal-assistant scenarios judged blind by two Claude Opus 5.5 judges. Its
claims were fixed before any run. Version 1.1 (31 October 2026):

- with the daemon, Hermes used every earlier fact in 16 scenarios against 12,
  never asked again for something already said (0 against 2), used outdated
  context less (1 against 5) and was preferred in 13 scenarios against 8;
- it passed 12 scenarios against 10, short of the required +6: **no claim that
  the daemon improves Hermes as a personal assistant is made**. Its main
  failure is a chain of single questions in place of help;
- Hermes kept its capabilities: 12 of 12 native tasks on both sides
  (version 1.0);
- a full turn, after-turn memory work included, used 3.42 times the tokens of
  Hermes alone.

[Reports, records and limits](docs/50_continuity_benchmark.md).

## Documentation and development

The [documentation index](docs/README.md) includes both user guides and detailed
developer references:

- [The daemon at a glance](docs/overview.md): one turn in a diagram, then each step explained.
- [Architecture](docs/architecture.md): each component, each step of a turn and each rule.
- [Runtime configuration](docs/configuration.md) and [local bridge API](docs/bridge-api.md).
- [Memory structure, formats and lifecycle](docs/memory_structure_developer_guide.md).
- [Operations and troubleshooting](docs/operations.md).
- [Contributing and local tests](CONTRIBUTING.md).
- The
  [Continuity Benchmark](docs/50_continuity_benchmark.md) and its
  [reproduction guide](benchmarks/continuity/README.md).
- [App source and distribution](apps/companion/README.md).

The documentation distinguishes shipped behavior, optional activation and
proposals. Historical evidence remains accessible without being mistaken for
current setup instructions.

## Author, contact and license

Created by **Jonathan Pacifico**.
[Questions and issues](https://github.com/jpacifico/daemon-harness/issues) ·
[Security reporting](SECURITY.md) · [MIT license](LICENSE).
