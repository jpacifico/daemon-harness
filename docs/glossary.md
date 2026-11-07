# Developer glossary

This page maps internal names to their meaning. User guides prefer the
plain-language description.

| Term | Meaning |
| --- | --- |
| Daemon | The user's personal AI assistant, including its identity, memory and continuity. |
| Agent host | Hermes Agent, OpenClaw or another supported program supplying the model, tools, sessions and channels. |
| Companion | The desktop installation and management app, not the daemon itself. |
| Installation / instance | One local host environment with its own configuration and services, optionally connected to a daemon. |
| Channel | A way to exchange messages, such as Telegram. A host can support more than one. |
| Model provider | The service or local server running the language model. |
| PARGA | Projects, Areas, Resources, Garden and Archives: the five user-visible memory folders. |
| Garden | The PARGA folder for the user's own ideas, principles and lessons, kept apart from the facts of their life so they can serve elsewhere; a Garden note grows each time the user returns to the idea. |
| Memory note / souvenir | A Markdown document holding sourced personal context. “Souvenir” remains in some code identifiers. |
| Capsule | A legacy name for a memory note, retained in older paths and reports. It is not a separate new memory type. |
| Vault | The memory folder opened in Obsidian. |
| L0 | The exact conversation journal: original messages recorded without summarizing or rewriting them. |
| After-turn maintenance | Work done after the response to retain useful facts and refresh working context. |
| Worker | A background process that consumes queued maintenance tasks. |
| Outbox / spool | A durable queue of work references. It is not another store of personal facts. |
| Scene / Scene Model | A revisable working interpretation of the current situation, with evidence and uncertainty. |
| VoiceBrief | The context assembled for the host model: identity, relevant memory, situation and dialogue. |
| Radar | An internal model judgment that identifies relevant subjects or maintenance needs; it is not an extra conversational agent. |
| MemoryAgent | The internal faculty that judges factual updates to personal memory. |
| Temporal Curator | The faculty responsible for the meaning of entries in the personal agenda. |
| Dreaming | Background consistency and organization maintenance of memory. |
| REM | Sourced retrospective writing about past episodes. |
| Atlas / index | A rebuildable map of memory used to find notes. It does not replace their exact content. |
| Embedding | A numerical representation used to rank semantically related notes. |
| Receipt | A technical record that a processing step occurred; it is not proof of a personal fact. |
| Watermark | The last successfully processed position in an ordered journal or time interval. |
| Canary | A restricted, reversible trial. Some older configuration values retain this name after a feature's validation. |
| Rollback | Restoring the preceding software or configuration after a failed change. It does not necessarily delete newly created data. |
| Canonical path | The shared, supported processing path whose behavior the project aims to preserve across hosts. |
