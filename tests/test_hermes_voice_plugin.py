import importlib.util
import asyncio

import pytest
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "daemon_voice_resolution_test",
    ROOT / "integrations" / "hermes_voice_plugin" / "__init__.py",
)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def test_response_payload_removes_private_turn_metadata():
    leaked = (
        "[heure locale du tour : 2026-07-25T17:20:22]\n"
        "[daemon-turn: direct]\n"
        "Profitons de la soirée."
    )
    response = SimpleNamespace(
        output_text=leaked,
        output=[
            SimpleNamespace(
                content=[SimpleNamespace(text=leaked)],
            )
        ],
    )

    cleaned = plugin._sanitize_response_payload(response)

    assert cleaned.output_text == "Profitons de la soirée."
    assert cleaned.output[0].content[0].text == "Profitons de la soirée."


def test_degraded_response_payload_adds_one_transport_notice():
    from daemon_core.runtime_degradation import DEGRADED_RESPONSE_NOTICE

    response = SimpleNamespace(
        output_text="Je peux encore utiliser les outils.",
        output=[
            SimpleNamespace(
                content=[SimpleNamespace(text="Je peux encore utiliser les outils.")],
            )
        ],
    )

    cleaned = plugin._sanitize_response_payload(response, degraded=True)

    expected = (
        f"{DEGRADED_RESPONSE_NOTICE}\n\n"
        "Je peux encore utiliser les outils."
    )
    assert cleaned.output_text == expected
    assert cleaned.output[0].content[0].text == expected


def test_stream_probe_holds_and_removes_split_turn_metadata():
    class Consumer:
        def __init__(self):
            self.deltas = []
            self.finished = False

        def on_delta(self, text):
            self.deltas.append(text)

        def finish(self):
            self.finished = True

        async def _send_or_edit(
            self,
            _text,
            *,
            finalize=False,
            is_turn_final=True,
        ):
            return True

    assert plugin._install_stream_probe(Consumer) is True
    consumer = Consumer()

    consumer.on_delta("[heure locale du ")
    consumer.on_delta(
        "tour : 2026-07-25T17:20:22]\n"
        "[daemon-turn: direct]\n"
        "Bonsoir"
    )
    consumer.on_delta(" à toi.")
    consumer.finish()

    assert consumer.deltas == ["Bonsoir", " à toi."]
    assert consumer.finished is True


def test_stream_probe_accepts_and_sanitizes_hermes_021_final_text():
    class Consumer:
        def __init__(self):
            self.final_text = None

        def on_delta(self, _text):
            return None

        def finish(self, final_text=None):
            self.final_text = final_text

        async def _send_or_edit(
            self,
            _text,
            *,
            finalize=False,
            is_turn_final=True,
        ):
            return True

    assert plugin._install_stream_probe(Consumer) is True
    consumer = Consumer()

    consumer.finish(
        "[heure locale du tour : 2026-09-02T20:15:00]\n"
        "[daemon-turn: direct]\n"
        "Réponse finale augmentée par Hermes."
    )

    assert consumer.final_text == "Réponse finale augmentée par Hermes."


def test_stream_probe_filters_each_generation_after_a_tool_boundary():
    class Consumer:
        def __init__(self):
            self.deltas = []
            self.segment_breaks = 0

        def on_delta(self, text):
            if text is None:
                self.on_segment_break()
            else:
                self.deltas.append(text)

        def on_segment_break(self):
            self.segment_breaks += 1

        def finish(self):
            return None

        async def _send_or_edit(
            self,
            _text,
            *,
            finalize=False,
            is_turn_final=True,
        ):
            return True

    assert plugin._install_stream_probe(Consumer) is True
    consumer = Consumer()

    consumer.on_delta("[daemon-turn: direct]\n")
    consumer.on_delta(None)
    consumer.on_delta(
        "[daemon-turn:condition] Ton agenda ne couvre plus cette reprise."
    )
    consumer.on_delta("\nCommence par le reconnecter.")
    consumer.finish()

    assert consumer.segment_breaks == 1
    assert "".join(consumer.deltas) == (
        "Ton agenda ne couvre plus cette reprise.\n"
        "Commence par le reconnecter."
    )


def test_stream_probe_discards_private_preamble_before_tool_boundary():
    class Consumer:
        def __init__(self):
            self.deltas = []
            self.segment_breaks = 0

        def on_delta(self, text):
            if text is None:
                self.on_segment_break()
            else:
                self.deltas.append(text)

        def on_segment_break(self):
            self.segment_breaks += 1

        def finish(self):
            return None

        async def _send_or_edit(
            self,
            _text,
            *,
            finalize=False,
            is_turn_final=True,
        ):
            return True

    assert plugin._install_stream_probe(Consumer) is True
    consumer = Consumer()

    consumer.on_delta("\n")
    consumer.on_delta(
        "[daemon-turn: condition] Si la souvenir le prouve, je te le dis.\n"
    )
    consumer.on_delta(None)
    consumer.on_delta("\n[daemon-turn: direct]\nRéponse après lecture.")
    consumer.finish()

    assert consumer.segment_breaks == 1
    assert consumer.deltas == ["Réponse après lecture."]


def test_stream_probe_sanitizes_again_at_platform_boundary():
    class Consumer:
        def __init__(self):
            self.sent = []

        def on_delta(self, _text):
            return None

        def finish(self):
            return None

        async def _send_or_edit(
            self,
            text,
            *,
            finalize=False,
            is_turn_final=True,
        ):
            self.sent.append((text, finalize, is_turn_final))
            return True

    assert plugin._install_stream_probe(Consumer) is True
    consumer = Consumer()

    result = asyncio.run(
        consumer._send_or_edit(
            "\n[daemon-turn: direct]\nRéponse visible.",
            finalize=True,
        )
    )

    assert result is True
    assert consumer.sent == [("Réponse visible.", True, True)]


