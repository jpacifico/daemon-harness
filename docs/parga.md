# PARGA: How Memory Is Organized

**English** · [Français](parga.fr.md)

A daemon's memory is a folder of Markdown notes (a simple text format) on your
computer. Each note is a **souvenir**: it keeps what the daemon knows about a
lasting subject in your life, such as a person, a project, a preference, a
situation or an idea, with its history and where each fact comes from.

**PARGA** is how these souvenirs are filed. The name takes the initials of the
five folders: **Projects**, **Areas**, **Resources**, **Garden** and
**Archives**. This filing draws in particular on PARA (Tiago Forte), IPCRA
(Eliott Meunier) and the Zettelkasten.

## Why organize memory

A conversation history lets you find what was said, but you have to rebuild
the story every time. A list of small isolated facts, on the other hand, loses
what connects them: the goal, the attempts, the people involved, the reason
something is stuck, the last confirmed state.

So a souvenir gathers the whole continuity of one subject. The note about a
repair keeps the fault, the attempts that failed, the part on order and the
latest news from the workshop. When you mention it again, the daemon reads
your message in the light of that story, without rereading its whole memory.

Filing into folders answers another question: where to look, and how you find
your way in your own memory. The folders are real folders that you can
browse, open and reorganize, for example in [Obsidian](https://obsidian.md), a
note-taking application that opens this folder directly.

## The five folders

| Folder | What it holds | Example | Not to be confused with |
| --- | --- | --- | --- |
| **Projects** | An outcome you are pursuing, which will end: its scope, decisions, dependencies and progress. | Planning a birthday; rolling out a tool in a team. | A suggestion from the daemon is not a commitment: it does not create a project. |
| **Areas** | What lasts without an end date: responsibilities and roles, people and relationships, preferences, situations you are going through. | A colleague; health; a repair waiting to be done. | A routine is not a project. |
| **Resources** | A specific reference to use later, such as a book, a podcast or a guide, with the reason you keep it. | A podcast to listen to before a negotiation. | Being here does not prove it was listened to or read. |
| **Garden** | An idea, principle or lesson that you formulated or adopted, and that could serve in a completely different part of your life. The note grows each time you come back to the idea. | "Try a change on a small pilot that can be undone before generalizing it." | A fact about your life belongs to the folder of the subject concerned; a reference (book, article, video) belongs in `Resources`; an idea the daemon suggested and you did not adopt is not kept. |
| **Archives** | What is finished or inactive, with its exact final state, still searchable. | A past trip, with what was decided and lived; the account of a past episode. | Archived does not mean successful: a cancellation, a pause and a success remain different states. |

Each folder can hold subfolders, which change with your needs (`Areas/Work`,
`Areas/Family`, `Projects/Home`…). Folder names are in English; the content of
the notes stays in the language of your conversations.

## Garden: the garden of your ideas

**The other folders file what happens to you; Garden grows what you think.**

`Garden` files your ideas: a principle, a conviction, a method or a lesson
learned from experience, that you formulated or adopted. As in a garden, what
you plant grows: the note is enriched each time you come back to the idea or
apply it elsewhere, and its history keeps your refinements and changes of
mind.

An idea does not belong to a single subject. You renovate your kitchen and
learn that it is better to measure everything twice before ordering. The
renovation goes in `Projects`, then in `Archives` once it is over. The lesson
goes in `Garden`: it stays available for a move or another purchase, long
after the renovation ended. Filed inside the project, it would have been
archived with it.

**The question to ask.** Could this note be useful in a completely different
part of your life? If so, it goes in `Garden`. If it describes a specific
situation, it goes in that situation's folder.

What does not go in:

- **a fact about your life**, however important: it belongs to the project or
  area concerned. "My sister means a great deal to me" describes a
  relationship and goes in `Areas`; "a decision is better made after a night's
  sleep" is an idea that can serve anywhere and goes in `Garden`;
- **a reference** (book, article, video): it goes in `Resources`. An idea from
  it that you adopt can have its own Garden note, linked to that reference;
- **an idea from the daemon** that you did not take up.

If the idea itself becomes a commitment, for example you decide to run the
pilot it described, the note moves to `Projects`. The daemon does not rework a
Garden note by itself between conversations, and it does not invent ideas to
fill the folder.

## One subject, one note, links

Each souvenir has **one location**. A person involved in two projects keeps a
single note, in `Areas`, and each project refers to it with a link. Links are
written as in Obsidian, `[[Areas/Work/Nora]]`, followed by a sentence saying
why the two notes are connected. **MemoryAgent**, the after-reply judgment
that maintains souvenirs, links only notes that exist and
does not add links to fill a graph.

A folder name proves nothing. Belonging to the same household does not mean
being present today; a person's tastes stay attached to that person. Any
relationship between two subjects is written in a note and backed by what you
said.

## Anatomy of a souvenir

A souvenir starts with a few properties, which the daemon uses to find it,
then a text organized in sections. Here is a project note and the Garden note
it links to.

````markdown
---
memory_id: theme/work/orion
kind: project
memory_areas:
  - work
subjects:
  - orion
facets:
  - rollout
aliases:
  - Orion pilot
---
# Orion rollout

## Scope
Roll out Orion to the support team, starting with a pilot.

## Last confirmed state
On 2026-01-12 the pilot is approved; its launch date is still to be confirmed.

## History
- 2026-01-08: a global launch was considered.
- 2026-01-12: the team chose a pilot to limit the risk of disruption.

## Still unknown
- Confirmed launch date.

## Links
- [[Areas/Work/Nora]]: in charge of following the pilot.
- [[Garden/Work/Small reversible pilots]]: the principle behind choosing a pilot.
````

````markdown
---
memory_id: theme/work/small_reversible_pilots
kind: knowledge
memory_areas:
  - work
subjects:
  - pilots
facets:
  - risk
---
# Small reversible pilots

## Scope
A working principle adopted by the user: try a change on a small scope that
can be undone before generalizing it.

## History
- 2026-01-12: adopted while choosing the Orion pilot: "From now on I'll
  always start with a small pilot we can roll back."

## Still unknown
- Whether the user also applies it outside work.

## Links
- [[Projects/Work/Orion rollout]]: where the principle was adopted.
````

| Property | Role |
| --- | --- |
| `memory_id` | The souvenir's identifier, set when it is created. It does not change when the file is renamed or moved. |
| `kind` | The nature of the subject: project, area, situation, person, group, organization, place, object, resource, knowledge or episode. |
| `memory_areas` | The areas of life concerned, for example `work` or `health`. |
| `subjects`, `facets` | Labels that help the index find the souvenir. They prove no relationship. |
| `aliases` | Other names for the same subject. |

The sections say what is known and since when: the **scope**, the **last
confirmed state** and its date, the **history** of steps, what **is still
unknown** and the **links**. A section appears only when it has content.
Maintenance adds to the history instead of replacing it with a summary: it
should always be possible to understand how things got where they are.

Dates are written `YYYY-MM-DD`, or `YYYY-MM` and `YYYY` when nothing more
precise is known. The date of an event, the date it was mentioned and the date
the file was changed are three different things.

These properties and links are Obsidian's own: the memory can be read and
edited there without any plugin.

## Who files, and how a souvenir changes folder

After each exchange, MemoryAgent decides whether to create a note, complete
one or correct it. When it creates a note, it chooses its folder. It does not
create a note per exchange: many messages bring nothing lasting.

A souvenir can change folder, but only after something new you said justifies
it. MemoryAgent then quotes your exact words and explains the reason for the
move. Two typical cases:

- a project you say is finished or abandoned moves to `Archives`;
- an idea in `Garden` that becomes a concrete commitment moves to `Projects`.

During a move, the `memory_id` does not change, and the links that other notes
make to the moved note are updated to its new location. Nothing is archived
because it is no longer mentioned: silence does not resolve an open problem,
and an archived souvenir remains readable and searchable.

You can also file, rename or edit your notes yourself. The daemon keeps the
location you chose and finds the note by its `memory_id`.

## Why there is no "to sort" folder

Many note-taking methods start with an inbox, a folder where everything is
dropped before being sorted. PARGA has none, because nothing would be lost
without it:

- **L0**, the raw level of the conversation history (*Level 0*, also called
  *Ground Zero*), keeps every message word for word;
- pending maintenance work stays in a queue until it is done, even after a
  restart.

So an ambiguous piece of information remains an open question, and a failure
remains work to resume; neither becomes an invented fact just to be filed.

## Further reading

- [The daemon at a glance](overview.md): how memory is read before each reply
  and maintained after it.
- [Memory reference](memory_structure_developer_guide.md) (in French): formats,
  provenance, write protections and the code involved.
- [Your memory](memory.md): opening and managing memory in Obsidian.
