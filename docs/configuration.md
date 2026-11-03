# Runtime configuration reference

This reference connects the settings to the processes that consume them.
Use it with the [host installation guides](installation_technique.md);
copying an environment file does not initialize a daemon or install its services.

## Version and process scope

There are three separately versioned things: the desktop app, the daemon
runtime inside an installation, and the host. Updating Companion replaces the
app; it does not upgrade an existing daemon or Hermes/OpenClaw.

The distributed app can include runtime changes from its release source that
are not yet on `main`. Read the release manifest and
[Companion source guide](../apps/companion/README.md).
In particular, the API-provider support in the October 18 Companion release
must not be inferred from the older provider factory in this checkout.

Configuration also exists in separate processes: the host, local OpenClaw API,
after-turn worker and nightly job. A shell export affects a process started from
that shell, not an already running background service. Use explicit private
environment-file paths and restart only the affected processes after a validated
change. Never print an entire environment file to diagnose a public issue.

## Locations and identity

Use absolute paths in environment files. Do not depend on shell expansion of
`~` or `$HOME` inside values read by another process.

| Setting | Meaning and requirement |
| --- | --- |
| `DAEMON_CORE_SRC` | Durable source directory ending in `src`, used by the Hermes plugin. Its installer sets it. |
| `DAEMON_CORE_MEMORY_ROOT` | Technical memory root read by the runtime. In a root-folder vault this can be the hidden `.daemon` directory; open the parent vault in Obsidian. Follow the layout manifest rather than guessing from a folder name. |
| `DAEMON_CORE_USER_ID` | Explicit daemon user identity, shared by the intended hosts and maintenance. It is not the daemon display name. |
| `DAEMON_CORE_AGENT_NAME` | Daemon name used only when the memory has no valid `core/daemon_identity.json`, which Companion writes at setup and which takes precedence. Without either, the daemon is called `Daemon`. |
| `DAEMON_CORE_ENV_FILE` | Private configuration file loaded by the service entry point. Configure this on the service, not only in an interactive shell. |
| `DAEMON_CORE_PYTHON` | Interpreter used by manual macOS services. Must remain executable at a durable path. |
| `DAEMON_CORE_RUNTIME_REVISION` | Installed-code attestation set by deployment. Not a feature switch. |
| `DAEMON_CORE_AFTER_TURN_SERVICE_REVISION` | Expected independently deployed worker revision. It need not equal the host revision. |
| `DAEMON_CORE_LOG_DIR` | Override for service logs where supported by the service installer. |
| `HERMES_HOME` | Hermes profile location. Explicitly select it before running manual plugin installation. |

The source defaults are not a safe way to select a personal installation.
Companion has its own installation receipts and private paths.
See [installation impact](installation_impact.md) for app-managed locations.

## Model providers

The host configures the model that writes responses. Daemon provider settings
configure internal judgments and maintenance. In the current guided API setup,
the app deliberately configures the same chosen provider/model for both.

The provider factory on this branch accepts `openai`, `local`, `codex`/`codex_cli`
and `echo`. The distributed Companion also offers OpenAI and Anthropic API
credentials (see [Model providers](model_providers.md)); its own release source and credential loader define
that support. Do not add an unsupported provider name to an older runtime.

| Settings | Behavior in the source provider factory |
| --- | --- |
| `LLM_PROVIDER` | Global provider, used when a role does not override it. |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | OpenAI connection and model. Keep the key private. |
| `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_API_KEY`, `LOCAL_LLM_MODEL` | OpenAI-compatible local endpoint and model. “Local” describes the configured endpoint, not a guarantee that it runs on this computer. |
| `CODEX_CLI_BIN`, `CODEX_CLI_MODEL`, `CODEX_CLI_PROFILE` | Codex executable/model/profile for internal calls using the configured Codex login. |
| `CODEX_CLI_TIMEOUT_SECONDS` | Internal Codex-call deadline; the example uses 300 seconds. |
| `ROUTING_LLM_PROVIDER`, `CONVERSATION_LLM_PROVIDER`, `MEMORY_LLM_PROVIDER` | Per-role override; an empty value inherits the global provider. |
| `ROUTING_OPENAI_MODEL`, `CONVERSATION_OPENAI_MODEL`, `MEMORY_OPENAI_MODEL` | Role-specific model for the OpenAI adapter. Corresponding `*_LOCAL_LLM_MODEL` and `*_CODEX_MODEL` settings select models for those adapters. |
| `ROUTING_REASONING_EFFORT`, `CONVERSATION_REASONING_EFFORT`, `MEMORY_REASONING_EFFORT` | Optional `low`, `medium` or `high` reasoning override. The factory defaults routing/conversation judgments to medium and memory to high. |
| `FOLLOWUP_LLM_PROVIDER`, `FOLLOWUP_CODEX_MODEL` | Separate follow-up delivery judgment. Manual Hermes installation inserts `codex_cli` if no provider is already configured. |
| `REM_LLM_PROVIDER` | Provider selection for episodic maintenance; inspect the installed REM implementation for its model fallback. |

`CONVERSATION_*` is a legacy internal role name. It does **not** override the
host's conversation model. `echo` is a test stub, not a usable assistant model.
The [environment example](../.env.example) contains illustrative values; select
models your account actually exposes rather than treating examples as a catalogue.

App API keys use the OS credential store. Manual `.env` setup stores the values
in a private file instead. Do not mix these mechanisms by copying credentials
out of a managed installation.

## Durable after-turn processing

An **after-turn job** is memory work performed after the visible answer.
A **spool** is the directory holding that durable queue.
A **receipt** records a technical stage and its source references.

