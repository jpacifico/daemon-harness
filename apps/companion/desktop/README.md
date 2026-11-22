# Daemons Companion desktop app

Shared Electron UI using the existing private Python installer. New installation: Hermes Agent or OpenClaw, with ChatGPT sign-in or an OpenAI or Anthropic API key. Existing instance management: Hermes Agent and OpenClaw. System typography, light/dark/system appearance and Paris Métro-inspired blue accents.
The sidebar uses smooth Dervaux-inspired vector lettering, blue in Light and white in Dark.

Real operations: verified private bootstrap, host download, native browser OAuth,
account model choices, Telegram token validation/retry, local embeddings,
transactional installation, first-exchange/after-turn evidence. The setup summary reads
the owned receipts and opens the bot or vault. The Installations page separately
discovers local Hermes/OpenClaw profiles and offers explicit scoped gateway
control. The app updates itself from its channel (below).

The renderer has no filesystem or shell access. Answers consume a prompt ID.
Secrets are masked, cleared on submission and not included in protocol output.
Cancellation allows the installation engine to recover before the app closes.
Completed native sessions remain available for a subsequent setup attempt.

The app reserves `DaemonHarness/Companion/private` under Application Support on
macOS / LocalAppData on Windows for new installations. Discovery does not
automatically connect or reinstall any existing personal or terminal profile.
The fresh vault has five folders at its root, with technical files in `.daemon`.
An existing legacy vault is refused; no migration is silently performed.

From a clean committed revision, fetch the pinned uv vendor archive with
`scripts/fetch_companion_uv.py`. Supply the returned binary digest to
`scripts/build_companion_desktop_payload.py --uv <absolute-binary> --uv-sha256
<digest> --target macos|windows --output <absolute-desktop-directory>/runtime
--channel main|candidate`.
The builder creates ignored `runtime/` and `runtime.json`. It refuses dirty source.

The channel is the one the packaged app follows for its own updates. The app
reads `apps/companion/distribution/channel.json` on `main` or
`companion-candidate`, downloads the release's package and install script, and
checks both against their SHA-256 and each other. It then quits. A helper
(`updater/`) that outlives the app runs that install script, which keeps the
previous app on failure, and reopens the app. On Windows the app starts it
attached and waits until it has handed off to a copy outside the app's job,
which Electron kills on quit. Public URLs are tried first; a signed-in GitHub
CLI is the fallback for a release the public cannot read. Source runs, disk
images and translocated copies do not update themselves. The repository is
named only in `updates.cjs`.

```sh
cd apps/companion/desktop
npm ci
npm run check
npm test
npm start
npm run package:mac
# On Windows: npm run package:windows
```

For an isolated packaging stage, install that stage's production dependencies with
`npm ci --omit=dev --ignore-scripts --no-audit --no-fund` from its own lockfile.
Do not symlink another checkout's `node_modules`: electron-builder may omit
transitive dependencies without failing the build. Using a cached external
builder executable is separate from supplying the app's production dependencies.
Extract the resulting `app.asar` and check loading `json5`, `js-yaml` and `plist`,
including a YAML parse and a plist build/parse roundtrip, before native app smoke.
A successful installer build alone is not a startup check.

`npm run smoke` exercises the real window and duplex Python transport with a
synthetic Telegram validator, a new private Python bootstrap and an empty test
vault. It makes no account/model/channel calls and starts no daemon services.
The Installations UI uses a fake scheduler; a subsequent native control fixture
starts two short-lived Node processes as disposable user services, checks
start/stop/restart and removes them. No personal service is controlled. This
qualification hook requires Node on PATH; the installed app does not. Screenshots
are saved in a fresh system temporary directory. The smoke does not qualify
OAuth, real Telegram, or a complete daemon service installation.

The manual `desktop-companion` scenario of `windows-qualification.yml` builds
and tests packaged apps on macOS ARM64 and Windows x64 and uploads candidate
artifacts. OS signing/notarization is not provided by these commands.

## Display languages

