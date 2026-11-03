# Continuity Benchmark

Protocol version: `1.1.0`

Status: version 1.1.0 specified on 31 October 2026, before any 1.1 run.
Both versions ran on 31 October 2026 and are on `main` since 1 November 2026.

| Version | Campaign | Code | Report | Outcome |
| --- | --- | --- | --- | --- |
| 1.0.0 | `continuity-1.0.0-20261031` | tag `continuity-1.0.0` | [report](evaluations/2026-10-31_continuity_benchmark_1_0.md) | no improvement claim (8 vs 10 of 24); capabilities kept (12/12 both sides) |
| 1.1.0 | `continuity-1.1.0-20261031b` | tag `continuity-1.1.0` | [report](evaluations/2026-10-31_continuity_benchmark_1_1.md) | no improvement claim (10 vs 12 of 24) |

Records of both campaigns: [`benchmarks/continuity/records/`](../benchmarks/continuity/records/README.md).

Naming: the unit is a **scenario**. The code of both tags called it a "life"
(`LIVES`, `lives.jsonl`); the rename afterwards changed no scenario, message,
criterion or instruction, and the published scenario file is byte for byte the
same.

Language: English

The Continuity Benchmark asks one question: **does the daemon make Hermes a better personal
assistant across conversations, without making it a worse agent?** It compares
the same Hermes, with the same model and the same tools, with and without the
daemon. Every claim it can support is fixed below, before its results are
known.

It replaces nothing. The [Public Productivity Benchmark V1](44_public_productivity_benchmark_v1.md)
and the [Useful Continuity Benchmark v0.1](../benchmarks/useful_continuity/README.md)
stay as they are, with their own campaigns. It reuses V1's two native
tasks and their checkers, and learns from UCB v0.1: its useful-context and
counter-cases, and its finding that knowing the context is not the same as
acting usefully on it. Its corpus is new, so it is not fitted to cases already
seen.

## Version 1.1: the final conversation runs over several turns

Version 1.0 judged a single reply: the answer to the opening message of the
final conversation. An assistant that first asks a question it needs could
never show what it does with the answer. The owner asked that the
personal-assistant part of 1.0 be replayed with continuity judged over several
turns, and nothing else. Version 1.1 changes the following, and nothing else.

1. **The same 24 scenarios.** The scenarios, their messages and their criteria are
   those of 1.0, unchanged; 1.0's useful next step becomes `outcome`, judged at
   the end of the final conversation. Each scenario gains a `person` sheet for the
   simulated person (`benchmarks/continuity/corpus.py`). Nothing in the daemon
   or in Hermes changed between the two versions, so reusing the scenarios fits
   nothing to them, and the comparison isolates the effect of the final
   conversation's turns.
2. **Final conversation.** It opens with the scenario's final message, written
   without restating anything. After each assistant reply, a **simulated
   person** answers, up to three person messages in all, or ends the
   conversation once they have what they wanted. Every message of the final
   conversation goes to the same Hermes session.
3. **The simulated person** is a separate, isolated Claude Opus 5.5 call
   (Claude subscription, effort `medium`, no tool, no MCP server, no project
   setting, no saved session, an empty working directory). It receives the
   published instructions (`simulator_instructions.md`), what the person told
   the assistant before, the scenario's `person` sheet and the final conversation so
   far. It never receives the criteria or which system it is talking to. The
   `person` sheet says what the person wants and what they can say if asked:
   details never given before, so asking for them is legitimate. The simulated
   person does not repeat earlier facts unless asked, never adds a fact that
   its sheet lacks, and may correct the assistant. Every raw answer is kept.
4. **Judged as a whole.** The judges read the earlier messages and both
   versions of the final conversation. Per scenario and system:
   - **Remembers**: every earlier fact is relied on before the person restates
     it;
   - **Asks again**, **Outdated**, **Invents**: in any assistant reply;
   - **Concrete help by the end**: the help the scenario describes is there by the
     end of the final conversation. A question whose answer the person's
     messages did not hold is not a fault;
   - **Reply that first gives that help**: its number in the final
     conversation (1 for the first reply); reported as a median over scenarios
     with help, taking for each scenario the later of the two judges' numbers.
   A scenario passes when it remembers, asks nothing again, applies nothing
   outdated, invents nothing and gives the help by the end.
