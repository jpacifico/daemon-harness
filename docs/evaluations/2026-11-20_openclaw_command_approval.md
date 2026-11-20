# Owner approval for OpenClaw host commands

Date: 20 November 2026. Branch `openclaw/exec-approvals`, from `main` at
`1806a9c`.

## Terms

- **Host**: the program that runs the daemon's conversation, Hermes Agent or
  OpenClaw. Companion installs a private copy of it for each daemon.
- **Host command**: a shell command that the host runs on the computer when the
  model calls its `exec` tool.
- **Approval**: OpenClaw's own mechanism that holds a host command until a named
  person accepts or refuses it. Companion only writes its settings; OpenClaw
  owns the prompt, the decision and the execution.

## Starting point

A daemon that Companion creates on OpenClaw `2026.10.7` runs host commands
without asking anyone: `openclaw exec-policy show` reports
`full · no approval prompts`. On Hermes Agent, Companion already writes
`approvals.mode: manual`, so Hermes asks before the commands it detects as
dangerous. OpenClaw had no equivalent setting.

The owner classified the change as a defect fix under the V0 freeze
(20 November 2026): it restores, on OpenClaw, an approval that Companion
already requires on Hermes Agent.

## Change

`native_config` writes, for every new OpenClaw host:

- `tools.exec.mode: "ask"`;
- `channels.telegram.execApprovals`: enabled, the owner's Telegram user id as
  the only approver, prompts sent in direct messages.

OpenClaw then reports `allowlist · ask on miss · fallback deny`: a command not
yet allowed waits for the owner; without an answer it is refused. Existing
daemons keep their configuration. Hermes Agent is unchanged. Changing the
model or provider keeps the setting (`provider_settings.py` copies only model
and web-search entries).

## Exit criteria (fixed before the trial)

1. **Configuration.** The trial daemon's `openclaw.json` holds the two values,
   with the owner's id as the only approver, and `openclaw exec-policy show` on
   its host reports `allowlist · ask on miss · fallback deny`.
2. **Approval asked.** Asked to run a shell command that creates a probe file
   in its workspace, the daemon causes an approval request in the owner's
   Telegram direct messages. While the request waits, the probe file does not
   exist.
3. **Approved.** After the owner accepts, the command runs (the probe file
   exists) and the daemon answers.
4. **Refused.** For a second probe file, after the owner refuses, the file does
   not exist, the daemon tells the owner the command was not run, and the next
   message is answered normally.
5. **No answer.** For a third probe file, left unanswered past OpenClaw's
   default expiry (30 minutes), the file does not exist.
6. **Ordinary conversation.** Three ordinary messages (a plain message, a
   question about something said earlier in the trial, a question needing a
   web search) produce no approval request and are answered; the after-turn
   worker processes them without error. Reply times are recorded.
7. **Other daemons untouched.** The owner's daemons and their services are
   identical before and after (service definitions hashed, processes running).
8. **Suites.** Full Python suite on 3.12 passes; CI is green.

Windows is covered by unit tests only and reported as such.

## Protocol

- Isolated state: a build of this branch runs unpackaged with
  `DAEMONS_COMPANION_DEV_BASE` set to a throwaway folder outside any temporary
  directory. The installed Companion app is closed during the trial, because
  both share the app's settings folder. The owner's daemons, their services
  and their memory are not touched.
- One throwaway daemon created on OpenClaw with the owner's ChatGPT
  subscription and a throwaway Telegram bot. The owner does the ChatGPT and
  Telegram steps and types every Telegram message.
- Observation from files and logs only: configuration values, file presence,
  tool names, statuses and timings. No message text is read or kept.
- Which tool the model chooses is recorded. A probe file written through a
  file tool instead of `exec` is reported as such, not counted as an approval
  result.
- Cleanup: the trial daemon is removed with the app; its folders go to the
  Trash.

No control arm is run: the change adds approval settings and leaves the
composition and the turns without host commands unchanged. Reply times are
reported as observed, without a comparison claim.

## Results

Pending.