English is always the first-launch default. The bottom of the sidebar offers English
and Français. Only `display-preferences.json` in Electron userData is persisted;
no form values or secrets enter it. Switching languages retains the current input
and radio choice in renderer memory. The unchanged engine accepts indices and raw
answers. `locales/messages.json` has stable keys, paired locales and named arguments;
`i18n.js` is a display-only adapter for the existing English protocol. Never branch
installation logic on a translated label. Names, paths and unknown diagnostics stay
exact. Missing external errors have translated guidance and original detail.

`npm test` validates catalogs, interpolation and persistence. The isolated native
smoke test repeats the real Python transport in English and French, then exercises
cancellation and synthetic error/completion rendering. It does not use real accounts.

## Local instances

`instances.cjs` runs only in the main process. It reads bounded YAML/JSON/plist
metadata, discards secrets, canonicalizes profile paths and detects shared memory.
Named Hermes profiles, OpenClaw profile roots, installer roots and current-user
services are considered; the native folder picker adds other profiles. It does
not scan remote hosts, containers, conversations or memory note bodies.

The app registry stores explicit service grants bound to exact profile, label
and definition digest. Reconfirm a changed service. Start/stop/restart affect
only that host gateway, leaving shared daemon maintenance intact. This is not
an enduring pause across login, a full health check, a host uninstaller, or a
way to install Daemon Harness onto an existing host. Unsupported service layouts
remain visible without invented control commands.

`npm test` covers inventory, exact preservation, connection/revocation, ambiguity,
stale confirmation, alias deduplication and public metadata boundaries. The opt-in
`npm run test:instances-native` creates two disposable user services, controls
one, checks the sibling, and removes both. Never point that fixture at personal
profiles. The packaged smoke also runs this native fixture after its UI checks.

## Existing daemon settings

Manage daemon is separate from gateway control. The renderer sends a discovered
installation ID and an opaque review token, never a filesystem path. The main
process resolves the current profile, runtime memory, source and explicit account;
shared hosts are shown and confirmed. The Python module
`daemons_companion.desktop.daemon_settings` reuses the canonical identity validators,
checks source support without executing it, and atomically replaces only
`core/daemon_identity.json` under the configured runtime memory root.

Existing root-folder and legacy layouts are supported without migration. Unknown
source, unsupported addressing settings, ambiguous account, malformed data,
symlinks and concurrent changes are not guessed or overwritten. Shared metadata
keeps its original account/platform. Current conversation and corrected canonical
memory retain precedence over setup addressing answers; no prompt is changed.
The memory folder can be opened or its path copied for Obsidian; relocation is
not implemented. Installing this app does not update any existing daemon runtime;
the daemon's page offers that update when only Companion code differs.

The first form may prepare the app's private Python component, with visible
progress; it does not create or resume a daemon installation. Saving needs no host
restart. Closing the app waits for an in-flight settings operation to finish.
The packaged test uses real IPC and the Python writer on synthetic shared host
profiles, verifies cancellation, concurrency, reopened values and exact
preservation of all other fixture files on macOS and Windows.

Additional preferences is collapsed when empty, and open for an existing value.
The pronoun control offers she/her, he/him, unspecified and custom choices while saving
the same canonical string. Custom wording and draft choices survive display
changes. Accounts detected on hosts sharing this vault are listed separately
from the single binding to which these preferences apply; discovery does not
link accounts or grant access. Additional account linkage and post-V0 memory
relocation follow the scoped plan.

## My daemons, About and appearance

My daemons is the first destination and retains the definition of a daemon.
The only permanent destinations are My daemons and About. Each named daemon
appears before its host(s); canonical shared runtime identities group together.
Names alone and unverified memory pointers never merge cards. Shared-host actions
remain scoped to the host/account chosen by the user. Hostless installations are
listed separately. Gateway state is technical evidence, not overall daemon health.

Owned unfinished setups appear individually with Resume and their diagnostic.
The main process resolves opaque setup IDs to direct app-owned roots, refuses
completed or unreadable receipts, and selects that exact root only on resume.
Listing and report reads never select or start a setup. Starting a new setup is
still an explicit separate installation. Unknown names remain New daemon, with
the creation date to distinguish unfinished attempts.

