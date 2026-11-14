# OpenClaw integration

OpenClaw owns the response model, tools, sessions and messaging.
Daemon Harness integrates through a **context engine**, OpenClaw's extension point for
preparing model context, and a local Python HTTP bridge.

## Installed by Companion

Companion creates a private OpenClaw `2026.10.7` installation with its own Node
`24.22.1`, either as a first daemon or through **Add a host** on a Hermes Agent
daemon created by Companion. Hosts installed earlier with `2026.9.1-beta.1` keep
their version and stay recognised for services, attachment and removal.

- **Model:** ChatGPT sign-in, listed first as recommended, or an OpenAI or
  Anthropic API key. The pinned OpenClaw provider owns
  the device-code login, credential refresh and account-model discovery; only
  Astra and GPT-6.1-Sol are offered when the account returns them.
  Credentials stay in the private host profile; no Hermes sign-in is copied.
  A first daemon also connects a separate private Codex session (Codex CLI
  `0.159.2`) for memory maintenance; Add a host reuses the existing one.
- **Daemon:** the memory, after-turn worker, nightly maintenance, local search
  and loopback API, with the `parga-v1` profile and the reference voice. An added
  host shares the existing daemon's memory and worker instead.
- **Plugin:** the daemon memory tools declare `catalogMode: "direct-only"`, so
  OpenClaw's default Tool Search does not hide them; host tools keep OpenClaw's
  default. OpenClaw answers Telegram **Start** itself, which opens the welcome.
- **Settings:** the reference conversation settings below.

