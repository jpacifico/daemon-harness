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

## Results

Pending.