The brand subtitle no longer uses Companion. `assets/daemons-dervaux.svg` is an
original outline drawing inspired by the Dervaux terminal wordmark: condensed
capitals, a broad D, a shallow M notch and two fine stencil cuts. Curved outlines
replace the terminal pixel grid. It displays at 128 × 32 CSS pixels, aligned with
navigation labels, with antialiasing at every screen scale. Light uses `#0a0182`,
sampled from the blue in the user’s Paris Métro reference image. This is a
reference-image match, not an official transit color specification. Dark
(including resolved System) turns the same asset white. No font
installation, remote asset, animation or terminal banner is used. The image has
the accessible name Daemons and is listed for both native packages.

About explains continuity across conversations, portability between agents, and
local Markdown memory. It describes opening the memory folder as an Obsidian vault
to read, correct, organize, link and search the same notes used by the daemon.
The five root folders are named; Obsidian remains optional. This is explanatory
copy, not a new migration or synchronization feature.

About credits Jonathan Pacifico, reads version/revision from the packaged app,
and opens two fixed GitHub destinations. The renderer supplies only an enum key,
never a URL. Contact opens issues; no issue, message or diagnostic is submitted.

Appearance defaults to Light. A sun/moon button in the top-right corner toggles
Light/Dark without leaving the current screen. Its translated label names the
next mode through its tooltip and accessible name. The icon has no permanent
text or border; its 32 × 32 click area remains easy to target. It supports the keyboard, and its area is excluded from window dragging.
The bottom sidebar language picker remains the only language control.
`nativeTheme.themeSource` applies the saved preference before window creation; CSS uses the resolved `prefers-color-scheme` value. The
window background follows native changes too. System tracks the OS without changing
it. A previously saved System choice is retained until the user explicitly
chooses Light or Dark with the button. Language and theme are merged through one serialized atomic preferences writer;
legacy language-only files remain compatible. Changing display or navigating away
preserves draft text, radio selection and masked secrets without saving them to disk.

Native smoke checks both languages/themes at minimum window size, actual text
contrast, draft preservation, persistence after reload, System resolution and
fixed About links. Contact browser launches are intercepted during that test.

The shared accent is Metro blue `#0a0182`, matching the wordmark. Primary buttons
use it in both themes; Light also uses it for links, progress, radios and focus.
Active/selected surfaces use tints of the same hue. Dark uses lighter tints for
link text and focus, plus a visible primary-button edge, to preserve contrast
on dark surfaces. The wordmark remains white in Dark.

## Creating another compatible instance

Install a daemon creates a separate pinned Hermes Agent installation with the
selected model connection. Add an existing installation is a secondary action.
During setup, Return to setup retains the current operation. Automatic attachment
to arbitrary existing hosts and new OpenClaw creation remain unsupported.

The main process chooses a fresh `Companion/instance-<id>` directory. Each retained
root, including the legacy `private` root, is independently listed and resumable
when its receipts allow it. No renderer path is accepted. Downgrading preserves
these directories; older apps offer only their previously supported navigation.

Settings feedback stays in the fixed footer: pending spinner, saved confirmation,
unsaved edits or an error. A second edit immediately re-enables Save. The form
retains its scroll position across saving and display changes. The inventory is
called My daemons / Mes daemons; hosts remain visible beneath their daemon.

The they/them preset is removed from settings and onboarding at the user’s request.
A previously saved value is displayed as custom wording without rewriting it.

### Retrait ciblé depuis Companion — candidat macOS du 18 octobre

**Installations → Désinstaller** explique les deux choix : **Désinstaller Hermes
et le daemon**, ou **Retirer le daemon, garder Hermes**. Le premier arrête les
deux et exige le nom de l’installation affichée (« Hermes - Nova ») ; le second
conserve Hermes sans mémoire ni continuité Daemon Harness et exige le nom du daemon.
Le plan reste lié au profil et à sa révision ; le moteur de retrait est inchangé. Le premier périmètre est limité aux installations Hermes créées par
Companion, complètes et munies de reçus vérifiables. Une installation externe,
partagée ou modifiée de façon ambiguë est refusée ; OpenClaw et le retrait sous
Windows ne sont pas activés par ce candidat.

