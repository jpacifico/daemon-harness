# Daemon Harness

**English** · [Français](docs/README.fr.md)

**Daemon Harness** is an open-source cognitive system for a personal AI assistant:
your **daemon**. The daemon holds your conversations, with an identity, a
memory and a continuity that grow through your exchanges.

It runs on a **host agent**, Hermes Agent or OpenClaw, which provides the
model, the harness and the messaging channels. Your daemon thus runs on a
double harness, two layers: Daemon Harness, which carries its identity, memory
and continuity, and the host, which gives it its ability to act. Its identity
and memory belong neither to the host nor to the model: they are designed to
outlast a change of either, over years.

Your daemon keeps what matters, connects events and follows how your projects,
preferences and situation evolve. For each message, it finds the context that
matters, without starting from scratch or reciting everything it knows.

## The pillars of a personal assistant

Daemon Harness starts from a conviction: a very good personal assistant rests
on six pillars. Here is how Daemon Harness addresses each one.

**Context.** To help you, an assistant has to understand your situation:
whether you are at work, at home, travelling, in a meeting, exercising or with
family. The richer the context, the more useful and personal the help. Your
daemon keeps a dated, revisable reading of your present situation (what is
established, what is only assumed, upcoming deadlines), updated after each
exchange from what you share with it.

**Memory.** Over time, an assistant learns your preferences, ways of working,
habits, relationships, routines and earlier conversations. Your daemon keeps
them in local Markdown notes filed in five folders ([PARGA](docs/parga.md)),
each fact with its source. To reply, it reads only what concerns the subject.

**Personality.** A good conversation is fluid and natural, with a tone and
patterns that suit the person, like talking to a long-time friend. Your daemon
keeps one stable voice; its personality and way of conversing are described in
two files you can edit. No second assistant rewrites its replies, and
preparing the context adds no model call before the reply.

**Skills.** An assistant also has to act: research, programming, planning,
financial analysis, design tools, travel arrangements, health tracking or
working with other specialized agents. Daemon Harness leaves this to a host built for
it: Hermes Agent or OpenClaw, with others to come. The host keeps its own
tools and permissions.

**Trust.** You can only rely on an assistant that is dependable and accurate:
it says what it knows, admits what it does not and checks before asserting.
Your daemon is designed to tell apart what it remembers from what it assumes,
and to check its notes before asserting a fact: an empty search does not prove
that something never happened, and an old memory does not describe the present.

**Continuity.** A personal assistant is meant for years: it should depend on
no single model provider, agent or hosting service. Your daemon's identity and
memory live on your computer, outside the model and the host. A daemon created
with the app can move from Hermes Agent to OpenClaw and back with its memory,
and an encrypted backup moves it to another computer.

### Your memory, also in Obsidian

