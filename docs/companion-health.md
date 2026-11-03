# Check your daemon's health

In Companion, open **My daemons**, then **Health** on the daemon you
want to inspect. For a daemon shared by several hosts, choose the host to inspect. This also works after setup. **Refresh** reads a new snapshot;
the app does not continuously probe your model or send messages.

Each check shows its evidence and date. **Verified** describes that observation,
not a guarantee that the next request will succeed. **Check needed** means the
result is old or needs inspection. **Problem detected** identifies an observed failure. **Action required** is reserved
for a supported direct intervention; **Recovery pending** describes matching queued
work with a current worker. **Not yet verified** means the evidence is absent or insufficient.
A new daemon normally has no Dreaming or REM history yet.

| Check | What it establishes |
| --- | --- |
| Host Gateway | Whether a recognized local host service is running. |
| Daemon integration | Whether a matching conversation receipt records use of this memory and runtime. |
| Memory folder | Whether the configured folder is accessible. No notes are opened or changed. |
| Model response | The latest recorded generation result, without making a new provider request. |
| Conversation channel | Whether an account was identified. Delivery remains unverified without a channel test. |
| Memory after conversations | The latest recorded after-turn cycle result. |
| Memory worker | Whether a matching worker heartbeat is current. |
| Pending memory work | Pending, running and failed jobs for the selected account and host. Only bounded structural job metadata is retained. |
| Dreaming and REM | The latest recorded maintenance verdicts for the account. |
| Memory search | Explicitly unverified: opening this screen does not execute a search query. |

The worker heartbeat must be younger than 15 seconds. Model results older than
24 hours, maintenance older than 48 hours, or pending work older than five
minutes suggest a check. An old maintenance result can simply mean the computer
was asleep or there were no recent conversations. Unsupported or older host
profiles may provide only partial evidence. Shared maintenance concerns the
shared memory, even when accessed through a particular host.

## Diagnose an installation failure

**Diagnostic report** is available during every setup step. After reopening
Companion, choose **View diagnostic** on the relevant unfinished installation.
A completed daemon keeps it under **Installation report** when a report exists. It records app preparation, Python, host acquisition,
identity, model access, Telegram, daemon installation and first-conversation
checks. Transaction checkpoints distinguish backup, application, verification
and rollback. A failure before Python starts is covered too.

If the app is killed, it cannot record its final exception. On reopening, its
last saved unfinished attempt is shown as **Interrupted before completion**.
A disk-permission problem is shown explicitly; export the in-memory report
before closing in that case.

Use **Export diagnostic** to save a JSON report for an issue. Nothing is uploaded
automatically. The report excludes credentials, account identifiers, private
paths, answers, URLs, raw command output, conversations and memory content.
It contains only technical states, dates, source revision, platform and bounded
error categories/codes. The existing, more detailed private transaction report
remains beside a failed installation when available.

The latest 20 attempts per installation are retained locally, with up to 256
checkpoints each. Replace `instance-<id>` with that installation's directory
(or `private` for the legacy root):

- macOS: `~/Library/Application Support/DaemonHarness/Companion/instance-<id>/diagnostics/`
- Windows: `%LOCALAPPDATA%\DaemonHarness\Companion\instance-<id>\diagnostics\`

The screen shows the latest attempt for the selected installation. Older reports
remain in its directory until retention removes them. Reports from older app
versions remain in `Companion/bootstrap/diagnostics/`; those files have no reliable
installation binding and are not attributed to a daemon in the new interface. These reports are separate from personal memory.

## Scope and limits

Health inspection and report export do not restart services, repair queues,
change model access, execute Dreaming/REM, edit notes or call an external model.
A generated response does not prove Telegram delivery. Local process and folder
checks do not prove semantic search. These distinctions avoid a misleading
all-green status.

Receipt inspection is bounded: up to 2,000 directory entries, the latest 32
candidate files, and a 256 KiB tail per file. Missing or incomplete evidence is
reported as unverified. The maintenance register is also read as a bounded tail;
only verdicts and dates from that register appear in the export.

Updating the app exposes these screens for existing compatible installations.
The expanded setup diagnostics apply to attempts made with the updated app;
they do not reconstruct earlier steps retroactively. App replacement does not
upgrade or restart an existing daemon runtime.

For per-check causes, recovery and exports, read [Understand a health problem](companion-health-actions.md).