def test_stream_probe_marks_degraded_delivery_without_changing_model_text():
    from daemon_core.runtime_degradation import DEGRADED_RESPONSE_NOTICE

    class Consumer:
        def __init__(self):
            self.sent = []
            self._daemon_latency_record = {"degraded": True}

        def on_delta(self, _text):
            return None

        async def _send_or_edit(
            self,
            text,
            *,
            finalize=False,
            is_turn_final=True,
        ):
            self.sent.append((text, finalize, is_turn_final))
            return True

    assert plugin._install_stream_probe(Consumer) is True
    consumer = Consumer()

    result = asyncio.run(
        consumer._send_or_edit("Réponse native.", finalize=True)
    )

    assert result is True
    assert consumer.sent == [
        (
            f"{DEGRADED_RESPONSE_NOTICE}\n\nRéponse native.",
            True,
            True,
        )
    ]


def test_stream_probe_deletes_exact_canary_commentary_after_final_delivery(
    monkeypatch,
):
    monkeypatch.setenv("DAEMON_CORE_HERMES_PROGRESS_PREAMBLE", "canary")
    monkeypatch.setenv(
        "DAEMON_CORE_HERMES_PROGRESS_PREAMBLE_PLATFORMS",
        "telegram",
    )

    class Result:
        success = True
        message_id = "commentary-41"
        continuation_message_ids = ()

    class Adapter:
        platform = SimpleNamespace(value="telegram")

        def __init__(self):
            self.sent = []
            self.deleted = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)
            return Result()

        async def delete_message(self, chat_id, message_id):
            self.deleted.append((chat_id, message_id))
            return True

    class Consumer:
        def __init__(self):
            self.adapter = Adapter()
            self.chat_id = "4100000002"
            self.metadata = {"thread": "dm"}
            self._already_sent = False
            self._last_sent_text = ""
            self._message_id = None
            self.notifications = 0

        def _clean_for_display(self, text):
            return text.strip()

        def _notify_new_message(self):
            self.notifications += 1

        def on_delta(self, _text):
            return None

        async def _send_commentary(self, _text):
            raise AssertionError("the canary path must retain the exact send id")

        async def _send_or_edit(
            self,
            text,
            *,
            finalize=False,
            is_turn_final=True,
        ):
            self._already_sent = True
            self._last_sent_text = text
            self._message_id = "final-42"
            return True

    assert plugin._install_stream_probe(Consumer) is True
    consumer = Consumer()

    commentary_sent = asyncio.run(
        consumer._send_commentary(" Je vérifie la météo exacte. ")
    )

    assert commentary_sent is True
    assert consumer.adapter.sent == [
        {
            "chat_id": "4100000002",
            "content": "Je vérifie la météo exacte.",
            "metadata": {"thread": "dm"},
        }
    ]
    assert consumer._daemon_progress_preamble_message_ids == ["commentary-41"]
    assert consumer.notifications == 1
    assert consumer.adapter.deleted == []

    final_sent = asyncio.run(
        consumer._send_or_edit(
            "Réponse finale.",
            finalize=True,
            is_turn_final=True,
        )
    )

    assert final_sent is True
    assert consumer.adapter.deleted == [("4100000002", "commentary-41")]
    assert consumer._daemon_progress_preamble_message_ids == []
    assert consumer._message_id == "final-42"


def test_stream_probe_cleanup_is_fail_open_and_never_deletes_final_message(
    monkeypatch,
):
    monkeypatch.setenv("DAEMON_CORE_HERMES_PROGRESS_PREAMBLE", "canary")
    monkeypatch.setenv(
        "DAEMON_CORE_HERMES_PROGRESS_PREAMBLE_PLATFORMS",
        "telegram",
    )

    class Adapter:
        platform = SimpleNamespace(value="telegram")

        def __init__(self):
            self.deleted = []

        async def delete_message(self, chat_id, message_id):
            self.deleted.append((chat_id, message_id))
            return False

    class Consumer:
        def __init__(self):
            self.adapter = Adapter()
            self.chat_id = "4100000002"
            self._already_sent = False
            self._last_sent_text = ""
            self._message_id = None
            self._daemon_progress_preamble_message_ids = [
                "commentary-51",
                "final-52",
            ]

        def on_delta(self, _text):
            return None

        async def _send_or_edit(
            self,
            text,
            *,
            finalize=False,
            is_turn_final=True,
        ):
            self._already_sent = True
            self._last_sent_text = text
            self._message_id = "final-52"
            return True

    assert plugin._install_stream_probe(Consumer) is True
    consumer = Consumer()

    final_sent = asyncio.run(
        consumer._send_or_edit(
            "Réponse finale.",
            finalize=True,
            is_turn_final=True,
        )
    )

    assert final_sent is True
    assert consumer.adapter.deleted == [("4100000002", "commentary-51")]
    assert consumer._daemon_progress_preamble_message_ids == [
        "commentary-51",
        "final-52",
    ]
    assert consumer._message_id == "final-52"


def test_stream_probe_tracks_streamed_preamble_at_the_tool_boundary(
    monkeypatch,
):
    monkeypatch.setenv("DAEMON_CORE_HERMES_PROGRESS_PREAMBLE", "canary")
    monkeypatch.setenv(
        "DAEMON_CORE_HERMES_PROGRESS_PREAMBLE_PLATFORMS",
        "telegram",
    )

    class Adapter:
        platform = SimpleNamespace(value="telegram")

        def __init__(self):
            self.deleted = []

        async def delete_message(self, chat_id, message_id):
            self.deleted.append((chat_id, message_id))
            return True

    class Consumer:
        def __init__(self):
            self.adapter = Adapter()
            self.chat_id = "4100000002"
            self._already_sent = False
            self._last_sent_text = ""
            self._message_id = None
            self._preview_message_ids = set()

        def on_delta(self, _text):
            return None

        async def _send_or_edit(
            self,
            text,
            *,
            finalize=False,
            is_turn_final=True,
        ):
            self._already_sent = True
            self._last_sent_text = text
            self._message_id = (
                "final-62" if is_turn_final else "commentary-61"
            )
            self._preview_message_ids.add(self._message_id)
            return True

    assert plugin._install_stream_probe(Consumer) is True
    consumer = Consumer()

    preamble_sent = asyncio.run(
        consumer._send_or_edit(
            "Je vérifie la météo exacte.",
            finalize=True,
            is_turn_final=False,
        )
    )

    assert preamble_sent is True
    assert consumer._daemon_progress_preamble_message_ids == [
        "commentary-61"
    ]
    assert consumer.adapter.deleted == []

    final_sent = asyncio.run(
        consumer._send_or_edit(
            "Réponse finale.",
            finalize=True,
            is_turn_final=True,
        )
    )

    assert final_sent is True
    assert consumer.adapter.deleted == [("4100000002", "commentary-61")]
    assert consumer._daemon_progress_preamble_message_ids == []
    assert consumer._message_id == "final-62"


