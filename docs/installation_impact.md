# What installation changes

This page describes what the distributed Companion app installs and
changes. Installing a plugin by hand on an existing host is a separate developer
operation, described in the [Hermes Agent](../integrations/hermes_plugin/INSTALL.md)
and [OpenClaw](../integrations/openclaw_plugin/INSTALL.md) guides.

## Installing or updating Companion

The download command verifies a pinned package and installs the desktop app:

- Mac: ~/Applications/Daemons Companion.app
- Windows: %LOCALAPPDATA%\Programs\Daemons Companion

The app checks for its own updates shortly after it opens and every
six hours; **Settings > Check for updates** checks at once. It reads the
release named by `apps/companion/distribution/channel.json` on `main`, verifies
every downloaded file against its SHA-256, and replaces itself through the
release's own install script, which keeps the previous app when anything fails.

Updating the app replaces only the app. It does not reinstall, upgrade or
restart an existing daemon, move memory or reconnect accounts.

## Updating a daemon

A daemon runs its own copy of the Daemon Harness code, installed when it was
created. When the app carries a newer copy, the daemon's page offers
**Update**, for a daemon created by the app on Hermes Agent or on OpenClaw. It
runs only when you choose it.

The update replaces only the part of the copy that installs and connects the
daemon: how its services are scheduled and started. The engine (memory, voice,
prompts, host plugins and dependencies), the memory itself, the model
connection and the Telegram bot do not change. The app stops the daemon's
services, installs the new copy beside the old one, points the host profile
at it, restarts the services and waits until the gateway, the memory worker
and, on OpenClaw, the daemon API and its plugin are ready. The daemon does not
answer on Telegram in the meantime. If any step fails, the previous copy,
profile and services are restored exactly; an interrupted update is offered
for restoration the next time the page opens. On OpenClaw, the profile then
loads the plugin from the new copy only; the previous copy is kept on disk for
that restoration. On Hermes Agent, the update also adds a host setting
introduced after the daemon was created, such as
`plugins.load_timeout_seconds: 120`; rollback restores the previous file.

The page explains instead why a daemon cannot be updated:

- the newer copy also changes the engine, which an existing daemon keeps: back
  up the memory and install a new daemon from that backup;
- the daemon also answers from an added host;
- its services, host settings or OpenClaw workspace changed since installation.

## Background mode, backups and Google Drive

From 0.11.0 these are off until you turn them on.

- **Background mode** (Settings > Background) keeps the app running with an
  icon in the menu bar (Mac) or notification area (Windows) when its window is
  closed. **Open at login** registers the app as a login item of your user
  account; turning background mode off removes it. Turning on a daily backup
  turns background mode on and says so.
