# Understand a health problem

Open **My daemons > Health**. Each check has **View problem** or
**View details and options**. It explains the impact, recorded technical evidence,
last recorded success when available and the next useful action.

- **Problem detected** is an observed failure, not an invitation to reinstall.
- **Action required** is used when the screen has a supported direct action,
  currently starting an authorized stopped Gateway with confirmation.
- **Recovery pending** requires matching queued work and a current healthy worker.
  The earliest available time is not a guarantee of start or completion.
- **Not yet verified** is missing evidence, not failure. Channel delivery and
  memory search are not actively tested by opening this screen.

**Export this diagnostic** saves only that check. The footer can export the full
snapshot. **Get help on GitHub** opens the project's issues; attach a report
yourself if needed. Nothing is uploaded automatically. There is no universal
Repair button, manual queue retry, provider reconnection or maintenance launch.
Gateway restart does not repair every memory failure.

The collector matches conversation receipts to the account, platform, memory and
source. It extracts fixed technical stage/reason/error enums and timestamps, never
exception text, account identifiers, paths, dialogue or notes. A cycle diagnosis
uses failures from that same turn. Job metadata is read from at most 32 bounded
files for the selected host/account; only schema-checked structural fields are
retained. Recovery additionally requires the matching spool namespace and current
worker heartbeat. Queue payloads and source references never reach the renderer
or export. Unknown errors and missing evidence are stated explicitly. Dreaming/REM
registers do not provide sanitized structured causes, so raw detail text is not
exported and their future schedule is not inferred.

Receipt scans remain bounded to 32 recent candidate files, up to 2,000 directory
entries, and a 256 KiB tail per file. A subsequent successful cycle supersedes an
older failure. A read-only snapshot is not an atomic view of all active services.

## Open memory in Obsidian

In **My daemons > Manage > Memory**, choose **Add and open memory in Obsidian**.
For the first addition only, quit Obsidian completely (⌘Q on macOS), then click
the button. Companion saves a backup of its existing settings, registers the
selected memory folder and requests opening that vault directly. Do not reopen
Obsidian manually while the button shows preparation in progress.

After registration the button becomes **Open memory in Obsidian**. This also
works while Obsidian is open. The same Markdown files are used: no import,
relocation or separate copy. Obsidian creates its own `.obsidian` settings folder.

If Obsidian is not installed, use **Download Obsidian**, launch it once and
refresh. If automatic addition is refused, open Obsidian yourself, use **Open
folder as vault** and select the exact memory path displayed in Companion. Then
refresh. A failed opening does not remove an already completed registration.

Backups are named `obsidian.daemons-backup-<unique-id>.json` beside Obsidian's
`obsidian.json`: `~/Library/Application Support/obsidian` on macOS, and
`%APPDATA%\obsidian` on Windows. They contain the exact previous configuration.
No backup is needed if there was no previous configuration. They are private
local files, not uploaded or included in health reports. Restoring a backup is a
manual recovery action with Obsidian closed; restoring an old one can undo vault
registrations made since, so it is never automatic.

The first-registration mechanism uses Obsidian's internal registry format.
Companion refuses malformed, oversized or symlinked settings, process-check
failures and detected concurrent changes. A small race remains if another
process starts Obsidian between the final check and the atomic replacement;
this is why the first-use instructions ask to leave it closed. Custom/portable
configuration paths are unsupported. No personal daemon service is restarted.