def test_stream_probe_leaves_native_commentary_unchanged_outside_canary(
    monkeypatch,
):
    monkeypatch.setenv("DAEMON_CORE_HERMES_PROGRESS_PREAMBLE", "off")

    class Adapter:
        platform = SimpleNamespace(value="telegram")

    class Consumer:
        def __init__(self):
            self.adapter = Adapter()
            self.original_commentary = []

        def on_delta(self, _text):
            return None

        async def _send_commentary(self, text):
            self.original_commentary.append(text)
            return True

        async def _send_or_edit(
            self,
            _text,
            *,
            finalize=False,
            is_turn_final=True,
        ):
            return True

    assert plugin._install_stream_probe(Consumer) is True
    consumer = Consumer()

    assert asyncio.run(consumer._send_commentary("Message natif.")) is True
    assert consumer.original_commentary == ["Message natif."]
    assert not hasattr(consumer, "_daemon_progress_preamble_message_ids")


class _TrackingAgent:
    def __init__(self):
        self._current_streamed_assistant_text = ""
        self._empty_content_retries = 0

    def _reset_stream_delivery_tracking(self):
        self._current_streamed_assistant_text = ""

    def _record_streamed_assistant_text(self, text):
        self._current_streamed_assistant_text += text


def test_visible_stream_tracking_does_not_count_commitment_as_partial_delivery():
    class Agent(_TrackingAgent):
        pass

    assert plugin._install_visible_stream_tracking(Agent) is True
    agent = Agent()
    agent._reset_stream_delivery_tracking()

    agent._record_streamed_assistant_text("[daemon-tu")
    agent._record_streamed_assistant_text("rn: exchange]")

    assert agent._current_streamed_assistant_text == ""


def test_visible_stream_tracking_installs_after_early_plugin_registration(
    monkeypatch,
):
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace())
    assert plugin._install_visible_stream_tracking() is False

    class Agent(_TrackingAgent):
        pass

    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        SimpleNamespace(AIAgent=Agent),
    )
    assert plugin._install_visible_stream_tracking() is True
    assert Agent._daemon_visible_stream_tracking_installed is True


def test_visible_stream_tracking_preserves_normal_and_partial_answers():
    class Agent(_TrackingAgent):
        pass

    assert plugin._install_visible_stream_tracking(Agent) is True
    agent = Agent()
    agent._reset_stream_delivery_tracking()

    agent._record_streamed_assistant_text("[daemon-turn: exchange]\n")
    agent._record_streamed_assistant_text("Début de réponse")

    assert agent._current_streamed_assistant_text == "Début de réponse"
    agent._empty_content_retries = 1
    agent._reset_stream_delivery_tracking()
    assert agent._empty_content_retries == 1

    agent._record_streamed_assistant_text("Réponse ordinaire.")
    assert agent._current_streamed_assistant_text == "Réponse ordinaire."


def test_visible_stream_tracking_sanitizes_commentary_before_hermes_dedupes_it():
    class Agent(_TrackingAgent):
        def __init__(self):
            super().__init__()
            self.delivered_commentary = set()
            self.sent_commentary = []

        def _fire_streamed_codex_commentary(self, text):
            key = text.strip()
            if key in self.delivered_commentary:
                return None
            self.delivered_commentary.add(key)
            self.sent_commentary.append(text)
            return None

    assert plugin._install_visible_stream_tracking(Agent) is True
    agent = Agent()

    agent._fire_streamed_codex_commentary(
        "[daemon-turn: direct]\nJe vérifie la trace exacte."
    )
    # Hermes later replays the same completed item after response sanitizing.
    agent._fire_streamed_codex_commentary("Je vérifie la trace exacte.")

    assert agent.sent_commentary == ["Je vérifie la trace exacte."]
    assert agent.delivered_commentary == {"Je vérifie la trace exacte."}


def test_visible_stream_tracking_discards_metadata_only_commentary():
    class Agent(_TrackingAgent):
        def __init__(self):
            super().__init__()
            self.sent_commentary = []

        def _fire_streamed_codex_commentary(self, text):
            self.sent_commentary.append(text)

    assert plugin._install_visible_stream_tracking(Agent) is True
    agent = Agent()

    agent._fire_streamed_codex_commentary("[daemon-turn: exchange]")

    assert agent.sent_commentary == []


def test_split_system_turns_preserves_live_state_at_the_turn_threshold():
    system, turns = plugin._split_system_turns(
        [
            {"role": "system", "content": "persona and memory"},
            {"role": "developer", "content": "durable background"},
            {"role": "user", "content": "previous"},
            {"role": "assistant", "content": "reply"},
            {"role": "system", "content": "live state"},
            {"role": "user", "content": "current"},
        ]
    )

    assert system == "persona and memory"
    assert [turn["role"] for turn in turns] == [
        "developer",
        "user",
        "assistant",
        "system",
        "user",
    ]


def test_hermes_progress_preamble_is_explicit_idempotent_and_platform_scoped(
    monkeypatch,
):
    prompt = "SYSTEME CANONIQUE"

    assert plugin._with_hermes_progress_preamble(prompt, enabled=False) == prompt

    enabled = plugin._with_hermes_progress_preamble(prompt, enabled=True)
    repeated = plugin._with_hermes_progress_preamble(enabled, enabled=True)

    assert repeated == enabled
    assert 'In the same generation as the first tool' in enabled
    assert 'without an unverified fact or tool name' in enabled
    assert 'Without a tool, respond directly' in enabled

    monkeypatch.setenv("DAEMON_CORE_HERMES_PROGRESS_PREAMBLE", "canary")
    monkeypatch.setenv(
        "DAEMON_CORE_HERMES_PROGRESS_PREAMBLE_PLATFORMS",
        "telegram",
    )
    assert plugin._hermes_progress_preamble_canary_enabled("telegram") is True
    assert plugin._hermes_progress_preamble_canary_enabled("discord") is False

    monkeypatch.setenv("DAEMON_CORE_HERMES_PROGRESS_PREAMBLE", "on")
    assert plugin._hermes_progress_preamble_canary_enabled("telegram") is False


