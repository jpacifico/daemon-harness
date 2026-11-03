# Operations and troubleshooting

Use this runbook after [installation](installation_technique.md).
It distinguishes a running host, a working conversation and working memory
maintenance. None of those checks alone proves the other two.

## Identify the installation first

Record the app version, host version, runtime revision, selected user identity,
memory root, environment-file location and relevant service names.
Companion shows recognized installations and their memory locations.
For manual profiles, inspect only the selected host's configuration.

Do not assume that `~/.hermes` or `~/.openclaw` identifies an app-managed
installation. Do not stop every process named Python or Hermes.
Private app services have installation-specific names; the manual macOS scripts
below manage the shared names listed here.

| Manual macOS component | Service label | Default log under `~/Library/Logs/daemon-harness` |
| --- | --- | --- |
| OpenClaw local API | `com.daemonharness.openclaw-api` | `openclaw_api.log` |
| Shared worker | `com.daemonharness.after-turn-worker` | `after_turn_worker.log` |
| Nightly maintenance | `com.daemonharness.dreaming` | `dreaming_cron.log` |

## Read-only checks

Run from the source checkout belonging to the manual deployment:

~~~sh
python3 scripts/check_runtime_pointers.py
hermes gateway status
hermes memory status
bash integrations/after_turn_service/service.sh status
bash integrations/dreaming_service/service.sh status
~~~

For OpenClaw's local bridge:

~~~sh
bash integrations/openclaw_plugin/service.sh status
curl --silent --show-error http://127.0.0.1:8000/health
curl --silent --show-error --write-out '\nHTTP %{http_code}\n' http://127.0.0.1:8000/ready
~~~

Interpret the result:

- `/health` returning `{"status":"ok"}` means the HTTP process responds.
- `/ready` returns HTTP 200 with `status: ready` when configured runtime
  attestations pass, or HTTP 503 with `status: degraded` and failure reasons.
- Worker status checks the deployed consumer, not just a process name.
- A passing readiness check is technical evidence, not proof of recall quality
  or provider access on the next request.

These checks do not authorize replaying conversations or writing personal
memory. Use a disposable profile for an end-to-end diagnostic.

## Symptom guide

| Symptom | First check | Interpretation / next action |
| --- | --- | --- |
| App updated, daemon behavior unchanged | App versus installed runtime versions | App replacement does not upgrade an existing daemon. |
| Bot does not reply | Selected host gateway, bot identity, channel authentication and model connection | Message delivery and provider access belong to the host; identify which step fails before touching memory. |
| Telegram Web Start does nothing | Open Telegram Web K and find the exact bot username | Distinguish a browser UI issue from a message that reached the gateway. |
| Replies work, new memories do not appear | Worker status, failed queue partitions, maintenance-provider authentication | A working response model does not prove the maintenance process has working credentials. |
| `/health` passes, `/ready` fails | `failures` in readiness and installed paths/revisions | Fix the reported mismatch; do not disable readiness to make the dashboard green. |
| Expected memory is not recalled | Exact note/journal exists, selected identity and memory root, tool results | First distinguish missing source, wrong installation and retrieval failure. Do not invent a replacement memory. |
| Obsidian has no Projects/Areas folders | Vault path displayed by Companion | Open the parent vault, not its hidden technical `.daemon` directory or another folder with the same name. |
| Semantic search unavailable | Configured embedding URL and model, cache status | The Markdown may still be intact. Repair the embedding service separately. |
| Note cannot move | Stable identity, collision, concurrent user edit or pending relocation journal | Preserve the files; resolve the conflict through the normal recovery mechanism. |
| Installation says recovery is required | Installation diagnostic and receipt | Keep the profile and diagnostic. Do not repeatedly reinstall into partially rolled-back state. |
| macOS service cannot read Documents | OS permission for the actual background interpreter | A Terminal permission is not necessarily the background process's permission. Grant only intended access. |

Provider errors may contain personal paths and request material. Redact those
before filing an issue. Never publish keys, bot tokens, authentication files,
raw prompt captures or your memory folder.

## Queue recovery

The durable queue records source references and stage completion. It has
pending, processing and failed states; expired work can be reclaimed after a
consumer dies. Failed work is inspectable rather than silently discarded.

A heartbeat proves the worker is alive. Verify that pending work progresses and
that failed or scene-specific queues are not accumulating.
Do not delete the spool to “reset” maintenance: that can discard unprocessed
turns. Do not replay the entire conversation merely because a replaceable scene
projection failed. Memory writes and projection refresh have separate completion
records.

The [worker reference](../integrations/after_turn_service/README.md) describes
exclusive consumption, projection archives and restart behavior.
The deployed code defines retry limits; a source branch that improves recovery
does not upgrade an already installed worker by itself.

## Dreaming and REM operations

Review the nightly job's actual environment-file path. The file used by an
interactive conversation may be different.

- To pause automatic Dreaming, use the runner's `DREAMING_ENABLED=0` setting.
- To inspect proposals without Dreaming's automatic corrections, use the
  documented observation mode. Other nightly work is separate.
- A global dry run is broader than observation of Dreaming. Changing it can
  enable REM or agenda writes, so do not toggle it as a troubleshooting shortcut.
- A new schedule should not cause an extra immediate maintenance cycle unless
  that is explicitly intended.

Examine the last completed stages and errors, not only the scheduler's exit
status. A provider failure must not advance completion as though curation
succeeded. Configuration details are in [configuration](configuration.md).

## Backups and restore

Preserve the whole vault, including hidden `.daemon`, plus configured external
state directories. Notes alone omit original journal evidence; indexes alone
omit authoritative notes. Include host configuration and installation receipts
when preparing an operational recovery.

Take a consistent backup while writers for the affected daemon are stopped,
or use a snapshot mechanism that guarantees consistency. Keep credentials in a
separate protected backup appropriate to their storage. Never commit backups.

A restore should restore known bytes and state, not reconstruct missing personal
facts by hand. Rebuild derived indexes only after restoring authoritative data.
Check both hosts if they share the daemon, and do not run two independent
consumers against the same queue.

## Updates and rollback

Before deployment, record current code paths, versions, service configuration
and the expected rollback. Validate the candidate on a disposable installation.
After deployment, verify loaded paths and restart only affected services.

For PARGA moves, recover pending relocation journals with code that understands
them before downgrading. Turning a feature off does not undo notes already
moved. A blanket old-vault restore can erase newer user edits.

Manual uninstallers and Companion removal are different operations.
See [exact installation impact](installation_impact.md) and each host guide.
Neither should be treated as permission to delete the memory folder.

Before removing a source worktree:

~~~sh
python3 scripts/check_runtime_pointers.py --before-remove /absolute/path/to/worktree
~~~

A live dependency blocks removal. Keep personal services on a durable checkout
or private release, never a temporary test directory.

## Report a useful issue

Include platform, versions, the failed stage, what you expected, what happened
and a minimal redacted diagnostic. Say whether the failure is on a new
installation or an existing one. Missing measurements are unknown, not zero.

A short local smoke test cannot establish conversation quality, long-term
retention or Windows behavior on a Mac. The
[alpha checklist](alpha-testing.md) and evaluation requirements
state the remaining validation.