Détacher retire les deux plugins et les seuls services privés après-tour,
Dreaming/REM et embeddings de cette installation. Les champs natifs encore
gérés sont restaurés depuis la transaction initiale, sans réécrire les autres
réglages. La passerelle Hermes retrouve son état de départ. Désinstaller retire
aussi sa passerelle et ses programmes privés. **Memory reste exactement à sa
place**, même imbriqué dans l’installation. Le profil hôte, les conversations,
les connexions locales et les diagnostics sont conservés ; aucun bot Telegram
ni compte fournisseur distant n’est supprimé. Le chemin du vault est ouvrable
et copiable avant/après. Aucun effacement ni déplacement de mémoire n’est proposé.

Les services doivent être arrêtés avant toute mutation. Un échec restaure les
fichiers et services précédents, ou conserve un journal de nettoyage reprenable
si le retrait des programmes avait commencé. Une intégration déplacée, une
relance Hermes encore dépendante du daemon ou une propriété incertaine bloque
le retrait automatique. Le chemin conversationnel, ses prompts et les hôtes
non sélectionnés ne changent pas. Aucun service personnel n’est retiré au titre
de la qualification.

Startup qualification: `scripts/qualify_companion_private_startup.py` exercises a
real neutral memory worker with an empty queue and a synthetic Keychain entry
under an isolated HOME. All socket connections are blocked. It also runs native
Hermes Agent plugin commands in a disposable profile and verifies exact rollback
and concurrent-edit refusal. Pass a pinned host interpreter using `--hermes-python`
and a private result path using `--output`. It does not qualify real model or
Telegram delivery.


## Health and diagnostic reports

Open **Installations > Health** for an installed host, including existing profiles.
The screen shows a dated, read-only snapshot: host process, daemon integration,
memory directory, last recorded model response and memory completion, worker
heartbeat, pending/failed queue counts, and Dreaming/REM verdicts. Missing receipts
are *Not yet verified*. A model success older than 24 hours and maintenance older
than 48 hours are marked *Check needed*, not failed: the computer may have been
asleep or no conversation may have required maintenance. The worker heartbeat
must be younger than 15 seconds. Pending jobs older than five minutes deserve
inspection. Counts are scoped to the selected host/account. A shared worker and
Dreaming/REM concern the shared memory.

No API/provider test, Telegram message, search query or memory write is made.
Channel delivery and semantic search are explicitly unverified. Legacy profiles
without compatible technical records remain inspectable with partial evidence.
Records are bounded to the latest 32 candidate receipt files among at most 2,000
directory entries and a 256 KiB tail per file; a missing match is never success.
The maintenance register is read as a 256 KiB tail, exporting only pass verdicts.

During setup, **Diagnostic report** is available at every screen and on its installation card after
reopening the app. It covers bootstrap, host, identity, provider, Telegram,
installation transactions and first conversation. Opening or exporting it does
not interrupt setup. Completed phase checkpoints are not proof that later phases
will succeed. A killed process cannot write its own final error; the next launch
shows the saved unfinished attempt as interrupted.

New local reports are in `diagnostics/` under each owned Companion setup root,
retaining the latest 20 attempts and 256 checkpoints each per installation.
Older global reports remain in `bootstrap/diagnostics/`; they lack an installation
binding and are not silently assigned to any daemon. **Export diagnostic** opens a native Save dialog. Export is
manual; there is no upload. Reports use allowlisted fields only, excluding keys,
account identifiers, private paths, answers, URLs, raw command output, exception
messages, dialogue and note content. A write failure is shown with an option to
export the in-memory report before closing. Existing private transaction reports
remain available beside the failed installation for deeper investigation.

## Failures caused by outside services

The app depends on outside services: GitHub (downloads and app updates),
OpenAI (ChatGPT sign-in and models), the API providers (Anthropic, Mistral AI,
OpenAI, xAI), Google Drive (backups), Telegram, Ollama and Node.js. When one of
them fails, the message names it, says what was kept and what to do.

`services.js` lists these services. A catalog entry in `locales/messages.json`
marks a failure as an outage of a service with `"status"`: either a service id
(`"github"`) or the template argument that holds its name (`"{service}"`).
Only outages carry it, not a refused key or a usage limit, which are about the
user's own access.