def test_codex_rewrite_keeps_instruction_roles_out_of_responses_input():
    request = {"instructions": "Hermes", "input": [], "tools": [{"name": "web"}]}
    turns = [
        {"role": "developer", "content": "durable background"},
        {"role": "system", "content": "live state"},
        {"role": "user", "content": "current"},
    ]

    rewritten = plugin._rewrite_request(
        request,
        "sovereign system",
        turns,
        "codex_responses",
    )

    assert rewritten["instructions"] == (
        "sovereign system\n\ndurable background\n\nlive state"
    )
    assert rewritten["input"][0] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "current"}],
    }
    assert all(item["role"] in {"user", "assistant"} for item in rewritten["input"])
    assert rewritten["tools"] == request["tools"]


def test_chat_tool_loop_restores_prefix_before_current_tool_tail():
    request = {
        "messages": [
            {"role": "system", "content": "Hermes"},
            {"role": "user", "content": "Ancien fil hôte"},
            {"role": "assistant", "content": "Ancienne réponse"},
            {"role": "user", "content": "Et demain ?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1"}],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "Samedi"},
        ],
        "tools": [{"name": "calendar"}],
    }
    prefix = [
        {"role": "user", "content": "On parlait de la reprise."},
        {"role": "assistant", "content": "Regardons demain."},
        {"role": "system", "content": "Conserve la date du fil."},
    ]

    rewritten = plugin._rewrite_request(
        request,
        "sovereign system",
        None,
        "chat",
        tool_loop_prefix=prefix,
    )

    assert [message["role"] for message in rewritten["messages"]] == [
        "system",
        "user",
        "assistant",
        "system",
        "user",
        "assistant",
        "tool",
    ]
    rendered = str(rewritten["messages"])
    assert "On parlait de la reprise" in rendered
    assert "Et demain ?" in rendered
    assert "Samedi" in rendered
    assert "Ancien fil hôte" not in rendered


def test_invalid_canonical_plan_uses_signaled_host_fallback_and_keeps_tools(monkeypatch):
    import daemon_core.canonical_voice as canonical_voice
    import daemon_core.single_pass_sovereign as single_pass
    import daemon_core.sovereign as sovereign
    import daemon_core.turn_handoff as turn_handoff

    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS_USER_IDS", "u1")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_USER_ID", "u1")
    monkeypatch.setenv("DAEMON_CORE_SRC", "/tmp/src")
    monkeypatch.setattr(
        canonical_voice,
        "compose_canonical_voice_messages",
        lambda *_args, **_kwargs: ([], object()),
    )
    monkeypatch.setattr(
        turn_handoff,
        "read_turn_handoff",
        lambda *_args: ("Un nouveau sujet", "trace-single", 10.0),
    )
    monkeypatch.setattr(
        single_pass,
        "compose_single_pass_sovereign_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("single_pass must stay cold")
        ),
        raising=False,  # Guard against reintroducing the retired fallback.
    )
    monkeypatch.setattr(
        sovereign,
        "read_scene_verdict_with_trace",
        lambda *_args: (_ for _ in ()).throw(AssertionError("P1c must stay cold")),
    )
    plugin._turn_systems.clear()
    plugin._turn_prefixes.clear()
    plugin._turn_latency.clear()
    request = {
        "instructions": "Hermes",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Un nouveau sujet"}],
            }
        ],
        "tools": [{"type": "function", "name": "web_search"}],
        "tool_choice": "auto",
    }

    rewritten = plugin._delegated_request_or_none(
        request,
        platform="telegram",
        api_mode="codex_responses",
        api_call_count=1,
        turn_id="turn-single-pass",
    )

    assert rewritten["instructions"].startswith("Hermes\n\n")
    assert 'Assert no personal fact' in rewritten["instructions"]
    assert rewritten["input"] == request["input"]
    assert rewritten["tools"] == request["tools"]
    assert rewritten["tool_choice"] == "auto"
    assert plugin._turn_latency["turn-single-pass"] == {
        "trace_id": "trace-single",
        "prefetch_started_at": 10.0,
        "turn_id": "turn-single-pass",
        "voice_path": "host_fallback",
        "degraded": True,
        "degradation_reason": "canonical_prepare_failed",
        "error_type": "ValueError",
    }


