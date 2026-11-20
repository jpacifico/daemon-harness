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

Trial on macOS, 20 November 2026. Build of commit `55d70de`, run
unpackaged with an isolated data folder. One throwaway daemon on OpenClaw
`2026.10.7`, ChatGPT subscription, model `gpt-6-astra`, one throwaway Telegram
bot. The owner typed every message and answered every approval request.

| Criterion | Result |
| --- | --- |
| 1. Configuration | Pass. `tools.exec.mode: ask`; `execApprovals` enabled, direct messages, one approver equal to the owner in `allowFrom`. `exec-policy show`: `allowlist · ask on miss · fallback deny` |
| 2. Approval asked | Pass (probe A) |
| 3. Approved | Pass (probe A) |
| 4. Refused | Pass (probe B) |
| 5. No answer | Pass with a reservation (probe C): the Mac slept during the wait |
| 6. Ordinary conversation | Pass with a reservation: no approval request, no tool call, all answered; the after-turn worker failed transiently, then recovered |
| 7. Other daemons untouched | Pass. Every pre-existing service definition byte-identical; same processes running before and after; the only additions were the trial daemon's five services, removed at the end |
| 8. Suites | Python 3.12: 3229 passed, 25 skipped; desktop 167/167. CI not run yet |

### Timeline of the command probes

Times are local, from the gateway log and the session database (tool names and
statuses only).

| | Probe A (accepted) | Probe B (refused) | Probe C (no answer) |
| --- | --- | --- | --- |
| `exec` call | 17:19:23.8 | 17:21:08 | 17:23:02 |
| Approval request sent | 17:19:26.6 | 17:21:12.1 | 17:23:04.1 |
| Owner's answer | 17:19:40.4 | 17:21:13.6 | none |
| Command | ran; probe file created 17:19:41 | not run: `failed`, no exit code, 0 ms | not run: `failed`, no exit code, 0 ms, at 17:57:43 |
| Probe file | present | absent | absent |
| Reply delivered | 17:19:45.6 | 17:21:20.2 | not delivered (below) |

After probe B, the next message was answered in 6.5 s without any tool call.

Probe C: the Mac went to sleep with its lid closed at 17:48:12, before the
30-minute expiry (17:53:04). OpenClaw settled the request at the first wake,
17:57:43, 34.7 minutes after it was sent; the command was not run. The model
reply that followed was cut during a few-second wake (`stopReason: error`, no
content) and its delivery failed at 18:19 while the network came back. Whether
a reply after an expiry is delivered on an awake computer was not observed.

### Ordinary conversation

| Message | Received → reply sent | Composition (received → context assembled) | Tool calls | Approval requests |
| --- | --- | --- | --- | --- |
| Plain message | 13.7 s | 1.6 s | 0 | 0 |
| Question about the first message | 3.5 s | 0.9 s | 0 | 0 |
| Web search question | 10.9 s | 0.6 s | 0 | 0 |

The subscription's hosted web search runs on the provider's side and leaves no
tool call in the session, so whether a search ran is not visible.

After-turn worker: the first turn completed; the second and third failed
four times each within seconds (`RuntimeError` in the memory step), then
completed on the worker's later retries (17:16 and 17:17); all turns,
including the probes, completed. During installation, the scene step had
received `429 Too Many Requests` from the ChatGPT backend three times, and
that scene job was quarantined. The worker records only the error type, so the
cause of the later `RuntimeError` is not established. These steps run through
the daemon's private Codex, not OpenClaw's `exec` tool.

## Observations outside the change

- **Leftover card after removing an OpenClaw daemon.** After the trial
  daemon was uninstalled with its OpenClaw, the app kept showing a card
  "Installed files retained" with no action. The setup list read the removal
  record only for daemons created on Hermes Agent; OpenClaw daemon removal,
  added on 28 October 2026, writes the same record. Fixed separately on branch
  `companion/openclaw-removal-card`.
- **Sleep.** OpenClaw's approval expiry and the reply after it depend on the
  computer being awake; a sleeping Mac delays both.
- **Approval scope.** The approval covers OpenClaw's `exec` tool. File tools of
  the `coding` profile are a separate permission and were not exercised.

## Decision

The exit criteria pass, with the reservations above. The change is ready for
review and merge; Windows is covered by unit tests only.
