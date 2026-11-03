#!/usr/bin/env python3
"""Opt-in disposable qualification of the seven-owner Hermes composition.

This script never targets the user's Hermes profile or native schedulers.  It
requires an already installed Hermes executable and an independently verified
uv 0.12.16 binary, then builds everything else under one temporary directory.
"""

from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from scripts import souvenir_read_fixture

from daemon_core.after_turn_service import read_after_turn_service_status
from daemons_companion.runtime.services import (
    AFTER_TURN_JOB,
    BackgroundJob,
)
from daemons_companion.setup.plan import (
    HostKind,
    HostProbe,
    ProviderKind,
    ProviderSelection,
    SetupRequest,
    build_setup_plan,
)
from daemons_companion.hermes.activation import (
    HermesCliBackend,
    HermesControlState,
)
from daemons_companion.hermes.composition import HermesCompositionLayout
from daemons_companion.hermes.profile import (
    create_disposable_profile_marker,
)
from daemons_companion.hermes.ready_composition import (
    HERMES_READY_STEP_CODES,
    HermesReadyCompositionAdapter,
    build_dormant_hermes_ready_plan,
)
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    create_disposable_companion_marker,
    verify_current_release,
)
from daemons_companion.runtime.environment import (
    SUPPORTED_UV_VERSION,
)
from daemons_companion.setup.transaction import (
    TransactionEngine,
    public_plan_digest,
)
from daemon_core.persona import DEFAULT_DAEMON_PERSONA


EXPECTED_RESPONSE = "HERMES_E2E_OK"
SYNTHETIC_USER_ID = "companion-e2e-user"
SYNTHETIC_MESSAGE = "Synthetic first exchange for the disposable qualification."
API_SERVER_KEY = "companion-e2e-" + "a" * 48
_SAFE_PROCESS_ENV = frozenset(
    {
        "COMSPEC",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the disposable Hermes companion end-to-end qualification."
    )
    parser.add_argument("--hermes", required=True, type=Path)
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument(
        "--uv-sha256",
        required=True,
        help="Expected SHA-256 of the independently downloaded uv binary.",
    )
    parser.add_argument("--exercise-souvenir-read", action="store_true")
    return parser


def _safe_environment(**updates: str) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key in _SAFE_PROCESS_ENV
    }
    environment.update(updates)
    return environment


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _is_port_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


