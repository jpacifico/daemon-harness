# Trying the alpha

The alpha is for checking the experience on real computers and accounts.
It is early software: a successful build or an automated check is not a promise
that every provider and operating-system combination has been exercised.

## What to try

1. Install the app using the [README command](../README.md#install-the-app).
2. Create a new daemon on Hermes Agent or OpenClaw, with a dedicated Telegram bot.
3. Connect your chosen model and exchange a first message.
4. Share a harmless fictional preference or situation. Later, refer to it
   naturally and see whether the daemon brings back the right context.
5. Open the memory folder and check whether the retained information is accurate.
6. Close and reopen Companion. Check that it recognizes the completed installation
   and that its controls describe their effects clearly.

Do not use an existing important assistant or irreplaceable information as a
disposable test. Keep a backup of any memory you care about.

## Current scope

| Capability | Status |
| --- | --- |
| Desktop app | Mac Apple Silicon and Windows x64; English and French |
| Guided new installation | Hermes Agent or OpenClaw, each with a dedicated Telegram bot |
| Model connections | ChatGPT subscription; OpenAI and Anthropic API keys |
| Add a host | Adds OpenClaw to a Hermes Agent daemon created by Companion, sharing its memory |
| Add Hermes Agent to an OpenClaw daemon | Not yet available |
| Attach a daemon to any existing host | Not yet available |
| App updates | From r10, from the app (**Settings**); older apps rerun the download command once |
| Host or daemon updates | Only the scoped runtime update before Add a host |
| Remove a daemon | Hermes Agent daemons created by Companion, on Mac and Windows; memory is kept unless deletion is chosen and confirmed twice (it then goes to the Trash) |
| Uninstall a completed OpenClaw-only daemon | Not yet available |
| Back up and move memory | Encrypted backup to a file or Google Drive, manual or daily; restore into a new daemon as a copy or a move |
| Background mode | Menu bar or notification area icon when the window is closed; optional open at login |

Unfinished new installations can be deleted on both systems while keeping
memory. r10 was qualified on macOS with automated suites, live isolated daemons
on both hosts and the regression screen of the quality recipe. On Windows, the
maintainer confirmed the in-app update and a complete new OpenClaw daemon with the
c25 and c26 candidates, whose code r10 ships.
0.11.0 adds backups, restore and background mode. A backup restored on the
other host answered with its memories in a live trial on both hosts; the real
Google sign-in, the tray icon and Windows were not checked before release.

## Report a problem

Open a [GitHub issue](https://github.com/jpacifico/daemon-harness/issues)
with:

- Your operating system and architecture.
- The app release, if known, and the step or screen involved.
- What you did, what you expected and what happened.
- The exact error or a cropped screenshot with private information removed.

Do not attach API keys, bot tokens, sign-in files, the whole memory folder or
raw conversation logs. A fictional example is often enough to reproduce a problem.
For a security concern, use the process in [SECURITY.md](../SECURITY.md).

## Updating or returning to the previous app

Close Companion and rerun the same README command to replace it.
The maintainer keeps the preceding release and its installation scripts so the
download can be reverted after a regression. Replacing Companion does not
delete memory or automatically roll back a daemon created in the meantime.

Repository preparation and alpha testing do not themselves make the repository
public. Opening access is a separate publication decision.