Your memory belongs to you. It stays on your computer, in Markdown files you
can read and edit directly or in [Obsidian](https://obsidian.md).
[Memory and Obsidian](docs/memory.md).

Local storage does not mean local model inference: relevant context is sent to
your chosen model provider.

## Install the app

<a id="télécharger-lassistant"></a>
<a id="télécharger-manuellement"></a>
<a id="installer-depuis-terminal-ou-powershell"></a>

The **Daemons Companion** app installs and manages your daemons on your
computer. With it, you can:

- create one or more daemons, each with its own host, Hermes Agent or OpenClaw,
  which the app installs, along with its model connection and Telegram bot;
- add OpenClaw to a Hermes Agent daemon, sharing the same memory, or move a
  daemon from one host to the other;
- open your daemon's memory, back it up encrypted to a file or Google Drive,
  and restore it to move your daemon to another computer.

Use these commands on **Apple Silicon Macs** or **Windows x64**. No account
is needed. Each run downloads the current published **alpha**, checks its
SHA-256 checksum against the value pinned in the script, installs it and opens
it. The commands never change: no version number needs to be edited.

### macOS — Terminal

~~~sh
(set -e; daemons_script=$(mktemp); trap 'rm -f "$daemons_script"' EXIT; curl --proto '=https' --tlsv1.2 -fsSL 'https://raw.githubusercontent.com/jpacifico/daemon-harness/main/apps/companion/distribution/install-macos.sh' -o "$daemons_script"; sh "$daemons_script")
~~~

### Windows — PowerShell

~~~powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; & ([scriptblock]::Create((Invoke-RestMethod -UseBasicParsing -ErrorAction Stop 'https://raw.githubusercontent.com/jpacifico/daemon-harness/main/apps/companion/distribution/install-windows.ps1')))
~~~

Close the app before running the command again to replace it.
The app also updates itself: **Settings > Check for updates**.
Replacing or updating the app does not reinstall or upgrade your existing daemon.
When the app carries changes to how it installs and runs daemons, the daemon's
page offers **Update**; see [installation impact](docs/installation_impact.md#updating-a-daemon).
The packages do not yet have recognized distribution signatures.
See the [installation guide](docs/companion_installation.md) for platform
prompts and troubleshooting.

**Alpha software, use with care.** Daemon Harness is under active development:
expect bugs, breaking changes and rough edges. It has not had an independent
security audit, and the app is not yet signed. Your daemon runs on your
computer with your model account and can use its host's tools there, so try it
on a computer and accounts you are comfortable testing with, keep a backup of
any memory you care about, and avoid giving it sensitive information. See
[available features and limits](docs/alpha-testing.md) before trying it.

## Set up your daemon

1. Open the app, choose **Install a daemon**, then Hermes Agent (recommended) or OpenClaw.
2. Name your daemon, choose its grammatical gender and the conversation language,
   and connect a model account.
3. Follow the Telegram instructions to create a dedicated bot and send a first message.
   The daemon introduces itself and learns how to address you.

The guided path creates a **separate private installation of your chosen host**.
Both hosts recommend ChatGPT subscription sign-in with the GPT-6.2 Astra model, and also
accept an OpenAI or Anthropic API key. **Add a host** can later add OpenClaw
to a Hermes Agent daemon, sharing the same memory. Adopting an arbitrary
existing host is not a guided operation.
Which models are offered, and why, is listed in [Model providers](docs/model_providers.md).

[First setup](docs/getting-started.md) · [Memory and Obsidian](docs/memory.md) ·
[What Daemon Harness does](docs/how-it-works.md)

## Exact installation impact

<a id="impact-exact-de-linstallation"></a>

When you confirm the setup plan, the app:

- installs a **private host**: its own copy of Hermes Agent or OpenClaw,
  reserved for your daemon and kept apart from any copy already on your
  computer, which it does not touch;
- creates your daemon's memory folder, its background services (the host's
  gateway, memory upkeep after each reply, a nightly maintenance job) and a
  local search model of about 640 MB; API keys go to your operating system's
  credential store;
- leaves the host its tools, but turns off the host's own personal memory,
  which would compete with the daemon's;
- makes the host ask your approval in your Telegram conversation before it runs
  a command on your computer: on OpenClaw, any command you have not allowed
  yet; on Hermes Agent, commands it detects as dangerous;
- keeps background mode, open at login, encrypted backups and Google Drive off
  until you turn them on;
- never deletes the memory folder: removing a daemon or its host keeps it.

The [complete installation-impact reference](docs/installation_impact.md) lists
paths, versions, settings, maintenance, removal and platform limits. Manual
plugin installation is in the [Hermes Agent](integrations/hermes_plugin/INSTALL.md)
and [OpenClaw](integrations/openclaw_plugin/INSTALL.md) guides.

## What has been measured

Existing benchmarks test an agent's skills, not whether it knows the person it
works for. So we built the [Continuity Benchmark](docs/continuity-benchmark.md):
it compares the same Hermes Agent, with the same model and tools, with and
without the daemon, on 24 invented people who each talk to their assistant
over several conversations. Its criteria were fixed before any run, and two
blind Claude Opus 5.5 judges must agree on each verdict.

Version 1.1 (31 October 2026):

| | Hermes alone | Hermes + daemon |
| --- | ---: | ---: |
| Uses every earlier fact | 12 / 24 | **16 / 24** |
| Asks again for something already said | 2 | **0** |
| Uses outdated or unrelated context | 5 | **1** |
| Invents a fact | 0 | 0 |
| Preferred in blind comparison | 8 | **13** (3 ties) |
| Native agent tasks passed (version 1.0) | 12 / 12 | 12 / 12 |

Measured with GPT-6-Astra through a ChatGPT subscription. A full turn currently
uses 3.42 times the tokens of Hermes alone, mostly for memory upkeep after the
reply; we are working to reduce it and will publish our progress as soon as
possible.

[Full results, method and limits](docs/continuity-benchmark.md).

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
  [Continuity Benchmark](docs/continuity-benchmark.md) and its
  [reproduction guide](benchmarks/continuity/README.md).
- [App source and distribution](apps/companion/README.md).

The documentation distinguishes shipped behavior, optional activation and
proposals. Historical evidence remains accessible without being mistaken for
current setup instructions.

## Author, contact and license

Created by **Jonathan Pacifico**.
[Questions and issues](https://github.com/jpacifico/daemon-harness/issues) ·
[Security reporting](SECURITY.md) · [MIT license](LICENSE).