class FixedProvider:
    """One loopback OpenAI-compatible endpoint with one invariant response."""

    def __init__(self, *, exercise_souvenir_read: bool = False, completion_callback=None) -> None:
        self.completion_callback = completion_callback
        self.exercise_souvenir_read = exercise_souvenir_read
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/") == "/v1/models":
                    self._json(
                        200,
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": "synthetic-model",
                                    "object": "model",
                                    "owned_by": "daemon-e2e",
                                }
                            ],
                        },
                    )
                    return
                self._json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:  # noqa: N802
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self._json(404, {"error": {"message": "not found"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length))
                except (ValueError, json.JSONDecodeError):
                    self._json(400, {"error": {"message": "invalid json"}})
                    return
                if not isinstance(payload, dict):
                    self._json(400, {"error": {"message": "invalid payload"}})
                    return
                with owner._lock:
                    owner.requests.append(payload)
                if payload.get("stream") is True:
                    self._stream(payload)
                    return
                self._json(200, owner.completion(payload))

            def log_message(self, _format: str, *_arguments: object) -> None:
                return

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, payload: dict) -> None:
                chunks = (
                    {
                        "id": "chatcmpl-daemon-e2e",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "synthetic-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": EXPECTED_RESPONSE,
                                },
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-daemon-e2e",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "synthetic-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                )
                chunks = souvenir_read_fixture.stream_chunks(chunks, owner.completion(payload))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in chunks:
                    data = json.dumps(chunk, separators=(",", ":"))
                    self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="companion-e2e-provider",
            daemon=True,
        )

    def completion(self, payload):
        response = souvenir_read_fixture.completion(payload, _completion_payload(), enabled=self.exercise_souvenir_read)
        return self.completion_callback(payload, response) if self.completion_callback else response

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _completion_payload() -> dict[str, Any]:
    return {
        "id": "chatcmpl-daemon-e2e",
        "object": "chat.completion",
        "created": 0,
        "model": "synthetic-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": EXPECTED_RESPONSE},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class ForegroundScheduler:
    """Injected scheduler that starts only the persistent job in foreground."""

    target_os = "macos"

    def __init__(self) -> None:
        self.definitions: dict[str, tuple[BackgroundJob, bytes]] = {}
        self.worker: subprocess.Popen[bytes] | None = None
        self._worker_log: io.BufferedWriter | None = None

    def capture(self, job_code: str) -> bytes | None:
        return None

    def install(self, job: BackgroundJob, definition: bytes) -> None:
        self.definitions[job.code] = (job, definition)
        if job.code != AFTER_TURN_JOB:
            return
        Path(job.log_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._worker_log = Path(job.log_path).open("ab")
        self.worker = subprocess.Popen(
            [job.executable, *job.arguments],
            cwd=job.working_directory,
            env=_safe_environment(),
            stdout=self._worker_log,
            stderr=subprocess.STDOUT,
        )

    def matches(self, job: BackgroundJob, definition: bytes) -> bool:
        current = self.definitions.get(job.code)
        if current != (job, definition):
            return False
        if not job.persistent:
            return True
        if self.worker is not None and self.worker.poll() is None:
            return True
        tail = _log_tail(Path(job.log_path))
        raise RuntimeError(
            "The fixture after-turn worker exited before readiness"
            f" (exit={None if self.worker is None else self.worker.returncode};"
            f" log={tail!r})."
        )

    def restore(self, job_code: str, _capture: bytes | None) -> None:
        if job_code == AFTER_TURN_JOB:
            self.stop()
        self.definitions.pop(job_code, None)

    def stop(self) -> None:
        if self.worker is not None and self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait(timeout=5)
        if self._worker_log is not None:
            self._worker_log.close()
        self._worker_log = None


class ForegroundHermesControl:
    """Real Hermes plugin CLI plus a fixture-owned foreground gateway."""

    def __init__(
        self,
        executable: Path,
        hermes_home: Path,
        *,
        api_port: int,
    ) -> None:
        self.executable = executable
        self.hermes_home = hermes_home
        self.api_port = api_port
        self.cli = HermesCliBackend(executable, hermes_home)
        self.gateway: subprocess.Popen[bytes] | None = None
        self._gateway_log: io.BufferedWriter | None = None

    def state(self) -> HermesControlState:
        state = self.cli.state()
        gateway = (
            "running"
            if self.gateway is not None and self.gateway.poll() is None
            else "stopped"
        )
        return HermesControlState(state.plugins, gateway)

    def enable_plugin(self, name: str) -> None:
        self.cli.enable_plugin(name)

    def disable_plugin(self, name: str) -> None:
        self.cli.disable_plugin(name)

    def start_gateway(self) -> None:
        if self.gateway is not None and self.gateway.poll() is None:
            return
        log_path = self.hermes_home / "gateway-e2e.log"
        self._gateway_log = log_path.open("ab")
        self.gateway = subprocess.Popen(
            [str(self.executable), "gateway", "run", "--force"],
            env=_safe_environment(
                HERMES_HOME=str(self.hermes_home),
                PYTHONDONTWRITEBYTECODE="1",
            ),
            stdout=self._gateway_log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_until(self.gateway_ready, timeout=45, label="Hermes API server")
        except TimeoutError as error:
            diagnostics = {
                item.name: _log_tail(item, maximum=12000)
                for item in (self.hermes_home / 'logs').glob('*.log')
                if item.is_file()
            }
            raise RuntimeError(
                "The fixture Hermes gateway did not become ready"
                f" (exit={self.gateway.poll()}; log={_log_tail(log_path, maximum=12000)!r}; diagnostics={diagnostics!r})."
            ) from error

    def restart_gateway(self) -> None:
        self.stop_gateway()
        self.start_gateway()

    def stop_gateway(self) -> None:
        if self.gateway is not None and self.gateway.poll() is None:
            self.gateway.terminate()
            try:
                self.gateway.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.gateway.kill()
                self.gateway.wait(timeout=5)
        if self._gateway_log is not None:
            self._gateway_log.close()
        self._gateway_log = None

    def gateway_ready(self) -> bool:
        if self.gateway is None or self.gateway.poll() is not None:
            return False
        try:
            with urlopen(
                f"http://127.0.0.1:{self.api_port}/health",
                timeout=0.5,
            ) as response:
                return response.status == 200
        except (OSError, HTTPError, URLError):
            return False


def _wait_until(check, *, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.2)
    raise TimeoutError(f"Timed out waiting for {label}.")


def _log_tail(path: Path, maximum: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-maximum:]
    except OSError:
        return "unavailable"


def _tracked_bundle(repository: Path, destination: Path) -> None:
    result = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
    )
    destination.mkdir(parents=True, mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("git archive contains an unsafe path")
            if not (member.isdir() or member.isfile()):
                raise RuntimeError("git archive contains a non-regular member")
        archive.extractall(destination, filter="data")


def _profile_config(provider_port: int) -> str:
    return yaml.safe_dump(
        {
            "model": {
                "provider": "custom",
                "default": "synthetic-model",
                "base_url": f"http://127.0.0.1:{provider_port}/v1",
                "api_mode": "chat_completions",
            },
            "platforms": {
                "api_server": {
                    "enabled": True,
                    "extra": {"host": "127.0.0.1"},
                }
            },
        },
        sort_keys=False,
    )


def _profile_environment(api_port: int) -> str:
    return (
        f"API_SERVER_KEY={API_SERVER_KEY}\n"
        "API_SERVER_HOST=127.0.0.1\n"
        f"API_SERVER_PORT={api_port}\n"
        "DAEMON_CORE_SOVEREIGN_PLATFORMS=telegram,api_server\n"
    )


def _post_exchange(port: int, session_id: str = "companion-e2e-session") -> str:
    body = json.dumps(
        {
            "model": "synthetic-model",
            "messages": [{"role": "user", "content": SYNTHETIC_MESSAGE}],
            "stream": False,
        }
    ).encode()
    request = Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_SERVER_KEY}",
            "Content-Type": "application/json",
            "X-Hermes-Session-Id": session_id,
            "X-Hermes-Session-Key": SYNTHETIC_USER_ID,
        },
    )
    with urlopen(request, timeout=90) as response:
        payload = json.loads(response.read())
    return str(payload["choices"][0]["message"]["content"]).strip()


