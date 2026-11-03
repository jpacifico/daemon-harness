"""The radar temporal-fact bench: inputs and counting, with a synthetic provider."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/evaluate_radar_temporal.py"


@pytest.fixture
def bench():
    spec = importlib.util.spec_from_file_location("evaluate_radar_temporal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Provider:
    def __init__(self, flag):
        self.flag, self.messages = flag, None

    def complete(self, messages, temperature=0.0):
        self.messages = messages
        return json.dumps({"context_depth": "light", "needs_temporal_memory": self.flag,
                           "reason": "synthetic", "confidence": 0.9})


def test_the_radar_sees_the_message_its_reply_and_the_routines(bench):
    provider = Provider(True)
    row = bench.run_call(bench.CASES[0], "opus", provider)
    assert row["flag"] is True and row["mode"] == "fast" and row["fallback"] is False
    prompt = "\n".join(m["content"] for m in provider.messages)
    case_id, _, _, message, reply = bench.CASES[0]
    assert message in prompt and reply in prompt
    assert "Chaque jour | Camille | réveil | 06h00" in prompt


def test_a_failing_provider_is_counted_as_a_fallback_not_a_judgment(bench):
    class Broken:
        def complete(self, messages, temperature=0.0):
            raise RuntimeError("synthetic outage")

    row = bench.run_call(bench.CASES[0], "astra", Broken())
    assert row["fallback"] is True and row["flag"] is False
    summary = bench.summary([row])
    assert summary["astra"]["fallbacks"] == 1 and summary["astra"]["positives_flagged"] == "0/0"


def test_cases_are_balanced_and_unique(bench):
    ids = [case[0] for case in bench.CASES]
    assert len(ids) == len(set(ids)) == 17
    assert sum(case[1] for case in bench.CASES) == 9