def test_canonical_voice_keeps_tool_loop_without_historical_feature_flags(monkeypatch):
    import daemon_core.canonical_voice as canonical_voice
    import daemon_core.single_pass_sovereign as single_pass
    import daemon_core.sovereign as sovereign
    import daemon_core.turn_handoff as turn_handoff
    import daemon_core.voice_brief as voice_brief

    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_USER_ID", "u1")
    monkeypatch.setenv("DAEMON_CORE_SRC", "/tmp/src")
    monkeypatch.setattr(
        turn_handoff,
        "read_turn_handoff",
        lambda *_args: ("Question mémoire", "trace-atlas", 10.0),
    )
    hot_handoff = {}
    monkeypatch.setattr(
        turn_handoff,
        "write_turn_hot_souvenirs",
        lambda user_id, trace_id, names: hot_handoff.update(
            user_id=user_id,
            trace_id=trace_id,
            names=tuple(names),
        )
        or True,
    )
    context = type(
        "Context",
        (),
        {
            "atlas": type("Atlas", (), {"souvenir_names": ("theme/a", "theme/b"), "rendered_chars": 120})(),
            "held_thought": object(),
            "hot_memory": type("Hot", (), {"souvenir_names": ("theme/a",), "rendered_chars": 30})(),
            "scene_scope": "telegram__u1",
            "dialogue_coverage": voice_brief.DialogueCoverage(
                status="recent_only",
                dialogue="",
            ),
        },
    )()
    monkeypatch.setattr(
        canonical_voice,
        "compose_canonical_voice_messages",
        lambda *_args, **_kwargs: (
            [
                {"role": "system", "content": "held atlas system"},
                {"role": "user", "content": "Question mémoire"},
            ],
            context,
        ),
    )
    monkeypatch.setattr(voice_brief, "compile_voice_brief", lambda plan: plan)
    monkeypatch.setattr(
        voice_brief,
        "render_reference_messages",
        lambda plan: plan.messages,
    )
    monkeypatch.setattr(
        single_pass,
        "compose_single_pass_sovereign_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("full book must stay cold")
        ),
        raising=False,  # Guard against reintroducing the retired fallback.
    )
    monkeypatch.setattr(
        sovereign,
        "read_scene_verdict_with_trace",
        lambda *_args: (_ for _ in ()).throw(AssertionError("P1c must stay cold")),
    )
    plugin._turn_systems.clear()
    plugin._turn_prefixes.clear()
    plugin._turn_latency.clear()
    request = {
        "instructions": "Hermes",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Question mémoire"}],
            }
        ],
        "tools": [{"type": "function", "name": "daemon_read_souvenirs"}],
        "tool_choice": "auto",
    }

    rewritten = plugin._delegated_request_or_none(
        request,
        platform="telegram",
        api_mode="codex_responses",
        api_call_count=1,
        turn_id="turn-sovereign-library",
    )

    assert rewritten["instructions"] == "held atlas system"
    assert rewritten["tools"] == request["tools"]
    assert hot_handoff == {
        "user_id": "u1",
        "trace_id": "trace-atlas",
        "names": ("theme/a",),
    }
    assert (
        plugin._turn_latency["turn-sovereign-library"]["voice_path"]
        == "canonical"
    )


def test_hermes_progress_preamble_changes_only_the_delegated_system(monkeypatch):
    import daemon_core.turn_handoff as turn_handoff
    import daemon_core.turn_runtime as turn_runtime

    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_USER_ID", "u1")
    monkeypatch.setenv("DAEMON_CORE_SRC", "/tmp/src")
    monkeypatch.setenv("DAEMON_CORE_HERMES_PROGRESS_PREAMBLE", "canary")
    monkeypatch.setenv(
        "DAEMON_CORE_HERMES_PROGRESS_PREAMBLE_PLATFORMS",
        "telegram",
    )
    monkeypatch.setattr(turn_handoff, "read_turn_handoff", lambda *_args: None)
    voice_context = SimpleNamespace(
        atlas=SimpleNamespace(souvenir_names=(), rendered_chars=0),
        hot_memory=SimpleNamespace(souvenir_names=(), rendered_chars=0),
        scene_model=None,
        scene_delta="",
    )
    monkeypatch.setattr(
        turn_runtime,
        "prepare_turn",
        lambda _request: SimpleNamespace(
            voice_context=voice_context,
            hot_souvenirs=(),
            voice_path="canonical",
            prompt_variant="thin",
            situation_source="current",
            memory_source="atlas",
            leading_system_prompt="SYSTEME CANONIQUE",
            turns=[{"role": "user", "content": "Vérifie cette information"}],
        ),
    )
    plugin._turn_systems.clear()
    plugin._turn_prefixes.clear()
    plugin._turn_latency.clear()
    request = {
        "instructions": "Hermes",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Vérifie cette information"}
                ],
            }
        ],
        "tools": [{"type": "function", "name": "web_search"}],
        "tool_choice": "auto",
    }

    rewritten = plugin._delegated_request_or_none(
        request,
        platform="telegram",
        api_mode="codex_responses",
        api_call_count=1,
        turn_id="turn-progress-preamble",
    )

    assert rewritten["instructions"].startswith("SYSTEME CANONIQUE\n\n")
    assert rewritten["instructions"].count('Hermes progress preamble') == 1
    assert rewritten["input"][0]["content"][0]["text"] == (
        "Vérifie cette information"
    )
    assert rewritten["tools"] == request["tools"]
    assert rewritten["tool_choice"] == "auto"
    assert request["instructions"] == "Hermes"


def test_sovereign_library_reuses_its_system_across_iterative_reads(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_USER_ID", "u1")
    monkeypatch.setenv("DAEMON_CORE_SRC", "/tmp/src")
    plugin._turn_systems.clear()
    plugin._turn_prefixes.clear()
    plugin._turn_systems["turn-library-loop"] = "sovereign library system"
    request = {
        "instructions": "Hermes",
        "input": [
            {"type": "function_call_output", "call_id": "call-1", "output": "{}"},
        ],
        "tools": [{"type": "function", "name": "daemon_read_souvenirs"}],
        "tool_choice": "auto",
    }

    rewritten = plugin._delegated_request_or_none(
        request,
        platform="telegram",
        api_mode="codex_responses",
        api_call_count=2,
        turn_id="turn-library-loop",
    )

    assert rewritten["instructions"] == "sovereign library system"
    assert rewritten["input"] == request["input"]
    assert rewritten["tools"] == request["tools"]


def test_tool_loop_restores_injected_dialogue_and_keeps_tool_records(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_USER_ID", "u1")
    monkeypatch.setenv("DAEMON_CORE_SRC", "/tmp/src")
    plugin._turn_systems.clear()
    plugin._turn_prefixes.clear()
    plugin._turn_systems["turn-tool-context"] = "sovereign system"
    plugin._turn_prefixes["turn-tool-context"] = [
        {"role": "user", "content": "On parlait de MagicCon Europe 2027."},
        {"role": "assistant", "content": "Oui, peut-être en Europe."},
        {"role": "system", "content": "Garde le référent exact après les outils."},
    ]
    request = {
        "instructions": "Hermes",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Ancien fil hôte"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Ancienne réponse"}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Je ne sais pas si ça passera à Paris",
                    }
                ],
            },
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "web_search",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "{\"city\":\"Amsterdam\"}",
            },
        ],
        "tools": [{"type": "function", "name": "web_search"}],
    }

    rewritten = plugin._delegated_request_or_none(
        request,
        platform="telegram",
        api_mode="codex_responses",
        api_call_count=2,
        turn_id="turn-tool-context",
    )

    assert rewritten["instructions"] == (
        "sovereign system\n\nGarde le référent exact après les outils."
    )
    assert [item.get("role") or item.get("type") for item in rewritten["input"]] == [
        "user",
        "assistant",
        "user",
        "function_call",
        "function_call_output",
    ]
    rendered = str(rewritten["input"])
    assert "MagicCon Europe 2027" in rendered
    assert "Je ne sais pas si ça passera à Paris" in rendered
    assert "Amsterdam" in rendered
    assert "Ancien fil hôte" not in rendered


