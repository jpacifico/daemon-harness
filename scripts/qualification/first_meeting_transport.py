"""Native gateway transport probe using a loopback Bot API and real configured model.

No live Telegram token, account, or user is used. Host sources stay unchanged.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
import re
import time

from scripts.qualification.companion_telegram_fixture import TelegramFixture, TOKEN, USER_ID


class TelegramAdapterProbe:
    user_id = USER_ID

    def __init__(self):
        self.fixture = TelegramFixture()
        self.start_result = None
        self.exchanges = []

    def configure_openclaw(self, config):
        config["plugins"]["allow"].append("telegram")
        config["plugins"]["entries"]["telegram"] = {"enabled": True}
        config["channels"] = {"telegram": {"enabled": True, "botToken": TOKEN,
            "apiRoot": f"http://127.0.0.1:{self.fixture.port}",
            "dmPolicy": "allowlist", "allowFrom": [str(USER_ID)],
            "groupPolicy": "disabled"}}
        config["agents"]["defaults"]["thinkingDefault"] = "medium"

    def configure_hermes(self, profile, env):
        import yaml
        path = profile / "config.yaml"
        config = yaml.safe_load(path.read_text())
        config["plugins"]["enabled"] = ["daemon_core", "daemon_voice"]
        config["agent"] = {"reasoning_effort": "medium", "max_turns": 5}
        config["platforms"] = {"telegram": {"enabled": True, "extra": {
            "base_url": f"http://127.0.0.1:{self.fixture.port}/bot",
            "drop_pending_on_cold_boot": True}}}
        path.write_text(yaml.safe_dump(config))
        env.update(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_ALLOWED_USERS=str(USER_ID),
                   TELEGRAM_HOME_CHANNEL=str(USER_ID))
        with (profile / ".env").open("a") as stream:
            for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHANNEL"):
                stream.write(name + "=" + env[name] + "\n")

    def start(self):
        self.fixture.start()

    def stop(self):
        self.fixture.stop()

    def wait_ready(self, process):
        # Require a fresh poll after this gateway startup, not an old getMe.
        before = self.fixture.methods.count("getupdates")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Native Telegram gateway exited during startup")
            if self.fixture.methods.count("getupdates") > before:
                return
            time.sleep(0.2)
        raise TimeoutError("Native gateway did not poll the loopback Bot API")

    def hermes_start(self, process, log):
        sent = len(self.fixture.sent)
        self.fixture.submit("/start")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            native_log = log.parent / "hermes-home/logs/gateway.log"
            content = native_log.read_text(errors="replace") if native_log.exists() else ""
            if "Ignoring /start platform ping" in content:
                assert len(self.fixture.sent) == sent, "Unexpected Hermes /start reply"
                self.start_result = {"handled": True, "reply": "", "evidence": "native gateway log and Bot API sends"}
                return
            if process.poll() is not None:
                raise RuntimeError("Hermes exited while handling /start")
            time.sleep(0.2)
        raise TimeoutError("No native Hermes /start receipt")

    @staticmethod
    def plain(text):
        return html.unescape(re.sub(r"<[^>]+>", "", text)).replace("**", "").replace("*", "").strip()

    def exchange(self, prompt, memory, process):
        from daemon_core.scene_model import _parse_dialogue_turns
        before = len(self.fixture.displayed)
        began = time.monotonic()
        self.fixture.submit(prompt)
        deadline = began + 150
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Native Telegram gateway exited during response")
            turns = [t for p in (memory / "levels/L0_ground_zero/conversations").glob("*.md")
                     for t in _parse_dialogue_turns(p.read_text())]
            answer = None
            for index, (_, role, body) in enumerate(turns):
                if role == "user" and body.strip() == prompt and index + 1 < len(turns):
                    if turns[index + 1][1] == "assistant":
                        answer = turns[index + 1][2].strip()
            displayed = self.fixture.displayed[before:]
            if answer and any(self.plain(answer) == self.plain(text) for text in displayed):
                timing = {"voice_seconds": time.monotonic() - began,
                          "first_delta_seconds": self.fixture.delivery_seconds[before]}
                self.exchanges.append({"prompt": prompt, "answer": answer, **timing,
                                       "bot_api_deliveries": len(displayed)})
                return answer, timing
            time.sleep(0.2)
        raise TimeoutError("No matching canonical answer and native Bot API delivery")

    def summary(self):
        return {"remote_telegram": False, "hermes_start": self.start_result,
                "exchanges": self.exchanges, "unknown_methods": self.fixture.unknown,
                "methods": sorted(set(self.fixture.methods)), "displayed": self.fixture.displayed}
