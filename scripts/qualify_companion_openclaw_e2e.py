#!/usr/bin/env python3
"""Opt-in disposable qualification of the OpenClaw companion composition."""

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
from urllib.request import urlopen

from scripts import souvenir_read_fixture

from daemon_core.after_turn_service import read_after_turn_service_status
from daemons_companion.runtime.services import (
    AFTER_TURN_JOB,
    OPENCLAW_API_JOB,
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
from daemons_companion.openclaw.activation import OpenClawControlState
from daemons_companion.openclaw.profile import (
    create_disposable_openclaw_marker,
)
from daemons_companion.openclaw.ready_composition import (
    OPENCLAW_READY_STEP_CODES,
    OpenClawReadyCompositionAdapter,
    OpenClawReadyCompositionLayout,
    build_dormant_openclaw_ready_plan,
)
from daemons_companion.runtime.release import (
    CompanionRuntimeLayout,
    create_disposable_companion_marker,
    verify_current_release,
)
from daemons_companion.runtime.environment import SUPPORTED_UV_VERSION
from daemons_companion.setup.transaction import (
    TransactionEngine,
    public_plan_digest,
)
from daemon_core.persona import DEFAULT_DAEMON_PERSONA


EXPECTED_RESPONSE = "OPENCLAW_E2E_OK"
SYNTHETIC_USER_ID = "companion-openclaw-e2e-user"
SYNTHETIC_MESSAGE = "Synthetic first OpenClaw exchange for qualification."
_SAFE_PROCESS_ENV = frozenset(
    {
        "COMSPEC",
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
        description="Run the disposable OpenClaw companion E2E qualification."
    )
    parser.add_argument("--openclaw", required=True, type=Path)
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--uv-sha256", required=True)
    parser.add_argument(
        "--restart-session",
        choices=("new", "same"),
        default="new",
        help="Select the native session tested after restart; reported explicitly.",
    )
    parser.add_argument("--exercise-souvenir-read", action="store_true")
    return parser


def _safe_environment(home: Path, **updates: str) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key in _SAFE_PROCESS_ENV
    }
    environment.update(
        {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
        }
    )
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
    def __init__(self, *, exercise_souvenir_read: bool = False) -> None:
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
                else:
                    self._json(200, souvenir_read_fixture.completion(payload, _completion_payload(), enabled=owner.exercise_souvenir_read))

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
                        "id": "chatcmpl-openclaw-e2e",
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
                        "id": "chatcmpl-openclaw-e2e",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "synthetic-model",
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "stop"}
                        ],
                    },
                )
                chunks = souvenir_read_fixture.stream_chunks(chunks, souvenir_read_fixture.completion(payload, _completion_payload(), enabled=owner.exercise_souvenir_read))
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
            name="companion-openclaw-e2e-provider",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _completion_payload() -> dict[str, Any]:
    return {
        "id": "chatcmpl-openclaw-e2e",
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
    target_os = "macos"

    def __init__(self, home: Path) -> None:
        self.home = home
        self.definitions: dict[str, tuple[BackgroundJob, bytes]] = {}
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.logs: dict[str, io.BufferedWriter] = {}

    def capture(self, _job_code: str) -> bytes | None:
        return None

    def install(self, job: BackgroundJob, definition: bytes) -> None:
        self.definitions[job.code] = (job, definition)
        if job.code not in {AFTER_TURN_JOB, OPENCLAW_API_JOB}:
            return
        Path(job.log_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = Path(job.log_path).open("ab")
        self.logs[job.code] = log
        self.processes[job.code] = subprocess.Popen(
            [job.executable, *job.arguments],
            cwd=job.working_directory,
            env=_safe_environment(self.home),
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    def matches(self, job: BackgroundJob, definition: bytes) -> bool:
        if self.definitions.get(job.code) != (job, definition):
            return False
        process = self.processes.get(job.code)
        if process is None:
            return not job.persistent
        if process.poll() is None:
            return True
        raise RuntimeError(
            f"Fixture job exited: {job.code} ({process.returncode}); "
            f"log={_log_tail(Path(job.log_path))!r}."
        )

    def restore(self, job_code: str, _capture: bytes | None) -> None:
        self._stop_one(job_code)
        self.definitions.pop(job_code, None)

    def stop(self) -> None:
        for code in reversed(tuple(self.processes)):
            self._stop_one(code)

    def _stop_one(self, code: str) -> None:
        process = self.processes.pop(code, None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log = self.logs.pop(code, None)
        if log is not None:
            log.close()


class ForegroundOpenClawControl:
    def __init__(
        self,
        executable: Path,
        openclaw_state: Path,
        home: Path,
        gateway_port: int,
    ) -> None:
        self.executable = executable
        self.openclaw_state = openclaw_state
        self.home = home
        self.gateway_port = gateway_port
        self.gateway: subprocess.Popen[bytes] | None = None
        self._log: io.BufferedWriter | None = None

    def _environment(self) -> dict[str, str]:
        return _safe_environment(
            self.home,
            OPENCLAW_STATE_DIR=str(self.openclaw_state),
            OPENCLAW_CONFIG_PATH=str(self.openclaw_state / "openclaw.json"),
            OPENCLAW_SKIP_CHANNELS="1",
        )

    def state(self) -> OpenClawControlState:
        return OpenClawControlState(
            "running"
            if self.gateway is not None and self.gateway.poll() is None
            else "stopped"
        )

    def start_gateway(self) -> None:
        if self.gateway is not None and self.gateway.poll() is None:
            return
        log_path = self.openclaw_state / "gateway-e2e.log"
        self._log = log_path.open("ab")
        self.gateway = subprocess.Popen(
            [
                str(self.executable),
                "gateway",
                "run",
                "--port",
                str(self.gateway_port),
                "--bind",
                "loopback",
                "--auth",
                "none",
            ],
            cwd=self.openclaw_state / "workspace-daemon-harness",
            env=self._environment(),
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        try:
            # Production activation allows 180 s; OpenClaw 2026.10 needs ~55 s here.
            _wait_until(self.gateway_ready, timeout=180, label="OpenClaw gateway")
        except TimeoutError as error:
            raise RuntimeError(
                "Fixture OpenClaw gateway did not become ready "
                f"(exit={self.gateway.poll()}; log={_log_tail(log_path)!r})."
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
        if self._log is not None:
            self._log.close()
        self._log = None

    def gateway_ready(self) -> bool:
        if self.gateway is None or self.gateway.poll() is not None:
            return False
        result = subprocess.run(
            [
                str(self.executable),
                "gateway",
                "health",
                "--json",
                "--port",
                str(self.gateway_port),
                "--timeout",
                "1000",
            ],
            cwd=self.openclaw_state / "workspace-daemon-harness",
            env=self._environment(),
            check=False,
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0

    def plugin_ready(self) -> bool:
        result = subprocess.run(
            [
                str(self.executable),
                "plugins",
                "info",
                "daemon-harness",
                "--json",
                "--runtime",
            ],
            cwd=self.openclaw_state / "workspace-daemon-harness",
            env=self._environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        plugin = payload.get("plugin", payload) if isinstance(payload, dict) else None
        return isinstance(plugin, dict) and plugin.get("id") == "daemon-harness"

    def exchange(self, session_key: str = "agent:main:explicit:companion-openclaw-e2e") -> dict[str, Any]:
        result = subprocess.run(
            [
                str(self.executable),
                "agent",
                "--json",
                "--session-key",
                session_key,
                "--model",
                "openai/synthetic-model",
                "--thinking",
                "off",
                "--timeout",
                "90",
                "--message",
                SYNTHETIC_MESSAGE,
            ],
            cwd=self.openclaw_state / "workspace-daemon-harness",
            env=self._environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "OpenClaw fixture exchange failed: "
                + (result.stderr or result.stdout)[-4000:]
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("OpenClaw returned invalid exchange JSON.") from error
        if EXPECTED_RESPONSE not in json.dumps(payload, ensure_ascii=False):
            raise RuntimeError(f"Unexpected OpenClaw exchange payload: {payload!r}")
        return payload


def _fixture_directory():
    """Disposable fixture; QUALIFY_KEEP_FIXTURE keeps it for diagnosis."""
    if os.environ.get("QUALIFY_KEEP_FIXTURE"):
        import contextlib

        kept = tempfile.mkdtemp(prefix="daemon-companion-openclaw-e2e-")
        print("FIXTURE_KEPT", kept, flush=True)
        return contextlib.nullcontext(kept)
    return tempfile.TemporaryDirectory(prefix="daemon-companion-openclaw-e2e-")


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


def _initial_openclaw_config(
    provider_port: int,
    gateway_port: int,
) -> dict[str, Any]:
    return {
        "agents": {
            "defaults": {
                "model": {"primary": "openai/synthetic-model"},
                "timeoutSeconds": 120,
            }
        },
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": f"http://127.0.0.1:{provider_port}/v1",
                    "apiKey": "fixture-key",
                    "api": "openai-completions",
                    "agentRuntime": {"id": "openclaw"},
                    "models": [
                        {
                            "id": "synthetic-model",
                            "name": "Synthetic model",
                            "contextWindow": 65536,
                            "maxTokens": 1024,
                        }
                    ],
                }
            }
        },
        "gateway": {
            "mode": "local",
            "port": gateway_port,
            "bind": "loopback",
            "auth": {"mode": "none"},
        },
    }


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
        return max(candidates, key=lambda item: len(item[1]))[0]
    raise RuntimeError("Visible OpenClaw request did not reach the local provider.")


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
    if missing and os.environ.get("QUALIFY_REQUEST_DUMP"):
        # Diagnostic only: the synthetic request carries no personal data.
        Path(os.environ["QUALIFY_REQUEST_DUMP"]).write_text(rendered, encoding="utf-8")
    if missing:
        raise RuntimeError(
            f"OpenClaw provider request is missing: {', '.join(missing)}"
        )
    if any(value and value in rendered for value in forbidden):
        raise RuntimeError("OpenClaw provider request contains a forbidden local value.")


def _memory_evidence_or_empty(memory_root: Path) -> tuple[str, ...]:
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
    return tuple(evidence)


def _l0_turn_count(memory_root: Path) -> int:
    return sum(
        path.read_text(encoding="utf-8").count(SYNTHETIC_MESSAGE)
        for path in (memory_root / "levels/L0_ground_zero/conversations").glob("*.md")
    )


def _ready(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/ready", timeout=1) as response:
            return response.status == 200
    except (OSError, HTTPError, URLError):
        return False


def _ready_report(port: int) -> dict[str, Any]:
    try:
        with urlopen(f"http://127.0.0.1:{port}/ready", timeout=2) as response:
            payload = json.load(response)
    except HTTPError as error:
        try:
            payload = json.load(error)
        except (OSError, ValueError, json.JSONDecodeError):
            return {"status": "http-error", "code": error.code}
    except (OSError, URLError) as error:
        return {"status": "unavailable", "reason": type(error).__name__}
    return payload if isinstance(payload, dict) else {"status": "invalid"}


def _profile_fingerprint(home: Path) -> str:
    digest = hashlib.sha256()
    path = home / "openclaw.json"
    digest.update(path.read_bytes() if path.is_file() and not path.is_symlink() else b"absent")
    return digest.hexdigest()


def _openclaw_processes() -> tuple[str, ...]:
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
            if "openclaw gateway" in line and "daemon-companion-openclaw-e2e-" not in line
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
    openclaw = _validated_file(args.openclaw, "OpenClaw executable")
    uv = _validated_file(args.uv, "uv executable")
    uv_sha256 = hashlib.sha256(uv.read_bytes()).hexdigest()
    if uv_sha256 != args.uv_sha256.lower():
        raise RuntimeError("uv SHA-256 differs from the supplied value.")
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

    real_home = Path.home() / ".openclaw"
    real_profile_before = _profile_fingerprint(real_home)
    real_processes_before = _openclaw_processes()
    provider = FixedProvider(exercise_souvenir_read=args.exercise_souvenir_read)
    scheduler: ForegroundScheduler | None = None
    control: ForegroundOpenClawControl | None = None
    api_port = _free_port()
    gateway_port = _free_port()
    started_at = time.monotonic()
    result_summary: dict[str, Any] = {}
    provider.start()
    try:
        with _fixture_directory() as raw:
            fixture = Path(raw).resolve()
            bundle = fixture / "bundle"
            companion = fixture / "companion"
            openclaw_state = fixture / "openclaw-state"
            isolated_home = fixture / "home"
            isolated_home.mkdir(mode=0o700)
            _tracked_bundle(repository, bundle)
            create_disposable_companion_marker(companion)
            create_disposable_openclaw_marker(openclaw_state)
            (openclaw_state / "openclaw.json").write_text(
                json.dumps(
                    _initial_openclaw_config(provider.port, gateway_port),
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            memory_root = companion / "data/memory"
            persona = memory_root / "core/persona.md"
            conversation = (
                memory_root / "users" / SYNTHETIC_USER_ID / "conversation.md"
            )
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

            runtime = CompanionRuntimeLayout(companion, bundle)
            layout = OpenClawReadyCompositionLayout(
                runtime,
                openclaw_state,
                uv,
                uv_sha256,
                "macos",
                api_port,
                gateway_port,
            )
            selection = ProviderSelection(
                ProviderKind.OPENAI_COMPATIBLE_API,
                "synthetic-model",
                base_url=f"http://127.0.0.1:{provider.port}/v1",
            )
            reviewed = build_setup_plan(
                SetupRequest(HostKind.OPENCLAW, "Aster", "feminine", selection),
                (
                    HostProbe(
                        HostKind.OPENCLAW,
                        True,
                        True,
                        executable=str(openclaw),
                        config_path=str(openclaw_state),
                        version="2026.9.1-beta.1",
                        version_status="verified",
                    ),
                ),
            )
            plan = build_dormant_openclaw_ready_plan(reviewed, layout)
            if tuple(step.code for step in plan.steps) != OPENCLAW_READY_STEP_CODES:
                raise RuntimeError("OpenClaw plan is not the exact eight-owner plan.")
            scheduler = ForegroundScheduler(isolated_home)
            control = ForegroundOpenClawControl(
                openclaw,
                openclaw_state,
                isolated_home,
                gateway_port,
            )
            adapter = OpenClawReadyCompositionAdapter(
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
                transaction_id="openclaw-e2e-v0",
                confirmed_plan_digest=public_plan_digest(plan),
            )
            exchange = control.exchange()
            visible_request = _find_visible_request(provider.requests)
            _assert_request(
                visible_request,
                (str(Path.home()), str(repository)),
            )
            if persona.read_bytes() != persona_before:
                raise RuntimeError("persona.md changed during qualification.")
            if conversation.read_bytes() != conversation_before:
                raise RuntimeError("conversation.md changed during qualification.")
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
                timeout=30,
                label="durable L0 evidence",
            )
            try:
                _wait_until(
                    lambda: _ready(api_port),
                    timeout=180,
                    label="OpenClaw runtime attestation",
                )
            except TimeoutError as error:
                raise RuntimeError(
                    "OpenClaw runtime attestation failed: "
                    f"{_ready_report(api_port)!r}."
                ) from error
            restart_started = time.monotonic()
            l0_before_restart = _l0_turn_count(runtime.memory_root)
            request_count = len(provider.requests)
            control.restart_gateway()
            restart_session = (
                "agent:main:explicit:companion-openclaw-restarted"
                if args.restart_session == "new"
                else "agent:main:explicit:companion-openclaw-e2e"
            )
            control.exchange(restart_session)
            _assert_request(
                _find_visible_request(provider.requests[request_count:]),
                (str(Path.home()), str(repository)),
            )
            _wait_until(
                lambda: _l0_turn_count(runtime.memory_root) > l0_before_restart,
                timeout=30, label="new-session L0 after restart",
            )
            try:
                _wait_until(lambda: _ready(api_port), timeout=180, label="readiness after restart")
            except TimeoutError as error:
                raise TimeoutError(f"{error} Last report: {_ready_report(api_port)}") from error
            if args.exercise_souvenir_read and not souvenir_read_fixture.tool_round_trip_seen(provider.requests):
                raise RuntimeError("The native host never returned the exact souvenir tool proof.")
            result_summary = {
                "schema": "daemon.companion-openclaw-e2e-result.v1",
                "status": transaction.status,
                "step_codes": list(transaction.completed_steps),
                "runtime_revision": release.name,
                "visible_response": EXPECTED_RESPONSE,
                "exchange_json": bool(exchange),
                "provider_request_count": len(provider.requests),
                "souvenir_tool_round_trip": "ok" if args.exercise_souvenir_read else "not_tested",
                "memory_evidence": list(_memory_evidence_or_empty(runtime.memory_root)),
                "heartbeat": "ok",
                "ready": "ok",
                "restart_session": args.restart_session,
                "restart_new_session": "ok" if args.restart_session == "new" else "not_tested",
                "restart_same_session": "ok" if args.restart_session == "same" else "not_tested",
                "restart_exchange_seconds": round(time.monotonic() - restart_started, 3),
                "persona_unchanged": True,
                "conversation_unchanged": True,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
            }
            control.stop_gateway()
            scheduler.stop()
    finally:
        if control is not None:
            control.stop_gateway()
        if scheduler is not None:
            scheduler.stop()
        provider.stop()

    for port in (api_port, gateway_port, provider.port):
        if not _is_port_closed(port):
            raise RuntimeError(f"Qualification loopback port remained open: {port}.")
    if _profile_fingerprint(real_home) != real_profile_before:
        raise RuntimeError("Personal OpenClaw profile changed during qualification.")
    if _openclaw_processes() != real_processes_before:
        raise RuntimeError("Personal OpenClaw gateway processes changed during qualification.")
    print(json.dumps(result_summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
