# Public Engineering Dry-Runs

This directory retains every live engineering attempt made before the scored
Productivity V1 corpus. A failed or incomplete attempt is never overwritten.

| Campaign | Source commit | Status | Reason |
| --- | --- | --- | --- |
| `dry-run-native-files-aggregate-20260829` | `38a68984dc6334d75b5ebcf46e0a77830c0fcdc4` | non-conclusive | the OpenClaw execution path did not activate the daemon context engine and did not expose host tokens or tools |
| `dry-run-native-files-aggregate-20260829-r2` | `96f917aa76c8990c7f506a6f64b5c0e554686acd` | non-conclusive | the canonical daemon path worked, but the general OpenClaw hooks did not expose context and tool telemetry |
| `dry-run-native-files-aggregate-20260829-r3` | `0bc60a512107c41536b924e9848660de46867cf8` | conclusive engineering dry-run | all eleven predeclared engineering gates passed |
| `dry-run-native-tools-extract-20260829-r1` | `536c0fb06ba3611a81d686e6fb14d513fd6533ff` | valid measurements, old verdict non-conclusive | one authoritative host failure incorrectly made the old success-only engineering gate fail |
| `dry-run-native-tools-extract-20260829-r2` | `5d852467b1c8a0ff2be407f0f5f751fe369a4a34` | conclusive engineering dry-run | all eleven measurement gates passed; three of four product outcomes succeeded |

Each campaign contains its plan, manifest, run records, objective output
artifacts, offline summary, verdict, and checksums. Content-bearing host traces,
credentials, disposable profiles, system prompts, and personal paths are not
published.

Verify a campaign from its directory with:

```bash
shasum -a 256 -c SHA256SUMS
```

Recompute a summary without a model or network call with:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src .venv/bin/python -m \
  benchmarks.productivity_v1.report.recalculate \
  --manifest benchmarks/productivity_v1/results/<campaign>/manifest.json \
  --runs benchmarks/productivity_v1/results/<campaign>/runs.jsonl \
  --output /tmp/productivity-v1-summary.json
```

These campaigns use one seed and one scenario. They validate the measurement
path; they do not establish an average daemon effect or support an adoption
claim.