5. **Agent capabilities are not rerun.** Version 1.0 measured them with the
   same Hermes, model and daemon (12 of 12 native tasks on both sides). The 1.1
   report does not restate them as its own result.
6. **Claim threshold.** Unchanged: "Improves" needs C − A of at least 6 of 24
   scenarios passed, a 95% paired bootstrap interval that excludes zero, Invents
   and Outdated not higher for C, and C preferred more often. The token rule
   is unchanged.
7. **Engineering gate.** Before scored episodes, one engineering scenario per
   system must complete with the canonical voice, every after-turn finished,
   no daemon token before the reply, and at least one answer from the
   simulated person, a message or the end of the conversation.

Unchanged: the two systems and their versions, the model and its effort, the
daemon's fresh Companion profile, sessions and isolation, the blinding, the two
judges and both presentation orders, the rule that a criterion passes only
when both judges agree, and the full-turn time and token measures on both
sides.

Known limits of 1.1, stated before the run: the simulated person and the judges
are the same model family, Claude Opus 5.5; the scenarios and person sheets were
written by Claude Opus 5.5 too; the final conversation is capped at three
person messages; the scenarios were already seen in 1.0.

A first draft of 1.1 used 36 new scenarios and a threshold of 9 of 36. Its
campaign (`continuity-1.1.0-20261031-abandoned-36`) was stopped after its
engineering episodes and one scored scenario, whose replies were not read, when
the owner pointed out that only the 1.0 personal-assistant part was to be
replayed. Its corpus stays in the history at `00bf25c3`; none of its episodes
counts.

## What is compared

Both arms run Hermes `0.21.5` (`f97608f`), the version Companion installs, in a
fresh, separate, disposable installation. Neither arm uses an existing
installation, profile, memory, channel or credential store.

| | A — Hermes alone | C — Hermes with the daemon |
| --- | --- | --- |
| Voice model | ChatGPT subscription, `gpt-6-astra`, reasoning `medium` | same |
| Memory | Hermes native memory, user profile, session search, skill learning and background review, all enabled | the daemon, installed with the settings Companion applies (native memory, profile, skill nudges and background review off) |
| Daemon internal calls | none | `gpt-6-astra`, Companion's defaults: routing `medium`, conversation `medium`, memory `high` |
| Tools | Hermes CLI tools | same catalogue, checked by hash |
| Other host settings | Hermes defaults, agent reasoning `medium` | same |

In C, each episode starts like a fresh Companion daemon: an empty memory vault,
Companion's fixed voice templates, the daemon identity "Aster" (feminine), and
the `parga-v1` continuity profile Companion writes for new installations.

This is the configuration Companion recommends (ChatGPT with Astra). The daemon
is the `main` revision named in the campaign manifest. Nothing in either arm is
tuned for a case: the daemon receives no procedure for a scenario, and no
remembered fact is written by hand. Each arm builds its memory only from the
same ordinary user messages.

### Known differences from a real installation

- The conversation goes through the Hermes command line, not Telegram: every
  platform scope, including the canonical voice's implicit "telegram" default,
  also names the command line so the same faculties apply. The engineering run
  checks that the canonical voice actually answered and that the daemon made no
  call before the reply.
- Follow-up messages are off: there is no channel to deliver them.
- Nightly Dreaming and the local embedding search server are not run. A scenario's
  memory holds only what its four conversations created; whether the embedding
  selection would change anything at that size is not measured.
- There is no first-meeting welcome: the person starts with their own subject.

## Two parts

### Part 1 — Personal assistant (where C must be better)

Twenty-four synthetic **scenarios**. A scenario is one invented person talking to their
assistant over four separate conversations (four new Hermes sessions):

