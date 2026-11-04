# Contributing

Start with [how Daemon Harness works](docs/how-it-works.md), the
[developer glossary](docs/glossary.md), and [AGENTS.md](AGENTS.md).
The latter contains the full repository rules, currently in French.
Architecture changes also require the linked responsibility and quality references.

## Work in isolation

Create a branch and a durable worktree. Use synthetic identities, memory and
sessions for tests. Do not run installation scripts against a personal host,
change host source code or commit private data.

Keep one conversational voice. The host owns its model, tools and channels.
Exact Markdown notes and original messages are authoritative; search indexes
and working context are derived aids. Do not manually write expected facts into
personal memory to make a test pass.

## Set up the source environment

Python 3.11 or later is required. The current local verification uses Python 3.12.

~~~sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
PYTHONPATH=.:src .venv/bin/python -m pytest -q
node --test integrations/openclaw_plugin/index.test.js
.venv/bin/python -m ruff check src server integrations benchmarks tests
~~~

On Windows, use .venv\Scripts\python.exe instead of .venv/bin/python and set
PYTHONPATH to .;src for these source tests. Companion's packaged runtime is
separate from this development environment.

Use the checks appropriate to the change. Do not run paid provider evaluations,
real messaging or GitHub Actions merely to clean documentation.

## Code and documentation language

Write comments, internal descriptions and new developer documentation in English.
The app's English and French interface strings remain localized. Never translate
recorded user messages, saved memory, test fixtures or model instructions as part
of a cosmetic cleanup.

Write user documentation for someone arriving for the first time. Explain
purpose and consequences before internal names. Define an unfamiliar term at its
first use; for example, “exact conversation journal” before the internal name L0.
Do not assume readers know past development conversations, numbered milestones,
temporary branches or release nicknames.

## Change scope and verification

For a comments-only Python change, compare parsed syntax trees with docstrings
removed: executable statements and all other strings must remain unchanged.
For JavaScript and shell comments, inspect the diff and run syntax checks.

For behavior changes, define the expected result and affected path before testing.
Verify both hosts when a shared contract changes. Keep failed experiments and
limits in evaluation reports; do not turn a synthetic
test into a claim about real accounts or real-world latency.

The desktop app and main runtime currently have separate source references.
[Source and release status](apps/companion/README.md) explains how to inspect the
actual packaged revision. Do not assume that main alone reproduces the app.

## Documentation organization

- README: project introduction, install commands and clear limits.
- User guides: setup, memory, installation effects and alpha feedback.
- Developer references: architecture, contracts and implementation details.
- Evaluations: dated evidence, including negative results.
- Archives: superseded procedures and development history, never current setup instructions.

When replacing a guide, preserve necessary evidence and update relative links.
Remove duplicated instructions from active guides instead of maintaining several
conflicting versions. Keep the README download commands visible.

## License

Contributions are made under the repository's [MIT License](LICENSE).
External hosts, providers and bundled dependencies retain their own licenses.
