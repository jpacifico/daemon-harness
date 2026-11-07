# Your memory

Your daemon keeps memories as local Markdown files: plain-text documents that
you can read with a text editor. Obsidian provides an optional interface for
browsing and editing those same files. It is not a separate copy or a required
service.

## Find and open it

In Companion, select your installation, then **Manage daemon → Memory**.
Use **Open folder** or **Copy path**. The displayed path is the reference for
that installation; do not assume it is the memory folder inside a source checkout.

In Obsidian, open the vault switcher, choose **Open folder as vault**, and select
that exact folder. A **vault** is simply Obsidian's name for a folder of notes.

Avoid moving an active folder manually: the host and background services
still refer to its configured location. To move a memory to another daemon,
use a backup.

## Back up or move it

In a daemon's settings, **Back up memory** writes one encrypted file, or sends
it to your Google Drive once Drive is connected in Settings. Choose a password
you will keep: it cannot be recovered. A daily Drive backup can also be turned
on; it needs the app running, so it turns on background mode.

To move the memory to a new daemon, on this computer or another one, choose
**Restore from a backup** in My daemons, enter the password and follow the usual
setup. Choose **Move** when the original daemon will be stopped or removed: its
upcoming reminders come along. Choose **Copy** to keep both. The restored daemon
keeps its name, voice and memories.
[What backups contain and store](installation_impact.md#background-mode-backups-and-google-drive).

## The five folders

We call this organization **PARGA**, for the five folder names below.

| Folder | What belongs here | Fictional example |
| --- | --- | --- |
| Projects | Work with a concrete outcome and an end | Planning a birthday |
| Areas | Ongoing responsibilities, people and situations | Health, family, food preferences |
| Resources | Material worth keeping for future use | A podcast to listen to, a reference guide |
| Garden | Your ideas, principles and lessons, which grow as you come back to them | "Measure twice before ordering", learned during a renovation |
| Archives | Inactive projects and past episodes | A completed trip or a retrospective account |

A note can connect several subjects through links without being copied into
every folder. A completed project can remain useful later; archived does not
mean deleted or unsearchable.

The folder structure is inspired by PARA, with influences from IPCRA and linked
note practices such as Zettelkasten. Garden keeps your ideas apart from
the facts of your life and from reference material, so they can grow and serve
elsewhere. [PARGA: how memory is organized](parga.md) explains each
folder and these inspirations. The [developer guide](memory_structure_developer_guide.md)
explains the design and its implementation limits.

## What the daemon maintains

After a conversation, the system decides what is worth retaining and updates
notes using the exact exchange as evidence. Background maintenance checks
consistency and can organize eligible material. Historical episodes remain
distinguishable from statements about the present.

Automatic moves between folders depend on the version and enabled features of
the installed daemon. A new desktop-app version does not activate these features
on an existing installation.

## What you can edit

You can read and edit the ordinary Markdown notes in Obsidian. Keep a backup
before reorganizing many files. Preserve the note's metadata block at the top
and its source references: they help the daemon maintain identity, dates and links.

Files under the hidden **.daemon** directory contain the exact conversation
journal, indexes and internal state. They are not ordinary notes to rewrite.
The journal is the record from which facts can be checked; search indexes are
rebuildable aids.

## Local storage and model access

Your memory stays on your computer, but using a remote model sends relevant
conversation and memory context to the provider you connect. Local storage does
not mean all processing is offline. The model provider's access and billing
rules still apply.

Never attach the whole memory folder, conversation journal or authentication
files to a public issue. Share a fictional reproduction instead.

## Open the vault from Companion

In **My daemons > Manage > Memory**, choose **Add and open memory in Obsidian**.
Quit Obsidian completely before the first addition. Companion backs up the
existing Obsidian settings, registers this exact folder and opens the vault.
Leave Obsidian closed until preparation finishes. Later, **Open memory in
Obsidian** opens the registered vault directly, even while Obsidian is running.
A download link is available there and in About. No import or duplicate memory
is created. Manual **Open folder as vault** remains available if needed.
See [Obsidian setup and limits](companion-health-actions.md#open-memory-in-obsidian).