def test_rewrite_hides_web_extract_only_when_no_backend_exists(monkeypatch):
    monkeypatch.setattr(plugin, "_web_extract_available", lambda: False)
    request = {
        "instructions": "Hermes",
        "input": [],
        "tools": [
            {"type": "function", "name": "web_search"},
            {"type": "function", "name": "web_extract"},
            {"type": "function", "name": "browser_navigate"},
        ],
        "tool_choice": "auto",
    }

    rewritten = plugin._rewrite_request(
        request,
        "sovereign system",
        [{"role": "user", "content": "Vérifie ce fait."}],
        "codex_responses",
    )

    assert [tool["name"] for tool in rewritten["tools"]] == [
        "web_search",
        "browser_navigate",
    ]
    assert rewritten["tool_choice"] == "auto"
    assert [tool["name"] for tool in request["tools"]] == [
        "web_search",
        "web_extract",
        "browser_navigate",
    ]


def test_rewrite_hides_native_memory_but_keeps_daemon_memory_tools(monkeypatch):
    monkeypatch.setattr(plugin, "_web_extract_available", lambda: True)
    request = {
        "instructions": "Hermes",
        "input": [],
        "tools": [
            {"type": "function", "name": "memory"},
            {"type": "function", "name": "daemon_read_souvenirs"},
            {"type": "function", "name": "daemon_recall_l0"},
            {"type": "function", "name": "web_search"},
        ],
        "tool_choice": "auto",
    }

    rewritten = plugin._rewrite_request(
        request,
        "sovereign system",
        [{"role": "user", "content": "Souviens-toi précisément."}],
        "codex_responses",
    )

    assert [tool["name"] for tool in rewritten["tools"]] == [
        "daemon_read_souvenirs",
        "daemon_recall_l0",
        "web_search",
    ]
    assert rewritten["tool_choice"] == "auto"
    assert [tool["name"] for tool in request["tools"]] == [
        "memory",
        "daemon_read_souvenirs",
        "daemon_recall_l0",
        "web_search",
    ]


def test_degraded_contract_also_hides_native_memory(monkeypatch):
    monkeypatch.setattr(plugin, "_web_extract_available", lambda: True)
    request = {
        "messages": [{"role": "user", "content": "Bonjour"}],
        "tools": [
            {"type": "function", "function": {"name": "memory"}},
            {
                "type": "function",
                "function": {"name": "daemon_read_souvenirs"},
            },
        ],
    }

    rewritten, _ = plugin._append_degraded_host_contract(request, "chat")

    assert [
        tool["function"]["name"] for tool in rewritten["tools"]
    ] == ["daemon_read_souvenirs"]


def test_rewrite_keeps_web_extract_when_backend_exists(monkeypatch):
    monkeypatch.setattr(plugin, "_web_extract_available", lambda: True)
    request = {
        "messages": [{"role": "user", "content": "Lis cette page."}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "web_extract"},
            }
        ],
    }

    rewritten = plugin._rewrite_request(
        request,
        "sovereign system",
        [{"role": "user", "content": "Lis cette page."}],
        "chat",
    )

    assert rewritten["tools"] == request["tools"]


