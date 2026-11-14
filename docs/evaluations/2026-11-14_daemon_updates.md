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

## Results so far

| Criterion | Result |
| --- | --- |
| 1. Offer | Unit tests: available, current, refused with its code, interrupted. Page rendering not yet seen in a running app |
| 2. Hermes daemon | Existing transaction tests pass unchanged |
| 3. OpenClaw daemon | Pass (unit tests, macOS and Windows renderings): services and profile point at the new release, the plugin is loaded from it only, memory and account unchanged |
| 4. Rollback | Pass (unit tests): failure after each step, readiness failure and interrupted controller, both hosts |
| 5. Engine untouched | Pass, with the amendment above |
| 6. Add a host unchanged | Pass: same texts and refusal codes (unit tests) |
| 7. Suites | Python 3.12: 3229 passed, 25 skipped; desktop 166/166; lint clean. CI not run yet |
| 8. Real daemons, macOS | Not run yet |
| 9. Windows | Unit tests only |
