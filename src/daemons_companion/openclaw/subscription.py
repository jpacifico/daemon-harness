"""Companion adapter for the pinned OpenClaw provider's native ChatGPT login."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from daemons_companion.runtime.bootstrap import (
    clean_environment,
    private_home_environment,
)
from daemons_companion.openclaw.host_install import verify_installed, direct
from daemons_companion.setup.model_selection import SUPPORTED_MODELS


STAGES = {"starting", "waiting", "exchanging", "authenticated", "catalog"}
STAGE_MESSAGES = {
    "starting": "Preparing the ChatGPT connection...",
    "waiting": "Waiting for ChatGPT authorization. If you have already confirmed in your browser, keep this window open; confirmation can take several dozen seconds.",
    "exchanging": "Authorization received. Completing the ChatGPT connection...",
    "authenticated": "ChatGPT connected. Preparing model discovery...",
    "catalog": "ChatGPT connected. Loading the models reported by OpenClaw...",
}


class SubscriptionFailure(RuntimeError):
    def __init__(self, stage):
        self.stage = stage
        super().__init__(
            "ChatGPT is connected, but the model list could not be confirmed. Retry loading the models; your connection is retained."
            if stage in {"authenticated", "catalog"} else
            "ChatGPT authorization was not completed. Retry the connection or choose another provider."
        )


def valid_catalog(value):
    if not isinstance(value, dict) or set(value) != {
        "status",
        "raw_catalog_observed",
        "native_count",
        "excluded_count",
        "models",
    }:
        return False
    if value["status"] != "ready" or value["raw_catalog_observed"] is not False:
        return False
    if any(
        type(value[k]) is not int or not 0 <= value[k] <= 10000
        for k in ("native_count", "excluded_count")
    ):
        return False
    rows = value["models"]
    if not isinstance(rows, list) or len(rows) != len(SUPPORTED_MODELS):
        return False
    if any(
        not isinstance(row, dict)
        or set(row) != {"id", "status"}
        or row["id"] != model.identifier
        or not isinstance(row["status"], str)
        or row["status"] not in {"offered", "absent-native"}
        for row, model in zip(rows, SUPPORTED_MODELS)
    ):
        return False
    return value["native_count"] == value["excluded_count"] + sum(
        row["status"] == "offered" for row in rows
    )


def environment(host):
    verify_installed(host)
    env = clean_environment(host.attempt)
    env.update(private_home_environment(host.attempt / "os-home"))
    env.update(
        OPENCLAW_STATE_DIR=str(host.profile),
        OPENCLAW_CONFIG_PATH=str(host.profile / "openclaw.json"),
    )
    return env


def connect(host, emit=lambda _: None, *, catalog_only=False, cancelled=lambda: False):
    from daemons_companion.desktop.terminal import Quit

    env = environment(host)
    script = (
        Path(__file__).resolve().parents[3] / "scripts/companion_openclaw_chatgpt.mjs"
    )
    direct(script)
    process = subprocess.Popen(
        [
            str(host.node),
            str(script),
            str(host.entry.parent),
            str(host.profile),
            "catalog" if catalog_only else "login",
        ],
        env=env,
        cwd=host.attempt,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    events = queue.Queue()

    def read():
        while True:
            line = process.stdout.readline(8193)
            if not line:
                break
            events.put(line)
        events.put(None)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    models = None
    stage = "starting"
    deadline = time.monotonic() + (90 if catalog_only else 900)
    try:
        while time.monotonic() < deadline:
            if cancelled():
                raise Quit()
            try:
                line = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(value, dict):
                continue
            if (
                set(value) == {"stage"}
                and isinstance(value["stage"], str)
                and value["stage"] in STAGES
            ):
                stage = value["stage"]
                emit(value)
            elif set(value) == {"catalog"} and valid_catalog(value["catalog"]):
                emit(value)
            elif set(value) == {"models"}:
                allowed = {m.identifier for m in SUPPORTED_MODELS}
                if (
                    isinstance(value["models"], list)
                    and value["models"]
                    and all(
                        isinstance(m, str) and m in allowed for m in value["models"]
                    )
                ):
                    models = tuple(value["models"])
            elif (
                set(value) == {"url"}
                and value["url"] == "https://auth.openai.com/codex/device"
            ):
                emit(value)
            elif set(value) == {"code"}:
                import re

                if isinstance(value["code"], str) and re.fullmatch(
                    r"[A-Z0-9-]{4,32}", value["code"]
                ):
                    emit(value)
        if process.poll() is None:
            process.wait(timeout=2)
        if process.returncode or not models:
            raise SubscriptionFailure(stage)
        return models
    except (subprocess.TimeoutExpired, OSError) as error:
        raise SubscriptionFailure(stage) from error
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        thread.join(timeout=2)
        process.stdout.close()


def chatgpt_events(ui):
    """Show native OpenClaw ChatGPT sign-in progress: stages, code and link."""

    def emit(event):
        if "stage" in event:
            message = STAGE_MESSAGES[event["stage"]]
            if hasattr(ui, "emit"):
                ui.emit("chatgpt-stage", stage=event["stage"])
                ui.emit("activity", active=True, message=message)
                ui.emit(
                    "diagnostic",
                    phase="model",
                    operation="sign-in",
                    status="started",
                    auth_stage=event["stage"],
                )
            else:
                ui.say(message)
        if "catalog" in event and hasattr(ui, "emit"):
            ui.emit("model-catalog", catalog=event["catalog"])
            ui.emit(
                "diagnostic",
                phase="model",
                operation="model-check",
                status="completed",
                model_catalog=event["catalog"],
            )
        if "code" in event:
            if hasattr(ui, "emit"):
                ui.emit("chatgpt-code", code=event["code"])
            else:
                ui.say("Enter this code in the ChatGPT sign-in page: " + event["code"])
        if "url" in event:
            if hasattr(ui, "emit"):
                ui.emit("link", url=event["url"], open=True)
            else:
                ui.say(event["url"])

    return emit


def first_connection(host, ui, saved):
    from daemons_companion.setup.codex_install import install_private_codex
    from daemons_companion.setup.subscription import native_command
    from daemons_companion.setup.model_selection import select_model

    if hasattr(ui, "phase"):
        ui.phase("model")
    if hasattr(ui, "emit"):
        ui.emit("connection-start")
    runner = getattr(ui, "subscription_runner", native_command)
    with ui.activity("Preparing your private ChatGPT connection...") as progress:
        binary = install_private_codex(host, progress)
    env = environment(host)
    auth = direct(host.attempt / "os-home/.codex")
    auth.mkdir(mode=0o700, exist_ok=True)
    for name in ("auth.json", "config.toml"):
        direct(auth / name)
    env["CODEX_HOME"] = str(auth)
    command = [
        str(binary),
        "-c",
        'cli_auth_credentials_store="file"',
        "-c",
        'forced_login_method="chatgpt"',
    ]

    def ready():
        result = runner(command + ["login", "status"], env)
        return (
            result.returncode == 0
            and "logged in using chatgpt" in (result.stdout + result.stderr).lower()
        )

    if not ready():
        ui.say(
            "Connect ChatGPT for memory work first. OpenClaw conversations use a separate native connection next."
        )
        browser = dict(env)
        if host.target_os == "windows":
            browser.update(
                {
                    k: os.environ[k]
                    for k in ("USERPROFILE", "APPDATA", "LOCALAPPDATA")
                    if k in os.environ
                }
            )
        if (
            runner(command + ["login"], browser, interactive=True).returncode
            or not ready()
        ):
            raise RuntimeError(
                "ChatGPT memory connection was not completed. Setup has not advanced."
            )

    emit = chatgpt_events(ui)

    if hasattr(ui, "emit"):
        ui.emit("connection-start")
    with ui.activity("Connect ChatGPT for OpenClaw conversations..."):
        models = connect(host, emit, cancelled=lambda: getattr(ui, "cancelled", False))
    ui.say("ChatGPT connection verified.")
    first = True

    def load_models():
        nonlocal first, models
        if first:
            first = False
        else:
            models = connect(host, emit, catalog_only=True,
                             cancelled=lambda: getattr(ui, "cancelled", False))
        return tuple(m for m in SUPPORTED_MODELS if m.identifier in models)

    model = select_model(
        ui,
        load_models,
        saved.get("model", ""),
        resume=bool(saved.get("model")),
        show_unavailable=False,
    )
    return binary, model
