# Set up your first daemon

A **daemon** is your personal AI assistant. **Daemons** is the software that
gives it a persistent identity, memory and continuity between conversations.
An **agent host**, currently Hermes Agent or OpenClaw, supplies its model
connection, tools and messaging channels. **Companion** is the desktop app
used to install and manage these components.

For example, the assistant can connect today's update about a repair with the
problem and earlier attempts you discussed last week. Remembering the right
context is the aim; it is not a guarantee that every detail will be recalled.

## Before you start

You need:

- A Mac with Apple Silicon or a Windows x64 computer.
- Internet access and a Telegram account.
- A supported ChatGPT account, or an OpenAI or Anthropic API key.
  An API key is a credential supplied by the model provider. Access and usage
  charges depend on that provider and account.
- Access to the GitHub repository while the alpha remains private.

Your computer runs the assistant. It must be awake and connected for replies
and background work. Windows tasks run in your signed-in user session.

## Install the desktop app

Use the [download command for your system](../README.md#install-the-app).
The command downloads, verifies and installs the app. It does not create a
daemon until you follow the setup steps inside the app.

The app is not yet signed with a recognized distribution certificate.
If your system blocks it, keep the exact message and
[report the problem](alpha-testing.md#report-a-problem). Do not disable your
computer's security protections.

## Create a daemon

1. Open Companion. English is the default; the language selector offers French.
2. In **My daemons**, choose **Install a daemon**, then **Hermes Agent**
   (recommended) or **OpenClaw**. Choose your daemon's name, its grammatical
   gender and the conversation language. The daemon learns how to address you
   in its first conversation.
3. Choose your model connection. The ChatGPT subscription with the Astra model
   is recommended. With an OpenAI or Anthropic API key, paste it into the masked field and
   select an available model. The app checks access before continuing. Which models are
   offered, and why, is listed in [Model providers](model_providers.md).
4. Follow the Telegram instructions on the computer. Find your Telegram user ID first, then create a dedicated bot with BotFather
   and paste its token into the app.
   The ID identifies the person allowed to use this daemon; it is not a password.
5. After Telegram verification, the app creates the daemon runtime, its memory
   folder and private services for your chosen host.
6. Open your bot's conversation and press **Start**. Your daemon introduces
   itself and, over its first replies, learns how to address you.
   Wait for both the reply and the app's completion check.

Both hosts offer ChatGPT subscription sign-in first, as the recommended access,
then API keys. The ChatGPT path uses
separate connections for the host's conversation and the daemon's memory
maintenance, and the app guides both. API-key setup does not require installing
Codex.

If Telegram Web's Start button does nothing, try **Telegram Web K** or search
for the bot's exact @username in Telegram.

## Understand what was created

An **installation** is a local copy of a host with its own configuration,
conversations and services. It may have a daemon attached to it. Creating
another installation does not reuse or replace an existing one.

The app opens on **My daemons**. Each daemon appears by name, with its host
underneath. Hosts using the same verified daemon runtime share one card; names
alone never merge daemons. Hosts without a daemon are listed separately.
On a Hermes Agent daemon, **Add a host** installs a private OpenClaw with its own
model connection and Telegram bot, sharing the same memory. The app cannot yet
add Hermes Agent to an OpenClaw daemon, attach a daemon to an arbitrary existing
host or change an installed daemon's model provider.

[Read the exact installation effects](installation_impact.md) or
[open your memory in Obsidian](memory.md).

## Return later or update the app

Reopen Companion to see your daemons. Each unfinished installation has its own
**Resume setup** action and diagnostic report. Viewing a completed daemon never
reinstalls it.

From r10, Companion updates itself: it checks shortly after opening and every
six hours, and **Settings > Check for updates** checks at once. An app older
than r10 has no updater: close it and rerun the same download command once.
Updating replaces the app, not your existing daemon or its memory.
A scoped daemon-runtime update is offered when required before adding a host.
General host and daemon updates are not yet available.

Next: [try the alpha and report useful feedback](alpha-testing.md).
