# Run the local HTTP API in Docker

[Version française](docker.fr.md)

The repository's `Dockerfile` packages the daemon's
[local HTTP API](bridge-api.md). OpenClaw runs in JavaScript and the daemon's
engine, Daemon Core, in Python; this API is the bridge between them: OpenClaw calls it
to compose the daemon's context, run memory tools and record each turn.
Companion and the Hermes and OpenClaw procedures do not use Docker; this image
is for developers who want that API in a container, for example to test it in
isolation.

The container runs the API only. It does not start the after-turn worker (the
background process that updates memory after each reply) or nightly
maintenance; see [Architecture](architecture.md) for those components.

## Build the image

Build from a clean checkout. The commit is passed to the image so that the
API can report which revision it runs:

```bash
DAEMON_REVISION="$(git rev-parse HEAD)"
docker build \
  --build-arg DAEMON_CORE_RUNTIME_REVISION="$DAEMON_REVISION" \
  -t daemon-harness:"$DAEMON_REVISION" .
```

## Keep the memory outside the repository

The memory lives in a directory on the host machine, mounted into the
container. For a test, use a disposable, already initialized memory, never a
personal one. Check that it has the expected layout:

```bash
DAEMON_STATE_DIR=/path/to/isolated/daemon-harness-state
test -d "$DAEMON_STATE_DIR/memory/users"
```

## Start the container

The container reads its settings from a `.env` file (start from
`.env.example` and the [configuration reference](configuration.md)). The
revision built into the image is passed again with `-e`, so that an older
value left in `.env` cannot replace it:

```bash
docker run --detach --restart unless-stopped \
  --name daemon-harness \
  --env-file .env \
  -e DAEMON_CORE_RUNTIME_REVISION="$DAEMON_REVISION" \
  -v "$DAEMON_STATE_DIR/memory:/app/memory" \
  -p 127.0.0.1:8000:8000 \
  daemon-harness:"$DAEMON_REVISION"
```

The port is published on `127.0.0.1` only: the API is meant for hosts on the
same machine.

## Check that it is ready

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready
```

`/health` says that the process answers. `/ready` checks the runtime, the
paths and the attestation of the configured worker and host (whether what
runs matches what was deployed); the image's own health check passes only
when `/ready` reports `"promotable": true`. A degraded `/ready` means the
deployment is not usable as it stands: fix the cause rather than pointing the
host at another copy of the code. [Operations and troubleshooting](operations.md)
explains how to read these reports.
