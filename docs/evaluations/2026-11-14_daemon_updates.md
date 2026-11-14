# Updating an existing daemon from the app

Date: 14 November 2026. Branch `companion/daemon-updates`, from `main` at
`366c1d0` (Companion 0.11.16).

## Terms

- **Daemon runtime**: the private copy of the Daemon Harness code that a daemon
  runs. Companion installs it when it creates the daemon, next to the daemon's
  memory, and the daemon's services start from it.
- **Companion code**: the part of that copy that installs and connects the
  daemon (`src/daemons_companion/`, plus three host-connection files). It
  decides how services are scheduled, not what the daemon says or remembers.
- **Engine**: the rest of the copy (memory, voice, prompts, host plugins and
  dependencies). It is frozen for existing daemons.
- **Runtime update**: replacing a daemon's runtime by the one the app carries,
  as a transaction: services stopped, new copy installed beside the old one,
  host profile pointed at it, services restarted and checked, everything
  restored if a step fails.
- **Host**: the program that runs the daemon's conversation, Hermes Agent or
  OpenClaw. A daemon is created on one host; OpenClaw can be added to a Hermes
  daemon later.

## Starting point

Companion 0.11.16 can update an existing daemon's runtime, but:

1. it offers the update only inside **Add a host**, before adding OpenClaw;
2. it handles only daemons created on Hermes Agent;
3. it refuses any change to the engine, by design;
4. it refuses a daemon that already has an added host.

So a fix to Companion code, such as the 0.11.16 service class, reaches an
existing daemon only if its owner adds OpenClaw to it.

## Scope decided by the owner (14 November 2026)

- **Step 1**: an **Update** action for a daemon created on Hermes Agent, in the
  daemon's page, using the existing transaction.
- **Step 2**: the same action for a daemon created on OpenClaw.

Out of scope: engine updates (still a new daemon restored from a backup), a
daemon with an added host (still refused, with a plain explanation), host
version upgrades, and any automatic update without the owner's click.

## Exit criteria (fixed before the code)

1. **Offer.** The daemon's page shows an update only when the daemon's
   Companion code differs from the app's and the engine is identical. A current
   daemon shows nothing. A daemon the transaction refuses (engine changed,
   added host, changed services or profile, too old) gets a plain explanation
   and no update button. An interrupted update offers to resume or restore.
2. **Hermes daemon.** The update commits; services are rendered by the new
   code; memory, model connection, Telegram bot and identity files are
   byte-identical before and after; the previous runtime is kept.
3. **OpenClaw daemon.** The same as criterion 2 with the OpenClaw services
   (gateway, daemon API and background services) and profile (configuration and
   workspace). Readiness covers the gateway, its plugin, the daemon API and the
   after-turn worker.
4. **Rollback.** For each host, a failure after each step, a readiness failure
   and an interrupted controller each restore the exact previous runtime,
   profile and services. Tested on macOS and Windows renderings.
5. **Engine untouched.** The change is limited to Companion code, the desktop
   app, tests and documentation. No file under `src/daemon_core/`,
   `integrations/`, `memory/` or `server/` changes.
6. **Add a host unchanged.** The update inside Add a host behaves as before.
7. **Suites.** Full Python suite on 3.12, desktop tests and lint pass; CI is
   green.
8. **Real daemons, macOS.** A throwaway Hermes daemon and a throwaway OpenClaw
   daemon, each created with Companion 0.11.15, are updated by the button in a
   build of this branch. Before and after: service class, runtime revision,
   memory file hashes and a reply on Telegram. One forced failure restores
   0.11.15 exactly. The time the daemon is offline is measured.
9. **Windows.** Covered by unit tests only unless a native run is possible;
   reported as such.

### Amendment (14 November 2026, before any real-daemon trial)

Criterion 5 named `integrations/` as untouched. `AGENTS.md` requires each
integration's `INSTALL.md` to describe a change to an installer's rollback, so
`integrations/hermes_plugin/INSTALL.md` and `integrations/openclaw_plugin/INSTALL.md`
gain one paragraph each. The runtime update itself treats `INSTALL.md` as
documentation, outside the engine. No other file under `integrations/` changes.

## Change

- `runtime/upgrade.py`: the Hermes transaction keeps its steps; its
  host-specific parts (services, profile, profile check, readiness, added-host
  test) become methods. `OpenClawUpgrade` supplies them for a daemon created on
  OpenClaw: gateway, daemon API and background services; configuration and
  workspace; readiness of the after-turn worker, the daemon API, the gateway and
  its plugin, with up to ten minutes for the gateway to boot. A daemon created
  on OpenClaw registers its own host; only a registration for another profile
  counts as an added host. `check` tells the daemon's page what to offer.
