# GPT-6.2 with a ChatGPT subscription, and changing the model

Date: 22 November 2026. Branch `companion/chatgpt-models`, from `main` at
`20062b0` (Companion 0.11.19).

## Terms

- **ChatGPT subscription**: a daemon that uses its owner's ChatGPT account
  instead of an API key.
- **Host**: the program that runs the daemon's conversation, Hermes Agent or
  OpenClaw. Companion installs a private copy for each daemon.
- **Private Codex**: the official Codex command-line program that Companion
  installs inside each subscription daemon. The daemon's memory work (filing a
  conversation after each turn, the nightly pass) runs through it.
- **Model catalog**: the list of models ChatGPT returns for an account. ChatGPT
  lists the GPT-6.2 models only to a client reporting Codex 0.166.0 or later.

## Starting point

- The app offers GPT-6 Astra and GPT-6.1 Sol. On 18 November 2026, Hermes Agent
  called GPT-6.2 Astra and GPT-6.2 Sol successfully, but the private Codex
  0.159.2 was refused for them; Codex 0.170.0 answered both.
- OpenClaw 2026.10.7 asks the catalog as Codex 0.160.0 and never sees GPT-6.2.
  OpenClaw 2026.11.6 (19 November 2026) asks as the Codex its turns run, or
  0.169.0.
- A subscription daemon cannot change its model in the app; only API-key
  daemons can.

## Change

- Offered subscription models, in this order: GPT-6.2 Astra (recommended),
  GPT-6.2 Sol, GPT-6 Astra. GPT-6.1 Sol is no longer offered; daemons already
  using it keep working and can change model.
- Private Codex 0.170.0 for new subscription daemons.
- New OpenClaw daemons use OpenClaw 2026.11.6 (Node 24.22.1 unchanged). Existing
  OpenClaw daemons keep their host: the app does not replace a daemon's host.
- **Change model** on a subscription daemon's page: the models the account and
  the daemon's host offer; the change installs the current private Codex when
  the daemon has an older one, restarts the daemon, and is kept only after a
  reply, as a provider switch is.
- Unchanged: API-key models, the daemon's memory, identity and Telegram bot.

## Exit criteria (fixed before the trial)

1. **Existing Hermes daemon.** A throwaway Hermes subscription daemon created
   with Companion 0.11.19 (Codex 0.159.2, GPT-6 Astra) updates with a build of
   this branch; **Change model** lists GPT-6.2 Astra, GPT-6.2 Sol and GPT-6
   Astra when the account offers them; choosing GPT-6.2 Sol installs Codex
   0.170.0 in that daemon; afterwards the host config and the daemon's provider
   record both name `gpt-6.2-sol`, the daemon answers on Telegram, and its
   memory work after that turn completes with Codex 0.170.0.
2. **New OpenClaw daemon.** A throwaway OpenClaw subscription daemon created
   with the branch build runs OpenClaw 2026.11.6, is offered the GPT-6.2 models,
   is created with GPT-6.2 Astra, answers on Telegram with the daemon plugin
   loaded, still asks the owner before running a command, and its memory work
   after a turn completes.
3. **New Hermes daemon.** A throwaway Hermes subscription daemon created with
   the branch build is offered the three models with GPT-6.2 Astra first, gets
   Codex 0.170.0, answers, and its memory work after a turn completes.
4. **Existing OpenClaw daemon.** A throwaway OpenClaw daemon created with
   0.11.19 (OpenClaw 2026.10.7) still updates with the branch build and keeps
   answering; **Change model** lists only the models its host offers.
5. **Other daemons untouched.** The owner's service definitions are identical
   before and after; their processes keep running.
6. **Suites.** Python 3.12 full suite, desktop tests and lint pass; CI is green.

Reported as not covered: Windows (unit tests only), an attached host, a
change refused or undone after no reply (unit tests only).

**Amendment (22 November 2026, before any trial daemon was created).** At the
owner's request, to keep trials short, only criteria 1 and 2 are run on real
daemons. Criteria 3 (new Hermes daemon) and 4 (existing OpenClaw daemon) are
covered by unit tests only and reported as such.

## Protocol