def test_degraded_tool_loop_keeps_native_prompt_and_tools(monkeypatch):
    import daemon_core.canonical_voice as canonical_voice
    import daemon_core.turn_handoff as turn_handoff

    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_USER_ID", "u1")
    monkeypatch.setenv("DAEMON_CORE_SRC", "/tmp/src")
    monkeypatch.setattr(
        canonical_voice,
        "compose_canonical_voice_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("canonical unavailable")
        ),
    )
    monkeypatch.setattr(
        turn_handoff,
        "read_turn_handoff",
        lambda *_args: ("Bonjour", "trace-fallback", 11.0),
    )
    plugin._turn_latency.clear()
    first_request = {
        "instructions": "Hermes",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Bonjour"}],
            }
        ],
        "tools": [{"type": "function", "name": "web_search"}],
        "tool_choice": "auto",
    }

    first = plugin._delegated_request_or_none(
        first_request,
        platform="telegram",
        api_mode="codex_responses",
        api_call_count=1,
        turn_id="turn-fallback",
    )
    tool_request = {
        "instructions": "Hermes",
        "input": [
            *first_request["input"],
            {"role": "assistant", "content": [], "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "résultat"},
        ],
        "tools": first_request["tools"],
        "tool_choice": "auto",
    }
    continued = plugin._delegated_request_or_none(
        tool_request,
        platform="telegram",
        api_mode="codex_responses",
        api_call_count=2,
        turn_id="turn-fallback",
    )

    assert first["instructions"].startswith("Hermes\n\n")
    assert continued["instructions"].startswith("Hermes\n\n")
    assert first["tools"] == continued["tools"] == first_request["tools"]
    assert continued["input"] == tool_request["input"]
    assert first["tool_choice"] == continued["tool_choice"] == "auto"


@pytest.mark.parametrize("final_success", [True, False])
def test_preamble_is_cleaned_after_a_final_sent_outside_the_stream(
    monkeypatch, final_success
):
    """No Hermes tool ran (e.g. model-native search): Hermes delivers the final
    answer with send_final_ledgered instead of the stream consumer."""
    monkeypatch.setenv("DAEMON_CORE_HERMES_PROGRESS_PREAMBLE", "canary")
    monkeypatch.setenv("DAEMON_CORE_HERMES_PROGRESS_PREAMBLE_PLATFORMS", "telegram")
    monkeypatch.setattr(plugin, "_PENDING_PREAMBLES", {})

    class Result:
        def __init__(self, message_id, success=True):
            self.success = success
            self.message_id = message_id
            self.continuation_message_ids = ()

    class Adapter:
        platform = SimpleNamespace(value="telegram")
        deleted = []

        async def send(self, **kwargs):
            return Result("commentary-61")

        async def delete_message(self, chat_id, message_id):
            Adapter.deleted.append((chat_id, message_id))
            return True

        async def send_final_ledgered(self, event, session_key, text, metadata, *, reply_to):
            return Result("final-62", success=final_success), self

    class Consumer:
        def __init__(self, adapter):
            self.adapter = adapter
            self.chat_id = "4100000001"
            self.metadata = {}
            self._message_id = None

        def _clean_for_display(self, text):
            return text.strip()

        def _notify_new_message(self):
            pass

        def on_delta(self, _text):
            return None

        async def _send_commentary(self, _text):
            raise AssertionError("the canary path must retain the exact send id")

        async def _send_or_edit(self, text, *, finalize=False, is_turn_final=True):
            return True

    assert plugin._install_stream_probe(Consumer) is True
    assert plugin._install_final_delivery_cleanup(Adapter) is True
    adapter = Adapter()
    consumer = Consumer(adapter)
    assert asyncio.run(consumer._send_commentary("Je regarde.")) is True
    event = SimpleNamespace(source=SimpleNamespace(chat_id="4100000001"))
    result, _ = asyncio.run(adapter.send_final_ledgered(event, "s", "Réponse.", {}, reply_to=None))
    assert result.success is final_success
    if final_success:
        assert Adapter.deleted == [("4100000001", "commentary-61")]
        assert plugin._PENDING_PREAMBLES == {}
    else:
        # A refused final keeps the visible preamble and its pending cleanup.
        assert Adapter.deleted == []
        assert ("telegram", "4100000001") in plugin._PENDING_PREAMBLES


def _start_event(text="/start", *, user="1234567", platform="telegram", chat="dm", internal=False):
    source = SimpleNamespace(platform=SimpleNamespace(value=platform), chat_type=chat, user_id=user)
    return SimpleNamespace(text=text, source=source, internal=internal, allow_gateway_control=True)


@pytest.fixture
def open_meeting(tmp_path, monkeypatch):
    from daemon_core import first_meeting, paths

    monkeypatch.setenv("DAEMON_CORE_USER_ID", "1234567")
    monkeypatch.setenv("DAEMON_CORE_SRC", str(ROOT / "src"))
    monkeypatch.setattr(paths, "MEMORY_ROOT", tmp_path)
    first_meeting.initialize(tmp_path, "1234567", "fr")
    return tmp_path


def test_owner_start_reaches_the_voice_while_first_meeting_is_open(open_meeting):
    event = _start_event()
    assert plugin._first_meeting_start(event=event, gateway=None, session_store=None) is None
    assert event.allow_gateway_control is False
    assert event.text == "/start"


@pytest.mark.parametrize(
    "event",
    [
        _start_event(user="7654321"),
        _start_event(chat="group"),
        _start_event(platform="discord"),
        _start_event("/start campaign"),
        _start_event("/new"),
        _start_event("Salut"),
        _start_event(internal=True),
    ],
)
def test_other_start_events_keep_native_handling(open_meeting, event):
    assert plugin._first_meeting_start(event=event) is None
    assert event.allow_gateway_control is True


def test_start_after_first_meeting_keeps_native_silence(open_meeting):
    import json

    path = open_meeting / "users" / "1234567" / "first_meeting.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | {"phase": "complete"}) + "\n")
    event = _start_event()
    plugin._first_meeting_start(event=event)
    assert event.allow_gateway_control is True


def test_start_without_meeting_or_with_unreadable_state_keeps_native_silence(tmp_path, monkeypatch):
    from daemon_core import paths

    monkeypatch.setenv("DAEMON_CORE_USER_ID", "1234567")
    monkeypatch.setenv("DAEMON_CORE_SRC", str(ROOT / "src"))
    monkeypatch.setattr(paths, "MEMORY_ROOT", tmp_path)
    event = _start_event()
    plugin._first_meeting_start(event=event)
    assert event.allow_gateway_control is True
    (tmp_path / "users" / "1234567").mkdir(parents=True)
    (tmp_path / "users" / "1234567" / "first_meeting.json").mkdir()
    plugin._first_meeting_start(event=event)
    assert event.allow_gateway_control is True


def test_register_installs_the_start_hook(monkeypatch):
    calls = []
    ctx = SimpleNamespace(
        register_middleware=lambda kind, callback: calls.append(("middleware", kind)),
        register_hook=lambda name, callback: calls.append(("hook", name, callback)),
    )
    for name in ("_install_stream_probe", "_install_final_delivery_cleanup", "_install_visible_stream_tracking"):
        monkeypatch.setattr(plugin, name, lambda: False)
    plugin.register(ctx)
    assert ("middleware", "llm_execution") in calls
    assert ("hook", "pre_gateway_dispatch", plugin._first_meeting_start) in calls


def _first_contact_payload(text):
    return {
        'instructions': 'Native host context',
        'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text':
            text + '\n\n[System note: This is the user\'s very first message ever. '
            'Briefly introduce yourself and mention that /help shows available commands.]'}]}],
        'tools': [], 'tool_choice': 'auto',
    }