def _find_visible_request(requests: Sequence[dict[str, Any]]) -> dict[str, Any]:
    required = (
        "You are a conversational presence",
        "Aster",
        "feminine grammatical agreement",
        "daemon_read_souvenirs",
        "daemon_recall_l0",
    )
    candidates = []
    for request in requests:
        rendered = json.dumps(request, ensure_ascii=False)
        if SYNTHETIC_MESSAGE in rendered:
            candidates.append((request, rendered))
    for request, rendered in candidates:
        if all(value in rendered for value in required):
            return request
    if candidates:
        return max(
            candidates,
            key=lambda item: (
                len(item[0].get("tools", ())),
                len(item[1]),
            ),
        )[0]
    raise RuntimeError("The visible Hermes request did not reach the local provider.")


def _assert_request(request: Mapping[str, Any], forbidden: Sequence[str]) -> None:
    rendered = json.dumps(request, ensure_ascii=False)
    required = (
        "You are a conversational presence",
        "Aster",
        "feminine grammatical agreement",
        "daemon_read_souvenirs",
        "daemon_recall_l0",
    )
    missing = [value for value in required if value not in rendered]
    if missing:
        raise RuntimeError(f"Hermes provider request is missing: {', '.join(missing)}")
    leaked = [value for value in forbidden if value and value in rendered]
    if leaked:
        raise RuntimeError("Hermes provider request contains a forbidden local value.")