1. **Telling.** Two or three ordinary messages that give facts: people,
   constraints, plans, preferences, worries.
2. **Elsewhere.** Three or four messages on unrelated everyday topics, long
   enough that the facts leave the voice's verbatim window (about 6 000
   characters of dialogue).
3. **Change.** One or two messages that update the situation: a correction, a
   cancellation, a task done, a new date. Some scenarios have no change.
4. **Asking.** One scored message, written as a person would, without restating
   what they said before.

Both arms receive exactly the same user messages in the same order; their
replies come from their own normal paths. All sessions of one scenario run within
one calendar day, so "tomorrow" or "on Friday" keeps one meaning; the campaign
does not simulate the passage of days.

Six families, four scenarios each: three where earlier context must be used, one
**counter-case** where earlier context must **not** be applied.

| Family | What the scored reply must show |
| --- | --- |
| People and constraints | uses who is involved and their constraints without asking again |
| Ongoing work | resumes at the right point: what is pending, what is settled |
| Time and commitments | respects an upcoming commitment, a one-off exception to a routine, a recent change |
| Corrections | uses the latest corrected fact, not the earlier one |
| Not inventing | says it does not know something it was never told, instead of making it up |
| Relationship | follows up, briefly and appropriately, on something that matters to the person |

Counter-cases cover, for example, a constraint that belonged to someone who has
left, a decision that applied to another project, or a resolved worry that must
not be reopened.

### Part 2 — Agent capabilities (where C must not be worse)

V1's two implemented native tasks, `native.files.aggregate` and
`native.tools.extract`, six seeds each, one fresh episode per seed and arm. They
need no memory. Their checkers are deterministic and already validated: an
exact deliverable, unchanged inputs, actual access to every controlled source.

## Measures

Every measure is a count with a fixed rule, published per arm. There is no
combined score.

### Part 1 measures, per scored reply

Each scenario comes with written criteria, fixed in the corpus before any run:

- **must use**: the earlier facts the reply must rely on;
- **must not**: what the reply must not do (apply stale context, reopen a
  settled point, recommend redoing finished work);
- **no repeat**: what the reply must not ask again;
- **unknown**: for "Not inventing" scenarios, the fact that was never given.

From them:

| Measure | Rule |
| --- | --- |
| **Remembers** | every "must use" fact is relied on |
| **Asks again** | number of questions about something the person already said |
| **Outdated or unrelated context** | number of replies that do something listed under "must not" |
| **Invents** | number of replies that state as known a fact that was never given |
| **Useful next step** | the reply gives a concrete action or answer the person can use now, unless the case requires a question first |
| **Scenario passed** | Remembers, no repeat question, nothing from "must not", nothing invented, useful next step |
| **Preferred** | in a blind side-by-side comparison, which reply sounds more like an assistant that knows this person and helps them |

### Part 2 measures

| Measure | Rule |
| --- | --- |
| **Native task passed** | the deterministic checker passes on the first attempt |
| **Tool errors** | failed tool calls, as reported by the host |

### Time and tokens, both parts

- reply time of each scored message, from sending it to the final text;
- time to the first visible text, when the host trace exposes it;
- the work after the reply, on both sides, timed separately: Hermes's own
  background review in A (its native memory and skill upkeep) and the daemon's
  after-turn in C. The person does not wait for it, but it is part of what each
  side costs, so the **full turn** (reply plus after-turn work) is reported for
  both;
- tokens per scenario and per native episode, split into the reply (the host's
  visible calls), the daemon's calls before the reply (expected: none) and the
  after-turn work (Hermes's background review, plus the daemon's after-turn in
  C), each with its source (`provider_reported`, `host_reported`,
  `tokenizer_estimated` or `unavailable`). A missing count is never zero.

## Who judges

Not the people or agents who built the daemon, and not this protocol's author.

1. **Code first.** Native tasks are checked by code only. In Part 1, every
   verdict that says a reply did or did not do something must cite an exact
   quotation from that reply; code checks that the quotation occurs verbatim.
   A verdict whose quotation is not found counts as unverified, and an
   unverified "passed" does not count.
