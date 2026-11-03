# Continuity Benchmark — campaign records

The anonymized records of the two reference campaigns, copied by
`benchmarks/continuity/export_records.py`. Every person, fact and message is
synthetic; the assistant ran with a test identity and an empty vault.

| Directory | Version | Code that ran and judged it | Report |
| --- | --- | --- | --- |
| `continuity-1.0.0-20261031` | 1.0.0 | tag `continuity-1.0.0` | [1.0 report](../../../docs/evaluations/2026-10-31_continuity_benchmark_1_0.md) |
| `continuity-1.1.0-20261031b` | 1.1.0 | tag `continuity-1.1.0` | [1.1 report](../../../docs/evaluations/2026-10-31_continuity_benchmark_1_1.md) |

Each directory holds the plan, `runs.jsonl` (times, tokens, tool steps), the
episode records and evidence, the simulated person's raw answers (1.1), the
blinded judge packets, raw judge answers, verdicts, the blind keys
(`private/`, opened after judging), the merged verdicts and the report.
`MANIFEST.json` lists the sha256 of every file.

Left out: every runtime directory (Hermes and Codex homes, sign-ins, logs,
caches) and the bench daemon's vault. The 1.0 check that a fact had reached the
vault therefore cannot be audited from these records. The local home directory
is written as `~`.

The records keep the field names of the code that wrote them: a scenario is a
"life" there. Recompute them with that version's tag, from a copy, after
removing the derived files (`judging/merged.json`, `report/`):

```bash
git worktree add ../continuity-1.1.0 continuity-1.1.0
cd ../continuity-1.1.0
python -m benchmarks.continuity.judge merge --campaign <copy of continuity-1.1.0-20261031b>
python -m benchmarks.continuity.report --campaign <copy of continuity-1.1.0-20261031b>
```

Both campaigns were checked this way on 1 November 2026: the merged verdicts,
`summary.json` and `table.md` came out byte for byte identical.
