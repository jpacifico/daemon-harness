# How Daemon Harness works

A daemon is a personal AI assistant whose identity and memory can continue
across conversations and changes of model or host.

The **host** is the program that gives the assistant its tools, model connection
and messaging channel. Daemon Harness currently integrates with Hermes Agent and
OpenClaw. The **Companion app** installs and manages local installations.

## During a conversation

1. The host receives your message, for example from Telegram.
2. Daemon Harness prepares context from the assistant's identity, the recent
   conversation, its current situation and relevant memory.
3. The host's model responds. It can read more memories or use the host's
   normal tools when needed.
4. The host delivers the response.

There is one conversational voice. Daemon Harness does not place a second assistant
in front of the host to rewrite every reply. On the standard path, preparing
the initial context does not require an additional model call.

For example, if you mention an appointment, existing notes may connect it with
an unresolved problem. The assistant should use that context without reciting
your entire history, and should distinguish remembered facts from guesses.

## After the response

The system records the exact exchange and runs **after-turn maintenance**:
background work that decides which facts, intentions or corrections should be
kept. It can also update a working view of the present situation.

This work runs after delivery so it does not hold up the response. If it fails,
the system keeps enough technical state to retry or report a recoverable
problem. A working chat alone does not prove that every maintenance task succeeded.

## Between conversations

**Dreaming** is the name for background memory maintenance: checking consistency,
repairing supported issues and organizing eligible notes.

**REM** is the name for retrospective writing: producing a sourced account of
a past episode when there is enough material. It does not establish what is
true now just because it described something in the past.

Neither term means the assistant is conscious or asleep. Which operations
run depends on the installed version and configuration.

## How information is found

The [five memory folders](memory.md) make notes understandable to people.
Links, metadata and a search index help the assistant find related material.
A local embedding model can also compare meanings when the wording differs.
An **embedding** is a numerical representation used for search, not a new fact
or a substitute for the original note.

The exact Markdown note and the original exchange remain the sources to check.
A search result is a lead; an empty search is not proof that something never happened.

For implementation terms such as L0, worker and scene, see the
[developer glossary](glossary.md). Users do not need those terms to install the app.
