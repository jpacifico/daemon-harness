# Hermes integration

Hermes Agent is the **host**: it runs the response model, tools, session and
messaging channel. Daemon Harness supplies personal identity, persistent memory and
conversation context through two plugins: `daemon_core`, the memory provider,
and `daemon_voice`, the voice middleware.

## Installed by Companion

For a new private daemon, use [Companion](../../docs/getting-started.md). It
installs Hermes Agent `0.21.5` in its own private environment, with the locked
`messaging`, `bedrock`, `edge-tts` and `anthropic` extras that Hermes would otherwise download
during the gateway start and the first reply, both plugins,
the after-turn worker, the nightly maintenance and the local search service, and
records the `parga-v1` continuity profile. It also writes the reference daemon's
conversation settings in the private `config.yaml`: `web.search_backend: ddgs`,
`agent.reasoning_effort: medium`, `display.show_reasoning: false`,
`approvals.mode: manual`, Telegram response streaming,
`display.tool_preview_length: 0` and `display.busy_ack_detail: true`. Removal
restores the previous values.

The new memory receives the reference voice (`persona.md`, `conversation.md`)
and a first-meeting marker. While the welcome is open, the voice plugin hands
the owner's Telegram **Start** to the voice, so the daemon answers it; other
senders, chats and completed meetings keep Hermes's native handling. The plugin
also removes the first-contact notes Hermes appends to the very first message,
using the exact inbound text. A completed welcome is never replayed.

A Hermes Agent daemon created by Companion can receive an added OpenClaw host
(**Add a host**) that shares its memory, and can be removed with or without its
host; both removals keep the memory folder. The complete list of effects is in
[installation impact](../../docs/installation_impact.md).

## Manual installation on an existing profile

The following procedure modifies an **existing** Hermes profile. It is intended
for developers and operators, not a second step after app setup.

## Requirements and deployment scope

- A compatible Hermes version with a working model and channel.
- A durable Daemon Harness checkout and a Python environment with `requirements.txt`.
- An initialized daemon identity, conversation profile and memory.
- Credentials for the daemon's background model calls.
- Explicit profile, memory and state paths.

Use an isolated Hermes profile and synthetic memory for qualification.
Do not use a personal profile as a smoke test. Manual installers manage shared
service names; they are not the app's per-installation service manager.
Compatibility is established by the installed revision and its evaluation,
not by assuming that the newest upstream Hermes is compatible.

Read [configuration](../../docs/configuration.md) for the difference between
this source provider factory and the API adapters in the distributed Companion.

## 1. Record the existing configuration

Select the profile and verify the native host before adding the daemon:

~~~sh
export HERMES_HOME="/absolute/path/to/the-selected-hermes-profile"
hermes gateway status
hermes memory status
~~~

Back up that profile's configuration and record its plugin paths, source
revision and service definitions. Do not publish `.env` or authentication files.
The installer can create `config.yaml.bak`; that is not a substitute for a
complete, current backup.

The plugin runs inside Hermes's Python process. The separate Python environment
used below is for background maintenance; installing requirements only there
does not prove the host can import every plugin dependency.

## 2. Configure identity, memory and maintenance

Use the selected profile's private `.env`. The following is a template; replace
paths and identity with initialized values:

~~~dotenv
DAEMON_CORE_USER_ID=your-daemon-user-id
DAEMON_CORE_MEMORY_ROOT=/absolute/private/technical-memory-root
DAEMON_CORE_SOVEREIGN_VOICE=on
DAEMON_CORE_SOVEREIGN_PLATFORMS=telegram
LLM_PROVIDER=openai
OPENAI_MODEL=your-accessible-model
OPENAI_API_KEY=
~~~

Enter the key privately; do not put it in a command recorded in shell history.
Restrict the file to the account running the service:

~~~sh
chmod 600 "$HERMES_HOME/.env"
~~~