Under such a failure, the app reads that service's public status page
(`service-status.cjs`) and shows what it reports: an incident in progress with
its published name, no incident (then check the Internet connection), or a
status page that cannot be reached either. A link opens the status page. The
request goes only to the fixed address in `services.js`, carries no user data,
and is made only while the failure is on screen; answers are kept for two
minutes. The answer is information for the reader: it never decides a retry.

GitHub, OpenAI, Anthropic, Mistral AI and Google Drive publish a readable status
page. Telegram, Ollama, Node.js and xAI do not: their failures name them without
a status line. A daemon's health report adds the status of its model provider
when its last answer failed.

## Contextual health diagnostics and Obsidian

Companion provides a per-check Health diagnostic: matching technical error
types, failure stages, last recorded success, known queue recovery state and
targeted export. Only an already authorized stopped Gateway offers Start with
the existing confirmation. Inspection/export does not repair queues, reconnect
providers, run maintenance, call models or change memory. Obsidian integration
opens the exact registered memory vault. On first use, the user quits Obsidian,
then Companion backs up its existing global settings and registers the selected
memory directory before requesting its opening. Running Obsidian, unreadable
settings and detected concurrent changes block registration. Existing vaults and
settings are preserved. Notes are never copied, moved or rewritten by Companion;
Obsidian creates its own vault settings when opening. First-use help appears once.
Replacing the app does not upgrade or restart an existing daemon runtime. The
daemon's page offers **Update** (hosts/update.py, runtime/upgrade.py) for a
daemon created on Hermes Agent or OpenClaw whose engine matches the app's.

See [the user and technical guide](../../../docs/companion-health-actions.md).

### First registration in Obsidian

The user must quit Obsidian for the first addition. A native process check runs
before preparation and again before an atomic registry replacement. A unique
private backup retains the original `obsidian.json` bytes. Unknown settings and
other vault entries are preserved. Existing registered vaults open by exact ID
without rewriting settings, even while Obsidian is running.

The registry path is the standard OS app-data `obsidian/obsidian.json`. The main
process resolves the selected daemon's memory; the renderer supplies neither a
path nor a URL. Malformed/oversized/symlinked settings, uncertain process state,
changed snapshots and concurrent registration fail closed. A failed launch keeps
a completed registration, so retrying does not duplicate the vault or undo later
Obsidian writes. Backups are never automatically restored or deleted.

This uses Obsidian's internal registry format, not a supported registration API.
Process and file checks cannot provide an exclusive transaction with Obsidian:
leave it closed until Companion finishes. A future incompatible format must fall
back to manual registration. Custom/portable Obsidian configuration locations are
not supported. No plugins, sync or note content are configured by Companion.

## Application icon

`assets/app-icon.svg` is the editable vector master. It reuses the A and E
outlines from the Dervaux wordmark, enlarged and clipped inside the inner ring, with the same #0a0182 blue.
Run `npm run icons` on macOS to render the PNG, Windows ICO (16 to 256 pixels)
and macOS ICNS (16 to 1024 pixels) using the pinned Electron and native iconutil.
The generated assets are committed. Electron Builder uses the ICNS/ICO for the
bundle, installer and shortcuts; BrowserWindow uses the PNG on Windows.
No daemon, memory or account is needed to regenerate the icons.

## Unfinished installation cleanup

Companion can delete an unfinished app-owned Hermes Agent setup after a separate
confirmation. It removes the attempt's private programs, downloads and verified
background services, then hides the incomplete card. Active setup must stop first.
Profiles, saved local sign-ins and diagnostic/recovery records are retained;
the confirmation shows their folder. Memory is retained unless the owner also
chooses to delete it: offered only for the memory Companion created inside the
attempt, confirmed a second time, checked again after the cleanup, then moved
to the Trash. Telegram bots and provider accounts remain.
Completed installs use the existing uninstall flow. Unverified ownership, shared
services, changed plans or active processes block deletion; partial cleanup keeps
a retry card and a sanitized diagnostic. Other hosts and daemons are unchanged.

Unfinished-setup cleanup is enabled on macOS only. Windows cleanup stays disabled
until native deletion and service-recovery qualification is available.