| Setting | Values / dependency |
| --- | --- |
| `DAEMON_CORE_CONTINUITY_RECEIPTS` | `shadow` enables technical receipts; `off` disables them. |
| `DAEMON_CORE_CONTINUITY_RECEIPT_DIR` | Explicit receipt directory shared by attached hosts. |
| `DAEMON_CORE_AFTER_TURN_SPOOL` | `canary` enables the durable path; the raw example defaults to `off`. Here “canary” is a retained configuration token, not a request to deploy an experimental branch. |
| `DAEMON_CORE_AFTER_TURN_SPOOL_DIR` | Shared queue directory for this daemon. |
| `DAEMON_CORE_AFTER_TURN_OWNER` | `external` makes the separately installed worker the consumer. Hosts enqueue work. |
| `DAEMON_CORE_AFTER_TURN_USER_IDS` | Identity restriction for the feature where configured. |
| `DAEMON_CORE_SCENE_COALESCING` | `canary` enables coalescing of replaceable scene work; requires receipts and the durable queue. |

Example for one daemon, with placeholders replaced by real absolute paths:

~~~dotenv
DAEMON_CORE_CONTINUITY_RECEIPTS=shadow
DAEMON_CORE_CONTINUITY_RECEIPT_DIR=/absolute/private-state/continuity/shared
DAEMON_CORE_AFTER_TURN_SPOOL=canary
DAEMON_CORE_AFTER_TURN_SPOOL_DIR=/absolute/private-state/after-turn-spool
DAEMON_CORE_AFTER_TURN_OWNER=external
DAEMON_CORE_SCENE_COALESCING=canary
~~~

The host and worker must agree on identity, memory, spool and receipt locations.
Different daemon installations must not accidentally share these operational
directories. See [worker installation](../integrations/after_turn_service/README.md).

## Memory and semantic search

Root-folder PARGA requires a verified layout and initialized memory structure.
Setting a flag alone does not migrate a legacy vault.

`DAEMON_CORE_MEMORY_RELOCATION=v1` enables qualified after-turn note moves.
Its default is off; enabling it does not reclassify old memory automatically.
A move preserves identity, provenance, history and links. Pending relocation
journals must be recovered with compatible code before a downgrade.

Optional embedding settings:

| Setting | Meaning |
| --- | --- |
| `DAEMON_CORE_ATLAS_EMBEDDING_TOP12` | Activation mode for a semantic shortlist of exact notes. Keep disabled unless explicitly configured. |
| `DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS` | Explicit users allowed to use it. |
| `DAEMON_CORE_ATLAS_EMBEDDING_CACHE` | Rebuildable vector-cache location. |
| `DAEMON_CORE_ATLAS_EMBEDDING_MODEL` | Example model: `qwen3-embedding:0.6b`. |
| `DAEMON_CORE_ATLAS_EMBEDDING_URL` | Example local endpoint: `http://127.0.0.1:11434`. |

The vector model proposes candidates; exact Markdown still supplies facts.
Cache refresh takes place after queued work or nightly maintenance. A missing
embedding service must not be diagnosed as lost Markdown. Managed installations
provide their own private embedding runtime; do not assume a system-wide
Ollama installation is required.

The detailed [memory reference](memory_structure_developer_guide.md) specifies
format, compatibility markers, paths and activation conditions.

## Nightly maintenance

| Setting | Meaning |
| --- | --- |
| `DREAMING_ENABLED` | The daily runner can pause Dreaming with `0`; this is not a prohibition on explicitly running a module manually. |
| `DREAMING_OBSERVE_ONLY` | Review without applying Dreaming's automatic corrections. It does not mean the whole nightly pipeline is read-only. |
| `DREAMING_DRY_RUN` | Broader simulation control. Do not remove it accidentally when changing observation mode. |
| `DREAMING_SINCE_HOURS` | Review window; the example uses 24. |
| `DREAMING_CODEX_TIMEOUT_SECONDS` | Nightly Codex deadline; example 600 seconds. |
| `DREAMING_CONTINUITY_RECEIPT_DIRS` | Additional receipt roots, separated by `:` on Unix or `;` on Windows. |
| `DREAMING_PYTHON`, `DREAMING_ENV_FILE`, `DREAMING_MEMORY_ROOT`, `DREAMING_STATE_ROOT` | Explicit settings for the manual nightly-service installer. |
| `DREAMING_CRON_HOUR` | Manual service schedule hour, default 4. This is the installer setting; `DREAMING_DAILY_TIME` in the example is not its replacement. |

Dreaming and REM have distinct write ownership. Do not uninstall their shared
service merely to pause one faculty. Reinstalling a schedule is separate from
running maintenance immediately. See [operations](operations.md).

## Optional and diagnostic settings

The [environment example](../.env.example) also lists Scene Model, original
journal recall, follow-ups, episodic scenes, relation maintenance and
observational experiments. Their exact modes and allowlists are feature-specific;
`on`, `v1`, `canary` and `shadow` are not interchangeable.

Older experimental settings are not the recommended canonical path.
In particular, an experimental synchronous coverage judgment is not permission
to add a model call before every response. Follow the current architecture
contract and the corresponding evaluation before enabling a feature.

`DAEMON_CORE_VOICE_CAPTURE_FILE` captures private prompt material for a bounded
diagnostic. Leave it empty normally; never attach its raw output to a public
issue. Read [security reporting](../SECURITY.md) first.

Sources of truth: [provider factory](../src/daemon_core/providers/factory.py),
[environment example](../.env.example), host installers, and the installed
release revision. Missing settings and missing measurements do not mean “enabled”
or “zero cost.”
