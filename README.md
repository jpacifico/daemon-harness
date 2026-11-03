# Continuity Benchmark 1.0.0 — recalculation code

This tag holds only the `benchmarks/` directory of the commit that ran and
judged the Continuity Benchmark 1.0.0 campaign (`16c449e91039d74b96b59c951559ac5147eb4b95`, in the project's
earlier repository, whose history is not published). It exists so that the
published records can be recalculated with the exact code that produced them.
It is not a runnable copy of the daemon.

Python 3.11 with PyYAML (`pip install PyYAML==6.0.3`). From the root of this
tag, on a copy of the campaign directory from
`benchmarks/continuity/records/` on `main`, after removing its derived files
(`judging/merged.json` and `report/`):

```bash
PYTHONPATH=. python -m benchmarks.continuity.judge merge --campaign <copy>
PYTHONPATH=. python -m benchmarks.continuity.report --campaign <copy>
```

The merged verdicts, `report/summary.json` and `report/table.md` come out
byte for byte identical to the published ones.