@pytest.mark.parametrize("text", ["/start", "hello"])
def test_host_first_contact_note_is_dropped_without_a_memory_handoff(open_meeting, monkeypatch, text):
    from daemon_core import turn_runtime

    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    plugin._inbound_exact.clear()
    seen = []

    def compose(request):
        seen.append(request.message)
        return SimpleNamespace(
            voice_context=SimpleNamespace(
                atlas=SimpleNamespace(souvenir_names=(), rendered_chars=0),
                hot_memory=SimpleNamespace(souvenir_names=(), rendered_chars=0),
                scene_model=None, scene_delta='',
            ),
            hot_souvenirs=(), voice_path='canonical', prompt_variant='thin',
            situation_source='current', memory_source='atlas',
            leading_system_prompt='Canonical first meeting',
            turns=[{'role': 'user', 'content': request.message}],
        )

    monkeypatch.setattr(turn_runtime, 'prepare_turn', compose)
    plugin._first_meeting_start(event=_start_event(text))
    rewritten = plugin._delegated_request_or_none(
        _first_contact_payload(text), platform='telegram', api_mode='codex_responses',
        api_call_count=1, turn_id='first-contact-' + text,
    )
    assert seen == [text]
    assert rewritten['input'] == [{'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}]


def test_another_senders_text_is_never_used_as_the_owner_message(open_meeting):
    plugin._inbound_exact.clear()
    plugin._first_meeting_start(event=_start_event("hello", user="7654321"))
    assert plugin._inbound_exact == {}


def test_anthropic_rewrite_replaces_native_system_and_keeps_only_dialogue_roles():
    # Anthropic rejected the developer role (HTTP 400) and the native system
    # prompt stayed in place on the owner's Opus 5.5 test daemon.
    request = {
        "system": [{"type": "text", "text": "Hermes", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "current"}],
        "tools": [{"name": "web"}],
    }
    turns = [
        {"role": "developer", "content": "durable background"},
        {"role": "assistant", "content": "Bonjour, je suis Aster."},
        {"role": "user", "content": "earlier"},
        {"role": "user", "content": "unanswered"},
        {"role": "assistant", "content": "reply"},
        {"role": "system", "content": "live state"},
        {"role": "user", "content": "current"},
    ]

    rewritten = plugin._rewrite_request(request, "sovereign system", turns, "anthropic_messages")

    assert rewritten["system"] == [{
        "type": "text",
        "text": "sovereign system\n\ndurable background\n\nlive state",
        "cache_control": {"type": "ephemeral"},
    }]
    roles = [message["role"] for message in rewritten["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert rewritten["messages"][0]["content"] == "(empty)"
    assert rewritten["messages"][2]["content"] == [
        {"type": "text", "text": "earlier"}, {"type": "text", "text": "unanswered"}]
    assert "Hermes" not in str(rewritten["system"])
    assert rewritten["tools"] == request["tools"]


def test_anthropic_tool_loop_keeps_tool_use_and_results_after_the_current_user():
    tool_use = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "web", "input": {}}]}
    tool_result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}
    request = {
        "system": "Hermes",
        "messages": [
            {"role": "user", "content": "host history"},
            {"role": "assistant", "content": "host reply"},
            {"role": "user", "content": [{"type": "text", "text": "current"}]},
            tool_use,
            tool_result,
        ],
    }
    prefix = [
        {"role": "developer", "content": "durable background"},
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "reply"},
    ]

    rewritten = plugin._rewrite_request(
        request, "sovereign system", None, "anthropic_messages", tool_loop_prefix=prefix)

    assert rewritten["system"] == "sovereign system\n\ndurable background"
    assert rewritten["messages"] == [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": [{"type": "text", "text": "current"}]},
        tool_use,
        tool_result,
    ]


def test_anthropic_degraded_contract_extends_the_native_system_field():
    request = {"system": "Hermes", "messages": [{"role": "user", "content": "current"}]}

    degraded, system = plugin._append_degraded_host_contract(request, "anthropic_messages")

    assert degraded["system"] == system and system.startswith("Hermes\n\n")
    assert degraded["messages"] == request["messages"]


def test_managed_anthropic_key_stays_on_api_key_authentication(tmp_path, monkeypatch):
    # Hermes sent an owner's sk-ant-usr Console key as a Claude subscription
    # token and Anthropic refused it; the same key passed as x-api-key.
    import types

    def original(value):
        return value.startswith("sk-ant-") and not value.startswith("sk-ant-api")

    credentials = types.ModuleType("synthetic_anthropic_credentials")
    credentials._is_oauth_token = original
    adapter = types.ModuleType("synthetic_anthropic_adapter")
    adapter._is_oauth_token = original
    monkeypatch.setitem(sys.modules, "synthetic_anthropic_credentials", credentials)
    monkeypatch.setitem(sys.modules, "synthetic_anthropic_adapter", adapter)
    config = tmp_path / "provider.json"
    config.write_text('{"kind": "anthropic_api"}')
    monkeypatch.setenv("DAEMON_CORE_PROVIDER_CONFIG", str(config))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-usr01-synthetic")

    assert plugin._install_managed_anthropic_key(credentials) is True
    assert plugin._install_managed_anthropic_key(credentials) is True
    for module in (credentials, adapter):
        assert module._is_oauth_token("sk-ant-usr01-synthetic") is False
        assert module._is_oauth_token("sk-ant-oat01-other") is True
        assert module._is_oauth_token("sk-ant-api03-other") is False


@pytest.mark.parametrize("kind", ["codex_cli", "openai_api", ""])
def test_managed_anthropic_key_hook_ignores_other_daemons(tmp_path, monkeypatch, kind):
    import types

    credentials = types.ModuleType("synthetic_anthropic_credentials")
    credentials._is_oauth_token = lambda value: True
    config = tmp_path / "provider.json"
    config.write_text('{"kind": "%s"}' % kind)
    monkeypatch.setenv("DAEMON_CORE_PROVIDER_CONFIG", str(config))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-usr01-synthetic")

    assert plugin._install_managed_anthropic_key(credentials) is False
    assert credentials._is_oauth_token("sk-ant-usr01-synthetic") is True


def test_anthropic_response_text_blocks_lose_the_internal_turn_marker():
    # An Opus 5.5 reply sent without streaming reached Telegram with its
    # "[daemon-turn: exchange]" line (2026-10-30).
    from types import SimpleNamespace

    text_block = SimpleNamespace(type="text", text="[daemon-turn: exchange]\nEnchantée. Tu es où en ce moment ?")
    thinking = SimpleNamespace(type="thinking", thinking="[daemon-turn: exchange] private")
    response = SimpleNamespace(content=[thinking, text_block], stop_reason="end_turn")

    plugin._sanitize_response_payload(response)

    assert text_block.text == "Enchantée. Tu es où en ce moment ?"
    assert thinking.thinking == "[daemon-turn: exchange] private"

    as_dict = {"content": [{"type": "text", "text": "[daemon-turn: direct]\nOui."}]}
    plugin._sanitize_response_payload(as_dict)
    assert as_dict["content"][0]["text"] == "Oui."
