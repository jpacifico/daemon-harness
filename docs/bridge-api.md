# Local OpenClaw bridge API

OpenClaw runs in JavaScript; the shared daemon core runs in Python.
The bridge passes context, memory-tool requests and completed turns between
them. It does not replace OpenClaw's response model or native tool loop.

This is a local integration interface, not a public multi-tenant API.
The supplied service binds to `127.0.0.1:8000`. Do not expose it to a network
as though it provided production authentication or authorization.

The authoritative request/response definitions are the Pydantic models in
[server/routes/openclaw.py](../server/routes/openclaw.py).
The running FastAPI service exposes its generated schema at `/openapi.json`.
Use the schema from the installed revision when integrating.

## Endpoints

| Method and path | Purpose | Mutation / sensitivity |
| --- | --- | --- |
| `GET /health` | HTTP process liveness | No personal payload required |
| `GET /ready` | Runtime, paths and configured worker/host attestation | Returns readiness or degraded status |
| `POST /openclaw/assemble` | Build the canonical context for a turn | Reads personal memory; response contains private prompt material |
| `POST /openclaw/tool` | Execute a registered memory-tool request | Reads exact personal sources; can involve memory retrieval judgments |
| `POST /openclaw/ingest` | Record a turn and schedule maintenance | Writes journal / continuity state; may trigger subsequent memory work |
| `POST /openclaw/reconcile-transcript` | Recover a bounded recent gap from host messages | Writes missing original-journal entries with stable event provenance |

An assembly response is not an assistant reply. Ingestion success is not proof
that every asynchronous maintenance stage has completed.

## Assemble

Required fields are `user_id` and `message`; `session_id` and `trace_id` identify
the host session and diagnostic continuity when supplied.

Synthetic request, for an already initialized disposable test identity:

~~~json
{
  "user_id": "test-user",
  "message": "Can we return to the exhibition plan?",
  "session_id": "test-session",
  "trace_id": "test-turn-001"
}
~~~

Key response fields:

| Field | Meaning |
| --- | --- |
| `system_prompt_addition` and prompt variants | Context fragments for the host adapter |
| `canonical_recent_dialogue` | Recent dialogue selected by the canonical runtime |
| `souvenirs`, `hot_souvenirs` | Selected exact-note references |
| `selection_source`, `selection_reused` | Selection provenance and reuse information |
| `voice_path`, `scene_scope` | Context composition metadata |
| `timings_ms` | Internal measured timings, not full end-to-end delivery latency |
| `degraded`, `degradation_reason` | Explicit degraded context state |

Do not log the complete response in a public issue. A short `timings_ms` value
does not measure the host model, tool calls or Telegram delivery.

## Memory tool

The request carries `user_id`, `name` and `arguments`, with optional session,
trace and already-read-note information. The response returns `result`,
`read_souvenirs` and the preflight-consumption state.

Use the tool schemas registered by the installed plugin. Do not implement a
parallel note writer through this endpoint or infer tool arguments from a
different host release.

## Ingest and idempotence

The request carries `user_id`, `role` and `content`. Additional fields preserve
the last user message, selected/read memories, session, timing and source
journal references. An `idempotency_key` lets the adapter associate retries
with the same event instead of silently duplicating it.

The response includes `ingested`, `memory_updated`, `reason` and `l0_ref`.
Preserve those distinctions in a client. When work is queued, inspect the
worker/receipts to establish eventual completion rather than treating HTTP 200
as proof that a durable note was written.

## Transcript reconciliation

This is bounded recovery of host transcript gaps, not arbitrary memory import.
Each item contains an `event_id`, `role`, exact `content` and `occurred_at`.
The source implementation accepts at most 128 items, limits each message to
200,000 characters, and rejects timestamps older than seven days or more than
five minutes into the future.

It reports `scanned`, `already_recorded` and `restored`. Stable native event
identities are essential: inventing a new identity for the same turn defeats
deduplication. Do not use this endpoint to rewrite personal history.

## Status and validation

`/ready` returns HTTP 503 when attestation fails, including when its own
attestation code cannot complete. Read failure reasons before changing a host.
FastAPI validates request models; endpoint-specific errors and degradation
are defined in the installed source.

Local integration checks:

~~~sh
PYTHONPATH=.:src .venv/bin/python -m pytest -q tests/test_openclaw_bridge.py
node --test integrations/openclaw_plugin/index.test.js
~~~

These use isolated fixtures. They do not authorize sending test requests to a
personal service. See [operations](operations.md) for live read-only checks.
