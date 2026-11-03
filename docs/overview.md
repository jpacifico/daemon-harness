# The Daemon At A Glance

**English** · [Français](overview.fr.md)

## The idea

A **daemon** is a personal AI assistant that remembers you from one
conversation to the next. It does not run on its own: it is installed on a
**host**, a conversational agent program such as Hermes Agent or OpenClaw.
The host provides the **model** (the large language model, or LLM, that writes
the replies), the **channel** (for example Telegram), sessions, progressive
display of replies and its own **tools**, for example web search or file
access.

The daemon brings what the host lacks:

- a **stable voice**: a name, a personality and a way of conversing;
- a **personal memory**, written in Markdown (a simple text format) in files
  you can read and edit;
- a **reading of the present situation**, updated after each exchange.

The daemon never speaks in the host's place. It prepares what the model reads
before replying, gives it two tools to consult memory, then maintains that
memory after the reply.

```text
Host (Hermes Agent or OpenClaw)
  channel · session · model · display · external tools

Daemon
  identity · Markdown memory · present situation · maintenance
```

## Words to know

| Term | What it is |
| --- | --- |
| **L0** | The raw level of the conversation history: every message, yours and the daemon's, copied word for word with its date. L0 stands for *Level 0*; it is also called *Ground Zero*. Nothing in it is summarized or rewritten. |
| **Souvenir** | A Markdown memory note that keeps a durable personal fact: a person, a project, a preference, a situation. Each souvenir records where what it states comes from. |
| **Scene** | A working note that describes the present situation: what is established, what is only assumed, what remains uncertain, and upcoming deadlines. It is rewritten after each exchange and cites its sources. In the code it is called the *Scene Model*. |
| **Souvenir index** | A local search engine that finds the souvenirs closest to the subject of the conversation. It compares *embeddings*, numerical representations of the meaning of a text, computed on your computer. |
| **After-turn** | The memory maintenance done right after each reply, without making the conversation wait. Tasks wait in a **queue** and are handled by a **worker**, a separate program running in the background. |
| **Radar**, **Temporal Curator**, **MemoryAgent** | The three after-turn judgments, made by a model. The Radar rereads the exchange and decides what maintenance it deserves; Temporal Curator keeps the personal agenda; MemoryAgent creates or completes souvenirs. They are described in detail below. |
| **Dreaming**, **REM** | The two night-time maintenance tasks. Dreaming rereads and corrects recent souvenirs; REM writes accounts of past episodes. These are task names, not additional agents. |

## One complete turn

```text
YOUR MESSAGE
        │
        ├──→ exact copy into L0
        │
        └──→ LOCAL PREPARATION, NO MODEL CALL
                │
                ├─ main instructions
                │    identity, voice and way of conversing
                │    stable personal reference points
                │    rules of the turn and today's date
                │    the scene (or, without one, a starting point)
                │    exact excerpts from previous days
                │    account of a relevant past episode, if any
                │    agenda and recent facts not yet in the scene
                │    souvenirs already open
                │    12 souvenirs suggested by the index
                │    how to use the host's tools
                │
                ├─ latest exact messages (up to 24)
                ├─ final reminder: decide, check, never bluff
                └─ your message
                        ↓
                    HOST MODEL (LLM)
                        │
                        ├─ it knows enough → it replies
                        ├─ it needs a souvenir → daemon_read_souvenirs
                        ├─ it needs something said before → daemon_recall_l0
                        └─ it needs anything else → a host tool
                                ↓
                            back to the same model
                        ↓
                    VISIBLE REPLY
                        │
                        ├──→ exact copy into L0
                        └──→ after-turn queue
                                │
                                ├─ Radar, for each message
                                ├─ Temporal Curator if a date matters
                                ├─ MemoryAgent if a fact is worth keeping
                                ├─ possible follow-up to schedule
                                └─ new scene
```

Nothing stands between your message and the host's model: no program that
picks the reply in its place, no second model that rewrites it, no model call
before the first word. The preparation only reads files and assembles them;
the costly work comes after the reply.

## Before the reply: what the model reads

**Who is speaking.** The daemon's name and grammatical gender are chosen at
installation. Its personality (`persona.md`) and its way of conversing
(`conversation.md`) are files you can edit.

**What stays true.** `context.md` keeps a few stable personal reference points
that accompany every exchange.

**What is happening now.** The scene describes the present situation. When
there is none yet, for example at the first message, a simpler starting point
takes its place: the current context, the agenda and the state of the
conversation, presented as dated and revisable. The agenda and the facts that
arrived since the last scene are added next to it, and they prevail when they
disagree with it.

**What was said.** The latest 24 messages are taken word for word from L0,
with short exact excerpts from previous days. On OpenClaw, which keeps its
own history, the daemon adds only the messages missing from it.

**What can be remembered.** The index suggests at most twelve souvenirs
related to the subject. Those the scene already considers useful are included
in full. If the index also finds an account of a past episode written by REM,
it is included as a memory of the past, never as a description of the present.

**How to decide.** Just before your message, a reminder asks the model to
consider the whole situation before deciding, to open evidence when a fact is
missing, and never to pretend to know what it has not checked.

## During the reply: two memory tools

The daemon adds two tools to the host's:

- `daemon_read_souvenirs` opens up to eight souvenirs in full;
- `daemon_recall_l0` finds an exact past message in L0 when souvenirs are not
  enough.

These two tools replace the host's own memory, whose tools are hidden. On
Hermes, a web page extraction tool with no configured backend is hidden too,
because every call would fail. Every other tool is the host's, whatever it
is: what the model can look up or do beyond memory depends on the host and
its configuration, not on the daemon, which neither limits nor orders them.
It sets only one rule: an empty, blocked or ambiguous result proves nothing.

## After the reply: maintenance

Each message joins the after-turn queue. The worker handles it in order, even
if the host restarts in the meantime.

```text
for each message, in order
  → Radar: what does this exchange call for?
  → Temporal Curator: updates the agenda if a date matters
  → MemoryAgent: creates or completes souvenirs if needed

after a series of messages close together
  → possible follow-up to schedule
  → one new scene covering all these messages
  → souvenir index updated if souvenirs changed
```

**The Radar** is the first judgment, made for every message. It never answers
the user and writes nothing itself: as a radar sweeps an area to flag what
needs attention, it rereads the exchange to decide which maintenance to start.
A model receives the message in its context: recent messages, the agenda, the
current context, stable reference points and the map of existing souvenirs
(their names and subjects, not their content). It answers three questions in
particular, and each answer drives what follows:

- **Does the exchange bring anything worth keeping?** If it is light, such as
  a simple agreement, thanks or the continuation of a thread with nothing new,
  MemoryAgent does not run. Otherwise MemoryAgent is started.
- **Which existing souvenirs are concerned?** The Radar points them out on the
  map, and MemoryAgent reads them in full before completing or correcting
  them, instead of creating a duplicate.
- **Is a date, deadline or routine brought or corrected?** If so, Temporal
  Curator updates the agenda.

This way only useful work is done, and every write starts from a reading of
the whole exchange rather than from isolated words.

A **follow-up** is a message the daemon may send later on its own, for example
to ask how an appointment went. The daemon decides whether and when; the host
schedules and delivers it.

Facts are always handled message by message. Only the scene, which is
entirely rewritten each time, is computed once for a series of messages. The
previous scene stays in place until the new one is ready.

Each file has a single owner. MemoryAgent writes souvenirs, but not the
agenda, `conversation.md` or L0. Temporal Curator alone writes the agenda. An
automatic clean-up moves past deadlines to an archive without claiming they
took place.

## At night: Dreaming and REM

**Dreaming** rereads the souvenirs written during the last 48 hours, compares
them with the exact messages they come from, and carefully corrects them or
merges duplicates. It does not touch the agenda.

**REM** writes accounts of past episodes and stores them in
`Archives/Episodes`. The index can find these accounts, but they stay dated:
their interpretations never become current facts.

## Where the memory lives

```text
<memory folder>                   open it in Obsidian
├── Projects/ Areas/ Resources/   the souvenirs, sorted into five folders
├── Garden/ Archives/             (REM writes to Archives/Episodes)
└── .daemon/                      internal workings, do not delete
    ├── core/daemon_identity.json the daemon's name and gender
    ├── levels/L0_ground_zero/    L0: the exact history of exchanges
    └── users/<id>/               context.md, agenda.md, current context,
                                  scene and state of the conversation
```

The five folders follow a filing method called **PARGA**: *Projects* (ongoing
projects), *Areas* (responsibilities, people and situations followed over
time), *Resources* (material worth keeping for later), *Garden* (ideas and
lessons that can develop) and *Archives* (what is finished, still
searchable). [Obsidian](https://obsidian.md) is a note-taking application that
opens this folder directly. [PARGA: how memory is organized](parga.md)
explains each folder, the anatomy of a souvenir and where the method comes
from.

Companion, the application that installs the daemon, creates this folder for
each daemon and shows its location. The Markdown files are authoritative. The
index, its embeddings and the scene can be recomputed from them: they help
find and understand, but prove nothing.

## What counts as evidence

| Element | What it proves |
| --- | --- |
| Your current message | What you state or ask now. It always prevails. |
| L0 | What was said, exactly. |
| Open souvenir | A durable personal fact, within its scope. |
| `agenda.md` | A commitment or deadline you entrusted, not that it took place. |
| Host tool | What its result contains, at the moment it was obtained. |
| Scene | A sourced, revisable reading of the situation. |
| Souvenirs suggested by the index | Nothing: they are leads to open. |
| The daemon's previous reply | Only what the daemon said, unless you confirmed it. |

## If something is missing

- If preparation fails, the host still replies, with its own context and an
  instruction not to assert any personal fact absent from the visible
  dialogue. The reply then starts with a notice saying that personal memory
  was unavailable.
- Without a scene, the model replies from the simple starting point described
  above.
- If the index does not respond, the full list of souvenirs replaces it, and
  the incident is logged.
- If maintenance fails, the work stays in the queue, pending or failed with
  its diagnosis, and can be resumed. The conversation is neither blocked nor
  interrupted by a technical message.

## Further reading

- [Architecture](architecture.md): each component, each step of a turn and
  each rule, in detail.
- [Memory reference](memory_structure_developer_guide.md) (in French): formats,
  provenance and life cycle of souvenirs.
- [Configuration](configuration.md): locations, identity and settings.
- [Glossary](glossary.md): the project's other terms.
