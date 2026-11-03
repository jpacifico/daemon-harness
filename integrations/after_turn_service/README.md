# Shared after-turn worker

The worker processes memory maintenance after a visible reply. In the manual
macOS deployment, `com.daemonharness.after-turn-worker` is the sole consumer
of the shared durable queue. Hermes and OpenClaw append jobs; stopping either
host does not remove already queued work.

## Processing contract

The worker:

- Processes the `openclaw` and `hermes` partitions in their durable order.
- Holds an exclusive lock to prevent competing consumers.
- Preserves host-specific effects, such as Hermes follow-ups, through an adapter.
- Publishes a private technical heartbeat without conversation text or personal
  identifiers.
- Is checked by API readiness when `DAEMON_CORE_AFTER_TURN_OWNER=external`.

The host/API and worker must agree on `DAEMON_CORE_MEMORY_ROOT`,
`DAEMON_CORE_AFTER_TURN_SPOOL_DIR` and `DAEMON_CORE_CONTINUITY_RECEIPT_DIR`.
`DAEMON_CORE_AFTER_TURN_SERVICE_REVISION` identifies the expected deployed
worker; it need not match either host's own revision.

## Manual macOS installation

Configure explicit durable paths and the private environment file as described
in [configuration](../../docs/configuration.md). Then:

~~~sh
bash integrations/after_turn_service/service.sh install
bash integrations/after_turn_service/service.sh status
~~~

The installer backs up an existing LaunchAgent and restores it if the new
service cannot publish healthy status.
These shared service commands are not the management interface for
Companion's separate private installations. Managed installation uses its own
receipts and platform-specific jobs; see
[installation impact](../../docs/installation_impact.md).

## Projection recovery

A terminal projection superseded by a newer scene is archived under
`<projection_ref>-<sha256-of-exact-file>.json` with its matching receipt.
The active job key stays stable for coalescing. Older archives without the
suffix are not migrated or overwritten.

Under the user lock, the archive is published atomically, continuity is
finalized and its archive receipt is completed before the source job is removed.
An interruption leaves a discoverable source; a retry reuses the exact archive.
An integrity mismatch remains an explicit failure instead of overwriting data.

Coalescing replaceable projections does not combine the facts of distinct
turns or repeat durable memory writes already completed.

## Verify actual progress

A process and a heartbeat are necessary but insufficient. Check API `/ready`,
the `scene_pending`/`scene_failed` queues and progress through real queued jobs.
Do not delete the spool to hide a failed stage.
See [operations and troubleshooting](../../docs/operations.md).