A daemon created directly on OpenClaw is offered **Update** in its page when
the app carries a newer copy of the code that installs and runs it. The plugin,
workspace instructions, memory, model connection and bot stay as they are; the
profile then loads the plugin from the new copy only, and the previous copy is
kept to restore it if a step fails. An OpenClaw added to a Hermes Agent daemon
is not updated this way.
[Updating a daemon](../../docs/installation_impact.md#updating-a-daemon).

An unfinished first OpenClaw installation can be deleted from the app; an
unfinished Add a host is resumed. A completed daemon created directly on
OpenClaw is uninstalled from the app with its private OpenClaw, keeping its
memory folder; an OpenClaw added to a Hermes
Agent daemon is stopped and uninstalled, or disconnected, with that daemon.
[Installation impact](../../docs/installation_impact.md).

### Reference conversation settings

A new private OpenClaw host created by Companion starts with the reference
daemon's native settings:

```json
{
  "agents": {"defaults": {"heartbeat": {"every": "0m"}}},
  "messages": {
    "visibleReplies": "automatic",
    "ackReactionScope": "off",
    "statusReactions": {"enabled": false}
  },
  "session": {"dmScope": "per-channel-peer"},
  "tools": {"profile": "coding"},
  "channels": {
    "telegram": {
      "streaming": {
        "mode": "progress",
        "preview": {"toolProgress": true, "commandText": "status"},
        "progress": {"commentary": true, "label": false}
      }
    }
  }
}
```

Without them, OpenClaw's defaults (measured on `2026.9.1-beta.1`) run a
heartbeat turn every 30 minutes (API key) or hour (OAuth), share one main
session across direct messages, use the `full` tool profile and hide the
progress commentary, so the plugin's progress sentence is not shown. In `progress` mode the final reply arrives in
one message.

With ChatGPT sign-in, the host also enables the subscription's hosted web
search, `"tools": {"web": {"search": {"enabled": true, "openaiCodex":
{"enabled": true, "mode": "live"}}}}`. OpenClaw selects no managed search
provider without a key, and hosted search is opt-in, so these hosts otherwise
had no web search. The default `cached` mode does not request live access
outside the Codex app-server. Hosted search runs inside the model call: Telegram
shows no search tool line. API-key hosts still have no search provider.

The daemon projection keeps these values and only adds its own tools. Attaching
an existing host leaves them to its owner. Two reference values are not copied:
`channels.telegram.actions.deleteMessage`, which grants the model a delete
action the plugin does not use, and the reference daemon's managed search
provider, which uses a personal key.

## Manual installation on an existing host

The procedure below is manual and changes the selected host configuration.
Companion does not attach a daemon to an arbitrary existing OpenClaw host.

## Requirements and scope

Use a compatible OpenClaw release, its required Node runtime, an initialized
daemon identity/memory and a Python environment with the repository requirements.
Prepare maintenance credentials independently of the host's response-model
connection. Verify compatibility against this plugin revision before upgrading
OpenClaw.

Use separate profiles and synthetic memory for tests.
The macOS service scripts use shared launchd labels; they are not a manager
for arbitrary multiple app installations. A temporary checkout is unsuitable
for a persistent personal service.

## 1. Prepare the daemon runtime

From a durable checkout:

~~~sh
python3.12 -m venv /absolute/runtime/venv
/absolute/runtime/venv/bin/pip install -r requirements.txt
~~~

Prepare a private environment file with explicit initialized values:

~~~dotenv
DAEMON_CORE_USER_ID=your-daemon-user-id
DAEMON_CORE_MEMORY_ROOT=/absolute/private/technical-memory-root
LLM_PROVIDER=openai
OPENAI_MODEL=your-accessible-model
OPENAI_API_KEY=
~~~

Enter the key privately and protect the file:

~~~sh
chmod 600 /absolute/private/daemon.env
~~~

The conversation profile for this identity must already exist.
Use the verified layout for PARGA, not a guessed path.
[Configuration](../../docs/configuration.md) explains provider alternatives,
per-role overrides and feature flags. Changing daemon provider settings does
not change OpenClaw's response model.

## 2. Install shared maintenance and the API on macOS

Configure the durable queue/receipt block from the
[configuration reference](../../docs/configuration.md#durable-after-turn-processing)
in the private environment. Install the worker with explicit locations first:

~~~sh
DAEMON_CORE_PYTHON="/absolute/runtime/venv/bin/python" \
DAEMON_CORE_ENV_FILE="/absolute/private/daemon.env" \
DAEMON_CORE_MEMORY_ROOT="/absolute/private/technical-memory-root" \
DAEMON_CORE_AFTER_TURN_SPOOL_DIR="/absolute/private-state/after-turn-spool" \
DAEMON_CORE_CONTINUITY_RECEIPT_DIR="/absolute/private-state/continuity/shared" \
  bash integrations/after_turn_service/service.sh install
~~~

Then install the local bridge:

~~~sh
DAEMON_CORE_PYTHON="/absolute/runtime/venv/bin/python" \
DAEMON_CORE_ENV_FILE="/absolute/private/daemon.env" \
DAEMON_CORE_MEMORY_ROOT="/absolute/private/technical-memory-root" \
  bash integrations/openclaw_plugin/service.sh install
~~~

[service.sh](service.sh) manages `com.daemonharness.openclaw-api` and installs
the shared nightly schedule. It binds the API to `127.0.0.1:8000` and, by
default, logs under `~/Library/Logs/daemon-harness`.
Explicit existing service paths are retained unless overridden.

The worker, API and nightly process must use the same intended daemon state.
For shared Hermes/OpenClaw memory, configure one consumer, not two competing
workers. See [the worker contract](../after_turn_service/README.md).

## 3. Register and configure the plugin

Back up the selected OpenClaw configuration and record its context-engine slot,
workspace, tool permissions and bootstrap settings.

~~~sh
openclaw plugins install -l ./integrations/openclaw_plugin
~~~

Plugin registration may change the exclusive context-engine slot.
Keep the plugin disabled until the bridge checks pass.
Merge the following into the selected host configuration; do not replace the
whole file or erase other plugins:

~~~json
{
  "plugins": {
    "slots": { "contextEngine": "daemon-harness" },
    "entries": {
      "daemon-harness": {
        "enabled": false,
        "hooks": { "allowConversationAccess": true },
        "config": {
          "daemonUrl": "http://127.0.0.1:8000",
          "userId": "your-daemon-user-id",
          "runtimeHealthPath": "/absolute/private-state/openclaw-runtime-health.json"
        }
      }
    }
  }
}
~~~

[openclaw.plugin.json](openclaw.plugin.json) defines the plugin config fields.
`DAEMON_HARNESS_URL` and `DAEMON_CORE_USER_ID` are environment fallbacks.
Remove obsolete `sovereignVoice`/`sovereignLibrary` keys from older configs;
the current schema does not accept them.

The API's runtime-health setting must point to the same private attestation
file when that check is configured. Keep the host's agent database path
consistent with the selected profile.

## 4. Prepare the managed workspace and permissions

~~~sh
bash integrations/openclaw_plugin/workspace.sh install
bash integrations/openclaw_plugin/workspace.sh status
~~~

The default workspace is `~/.openclaw/workspace-daemon-harness`.
The previous workspace stays on disk. Record its configured path for rollback.

[workspace.sh](workspace.sh) applies these changes:

| Area | Effect |
| --- | --- |
| Workspace instructions | Installs a minimal canonical `AGENTS.md` instead of competing persona/native-memory instructions. |
| Bootstrap | Manages the workspace and enables `agents.defaults.skipBootstrap`; older state is repaired through the host's migration mechanism. |
| Native memory | Disables `memory-core` and native session-memory behavior; denies `memory_search` and `memory_get`, which address the host's separate corpus. |
| Memory tools | Adds `daemon_read_souvenirs` and `daemon_recall_l0` to `tools.alsoAllow`. |
| Browser | Enables the native browser plugin/tool; preserves an explicit headless setting and extends an existing plugin allowlist without replacing it. |
| Skills | Disables autonomous skill creation; explicit skills and external tool capabilities remain available. |
| Conversation hook | Allows the context-engine hook to access the conversation. |

The integration does not create its own web, mail or calendar service.
OpenClaw retains tool choice, ordering and permissions.
The host adapter removes redundant procedural sections from its projected
prompt; it does not modify the common Python composition or Hermes.

For the qualified native Codex harness, keep the host's default searchable tool
loading unless a separately validated deployment requires otherwise.
Do not migrate a shared Codex authentication directory during host onboarding.

## 5. Check readiness, then activate

~~~sh
curl --silent --show-error http://127.0.0.1:8000/health
curl --silent --show-error --write-out '\nHTTP %{http_code}\n' http://127.0.0.1:8000/ready
bash integrations/after_turn_service/service.sh status
bash integrations/dreaming_service/service.sh status
python3 scripts/check_runtime_pointers.py
~~~

A 200 health response proves liveness only. Readiness must be `ready`;
diagnose its failure reasons before activating.

After isolated validation, enable the plugin and restart the affected gateway:

~~~sh
openclaw config set plugins.entries.daemon-harness.enabled true --strict-json
openclaw gateway restart
~~~

Verify that the active context engine is `daemon-harness`, the bridge receives
assembly and ingestion, and after-turn work completes for the correct identity.
Use [bridge API](../../docs/bridge-api.md) to interpret responses.
Do not send synthetic messages into a personal service.

## Rollback

Disable the daemon plugin; restore the previous context-engine slot, workspace,
bootstrap, native-memory and tool/skill settings from the recorded configuration.
Do not assume every installation's previous slot or workspace was the default.

Restart only that gateway and check native operation.
The API can be removed separately:

~~~sh
bash integrations/openclaw_plugin/service.sh uninstall
~~~

This removes the API service, not the shared Dreaming/REM job.
Keep shared maintenance while Hermes or another host still uses it.
Retain memory, journal and queue state; uninstalling a host plugin is not
deleting the daemon's history.

See [operations](../../docs/operations.md) for backup and partial-failure recovery,
and [memory lifecycle](../../docs/memory_structure_developer_guide.md) before
downgrading a runtime that has pending note moves.

[Architecture](../../docs/architecture.md)

## Host setup progress and failure diagnostics

Host setup displays elapsed time and dependency-process output activity. Initial
dependency installation has a bounded 30-minute limit on both platforms. Timeout
errors retain the last command output for inspection; the exported app report
contains only structured error types and durations, never raw command output.
Cancelling stops only the private installer process tree.