- `openclaw/profile.py`: the profile projection drops plugin paths that point
  into another release of the same daemon, so an updated profile loads the
  plugin once. A first install has no such path and is unchanged. `current()`
  tells whether the profile still holds its installation's projection.
- `openclaw/gateway.py`: the gateway service is built by one function, shared
  by installation and update; the update probes a gateway it registered itself.
- `hosts/update.py`: the guided update, shared with Add a host, whose wording is
  unchanged. `desktop/protocol.py` serves it as `--update-daemon`.
- Desktop: the daemon's page shows the update section; the backend runs the
  guided session in its own private folder; English and French texts.

## Results

| Criterion | Result |
| --- | --- |
| 1. Offer | Pass. Unit tests: available, current, refused with its code, interrupted. Running app: the page of each trial daemon showed the update section before the update and nothing after it |
| 2. Hermes daemon | Pass: existing transaction tests, and the real trial below |
| 3. OpenClaw daemon | Pass: unit tests (macOS and Windows renderings) and the real trial below |
| 4. Rollback | Pass: unit tests for every step, both hosts; one forced failure per host on a real daemon restored it exactly |
| 5. Engine untouched | Pass, with the amendment above |
| 6. Add a host unchanged | Pass: same texts and refusal codes (unit tests) |
| 7. Suites | Python 3.12: 3229 passed, 25 skipped; desktop 166/166; lint clean. CI not run yet |
| 8. Real daemons, macOS | Pass, see below |
| 9. Windows | Unit tests only |

## Real-daemon trial (macOS, 14 November 2026)

### Protocol

- Isolated state: the Companion 0.11.15 app and a build of this branch
  (payload `6663461`, engine identical to 0.11.15) ran unpackaged with
  `DAEMONS_COMPANION_DEV_BASE` set to a throwaway folder. The owner's daemons,
  their services and their memory were not touched.
- One throwaway daemon per host, created with 0.11.15 on the owner's ChatGPT
  subscription (model Astra) and one throwaway Telegram bot, used by one daemon
  at a time. The owner did the ChatGPT and Telegram steps.
- State recorded as hashes only: service definitions and their service class,
  the runtime revision named in each service, memory files, profile and
  connection files. No contents were read or kept.
- Forced failure: the branch's transaction run from the command line with a
  failure injected after its last step (`upgrade-start`), when the services
  had already restarted on the new code, so every step had to be undone.
- Update: the Update button in the branch build, then a Telegram message.
  Offline time read from a sample of the service processes every 0.5 s.

### Measurements

| | Hermes Agent daemon | OpenClaw daemon |
| --- | --- | --- |
| Forced failure, whole operation | 90.1 s, `rolled_back` | 144.0 s, `rolled_back` |
| After rollback | Same release; new copy removed; service definitions byte-identical; memory 23/23 files identical; profile, model connection and bot settings identical | Same release; new copy removed; service definitions byte-identical; memory 16/16 identical; configuration, workspace, connections 17/17 identical |
| Confirmation to committed update | 82.0 s | 83.5 s |
| Gateway without a process | 40.7 s | 45.2 s |
| Services stopped to readiness verified | 68.8 s | 69.9 s |
| Runtime revision | `a9074739` → `e9243775`; previous kept | `a9074739` → `e9243775`; previous kept |
| Service class | Gateway and embeddings Background → Standard; worker and Dreaming stay Background | Gateway, daemon API and embeddings Background → Standard; worker and Dreaming stay Background |
| Profile change | `.env`: only `DAEMON_CORE_PYTHON` and `DAEMON_CORE_SRC`, by the release id; `config.yaml` identical; Telegram settings identical (compared key by key, values not shown) | `openclaw.json`: only `plugins.load.paths`, one path, moved to the new release; workspace identical |
| Memory | 23/23 identical | 16/16 identical |
| Reply after the update | Three replies, none degraded, composition 0.04–0.10 s; after-turn worker on the new revision, no error | Message received and answered (17 s); after-turn worker on the new revision, no error |

Each forced failure restarted the old services; only files the gateway
rewrites on every start (its process record and heartbeat) differed afterwards.

### Observations

- In the update flow, the screen kept the installation title (“Installation
  de votre daemon.”) and the final banner said the installation was finished.
  Corrected after the trial: the screen reads “Updating your daemon.” and ends
  with “The update is finished. {name}'s services restarted and were checked.”
  (texts only; not re-run on a real daemon).
- Found in Companion 0.11.15 while creating the trial daemons, unrelated to
  this change, and present on `main` too:
  - pressing Back on the Telegram token screen restarts the whole Telegram
    step and forgets the account ID already entered;
  - local memory search refuses to start below about 9.2 GiB of free disk,
    while its message says 11 GB; after that refusal the app returns to the
    host choice.
- In the unpackaged 0.11.15 run, the ChatGPT sign-in page did not open by
  itself; the owner used the link. Whether the installed app behaves the same
  was not checked.