- Companion 0.11.19 and a build of this branch run unpackaged with
  `DAEMONS_COMPANION_DEV_BASE` set to one throwaway folder outside any
  temporary directory. The trial builds also list the owner's real daemons
  (discovery reads the Mac's services); nothing is done on their cards.
- Throwaway daemons, the owner's ChatGPT subscription, a throwaway Telegram bot
  used by one daemon at a time. The owner does the ChatGPT and Telegram steps
  and types every message.
- Observation from files and logs only: config keys, receipts, service states,
  versions and timings. No message text is read or kept.
- Cleanup: each daemon is removed with the app; trial folders go to the Trash.

**Amendment (22 November 2026, during the trial).** Two defects found while
running criterion 2 were fixed on this branch, and the owner asked for one
addition; see "Found during the trial".

## Results

All times UTC.

1. **Existing Hermes daemon: passed.** Throwaway Hermes daemon created with
   Companion 0.11.19 (GPT-6 Astra, Codex 0.159.2). After **Update** and
   **Change model** to GPT-6.2 Sol: host config and provider record name
   `gpt-6.2-sol`; the daemon's private Codex is 0.170.0, the 0.159.2 folder is
   kept; no change left pending. Next turn: reply 10:30:19, memory work
   completed 10:31:06; Codex fetched the model catalog as 0.170.0; no error in
   the memory worker's log.
2. **New OpenClaw daemon: passed after two fixes.** Created with the branch
   build (`eb7498a`): OpenClaw 2026.11.6, offered GPT-6.2 Astra, GPT-6.2 Sol and
   GPT-6 Astra, created with GPT-6.2 Astra, private Codex 0.170.0, daemon plugin
   loaded, native memory off. First turns: replies 16:41:48 and 16:42:05,
   memory work completed 16:43:12. A command turn at 17:04 completed; the owner
   saw the approval request in Telegram (approvals leave no line in the host
   logs). The app did not confirm the first reply: see defect 2 below.
   With the fix, on the same daemon: **Update** applied, **Change model** to
   GPT-6.2 Sol applied 17:11:35, gateway restarted on `openai/gpt-6.2-sol`,
   reply 17:12:25, memory work 17:12:54; the app then offered to keep the
   change and the owner kept it.
3. **New Hermes daemon:** unit tests only (amendment).
4. **Existing OpenClaw daemon:** unit tests only (amendment). Change model on
   OpenClaw was run live on the new OpenClaw daemon (criterion 2), not on a
   2026.10.7 host.
5. **Other daemons untouched: passed.** The owner's 15 service definitions are
   byte-identical to their state before the trial; all their services kept
   the same processes.
6. **Suites:** Python 3.12 full suite 3,272 passed and 25 skipped; desktop
   tests 175 passed; native discard smoke passed. CI (`canonical-runtime`)
   passed on the pull request.

## Found during the trial

1. **Removal of a Hermes daemon whose provider or model changed failed**
   (`unconfirmed`). The app rewrites the host config with lists in a form the
   removal's text patch did not handle, producing invalid YAML. Present since
   0.11.16 for provider changes. Fixed (`68a7988`), with a test that fails on
   0.11.19.
2. **OpenClaw 2026.11.6 refused the app's configuration.** It retired
   `agents.defaults.heartbeat`; the gateway refused to start and the app rolled
   the installation back after 7 minutes. The key is no longer written
   (`eb7498a`). Checked on an isolated 2026.11.6 gateway: the configuration is
   valid and no periodic check is created, since that version creates one only
   through its own setup command, which Companion does not run. Hosts created
   with 2026.10.7 keep their setting. No test runs the real host against the
   configuration the app writes, which is why this was found only live.
3. **The app never confirmed an OpenClaw daemon's reply.** The live service
   writes a turn's reply to `state/continuity/shared`, the memory worker writes
   its memory stages to `state/receipts`; every reader read one directory. So a
   new OpenClaw daemon's first reply was never confirmed, a provider or model
   change on OpenClaw could never be kept, and health missed its memory stages.
   Present since the first public source. Readers now merge both directories
   by turn (`04828ca`); writers are unchanged, so existing daemons are covered
   without an update. Verified on the trial daemon's real receipts, then live
   by the kept model change above.
4. **Addition at the owner's request:** deleting an unfinished installation
   can also move its memory to the trash, as uninstalling a daemon can
   (`e738eb0`, `e3a9e29`): offered only for a memory folder the app created
   inside that installation, after a second confirmation. Covered by unit
   tests and the native discard smoke, which first caught a defect in it (the
   memory was kept); not run by hand.

## Limits

- The trial apps also listed the owner's real daemons; nothing was done on
  their cards.
- During criterion 2 the trial app was closed while it waited for the first
  reply; the installation was already committed.
- Not covered live: Windows, an attached host, a change refused or undone
  after no reply, Change model on an OpenClaw 2026.10.7 host.

## Decision

The owner validated the new texts. Merged on `main` (#27), for the next
Companion release. Existing daemons reach GPT-6.2 through **Change model**,
which installs Codex 0.170.0; existing OpenClaw daemons keep OpenClaw
2026.10.7, which does not list the GPT-6.2 models.
