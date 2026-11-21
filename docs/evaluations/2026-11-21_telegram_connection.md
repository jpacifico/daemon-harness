# Keeping daemons on Telegram after a slow boot

Date: 21 November 2026. Branch `companion/telegram-connection`, from `main`
at `7fd1430`; code commit `4666041`, trial fixes up to `316c9ca`.

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
- The daemons page reads the Telegram state again on its own, and the card
  follows a restart until the host reports Telegram again (added during the
  trial).

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

Run on 21 November 2026 on macOS, with the owner's throwaway Hermes daemon
(`test2`), ChatGPT subscription and throwaway bot. Times are local.

| Criterion | Result | Evidence |
| --- | --- | --- |
| 1. Baseline | Passed | Created with 0.11.18; Hermes state `connected`; no `load_timeout_seconds` in the config; the owner saw a reply. |
| 2. Update adds the setting | Passed | Update transaction `committed` at 18:22; the config diff against the copy taken before is exactly `+  load_timeout_seconds: 120`; the daemon runs the new runtime release; the owner saw a reply. |
| 3. Connected is shown | Passed | The owner saw "connected to Telegram" on the health page and no alert on the card. |
| 4. The failure is shown | Passed after fixes | Deadline set to `0.001`, gateway restarted at 18:31:54; Hermes logged `No adapter available for telegram` at 18:31:59 and kept running, as on 20 November. The alert and the health-page text appeared; three defects were found and fixed on the way (below). Repeated at 18:56:22 on the final build. |
| 5. Restart recovers | Passed | With 120 restored byte for byte, **Restart the daemon** on the card restarted the gateway at 19:05:12; Hermes logged `telegram connected` at 19:05:24. The card showed restarting, connecting, then reconnected; the owner saw a reply. |
| 6. Other daemons untouched | Passed | The 15 service definitions hashed before the trial are identical; every service of the owner runs with the same process as before. |
| 7. Suites | See below | |

### Defects found during the trial, fixed on the branch

1. **A Telegram entry left by the previous process counted as current**
   (`0a5a566`). Hermes keeps platform entries across restarts; the entry read
   after the forced failure was written by the gateway that had been stopped.
   With a gateway killed while connected, the card could have kept saying
   "connected". Only an entry stamped by the running process (`writer_pid`,
   `writer_start_time`) now counts, as in Hermes's own status page.
2. **No Restart button for daemons the app created** (`376b133`). The button
   required host controls enabled through the instance registry, which only
   existing installations added to the app have. One rule now covers both;
   a created daemon restarts like after a model change.
3. **A restart was not visible, and cards went stale** (`1971bee`, asked by
   the owner). The card now follows the restart, and the daemons page reads
   the Telegram state again every 30 seconds (every 5 seconds after a
   restart), redrawing only on a change.
4. **No reading while the window was covered** (`316c9ca`). macOS reports a
   covered window's page as hidden; the reading skipped it, so the alert did
   not appear until the window came to the front. The reading no longer
   depends on visibility. The final build showed the alert at launch; the
   covered-window case itself was not observed again after this fix.

### Suites

At `316c9ca`: Python 3.12 full suite 3250 passed, 25 skipped; desktop tests
174 passed; lint passed. CI (`canonical-runtime`) passed on the pull
request.

### Limits

- Contrary to the protocol, the trial builds also listed the owner's real
  daemons: discovery reads the Mac's services and the app's shared registry,
  not only the trial folder. Nothing was done on those cards; their state
  files were only read. The registry, empty before the trial, was rewritten
  empty by the removal.
- Not covered live: OpenClaw (unit tests only, including the check after a
  restart), a rejected token, a bot used by another program, Windows.
- The 120 s deadline is reached only on a slow boot; the trial forced the
  failure with a tiny deadline rather than a real slow boot.

### Decision

Merged on `main` (#25), for the next Companion release. Daemons created
before receive the 120 s deadline through **Update**; a daemon not managed by
the app needs the line added to its Hermes config.