2. **A blind judge from another provider.** Both arms answer with an OpenAI
   model, so an OpenAI model is not used to judge them. The judge is Claude
   Opus 5.5 (Anthropic), through a Claude subscription (no API key). Each call
   is one `claude -p` run with no tool, no MCP server, no project setting and
   an empty working directory; it receives only the published judge
   instructions and one batch of blinded packets. Claude Code itself adds its
   standard environment details (operating system, date, account address),
   which say nothing about the arms. A script replaces the arm names with
   random labels (`X`, `Y`) and shuffles the scenarios; the label key is opened
   only after every verdict is recorded. Each scenario is judged by two separate
   calls; a criterion passes only when both agree, and disagreements are
   published. The exact command is in `benchmarks/continuity/README.md`.
3. **Blind preference.** Each judging agent compares the two scored replies of
   a scenario twice, in both orders, without knowing which is which. A preference
   counts only if it survives the swap; otherwise it is a tie.
The judgment is entirely automatic: no person scores or reviews a reply. Its
safeguards are the code checks, the blinding, two independent judging agents
with published instructions, and the swap of presentation order. Agreement
between the two judges is published, and every disagreement is listed.

Amended on 31 October 2026, before any verdict, at the owner's request: the
first wording added a blind review by people; it is removed, and the
judgment is the automatic one above.

**Preview.** At the owner's request, the scenarios finished around mid-campaign
may be judged once as a preview, in a separate directory with its own label
key. The preview changes nothing in the campaign and never counts: the final
judgment draws new labels and judges all scenarios afresh.

Corrected on 31 October 2026, **after** the preview verdicts: the judge
instructions did not carry the rule this protocol set before any episode,
"unless the case requires a question first", for the useful next step. They
now say that a question counts as a useful next step when the reply could not
reasonably act without its answer, and does not count when the person's
messages already hold that answer. The owner asked for this after reading the
preview. Because the correction came after verdicts were seen, it is disclosed
here; the first preview, judged without the rule, is kept unchanged as
`preview-1-judging`, and the preview was judged again with the corrected
instructions. No criterion of any scenario changed.

## Results table

The report opens with this table, filled in exactly, followed by one row per
family and the complete side-by-side conversations of every scenario.

| | Hermes alone | Hermes + daemon |
| --- | ---: | ---: |
| **Personal assistant, 24 scenarios** | | |
| Scenarios passed | n / 24 | n / 24 |
| Remembers what was said earlier | n / N | n / N |
| Questions about something already said | n | n |
| Uses outdated or unrelated context | n | n |
| Invents a fact | n | n |
| Gives a useful next step | n / 24 | n / 24 |
| Preferred in blind comparison | n | n (ties: n) |
| **Agent capabilities, 12 tasks** | | |
| Native tasks passed | n / 12 | n / 12 |
| Tool errors | n | n |
| **Time and cost** | | |
| Reply time per message, median (90th percentile) | s (s) | s (s) |
| After-turn work per message, median | s | s |
| Full turn per message, median (90th percentile) | s (s) | s (s) |
| Tokens per scenario, median: reply / after-turn | n / n | n / n |
| Tokens per scenario, median: full turn | n | n |
| Tokens, whole campaign: ratio C / A | 1 | r |

Each denominator is the number of scenarios the row applies to, fixed in the corpus
(for example, "Remembers" counts only scenarios with earlier facts to use). Every
number links to its scenarios.

## What may be said publicly

The benchmark's results are published whatever they are. What they allow saying
depends on rules fixed now.

**"The daemon improves Hermes as a personal assistant"** only if all hold:

1. Scenarios passed: C − A is at least 6 of 24, and the 95% paired bootstrap
   interval of the difference excludes zero;
2. Invents: C is not higher than A;
3. Uses outdated or unrelated context: C is not higher than A;
4. Preferred: C is preferred in more scenarios than A.

