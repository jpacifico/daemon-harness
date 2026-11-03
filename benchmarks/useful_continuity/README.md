# Useful Continuity Benchmark — UCB v0.1

Exploratory English benchmark. Question: **does a personal agent use earlier
context to provide useful help without requiring the user to repeat it?**

This corpus is independent of the frozen Public Productivity Benchmark V1.
It compares native Hermes with the same Hermes plus the current daemon.
It does not evaluate autonomous monitoring, overnight consolidation, external
services, cross-host portability, or actual messaging delivery.

## Fixed corpus and comparison

Twelve synthetic situations: four families with two useful-context cases and
one counter-case each. Every episode contains two ordinary preparatory user
turns in one persistent session, followed by one scored turn in a new session.
Both arms receive exactly the same user messages. Their assistant replies and
memories are produced through their own normal paths, never hand-authored.
Preparatory replies, tool loops and immediate maintenance count toward costs.

The native arm retains native memory, profile, session search, skill learning
and background review. The daemon arm uses its installed memory/voice plugins,
durable after-turn worker, Temporal Curator and scene. Follow-up judgement and
delivery are disabled (`DAEMON_CORE_FOLLOWUPS=off`) in this channel-free test;
no Dreaming or REM pass is forced. There
are no personal files, channels, calendars, MCP credentials or external tasks.

Voice model: `gpt-6-astra`, `openai-codex`, `medium` reasoning in both arms.
Daemon internal calls use the same Astra model with its canonical per-faculty
reasoning defaults (`medium` routing/conversation, `high` memory). This is a
recorded test configuration, not a production model change.

Arm order alternates A/C and C/A. One separate engineering pair is excluded
from scored results but included in the campaign spend. No quality rerolls.
Each scored arm starts with empty, separate memory, home, cache and sessions.
The macOS outer sandbox blocks the personal home and other campaign directories,
with explicit access to the isolated runtime and required host/plugin code;
its denial is checked before any call. Ordinary system directories remain
accessible to native host tools.

## Completion and accounting

- 24 scored episodes, 72 normal voice turns, plus at most one engineering pair
  (six turns); tool-loop calls and internal calls add to these counts.
- The user explicitly requires all twelve comparisons to complete. There is
  no campaign token ceiling. All reported spend, including engineering runs,
  tool loops, failed attempts and immediate maintenance, is retained.
- Voice turn timeout: 120 seconds; deferred closure: 240 seconds per turn.
- No automatic replacements after the model has received a scored turn.
  Failed preparation or maintenance remains visible as an episode failure or
  a measurement limitation. Instrumentation failures pause generation until
  the measurement path is repaired; affected attempts remain in the report.

## Evaluation and reproducibility

Each case has three criteria fixed in `corpus.py`: useful current action,
correct handling of constraints/changes, and avoiding redundant or misplaced
clarification. Missing topic information can justify a new question. Mentioning
a remembered person alone is not success. All three criteria must pass for a
first-attempt success. No repair turn is generated in this small campaign.

Free conversational prose needs semantic review. Responses are shuffled and
arm labels withheld for an **explicitly model-assisted blind review**, not
claimed as independent human judgement. Every decision is accompanied by an
exact response quotation and reasoning. The deterministic recalculator checks
record integrity, quote provenance and the fixed all-criteria rule. It does
not claim that checking quotation presence proves the semantic judgement.
Naturalness and useful new questions remain qualitative observations.

The blind review uses one fresh Astra request at high reasoning, supplied only
the shuffled responses, the user messages and the fixed criteria. Arm mappings
are outside its filesystem permissions. Its tokens are reported separately
from A/C episodes and included in total campaign spend. Original judgements
are preserved; structural or evidential problems are examined before unblinding.

The report publishes each pair and both answers, including ties and failures.
It counts successes separately for eight positive and four counter-cases,
paired wins/losses, latency, calls, cache and token coverage. Review is locked
before unblinding; a later human review may disagree and is retained separately.
No general superiority claim follows from twelve exploratory comparisons.

Host usage and daemon provider usage are separate. Missing usage is unavailable,
never zero. First text is measured at the native stream hook, not Telegram.
Immediate memory maintenance is included; disabled follow-ups, unforced nightly
work and unreported native embeddings remain outside the observed total.
Two short history turns are a deliberate
limit: this is contextual usefulness after session change, not weeks of memory.

## Commands

From a clean committed checkout, with the existing Hermes and Codex login:

```bash
PYTHONPATH=.:src /path/to/daemon/python -m benchmarks.useful_continuity.run \
  --output /private/tmp/ucb-v01-campaign
```

Set `UCB_CODEX_BINARY` to the absolute path of the qualified Codex executable
when it differs from `/opt/homebrew/bin/codex`. The campaign records its version
and digest. The initial October 3 campaign uses the retained 0.159.2 client.

The runner creates `plan.json` before any generation and streams progress.
Use `--dry-run-only` for the engineering pair. After generation:

```bash
PYTHONPATH=.:src /path/to/daemon/python -m benchmarks.useful_continuity.review \
  prepare --campaign /private/tmp/ucb-v01-campaign
PYTHONPATH=.:src /path/to/daemon/python -m benchmarks.useful_continuity.judge \
  --campaign /private/tmp/ucb-v01-campaign
PYTHONPATH=.:src /path/to/daemon/python -m benchmarks.useful_continuity.review \
  recalculate --campaign /private/tmp/ucb-v01-campaign
```

Raw auth/environment files are private and removed at shutdown; public evidence
uses synthetic responses and content-safe telemetry, never provider secrets or
full host prompts. No host source, daemon prompt, production setting, service
or personal memory is changed by this benchmark.

## First completed campaign

The [October 3 report](../../docs/evaluations/2026-10-03_ucb_v01.md) records all
twelve comparisons, original arm-blind judgements and an auditable synthetic
evidence bundle. Complete-case scores are 6/12 with daemon and 4/12 native,
with five daemon wins and three native wins. The positive/counter-case split,
legitimate clarifications and 4.22× measured token cost matter: this is a mixed
baseline, not a general superiority claim. No runtime was tuned to these cases.