def _memory_evidence(memory_root: Path) -> tuple[str, ...]:
    evidence: list[str] = []
    for path in sorted(memory_root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if SYNTHETIC_MESSAGE in content or EXPECTED_RESPONSE in content:
            evidence.append(path.relative_to(memory_root).as_posix())
    if not evidence:
        raise RuntimeError("The synthetic turn was not deposited in private memory.")
    return tuple(evidence)


def _l0_turn_count(memory_root: Path) -> int:
    return sum(
        path.read_text(encoding="utf-8").count(SYNTHETIC_MESSAGE)
        for path in (memory_root / "levels/L0_ground_zero/conversations").glob("*.md")
    )


def _profile_fingerprint(home: Path) -> str:
    digest = hashlib.sha256()
    for relative in ("config.yaml", ".env", "plugins/daemon_core", "plugins/daemon_voice"):
        path = home / relative
        digest.update(relative.encode())
        if path.is_symlink():
            digest.update(b"link\0" + os.readlink(path).encode())
        elif path.is_file():
            digest.update(b"file\0" + path.read_bytes())
        elif path.is_dir():
            digest.update(b"dir\0")
        else:
            digest.update(b"absent\0")
    return digest.hexdigest()


def _hermes_processes() -> tuple[str, ...]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(
        sorted(
            line.strip()
            for line in result.stdout.splitlines()
            if "hermes gateway run" in line and "qualify_companion" not in line
        )
    )


def _validated_file(path: Path, label: str) -> Path:
    value = path.expanduser().absolute()
    if value.is_symlink() or not value.is_file():
        raise ValueError(f"{label} must be an existing direct file.")
    return value


def main() -> int:
    args = _parser().parse_args()
    repository = Path(__file__).resolve().parents[1]
    hermes = _validated_file(args.hermes, "Hermes executable")
    uv = _validated_file(args.uv, "uv executable")
    uv_sha256 = hashlib.sha256(uv.read_bytes()).hexdigest()
    if uv_sha256 != args.uv_sha256.lower():
        raise RuntimeError("uv SHA-256 differs from the independently supplied value.")
    version = subprocess.run(
        [str(uv), "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if version.split(maxsplit=2)[:2] != ["uv", SUPPORTED_UV_VERSION]:
        raise RuntimeError(f"Expected uv {SUPPORTED_UV_VERSION}, got {version!r}.")
    if subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout:
        raise RuntimeError("Qualification requires a clean committed candidate.")

    real_home = Path.home() / ".hermes"
    real_profile_before = _profile_fingerprint(real_home)
    real_processes_before = _hermes_processes()
    provider = FixedProvider(exercise_souvenir_read=args.exercise_souvenir_read)
    scheduler = ForegroundScheduler()
    control: ForegroundHermesControl | None = None
    api_port = _free_port()
    started_at = time.monotonic()
    result_summary: dict[str, Any] = {}
    provider.start()
    try:
        with tempfile.TemporaryDirectory(prefix="daemon-companion-hermes-e2e-") as raw:
            fixture = Path(raw).resolve()
            bundle = fixture / "bundle"
            companion = fixture / "companion"
            hermes_home = fixture / "hermes-home"
            _tracked_bundle(repository, bundle)
            create_disposable_companion_marker(companion)
            create_disposable_profile_marker(hermes_home)

            memory_root = companion / "data" / "memory"
            persona = memory_root / "core" / "persona.md"
            conversation = memory_root / "users" / SYNTHETIC_USER_ID / "conversation.md"
            persona.parent.mkdir(parents=True, mode=0o700)
            conversation.parent.mkdir(parents=True, mode=0o700)
            persona.write_text(DEFAULT_DAEMON_PERSONA + "\n", encoding="utf-8")
            conversation.write_bytes(
                (bundle / "memory/templates/users/conversation.md").read_bytes()
            )
            if args.exercise_souvenir_read:
                souvenir_read_fixture.seed(memory_root)
            persona_before = persona.read_bytes()
            conversation_before = conversation.read_bytes()
            (hermes_home / "config.yaml").write_text(
                _profile_config(provider.port), encoding="utf-8"
            )
            (hermes_home / ".env").write_text(
                _profile_environment(api_port), encoding="utf-8"
            )

            runtime = CompanionRuntimeLayout(companion, bundle)
            host_version = subprocess.run(
                [str(hermes), "--version"], check=True, capture_output=True,
                text=True, timeout=20,
                env=_safe_environment(HERMES_HOME=str(hermes_home), PYTHONDONTWRITEBYTECODE="1"),
            ).stdout.strip()
            layout = HermesCompositionLayout(
                runtime,
                hermes_home,
                uv,
                uv_sha256,
                "macos",
            )
            selection = ProviderSelection(
                ProviderKind.OPENAI_COMPATIBLE_API,
                "synthetic-model",
                base_url=f"http://127.0.0.1:{provider.port}/v1",
            )
            reviewed = build_setup_plan(
                SetupRequest(HostKind.HERMES, "Aster", "feminine", selection),
                (
                    HostProbe(
                        HostKind.HERMES,
                        True,
                        True,
                        executable=str(hermes),
                        config_path=str(hermes_home),
                        version=host_version,
                        version_status="verified",
                    ),
                ),
            )
            plan = build_dormant_hermes_ready_plan(reviewed, layout)
            if tuple(step.code for step in plan.steps) != HERMES_READY_STEP_CODES:
                raise RuntimeError("Hermes plan is not the exact seven-owner plan.")
            control = ForegroundHermesControl(
                hermes,
                hermes_home,
                api_port=api_port,
            )
            adapter = HermesReadyCompositionAdapter(
                layout,
                plan=plan,
                provider_selection=selection,
                credential_store=None,
                credential_value=None,
                user_id=SYNTHETIC_USER_ID,
                scheduler=scheduler,
                control=control,
            )
            transaction = TransactionEngine(fixture / "transactions").execute(
                plan,
                adapter,
                transaction_id="hermes-e2e-v0",
                confirmed_plan_digest=public_plan_digest(plan),
            )
            response = _post_exchange(api_port)
            if response != EXPECTED_RESPONSE:
                raise RuntimeError(f"Unexpected visible response: {response!r}")
            visible_request = _find_visible_request(provider.requests)
            _assert_request(
                visible_request,
                (str(Path.home()), str(repository), API_SERVER_KEY),
            )
            if persona.read_bytes() != persona_before:
                raise RuntimeError("persona.md changed during the qualification.")
            if conversation.read_bytes() != conversation_before:
                raise RuntimeError("conversation.md changed during the qualification.")
            release = verify_current_release(runtime)
            spool = runtime.state_root / "after-turn-spool"
            _wait_until(
                lambda: read_after_turn_service_status(
                    spool_root=spool,
                    memory_root=runtime.memory_root,
                    expected_revision=release.name,
                )["ok"],
                timeout=20,
                label="after-turn heartbeat",
            )
            _wait_until(
                lambda: bool(_memory_evidence_or_empty(runtime.memory_root)),
                timeout=20,
                label="durable L0 evidence",
            )
            evidence = _memory_evidence(runtime.memory_root)
            if args.exercise_souvenir_read:
                proof = souvenir_read_fixture.tool_round_trip_seen(provider.requests)
                print(json.dumps({'phase': 'before_restart', 'souvenir_tool_round_trip': proof}), flush=True)
                if not proof:
                    raise RuntimeError('Hermes did not return the exact souvenir proof before restart.')
            restart_started = time.monotonic()
            l0_before_restart = _l0_turn_count(runtime.memory_root)
            request_count = len(provider.requests)
            control.restart_gateway()
            restarted_response = _post_exchange(api_port, "companion-e2e-restarted")
            if restarted_response != EXPECTED_RESPONSE:
                raise RuntimeError("Response after restart differs from the fixture.")
            _assert_request(
                _find_visible_request(provider.requests[request_count:]),
                (str(Path.home()), str(repository), API_SERVER_KEY),
            )
            _wait_until(
                lambda: _l0_turn_count(runtime.memory_root) > l0_before_restart,
                timeout=20, label="new-session L0 after restart",
            )
            if args.exercise_souvenir_read and not souvenir_read_fixture.tool_round_trip_seen(provider.requests):
                raise RuntimeError("The native host never returned the exact souvenir tool proof.")
            result_summary = {
                "schema": "daemon.companion-hermes-e2e-result.v1",
                "status": transaction.status,
                "host_version": host_version,
                "step_codes": list(transaction.completed_steps),
                "runtime_revision": release.name,
                "visible_response": response,
                "provider_request_count": len(provider.requests),
                "souvenir_tool_round_trip": "ok" if args.exercise_souvenir_read else "not_tested",
                "memory_evidence": list(evidence),
                "heartbeat": "ok",
                "restart_new_session": "ok",
                "restart_exchange_seconds": round(time.monotonic() - restart_started, 3),
                "persona_unchanged": True,
                "conversation_unchanged": True,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
            }
            control.stop_gateway()
            scheduler.stop()
            if control.gateway is not None and control.gateway.poll() is None:
                raise RuntimeError("The fixture Hermes gateway did not stop.")
            if scheduler.worker is not None and scheduler.worker.poll() is None:
                raise RuntimeError("The fixture after-turn worker did not stop.")
    finally:
        if control is not None:
            control.stop_gateway()
        scheduler.stop()
        provider.stop()

    if not _is_port_closed(api_port) or not _is_port_closed(provider.port):
        raise RuntimeError("A qualification loopback port remained open.")
    if _profile_fingerprint(real_home) != real_profile_before:
        raise RuntimeError("The personal Hermes profile changed during qualification.")
    if _hermes_processes() != real_processes_before:
        raise RuntimeError("Personal Hermes gateway processes changed during qualification.")
    print(json.dumps(result_summary, indent=2, sort_keys=True))
    return 0


def _memory_evidence_or_empty(memory_root: Path) -> tuple[str, ...]:
    try:
        return _memory_evidence(memory_root)
    except RuntimeError:
        return ()


if __name__ == "__main__":
    raise SystemExit(main())