- **Back up memory** (a daemon's settings) writes one file encrypted with a
  password you choose (scrypt, then AES-256-GCM). Daemon Harness cannot recover the
  password. The daemon keeps running: the copy waits for its after-turn work
  and is retried if a file changes while it is read. Embeddings, indexes,
  channel state and credentials are not in a backup.
- **Google Drive** (Settings > Google Drive) signs in through your browser and
  asks only for access to the files the app creates (`drive.file`), in a
  visible `Daemons/Backups` folder. The sign-in, and the key of each daemon's
  daily backup, are stored in app data files encrypted by the macOS Keychain or
  Windows DPAPI, never in plain text. A daily backup runs while the app is
  running and keeps the last seven automatic backups of each daemon; manual
  backups are never deleted. Disconnecting removes the stored sign-in and keys.
  During the alpha, Google accepts only the accounts the publisher lists as
  testers.
- **Restore from a backup** (My daemons) creates a new daemon with the usual
  setup, around the restored memory: name, identity, voice and memories are
  kept and the first meeting is not replayed. It needs the password, a new
  model connection and the Telegram account the memory belongs to. A restore
  never writes into an existing memory folder and never changes the original
  daemon. **Copy** leaves scheduled reminders with the original; **Move**, for
  an original you stop or remove, also brings them, and the new host schedules
  them after its first conversation turn.

## Creating a daemon

After you approve the setup plan, Companion creates a separate private
installation of the chosen host (Hermes Agent or OpenClaw), a memory folder
and background services. The installation lives under
DaemonHarness/Companion/instance-&lt;id&gt; in the user's Application Support
directory on Mac or LocalAppData directory on Windows. Use the app's displayed
locations; older installations can have different paths.

| Part | What is installed |
| --- | --- |
| Host | Hermes Agent `0.21.5` with its locked `messaging`, `bedrock`, `edge-tts` and `anthropic` extras, or OpenClaw `2026.10.7` with its own Node `24.22.1` |
| Runtime | A private Python environment with the Daemon Harness plugins and the host's **gateway**, the service that receives and sends messages |
| Memory maintenance | An after-turn worker and a nightly maintenance job (Dreaming and episodic REM) |
| Memory search | A local embedding service with Qwen3-Embedding 0.6B (about 640 MB), separate from the model that writes replies |
| OpenClaw only | A loopback API that the OpenClaw plugin calls |

On Windows the services are scheduled tasks in the signed-in user's session;
they run without console windows. A service that crashes is restarted at most
once a minute: up to three times on Windows, and without limit on Mac, where
launchd keeps it alive. Existing unrelated host installations are not
adopted or modified.

On Mac, the services that work while the user waits for a reply (the gateway,
the memory search and the OpenClaw API) run in launchd's standard class. The
after-turn worker and the nightly maintenance run in the background class,
which macOS confines to slower cores and throttled disk access. Daemons created
by 0.11.15 or earlier run every service in the background class. Updating the
app does not rewrite their services.

## Continuity profile

A new daemon receives the `parga-v1` continuity profile, recorded in a marker
inside its memory. One profile governs the conversation, the after-turn worker
and the nightly maintenance. It enables:

- PARGA filing and relocation, current context, agenda maintenance and episodic memory;
- Common Ground, consolidated by the MemoryAgent and read by the voice, and the
  relation shadows inside the same MemoryAgent call;
- on Hermes Agent, the progress preamble on Telegram, without a receipt reaction;
- a nightly Dreaming pass over the last 48 hours.

The profile adds no model call before the first text of a reply. Setup verifies
that the gateway and the worker loaded the complete profile for the right owner
and memory folder. A marker keeps the profile it was created with: daemons
created by r9 also use `parga-v1`, and daemons created by earlier releases keep
`parga-v0`. Prompt identity evidence.

## Voice and first meeting

A new memory receives the reference daemon's voice:
`users/<id>/persona.md`, its character, and `users/<id>/conversation.md`, its
way of conversing. The persona carries neither name nor gender, which come from
the identity settings at every reply, and refers to the user neutrally. Both
are fixed texts: the MemoryAgent never edits `conversation.md` and changes
`persona.md` only on an explicit request. `context.md` starts empty and grows.

The first conversation is a short welcome in the language chosen during setup.
The daemon gives its name and asks how to address you, then where you are, then
says what it can do with its tools and ends with one open question. It asks at
most one question per reply and never offers a choice between alternatives; a
subject or a request ends the welcome at once. The guidance stops after three of
your messages, and a completed welcome is never replayed, even after a restart
or a host change. Existing memories are not opted in.
Comparison.

## Host settings written by Companion

Companion writes these values in the new private host's own configuration.
Removal restores the previous values.

**Hermes Agent**: `web.search_backend: ddgs` (searches run as visible Hermes
tools instead of hidden model-native search), `agent.reasoning_effort: medium`,
`display.show_reasoning: false`, `approvals.mode: manual`, the Telegram
display values the gateway does not infer from defaults
(`display.platforms.telegram.streaming: true`, `display.tool_preview_length: 0`,
`display.busy_ack_detail: true`), and `plugins.load_timeout_seconds: 120`.
Hermes otherwise gives each messaging plugin 10 seconds to load: after a slow
boot, the Telegram plugin can overrun it, and the gateway then keeps running
without Telegram until it is restarted.

**OpenClaw**: Telegram `progress` streaming with tool progress, status command
text and the voice's brief commentary, without a progress label; automatic
visible replies, no acknowledgement or status reaction; no periodic heartbeat
turn; one session per channel peer; the native `coding` tool profile plus the
daemon's memory tools. With ChatGPT sign-in, the subscription's hosted web
search is enabled in live mode. API-key OpenClaw hosts have no web search until
a search provider is configured in OpenClaw.
Host commands require approval: `tools.exec.mode: ask`, with Telegram
`execApprovals` naming the owner as the only approver, in direct messages. A
command not yet allowed waits for the owner's answer; without one, OpenClaw's
default fallback refuses it (approvals expire after 30 minutes by default).
Without these values, OpenClaw `2026.10.7` runs host commands without approval.
Daemons created earlier keep their configuration; the
[OpenClaw guide](../integrations/openclaw_plugin/INSTALL.md) gives the values.
Evidence.

## Model connections and credentials

Both hosts accept ChatGPT subscription sign-in, or an OpenAI or Anthropic API
key. Only models qualified for the complete daemon are offered
([Model providers](model_providers.md)): Mistral, GPT-5.6-Terra and SpaceXSI
(Grok 4.7) were withdrawn on 2026-10-29. A daemon created earlier with a Mistral
or SpaceXSI key keeps working with it, including when it changes host.
Hermes Agent recommends the subscription; OpenClaw lists API
keys first. With an API key, the selected provider and model serve the host's
conversation and the daemon's memory maintenance. An available-model list and a
small request check access before Telegram setup; a failed check keeps the key
for a retry or another model.

With ChatGPT, the host signs in natively and the daemon's maintenance uses a
separate private Codex session (Codex CLI `0.159.2`, installed inside the
private installation; a Codex already on the computer is not used). On OpenClaw, only Astra
and GPT-6.1-Sol are offered, when the account's native catalogue returns them. Cancelling or failing a sign-in starts no service.

API keys are stored in the macOS Keychain or Windows Credential Manager. The
host receives the key when it starts; it is not written into the service
definition. Other native sign-in state and the bot token stay in the private
installation's files. A failed installation rolls back only what its own
recorded operations created.

**Model** (a daemon's settings) shows the provider, the model and whether an
API key is saved, and can check the key with the provider. For a daemon created
by the app with an API key, **Change key or model**
lists the models offered with the current or a new key, sends the same small
test request as at creation, then writes the new model into the host's own
settings (`openclaw.json` or Hermes `config.yaml`) and the daemon's
`provider.json`, stores a new key under new Keychain or Credential Manager
entries, and restarts the gateway if it was running. The previous files and
keys are kept until the restart succeeds; a failed restart, or a change left
unfinished, restores them exactly. The model of a ChatGPT subscription daemon
is changed by choosing the subscription again through **Change provider**.

**Change provider** moves a daemon created by the app, on Hermes Agent or
OpenClaw, to another provider offered to new daemons: an OpenAI or Anthropic API
key, or the ChatGPT subscription. It is a guided session: the person connects
the new provider (the same key check and model test, or the same ChatGPT
sign-ins and private Codex as at creation) and chooses an approved model before
anything changes. The daemon's gateway is then stopped, its after-turn queue
allowed to finish, its other services stopped (the local search service keeps
running), and these files rewritten as a new install of that provider writes
them: `provider.json`, `connection.json`, the host's settings (`config.yaml`,
or `openclaw.json` with `openclaw-connection.json`), plus a new Keychain or
Credential Manager entry for a key. The services restart with their unchanged
definitions. The previous files, keys and service definitions are kept until
the person confirms a reply on Telegram; going back, a failure or a session
left unfinished restores them exactly and restarts the previous services.
Memory, identity, conversation history and the Telegram bot are not touched.
Sign-ins made for ChatGPT stay in the private installation if the change is
undone.

Memory is local. Relevant context is sent to the model provider you choose.
OpenAI and Anthropic API billing is separate from consumer chat subscriptions.

## Host memory, tools and maintenance

The daemon supplies the personal memory and the voice context. The managed host
configuration disables the host's competing native personal memory and its
automatic memory and skill rewriting. Tools and their permissions stay the
host's; Daemon Harness adds no email, web or professional-calendar integration of its
own. The precise effects differ by host:
[Hermes Agent](../integrations/hermes_plugin/INSTALL.md) and
[OpenClaw](../integrations/openclaw_plugin/INSTALL.md).

## Adding a host

**Add a host** adds a private OpenClaw installation to a Hermes Agent daemon
created by Companion. OpenClaw gets its own model connection and its own
Telegram bot, and shares the same memory and maintenance; no second memory or
worker is created. When required, the app first reviews an explicit update of
the daemon's runtime. Adding Hermes Agent to an OpenClaw daemon, moving a bot
between hosts and adopting an arbitrary existing host are not available.

## Changing a daemon's host

**Move to OpenClaw** or **Move to Hermes Agent** (a daemon's settings) moves a
daemon created by the app to the other host, on Mac and Windows. A daemon that
already runs on both hosts is refused.

1. A new private installation of the other host is created while the daemon
   keeps answering. Name, gender and conversation language come from its
   memory. The same provider and model are checked again with the API key read
   from the previous host's Keychain or Credential Manager entry; a ChatGPT
   subscription signs in again on the new host. The bot token is read the same
   way and checked with Telegram `getMe` only. A rejected key or token is asked
   for.
2. After one confirmation, the previous host's service definitions are saved,
   its gateway stops, the after-turn queue is waited for (up to seven minutes),
   and its other services stop. The memory is archived in Move mode, with
   pending reminders, and restored into the new installation before it starts.
   The memory folder path therefore changes; an Obsidian vault must be added
   again from the new location.
3. The app waits for a real reply on Telegram and its completed memory work,
   then asks whether the reply arrived. **Yes** uninstalls the previous host as
   described below. **Go back** uninstalls the new host and restores the
   previous services exactly.

Until that answer, a failure or a cancelled session restarts the previous
host's services from their saved definitions. A session closed while the new
host awaits the check resumes from its card. The new host stores the key and
token under its own new credential entries. The previous memory folder stays
as an archive and is no longer used. Uninstalling a previous Hermes Agent
keeps its saved API key entry, as any Hermes Agent uninstall does.

## Managing an existing installation

Companion displays a recognized installation without changing it. Explicitly
connecting its controls allows supported start, stop and restart actions.
**Open host interface** opens the Hermes dashboard or the running OpenClaw
Control UI on loopback. Forgetting an installation removes only the app's
connection to it.

Identity and address preferences can be edited when the installed daemon
supports them. The memory location is visible, openable and can be added to
Obsidian. Changing a setting shared by several hosts requires confirmation.

## Removing a daemon or host

For a completed Hermes Agent daemon created by Companion, on Mac and Windows:

- **Uninstall Hermes and the daemon** stops both and removes their programs,
  services and local search model. An OpenClaw added to this daemon is stopped
  and uninstalled too. Confirmation requires the installation's displayed name.
- **Remove the daemon, keep Hermes** removes the Daemon Harness integration,
  maintenance and local search model. Hermes remains installed without Daemon Harness
  memory and continuity. An added OpenClaw is stopped and disconnected first; it
  stays installed. Confirmation requires the daemon's name.

For a completed daemon created directly on OpenClaw, which has its own private
OpenClaw, **Uninstall OpenClaw and the daemon** stops its five background
services, removes OpenClaw, its Node runtime, caches, local search model and
the daemon's runtime, and deletes its saved API key and bot and gateway tokens
from the Keychain or Credential Manager. Confirmation requires the
installation's displayed name. Keeping OpenClaw without the daemon is not
offered.

**The memory folder is preserved by default**, and its path stays visible.
A full uninstall can also delete it, only on request: an unchecked option on
the review page, then a second, system confirmation before anything starts.
After the uninstall has finished, the app checks again that the folder is the
memory Companion created inside that daemon's own folder and that no listed
host or scheduled job still uses it, then moves it to the Trash (Recycle Bin on
Windows), where it can be recovered until emptied. A memory anywhere else is
never offered for deletion; if the move is refused or fails, the memory stays.
Profiles and conversation history are kept, as are Hermes Agent's saved
native sign-ins; bots and provider accounts are not deleted.

An unfinished new installation, on Hermes Agent or OpenClaw, can be deleted
from its card, keeping memory, profiles and recovery records. An unfinished
OpenClaw from Add a host is resumed, not deleted, and is uninstalled only with
its Hermes Agent daemon. Shared, changed or
ambiguous configurations are refused rather than removed unsafely. If recovery
is required, use the same operation on the same installation instead of
deleting folders by hand.

See [your memory](memory.md) and [alpha limitations](alpha-testing.md).