This example configures **maintenance**, not Hermes's response model.
The [provider reference](../../docs/configuration.md#model-providers) explains
role overrides and Codex/local alternatives.
The manual installer inserts `FOLLOWUP_LLM_PROVIDER=codex_cli` when absent.
If using API-only maintenance, configure that role explicitly too.

An initialized conversation profile must already exist for the intended user.
For PARGA, use the technical root linked by the verified vault layout; the
visible Obsidian root is its parent. Do not manufacture a layout by renaming
folders or copying an internal marker.

## 3. Install the plugins and shared nightly job

From the durable checkout, supply the maintenance paths explicitly:

~~~sh
export DREAMING_PYTHON="/absolute/runtime/venv/bin/python"
export DREAMING_ENV_FILE="$HERMES_HOME/.env"
export DREAMING_MEMORY_ROOT="/absolute/private/technical-memory-root"
export DREAMING_STATE_ROOT="/absolute/private/maintenance-state"
bash integrations/hermes_plugin/install.sh
~~~

[install.sh](install.sh) performs these changes:

| Area | Applied change |
| --- | --- |
| Memory provider | Links `plugins/daemon_core` to this integration and selects `memory.provider: daemon_core`. |
| Voice middleware | Links `plugins/daemon_voice` and enables the plugin through Hermes when available. |
| Source path | Sets `DAEMON_CORE_SRC` to this checkout's `src` directory. |
| Native memory | Sets `memory_enabled: false` and `user_profile_enabled: false` to avoid a competing personal-memory authority. |
| Native automatic review | Disables automatic native memory/skill review; explicitly used tools and skills remain available. |
| Telegram delivery | Applies progress/cleanup and steering settings, and the string `reply_to_mode: "off"`. |
| Maintenance | Installs the shared Dreaming/REM schedule and validates runtime pointers. |

The installer does not create a Telegram account, initialize every user field,
install an arbitrary host version or grant access to external services.
Its final message is not proof that all background credentials work.

For app-managed setup, additional private runtime, embedding and rollback
operations are described in [installation impact](../../docs/installation_impact.md).

## 4. Configure durable after-turn work

Without the durable queue, an in-process background task can be interrupted
when the host stops. Configure the shared queue and receipts in the profile:

~~~dotenv
DAEMON_CORE_CONTINUITY_RECEIPTS=shadow
DAEMON_CORE_CONTINUITY_RECEIPT_DIR=/absolute/private-state/continuity/shared
DAEMON_CORE_AFTER_TURN_SPOOL=canary
DAEMON_CORE_AFTER_TURN_SPOOL_DIR=/absolute/private-state/after-turn-spool
DAEMON_CORE_AFTER_TURN_OWNER=external
DAEMON_CORE_SCENE_COALESCING=canary
~~~

Here `canary` is a retained setting value for the qualified durable path.
The [configuration reference](../../docs/configuration.md) explains dependencies.

On macOS, install the independently supervised consumer with matching paths:

~~~sh
DAEMON_CORE_PYTHON="$DREAMING_PYTHON" \
DAEMON_CORE_ENV_FILE="$HERMES_HOME/.env" \
DAEMON_CORE_MEMORY_ROOT="$DREAMING_MEMORY_ROOT" \
DAEMON_CORE_AFTER_TURN_SPOOL_DIR="/absolute/private-state/after-turn-spool" \
DAEMON_CORE_CONTINUITY_RECEIPT_DIR="/absolute/private-state/continuity/shared" \
  bash integrations/after_turn_service/service.sh install
~~~

Hermes and OpenClaw attached to the same daemon must use the same identity,
memory and queue. Separate daemons need separate operational state.
See [worker internals](../after_turn_service/README.md).

## 5. Verify activation

After isolated validation of the revision you install:

~~~sh
hermes memory status
python3 scripts/check_runtime_pointers.py
bash integrations/after_turn_service/service.sh status
bash integrations/dreaming_service/service.sh status
hermes gateway restart
hermes gateway status
~~~

Confirm `daemon_core` is selected and available, the intended voice plugin is
loaded, and the worker/nightly job use the expected source and environment.
Use an isolated conversation to verify response, exact-memory access and
after-turn completion. A provider listing alone does not establish those.

Nightly work normally runs at the configured schedule. Do not trigger a
personal Dreaming/REM cycle merely to check that the schedule exists.

## Optional memory behavior

PARGA has real root folders and hidden technical state. Embeddings are a
rebuildable discovery cache; they do not replace original notes.
After-turn category moves require `DAEMON_CORE_MEMORY_RELOCATION=v1` and a
verified PARGA layout; the default remains off. Dreaming's existing PARGA
archiving and REM's `Archives/Episodes` have their own contracts.

Read the [memory format and lifecycle](../../docs/memory_structure_developer_guide.md)
before changing either. App replacement does not activate these flags.

## Troubleshooting and rollback

Use [operations](../../docs/operations.md) for credential failures, pending
queues, missing context and macOS access permissions.

For a manual rollback, first inspect [uninstall.sh](uninstall.sh) and the saved
configuration. It removes plugin links, restores `config.yaml.bak` when present,
and retains `.env` and shared nightly maintenance. Restoring an old backup can
also undo later host-setting changes: compare it before running the script.

Restart only the affected gateway after restoring a coherent configuration.
Do not remove the shared worker/nightly service if another host uses it.
Memory and remote accounts are not deleted by plugin removal.

Companion-managed installations use the app's receipt-aware removal instead;
supported actions and platform limits are in
[installation impact](../../docs/installation_impact.md#removing-a-daemon-or-host).

[Architecture](../../docs/architecture.md)


## Companion health and setup diagnostics

Companion opens on **My daemons**, with the daemon identity above its hosts.
Resume and diagnostic actions refer to one owned installation. Host gateway
control and removal remain explicit within its details. This UI update does not
modify the Hermes Agent runtime, memory, account or background services.

The distributed desktop app can inspect installed daemon health through bounded
technical metadata. It does not run Hermes commands, make provider/Telegram
calls, edit memory, drain queues or restart services for these checks. Model
response, after-turn completion, host service and worker heartbeat are separate
observations. Channel delivery and semantic search are not inferred from an
alive process. Missing nightly history is not an installation failure.

Setup checkpoints cover the phases before the installation transaction as well
as snapshot/apply/verify/rollback. Reports survive child-process failure and
mark unfinished attempts as interrupted on reopening. Exports use an allowlist
of technical fields, without exception text, command output, credentials,
account identifiers, paths, dialogue or memory content. Existing detailed
private transaction reports remain unchanged. No prompt or plugin contract is
changed by this feature. See the [user guide](../../docs/companion-health.md).

## Host setup progress and failure diagnostics

Host setup displays elapsed time and dependency-process output activity. Initial
dependency installation has a bounded 30-minute limit on both platforms. Timeout
errors retain the last command output for inspection; the exported app report
contains only structured error types and durations, never raw command output.
Cancelling stops only the private installer process tree.
