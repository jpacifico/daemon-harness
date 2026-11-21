# Keeping daemons on Telegram after a slow boot

Date: 21 November 2026. Branch `companion/telegram-connection`, from `main`
at `7fd1430`, code commit `4666041`.

## Terms

- **Host**: the program that runs the daemon's conversation, Hermes Agent or
  OpenClaw. Companion installs a private copy for each daemon.
- **Gateway**: the host's long-running service that connects to Telegram.
- **Plugin load deadline**: the time Hermes gives each of its plugins to load
  at startup (`plugins.load_timeout_seconds`, 10 s by default). A plugin that
  overruns it is skipped.
- **Runtime update**: the **Update** action on a daemon's page, which replaces
  the daemon's copy of the Companion code as a reversible transaction.

## Starting point

On 20 November 2026, after a macOS update and reboot, both of the owner's
Hermes daemons started while the Mac was busy. Their Telegram plugin overran
the 10 s deadline (`load timed out after 10s`); Hermes started without any
Telegram connection (`No adapter available for telegram`), kept running, and
never retried. Neither daemon received messages until its gateway was
restarted by hand the next morning. The app showed nothing: its health page
reported the conversation channel as "not tested".

## Change

- Hermes profile: `plugins.load_timeout_seconds: 120` for new daemons. The
  runtime update adds it to daemons created before; the check before an update
  accepts it missing, never different; rollback restores the previous config.
- Health: the conversation check reads the host's own Telegram state, from
  Hermes's `gateway_state.json` or OpenClaw's gateway health answer.
- Health page and daemon card: "not connected to Telegram" with **Restart the
  daemon**; a rejected bot token or a bot used by another program is named,
  without a restart.

## Exit criteria (fixed before the trial)

1. **Baseline.** A throwaway Hermes daemon created with Companion 0.11.18
   answers on Telegram; its config has no `plugins.load_timeout_seconds`.
2. **Update adds the setting.** The **Update** action of a build of this branch
   commits; the config differs only by `plugins.load_timeout_seconds: 120`;
   the daemon answers on Telegram afterwards.
3. **Connected is shown.** The health page reports the Telegram connection as
   connected, and the daemon card shows no alert.
4. **The failure is shown.** With the deadline forced to a value Telegram's
   plugin cannot meet and the gateway restarted, the gateway runs without
   Telegram; after the two-minute grace, the card shows the alert with
   **Restart the daemon**, and the health page shows "not connected to
   Telegram" with the same button.
5. **Restart recovers.** With the 120 s deadline restored, **Restart the
   daemon** from the card reconnects the gateway to Telegram; the alert
   disappears on refresh, and the daemon answers.
6. **Other daemons untouched.** The owner's daemons and services are identical
   before and after (service definitions hashed, processes running).
7. **Suites.** Python 3.12 full suite, desktop tests and lint pass; CI is green.

Not covered by the trial, reported as such: an OpenClaw daemon (unit tests
only; its gateway query is the readiness query already used after install and
update), a rejected token and a bot used by another program (unit tests only),
Windows (unit tests only).

## Protocol

- Isolated state: Companion 0.11.18 and a build of this branch run unpackaged
  with `DAEMONS_COMPANION_DEV_BASE` set to one throwaway folder outside any
  temporary directory, so the branch build sees the daemon the release
  created. The installed app is closed during the trial.
- One throwaway Hermes daemon, the owner's ChatGPT subscription and the
  throwaway Telegram bot. The owner does the ChatGPT and Telegram steps and
  types every message.
- Observation from files and logs only: config keys, gateway state, service
  states and timings. No message text is read or kept.
- The forced failure edits only the throwaway daemon's host config.
- Cleanup: the daemon is removed with the app; its folders go to the Trash.

## Results

Pending.