**"It keeps Hermes's capabilities"** only if all hold:

1. Native tasks passed: C loses at most one task of twelve against A, and no
   task fails in all six seeds for C while passing for A;
2. Reply time on native tasks: median ratio C/A at most `1.15`.

**Token language.** The report states the token ratio C/A over the full turn,
after-turn work included on both sides, for the whole campaign and per scenario. Words such as "efficient" or "cheap" are not allowed unless the
ratio is at most `1.00`; otherwise the report shows the overhead and what it
buys.

When a rule fails, the report says which, and the claim is not made.

## Runs, order and exclusions

- One episode per scenario and arm, and one per native seed and arm: 48 scenario episodes and
  24 native episodes, plus one unscored engineering scenario and one engineering
  native task per arm, published as such.
- Arm order alternates from a public random seed; families are interleaved.
- Every episode starts from an empty memory, a fresh profile and fresh sessions.
  Arms never share a directory, cache, session or identifier.
- The campaign is spread over at least two time windows.
- No reroll. A wrong answer, a slow reply, a tool loop or a high token count
  after the host received the message is a **result**.
- An episode may be excluded only for: a wrong model or host version; a
  corrupted fixture; proven contamination between episodes; a runner failure
  before the host receives the first message; a provider outage confirmed by a
  health check run on both arms. Every exclusion stays in the published records
  with its reason and replacement.

## Isolation and credentials

The benchmark never touches a personal installation: not the owner's Hermes,
Companion daemons, memory or channels. Hermes is installed by Companion's own
installer into a dedicated benchmark directory. The voice and the daemon's
internal calls use two separate ChatGPT sign-ins made for the benchmark, because
ChatGPT refresh tokens rotate and must not be shared between programs. Episodes
run one at a time; each uses a copy of its sign-in and returns the refreshed
copy to the benchmark store when it ends. No credential, personal path or host
system prompt is published.

## Publication and reproducibility

The scenarios are also published as a reusable dataset in the Hugging Face
layout, `benchmarks/continuity/dataset/`: one JSON line per scenario with its
conversations and criteria, the native task list, the judge instructions and a
manifest of checksums. The dataset is generated from the preregistered corpus
and checked against it, so the two can never differ.

All conversations are synthetic, so everything is published: the corpus, the
criteria, every user message and reply of both arms, both judges' verdicts with
their quotations, timings, token counts, exclusions, the
manifest (daemon revision, Hermes version and checksum, model, reasoning
efforts, Codex CLI version, tool catalogue hashes, random seed) and the report.

The records are copied for publication by `benchmarks/continuity/export_records.py`:
the plan, runs, episode records and evidence, the simulated person's raw
answers, blinded packets, raw judge answers, verdicts, blind keys and reports.
Every runtime directory is left out (Hermes and Codex homes, sign-ins, logs,
caches and the bench daemon's vault), the local home directory is written as
`~`, and the export is refused if a credential or personal-path pattern
remains. The records keep the field names of the code that wrote them (a
scenario is a "life" there) and are recalculated with that version's tag.
The records of the 1.0 and 1.1 reference campaigns are in
`benchmarks/continuity/records/`.

- **Offline recalculation:** from the published records, a script with no model
  call regenerates every number of the report and its checksums.
- **Full rerun:** with their own ChatGPT and Claude access, anyone can rerun
  the campaign in disposable profiles and the judgment with the published judge
  instructions. Wording and timing will differ; the
  protocol, criteria and calculations are identical.

## Order of work

1. This specification and the corpus with its criteria are committed together
   before any model call; the corpus file's checksum is recorded.
2. Runner, judges and offline report, tested on synthetic data.
3. One engineering scenario and one engineering native task per arm. They check the
   measurement path only and are published as unscored.
4. The scored campaign, without changing the corpus, criteria or rules.
5. The report, with all records, added to `docs/evaluations/` and indexed.

A change to the corpus, criteria or rules after step 1 makes a new protocol
version and a new campaign; earlier ones stay published.
