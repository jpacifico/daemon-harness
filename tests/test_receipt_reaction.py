import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daemon_core import receipt_reaction
from daemon_core.receipt_reaction import receipt_reaction_emoji, schedule_receipt_reaction

ORIGIN = {"platform": "telegram", "chat_id": "12345"}


def _capture(monkeypatch, calls):
    def _fake(req, timeout=0):
        calls.append(json.loads(req.data.decode("utf-8")))

    monkeypatch.setattr(receipt_reaction._urllib_request, "urlopen", _fake)


def test_reaction_fires_after_delay(monkeypatch):
    monkeypatch.delenv("DAEMON_CORE_RECEIPT_REACTION", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    calls = []
    _capture(monkeypatch, calls)

    cancel = schedule_receipt_reaction(ORIGIN, "777", delay_seconds=0.05)
    assert cancel is not None
    time.sleep(0.15)

    assert len(calls) == 1
    assert calls[0]["message_id"] == 777
    assert calls[0]["reaction"] == [{"type": "emoji", "emoji": "👀"}]


def test_fast_turn_cancels_before_delay(monkeypatch):
    monkeypatch.delenv("DAEMON_CORE_RECEIPT_REACTION", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    calls = []
    _capture(monkeypatch, calls)

    cancel = schedule_receipt_reaction(ORIGIN, "777", delay_seconds=0.3)
    cancel.set()
    time.sleep(0.1)

    assert calls == []


def test_flag_off_and_custom_emoji(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_RECEIPT_REACTION", "off")
    assert receipt_reaction_emoji() is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert schedule_receipt_reaction(ORIGIN, "1") is None
    monkeypatch.setenv("DAEMON_CORE_RECEIPT_REACTION", "🤔")
    assert receipt_reaction_emoji() == "🤔"


def test_requires_token_platform_and_message_id(monkeypatch):
    monkeypatch.delenv("DAEMON_CORE_RECEIPT_REACTION", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert schedule_receipt_reaction(ORIGIN, "1") is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert schedule_receipt_reaction({"platform": "web", "chat_id": "1"}, "1") is None
    assert schedule_receipt_reaction(ORIGIN, None) is None
    assert schedule_receipt_reaction(None, "1") is None
