import sys
import pytest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routes import openclaw
from daemon_core.context_router import ContextRoute
from daemon_core.voice_brief import DialogueCoverage


def _client(monkeypatch, l0_sink):
    # Stub providers: no real LLM calls.
    openclaw._routing_provider = object()
    openclaw._selection_provider = object()
    openclaw._memory_provider = object()
    monkeypatch.setattr(
        "daemon_core.context_router.route_context_depth",
        lambda **k: ContextRoute(
            mode="fast",
            context_depth="light",
            active_request="salut",
            ambiguity="none",
            scene_register="pratique",
            reason="test",
        ),
    )
    monkeypatch.setattr(
        "daemon_core.canonical_voice.compose_canonical_voice_messages",
        lambda *args, **kwargs: (
            [
                {"role": "system", "content": "SYSTEME CANONIQUE"},
                {"role": "user", "content": args[1]},
            ],
            SimpleNamespace(
                atlas=SimpleNamespace(souvenir_names=(), rendered_chars=0),
                hot_memory=SimpleNamespace(souvenir_names=(), rendered_chars=0),
                dialogue_coverage=DialogueCoverage(
                    status="recent_only",
                    dialogue="",
                ),
                scene_scope="openclaw__u",
                prompt_variant="voice_brief_reference",
                situation_source="seed",
                scene_model=None,
                scene_delta="",
            ),
        ),
    )
    # These adapter tests exercise host transport, not VoiceBrief parsing. Keep
    # their deliberately minimal prompt while honoring the canonical boundary.
    monkeypatch.setattr(
        "daemon_core.voice_brief.compile_voice_brief",
        lambda plan: plan,
    )
    monkeypatch.setattr(
        "daemon_core.voice_brief.render_reference_messages",
        lambda plan: plan.messages,
    )
    monkeypatch.setattr(
        "daemon_core.memory.writer.append_ground_zero",
        lambda **k: l0_sink.append((k["role"], k["content"])),
    )
    app = FastAPI()
    app.include_router(openclaw.router)
    return TestClient(app)


def test_openclaw_bridge_is_not_an_after_turn_worker_owner():
    assert not hasattr(openclaw, "_ensure_openclaw_durable_worker")
    assert not hasattr(openclaw, "resume_openclaw_durable_jobs")


def test_openclaw_bridge_silences_private_commitment_before_native_streaming(
    monkeypatch,
):
    from daemon_core.thin_sovereign import THIN_VOICE_CONTRACT

    l0 = []
    client = _client(monkeypatch, l0)
    monkeypatch.setattr(
        "daemon_core.canonical_voice.compose_canonical_voice_messages",
        lambda *args, **kwargs: (
            [
                {
                    "role": "system",
                    "content": '# Turn contract\n\n' + THIN_VOICE_CONTRACT,
                },
                {"role": "user", "content": args[1]},
            ],
            SimpleNamespace(
                atlas=SimpleNamespace(souvenir_names=(), rendered_chars=0),
                hot_memory=SimpleNamespace(souvenir_names=(), rendered_chars=0),
                dialogue_coverage=DialogueCoverage(
                    status="recent_only",
                    dialogue="",
                ),
                scene_scope="openclaw__u",
                prompt_variant="voice_brief_reference",
                situation_source="seed",
                scene_model=None,
                scene_delta="",
            ),
        ),
    )

    response = client.post(
        "/openclaw/assemble",
        json={"user_id": "u", "message": "salut"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "[daemon-turn" not in body["primary_system_prompt"]
    assert "[daemon-turn" not in body["sovereign_system_prompt"]
    assert "entirely internal" in body["primary_system_prompt"]
    assert l0 == []


def test_assemble_is_read_only_no_l0_write(monkeypatch):
    l0 = []
    client = _client(monkeypatch, l0)
    resp = client.post("/openclaw/assemble", json={"user_id": "u", "message": "salut"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "system_prompt_addition",
        "sovereign_system_prompt",
        "primary_system_prompt",
        "near_turn_system_prompt",
        "canonical_recent_dialogue",
        "souvenirs",
        "selection_source",
        "selection_reused",
        "depth",
        "scene_register",
        "route",
        "timings_ms",
        "voice_path",
        "canary_trace_id",
        "source_turn_started_at",
        "scene_scope",
        "hot_souvenirs",
        "prompt_variant",
        "degraded",
        "degradation_reason",
    }
    assert body["depth"] == "contextual"
    assert body["voice_path"] == "canonical"
    assert set(body["timings_ms"]) == {
        "load",
        "radar",
        "selection",
        "addition",
        "sovereign",
        "total",
    }
    # Assembly is read-only; ingest is the sole L0 writer.
    assert l0 == []


def test_legacy_openclaw_switches_cannot_reactivate_the_radar(monkeypatch):
    l0 = []
    client = _client(monkeypatch, l0)
    monkeypatch.setenv("DAEMON_CORE_RADAR_FINAL_SELECTION", "on")
    monkeypatch.setattr(
        "daemon_core.context_router.route_context_depth",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("the retired radar path must stay unreachable")
        ),
    )

    resp = client.post(
        "/openclaw/assemble",
        json={
            "user_id": "u",
            "message": "question personnelle",
            "sovereign_voice": False,
            "sovereign_library": False,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["voice_path"] == "canonical"
    assert body["selection_source"] == "canonical"
    assert body["timings_ms"]["radar"] == 0


def test_ingest_user_writes_l0_without_memory_agent(monkeypatch):
    l0 = []
    client = _client(monkeypatch, l0)
    resp = client.post("/openclaw/ingest", json={"user_id": "u", "role": "user", "content": "hello"})
    assert resp.status_code == 200
    assert resp.json()["ingested"] is True
    assert resp.json()["memory_updated"] is False
    assert ("user", "hello") in l0


def test_ingest_user_idempotency_key_writes_l0_once(
    monkeypatch,
    tmp_path,
):
    import daemon_core.memory.writer as memory_writer

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(memory_writer, "MEMORY_ROOT", memory_root)
    app = FastAPI()
    app.include_router(openclaw.router)
    client = TestClient(app)
    payload = {
        "user_id": "u",
        "role": "user",
        "content": "message OpenClaw unique",
        "canary_trace_id": "trace-1",
        "idempotency_key": "host-message-1",
    }

    first = client.post("/openclaw/ingest", json=payload)
    second = client.post("/openclaw/ingest", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json()["l0_ref"] == second.json()["l0_ref"]
    l0_path = next(
        (
            memory_root
            / "levels"
            / "L0_ground_zero"
            / "conversations"
        ).glob("u_*.md")
    )
    assert (
        l0_path.read_text(encoding="utf-8").count(
            "message OpenClaw unique"
        )
        == 1
    )


def test_historical_recovery_keeps_original_date_without_reprojecting_current_state(
    monkeypatch,
    tmp_path,
):
    import daemon_core.memory.writer as memory_writer

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(memory_writer, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(openclaw, "_observe_continuity", lambda *a, **k: None)
    monkeypatch.setattr(
        openclaw,
        "_schedule_memory_agent",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("historical recovery must not update current state")
        ),
    )
    occurred_at = datetime(2026, 8, 28, 7, 15, 20).timestamp()
    app = FastAPI()
    app.include_router(openclaw.router)
    client = TestClient(app)
    common = {
        "user_id": "u",
        "idempotency_key": "historical-turn-1",
        "historical_recovery": True,
    }

    user = client.post(
        "/openclaw/ingest",
        json={
            **common,
            "role": "user",
            "content": "Parole ancienne.",
            "occurred_at": occurred_at,
        },
    )
    assistant = client.post(
        "/openclaw/ingest",
        json={
            **common,
            "role": "assistant",
            "content": "Réponse ancienne.",
            "last_user_message": "Parole ancienne.",
            "occurred_at": occurred_at + 7,
            "user_l0_ref": user.json()["l0_ref"],
        },
    )

    assert user.status_code == assistant.status_code == 200
    assert assistant.json()["memory_updated"] is False
    assert "état courant inchangé" in assistant.json()["reason"]
    historical_path = (
        memory_root
        / "levels"
        / "L0_ground_zero"
        / "conversations"
        / "u_2026-08-28.md"
    )
    text = historical_path.read_text(encoding="utf-8")
    assert "2026-08-28T07:15:20 — user" in text
    assert "2026-08-28T07:15:27 — assistant" in text
    assert not (historical_path.parent / "u_2026-08-29.md").exists()


def test_transcript_reconciliation_restores_only_missing_exact_messages(
    monkeypatch,
    tmp_path,
):
    import daemon_core.memory.writer as memory_writer

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(memory_writer, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(openclaw, "_observe_continuity", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(openclaw.router)
    client = TestClient(app)
    occurred_at = datetime.now().replace(microsecond=0).timestamp() - 60

    existing = client.post(
        "/openclaw/ingest",
        json={
            "user_id": "u",
            "role": "user",
            "content": "Message déjà durable.",
            "occurred_at": occurred_at,
            "idempotency_key": "existing-turn",
        },
    )
    assert existing.status_code == 200

    payload = {
        "user_id": "u",
        "session_id": "native-session",
        "messages": [
            {
                "event_id": "native-user",
                "role": "user",
                "content": "Message déjà durable.",
                "occurred_at": occurred_at,
            },
            {
                "event_id": "native-assistant",
                "role": "assistant",
                "content": "Réponse restée seulement chez l’hôte.",
                "occurred_at": occurred_at + 3,
            },
        ],
    }
    first = client.post("/openclaw/reconcile-transcript", json=payload)
    second = client.post("/openclaw/reconcile-transcript", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json() == {
        "scanned": 2,
        "already_recorded": 1,
        "restored": 1,
    }
    assert second.json() == {
        "scanned": 2,
        "already_recorded": 2,
        "restored": 0,
    }
    l0_path = (
        memory_root
        / "levels"
        / "L0_ground_zero"
        / "conversations"
        / f"u_{datetime.fromtimestamp(occurred_at):%Y-%m-%d}.md"
    )
    text = l0_path.read_text(encoding="utf-8")
    assert text.count("Message déjà durable.") == 1
    assert text.count("Réponse restée seulement chez l’hôte.") == 1


def test_ingest_assistant_schedules_memory_agent_off_critical_path(monkeypatch):
    """MemoryAgent must not delay the HTTP response.

    Synchronous consolidation previously timed out the adapter and caused
    an OpenClaw Telegram session conflict."""
    import threading

    l0 = []
    client = _client(monkeypatch, l0)

    class _Result:
        updated_souvenirs = ["theme/x"]
        updated_user_context = False
        reason = "ok"

    calls = {}
    done = threading.Event()

    class _FakeHarness:
        def __init__(self, **kwargs):
            calls["providers"] = kwargs

        def update_memory_after_response(self, **kwargs):
            calls.update(kwargs)
            done.set()
            return _Result()

    monkeypatch.setattr("daemon_core.loop.DaemonCore", _FakeHarness)
    resp = client.post(
        "/openclaw/ingest",
        json={
            "user_id": "u",
            "role": "assistant",
            "content": "réponse",
            "last_user_message": "salut",
            "selected_souvenirs": ["theme/x"],
            "route": {
                "mode": "contextual",
                "context_depth": "contextual",
                "reason": "test",
            },
        },
    )
    # The response returns immediately, without waiting for consolidation.
    assert resp.status_code == 200
    assert resp.json()["ingested"] is True
    assert ("assistant", "réponse") in l0

    # MemoryAgent still runs in the background with the correct message pair.
    assert done.wait(timeout=5), "le MemoryAgent de fond n'a pas été déclenché"
    assert calls["message"] == "salut"
    assert calls["response"] == "réponse"
    assert calls["selected_souvenirs"] == ["theme/x"]
    assert calls["route"].mode == "contextual"


def test_ingest_assistant_without_user_pair_stays_in_l0_but_skips_curation(monkeypatch):
    l0 = []
    client = _client(monkeypatch, l0)
    scheduled = []
    monkeypatch.setattr(openclaw, "_schedule_memory_agent", lambda **kwargs: scheduled.append(kwargs))

    resp = client.post(
        "/openclaw/ingest",
        json={"user_id": "u", "role": "assistant", "content": "réponse"},
    )

    assert resp.status_code == 200
    assert ("assistant", "réponse") in l0
    assert scheduled == []
    assert "incomplète" in resp.json()["reason"]


def test_ingest_strips_the_transport_degradation_notice(monkeypatch):
    from daemon_core.runtime_degradation import DEGRADED_RESPONSE_NOTICE

    l0 = []
    client = _client(monkeypatch, l0)

    resp = client.post(
        "/openclaw/ingest",
        json={
            "user_id": "u",
            "role": "assistant",
            "content": f"{DEGRADED_RESPONSE_NOTICE}\n\nRéponse native.",
        },
    )

    assert resp.status_code == 200
    assert ("assistant", "Réponse native.") in l0
    assert all(DEGRADED_RESPONSE_NOTICE not in content for _, content in l0)


def test_legacy_memory_agent_is_attributed_to_after_turn(monkeypatch):
    events = []

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    class Slot:
        def acquire(self, *, blocking):
            assert blocking is False
            return True

        def release(self):
            events.append("release")

    class Phase:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            events.append(("phase_enter", self.name))

        def __exit__(self, *_args):
            events.append(("phase_exit", self.name))

    monkeypatch.setattr(openclaw.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(openclaw, "_memory_slot", Slot())
    monkeypatch.setattr(
        openclaw,
        "_run_memory_update",
        lambda **_kwargs: events.append("memory"),
    )
    monkeypatch.setattr(
        "daemon_core.providers.telemetry.llm_telemetry_phase",
        lambda name: Phase(name),
    )

    barrier = openclaw._schedule_memory_agent(
        user_id="u",
        user_message="question",
        assistant_response="réponse",
    )

    assert barrier.succeeded is True
    assert events == [
        ("phase_enter", "after_turn.memory"),
        "memory",
        ("phase_exit", "after_turn.memory"),
        "release",
    ]


def test_canonical_composition_failure_returns_observable_native_fallback(monkeypatch):
    from daemon_core.runtime_degradation import DEGRADED_HOST_CONTRACT

    l0 = []
    client = _client(monkeypatch, l0)

    monkeypatch.setattr(
        "daemon_core.canonical_voice.compose_canonical_voice_messages",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("canonical unavailable")
        ),
    )
    resp = client.post(
        "/openclaw/assemble",
        json={"user_id": "u", "message": "salut"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sovereign_system_prompt"] == ""
    assert body["system_prompt_addition"] == DEGRADED_HOST_CONTRACT
    assert body["voice_path"] == "host_fallback"
    assert body["degraded"] is True
    assert body["degradation_reason"] == "canonical_prepare_failed"
    assert body["route"]["mode"] == "degraded"
    assert l0 == []


def test_canonical_voice_bypasses_radar_without_library_feature_flag(monkeypatch):
    from types import SimpleNamespace

    l0 = []
    client = _client(monkeypatch, l0)
    monkeypatch.setattr(
        "daemon_core.context_router.route_context_depth",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("radar should be skipped")),
    )
    monkeypatch.setattr(
        "daemon_core.canonical_voice.compose_canonical_voice_messages",
        lambda *args, **kwargs: (
            [
                {"role": "system", "content": "SYSTEME ATLAS"},
                {"role": "developer", "content": "ARRIERE-PLAN DURABLE"},
                {"role": "assistant", "content": "tour precedent"},
                {"role": "system", "content": "ETAT AU SEUIL"},
                {"role": "user", "content": "question"},
            ],
            SimpleNamespace(
                atlas=SimpleNamespace(souvenir_names=("theme/a", "theme/b"), rendered_chars=42),
                held_thought=object(),
                hot_memory=SimpleNamespace(souvenir_names=("theme/a",)),
                dialogue_coverage=DialogueCoverage(
                    status="recent_only",
                    dialogue="",
                ),
                scene_scope="openclaw__u",
                prompt_variant="voice_brief_reference",
            ),
        ),
    )

    resp = client.post(
        "/openclaw/assemble",
        json={
            "user_id": "u",
            "message": "question",
            "session_id": "session-1",
            "trace_id": "trace-fixed",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sovereign_system_prompt"] == "SYSTEME ATLAS\n\nETAT AU SEUIL"
    assert body["primary_system_prompt"] == "SYSTEME ATLAS"
    assert body["near_turn_system_prompt"] == "ETAT AU SEUIL"
    assert body["system_prompt_addition"] == "ARRIERE-PLAN DURABLE"
    assert body["canonical_recent_dialogue"] == [
        {"role": "assistant", "content": "tour precedent"}
    ]
    assert body["voice_path"] == "canonical"
    assert body["selection_source"] == "canonical"
    assert body["hot_souvenirs"] == ["theme/a"]
    assert body["scene_scope"] == "openclaw__u"
    assert body["prompt_variant"] == "voice_brief_reference"
    assert body["degraded"] is False
    assert body["degradation_reason"] == ""
    assert body["canary_trace_id"] == "trace-fixed"
    assert body["source_turn_started_at"] > 0
    assert body["timings_ms"]["radar"] == 0
    assert l0 == []


def test_openclaw_souvenir_tool_returns_exact_bounded_sources(monkeypatch):
    from daemon_core.held_atlas_sovereign import ExactSouvenirRead

    l0 = []
    client = _client(monkeypatch, l0)
    observed = []
    monkeypatch.setattr(
        "daemon_core.evidence_observability.observe_evidence_tool_result",
        lambda trace_id, **kwargs: observed.append((trace_id, kwargs)),
    )
    monkeypatch.setattr(
        "daemon_core.held_atlas_sovereign.read_exact_active_souvenirs",
        lambda paths, **_kwargs: ExactSouvenirRead(
            souvenirs=(("theme/a", "# A\nFait exact"),),
            rejected_paths=("theme/missing",),
            rendered_chars=20,
        ),
    )

    resp = client.post(
        "/openclaw/tool",
        json={
            "user_id": "u",
            "session_id": "session-1",
            "trace_id": "trace-openclaw-tool",
            "sovereign_library": False,
            "name": "daemon_read_souvenirs",
            "arguments": {
                "paths": ["theme/a", "theme/missing"],
                "scene": {
                    "established": ["Une personne est explicitement concernée."],
                    "uncertain": ["Une autre personne pourrait modifier le choix."],
                    "need": "Établir les préférences utiles à la décision.",
                    "handling": {
                        "mode": "condition",
                        "plan": "Si elle est concernée aussi, le choix sera différent.",
                    },
                },
            },
            "read_souvenirs": ["theme/hot"],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["status"] == "partial"
    assert body["result"]["capsules"][0]["content"] == "# A\nFait exact"
    assert body["result"]["scene_assessment"]["uncertain"] == [
        "Une autre personne pourrait modifier le choix."
    ]
    assert body["result"]["scene_assessment"]["handling"]["mode"] == "condition"
    assert 'begin with this condition' in body["result"]["coverage_checkpoint"]
    assert body["read_souvenirs"] == ["theme/hot", "theme/a"]
    assert observed[0][0] == "trace-openclaw-tool"
    assert observed[0][1]["tool_name"] == "daemon_read_souvenirs"
    assert observed[0][1]["result"]["status"] == "partial"


def test_openclaw_souvenir_tool_expands_collection_in_same_call(monkeypatch):
    from daemon_core.held_atlas_sovereign import ExactSouvenirRead
    from daemon_core.memory.collection_read import (
        CollectionMember,
        CollectionPage,
    )

    client = _client(monkeypatch, [])
    observed = {}

    def read(paths, **kwargs):
        observed["paths"] = paths
        observed.update(kwargs)
        return ExactSouvenirRead(
            souvenirs=(
                ("theme/episode", "# Episode"),
                ("theme/member", "# Member\n\nFait exact."),
            ),
            rejected_paths=(),
            rendered_chars=40,
            collection_page=CollectionPage(
                center="theme/episode",
                coverage="open",
                known_member_count=1,
                offset=0,
                members=(
                    CollectionMember(
                        path="theme/member",
                        role="membre établi",
                        source_refs=("levels/L0/example#turn",),
                    ),
                ),
                next_offset=None,
            ),
        )

    monkeypatch.setattr(
        "daemon_core.held_atlas_sovereign.read_exact_active_souvenirs",
        read,
    )
    response = client.post(
        "/openclaw/tool",
        json={
            "user_id": "u",
            "name": "daemon_read_souvenirs",
            "arguments": {
                "paths": ["theme/episode"],
                "collection_path": "theme/episode",
                "collection_offset": 0,
                "scene": {
                    "established": [],
                    "uncertain": [],
                    "need": "Retrouver l'ensemble établi.",
                    "handling": {
                        "mode": "direct",
                        "plan": "Répondre depuis les membres exacts.",
                    },
                },
            },
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert observed["collection_path"] == "theme/episode"
    assert observed["collection_offset"] == 0
    assert result["collection"]["coverage"] == "open"
    assert result["collection"]["known_members_complete"] is True
    assert 'never present this list as exhaustive' in (
        result["coverage_checkpoint"]
    )


def test_openclaw_souvenir_tool_reads_before_rejudging_provisional_handling(
    monkeypatch,
):
    from daemon_core.held_atlas_sovereign import ExactSouvenirRead

    client = _client(monkeypatch, [])
    monkeypatch.setattr(
        "daemon_core.held_atlas_sovereign.read_exact_active_souvenirs",
        lambda paths, **_kwargs: ExactSouvenirRead(
            souvenirs=(("theme/family", "# Family\nExact composition."),),
            rejected_paths=(),
            rendered_chars=30,
        ),
    )

    response = client.post(
        "/openclaw/tool",
        json={
            "user_id": "u",
            "session_id": "session-1",
            "name": "daemon_read_souvenirs",
            "arguments": {
                "paths": ["theme/family"],
                "scene": {
                    "established": ["A family group is present."],
                    "uncertain": ["Its exact composition remains unknown."],
                    "need": "Read the exact family composition.",
                    "handling": {
                        "mode": "direct",
                        "plan": "Answer if the source resolves it.",
                    },
                },
            },
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "ok"
    assert result["capsules"][0]["path"] == "theme/family"
    assert 'proposed before reading does not resolve' in result["coverage_checkpoint"]


def test_openclaw_daemon_route_no_longer_owns_professional_calendar(monkeypatch):
    l0 = []
    client = _client(monkeypatch, l0)

    resp = client.post(
        "/openclaw/tool",
        json={
            "user_id": "u",
            "session_id": "session-1",
            "name": "daemon_read_source",
            "arguments": {"source": "professional_calendar"},
            "read_souvenirs": ["theme/hot"],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == {"status": "error", "reason": "Unknown tool."}
    assert body["read_souvenirs"] == []


def test_openclaw_l0_tool_reads_plausible_durable_sources_first(monkeypatch):
    from daemon_core.held_atlas_sovereign import (
        SouvenirLibraryPreflight,
        ExactSouvenirRead,
        ExactMemoryAtlas,
    )

    l0 = []
    client = _client(monkeypatch, l0)
    monkeypatch.setattr(
        "daemon_core.held_atlas_sovereign.build_exact_memory_atlas",
        lambda **_kwargs: ExactMemoryAtlas(
            "- theme/other",
            ("theme/other",),
            13,
            "hash",
        ),
    )
    monkeypatch.setattr(
        "daemon_core.held_atlas_sovereign.judge_unread_souvenirs_before_l0",
        lambda *args, **kwargs: SouvenirLibraryPreflight(
            ("theme/other",), "Source durable plausible.", 0.9
        ),
    )
    monkeypatch.setattr(
        "daemon_core.held_atlas_sovereign.read_exact_active_souvenirs",
        lambda paths, **_kwargs: ExactSouvenirRead(
            souvenirs=(("theme/other", "preuve durable"),),
            rejected_paths=(),
            rendered_chars=15,
        ),
    )
    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_forensic_recovery",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("L0 should not run")),
    )

    resp = client.post(
        "/openclaw/tool",
        json={
            "user_id": "u",
            "name": "daemon_recall_l0",
            "arguments": {"need": "un fait personnel"},
            "read_souvenirs": ["theme/already"],
        },
    )

    body = resp.json()
    assert body["result"]["status"] == "capsule_sources"
    assert body["result"]["l0_searched"] is False
    assert body["read_souvenirs"] == ["theme/already", "theme/other"]
    assert body["library_preflight_consumed"] is True


def test_openclaw_l0_chronology_bypasses_durable_preflight(monkeypatch):
    l0 = []
    client = _client(monkeypatch, l0)
    monkeypatch.setattr(
        "daemon_core.held_atlas_sovereign.judge_unread_souvenirs_before_l0",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("chronology must not use thematic preflight")
        ),
    )
    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_chronology_recovery",
        lambda **kwargs: type(
            "Recovery",
            (),
            {
                "status": "found",
                "source_file": "u_2026-07-29.md",
                "evidence": (
                    "## 2026-07-29T18:50:00 — user\n\n"
                    "Je vais rester au frais.\n\n"
                    "## 2026-07-29T18:53:00 — user\n\n"
                    "Et côté MagicCon ?"
                ),
                "reason": "Frontière exacte.",
                "confidence": 0.97,
                "relation_status": "established",
                "conclusion": "Le sujet précédent était la chaleur.",
            },
        )(),
    )

    resp = client.post(
        "/openclaw/tool",
        json={
            "user_id": "u",
            "name": "daemon_recall_l0",
            "arguments": {
                "need": "le sujet discuté juste avant Magic",
                "temporal_context": "plus tôt dans cette conversation",
                "lookup_kind": "conversation_chronology",
            },
            "read_souvenirs": ["theme/already"],
        },
    )

    body = resp.json()
    assert body["result"]["evidence_status"] == "exact_evidence_found"
    assert body["result"]["relation_status"] == "established"
    assert body["result"]["conclusion"] == "Le sujet précédent était la chaleur."
    assert 'only' in body["result"]["response_constraint"]
    assert body["result"]["evidence_kind"] == "conversation_chronology"
    assert body["result"]["source_file"] == "u_2026-07-29.md"
    assert body["read_souvenirs"] == ["theme/already"]
    assert body["library_preflight_consumed"] is False


def test_openclaw_l0_chronology_not_found_is_fail_closed(monkeypatch):
    l0 = []
    client = _client(monkeypatch, l0)
    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_chronology_recovery",
        lambda **kwargs: type(
            "Recovery",
            (),
            {
                "status": "not_found",
                "source_file": "",
                "evidence": "",
                "reason": "Aucune plage exacte.",
                "confidence": 0.2,
                "relation_status": "not_established",
                "conclusion": "",
            },
        )(),
    )

    resp = client.post(
        "/openclaw/tool",
        json={
            "user_id": "u",
            "name": "daemon_recall_l0",
            "arguments": {
                "need": "le sujet discuté avant ce pivot",
                "lookup_kind": "conversation_chronology",
            },
        },
    )

    result = resp.json()["result"]
    assert result["status"] == "not_found"
    assert result["evidence"] == ""
    assert result["conclusion"] == ""
    assert result["relation_status"] == "not_established"
    assert result["response_constraint"].startswith(
        'No usable exact evidence'
    )
    assert "reason" not in result


def test_openclaw_l0_chronology_invalid_evidence_is_fail_closed(monkeypatch):
    l0 = []
    client = _client(monkeypatch, l0)
    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_chronology_recovery",
        lambda **kwargs: type(
            "Recovery",
            (),
            {
                "status": "invalid_evidence",
                "source_file": "u_2026-07-29.md",
                "evidence": "",
                "reason": "Plage non continue.",
                "confidence": 0.0,
                "relation_status": "ambiguous",
                "conclusion": "",
            },
        )(),
    )

    resp = client.post(
        "/openclaw/tool",
        json={
            "user_id": "u",
            "name": "daemon_recall_l0",
            "arguments": {
                "need": "le sujet discuté avant ce pivot",
                "lookup_kind": "conversation_chronology",
            },
        },
    )

    result = resp.json()["result"]
    assert result["status"] == "invalid_evidence"
    assert result["evidence_status"] == "invalid_evidence"
    assert result["evidence"] == ""
    assert result["source_file"] == ""
    assert result["relation_status"] == "not_established"
    assert result["conclusion"] == ""
    assert "reason" not in result


def test_openclaw_l0_chronology_provider_error_is_a_material_boundary(
    monkeypatch,
):
    l0 = []
    client = _client(monkeypatch, l0)
    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_chronology_recovery",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
    )

    resp = client.post(
        "/openclaw/tool",
        json={
            "user_id": "u",
            "name": "daemon_recall_l0",
            "arguments": {
                "need": "le sujet discuté avant ce pivot",
                "lookup_kind": "conversation_chronology",
            },
        },
    )

    result = resp.json()["result"]
    assert result["status"] == "error"
    assert result["evidence_status"] == "error"
    assert result["evidence_kind"] == "conversation_chronology"
    assert result["error_code"] == "forensic_lookup_failed"
    assert result["evidence"] == ""
    assert result["conclusion"] == ""
    assert 'Do not assert' in result["response_constraint"]
    assert "reason" not in result


def test_openclaw_l0_personal_fact_returns_structured_counterevidence(
    monkeypatch,
):
    from daemon_core.held_atlas_sovereign import SouvenirLibraryPreflight

    l0 = []
    client = _client(monkeypatch, l0)
    monkeypatch.setattr(
        "daemon_core.held_atlas_sovereign.judge_unread_souvenirs_before_l0",
        lambda *args, **kwargs: SouvenirLibraryPreflight((), "", 0.9),
    )
    captured = {}

    def recover(**kwargs):
        captured.update(kwargs)
        return type(
            "Recovery",
            (),
            {
                "status": "found",
                "source_file": "u_2026-07-19.md",
                "evidence": (
                    "Je prévois déjà de regarder la finale de la Coupe du "
                    "Monde sur mon iPad à l'aéroport"
                ),
                "reason": "La formulation proposée mentionne l'avion.",
                "confidence": 0.99,
                "relation_status": "not_established",
                "conclusion": (
                    "La finale devait être regardée à l'aéroport, pas dans l'avion."
                ),
            },
        )()

    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_forensic_recovery",
        recover,
    )
    monkeypatch.setattr(
        "daemon_core.held_atlas_sovereign.verify_l0_evidence_coverage",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("counterevidence needs no positive coverage pass")
        ),
    )

    resp = client.post(
        "/openclaw/tool",
        json={
            "user_id": "u",
            "name": "daemon_recall_l0",
            "arguments": {
                "need": "lieu prévu pour regarder la finale",
                "temporal_context": "retour de Bordeaux en juillet 2026",
            },
            "read_souvenirs": ["theme/football"],
            "library_preflight_calls": 1,
        },
    )

    body = resp.json()
    assert body["result"]["status"] == "found"
    assert body["result"]["relation_status"] == "not_established"
    assert body["result"]["evidence_status"] == "exact_evidence_found"
    assert body["result"]["evidence_kind"] == "personal_fact"
    assert body["result"]["response_constraint"].startswith('Do not confirm')
    assert "aéroport" in body["result"]["conclusion"]
    assert "reason" not in body["result"]
    assert captured["message"] == (
        "Recall need: lieu prévu pour regarder la finale"
    )
    assert "retour de Bordeaux" not in captured["message"]


def test_openclaw_l0_personal_fact_downgrades_incomplete_quote_to_partial(
    monkeypatch,
):
    from daemon_core.held_atlas_sovereign import SouvenirLibraryPreflight

    l0 = []
    client = _client(monkeypatch, l0)
    monkeypatch.setattr(
        "daemon_core.held_atlas_sovereign.judge_unread_souvenirs_before_l0",
        lambda *args, **kwargs: SouvenirLibraryPreflight((), "", 0.9),
    )
    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_forensic_recovery",
        lambda **kwargs: type(
            "Recovery",
            (),
            {
                "status": "found",
                "source_file": "u_2026-07-01.md",
                "evidence": "Ah si nous rentrons",
                "reason": "Retour confirmé, destination absente.",
                "confidence": 0.78,
                "relation_status": "established",
                "conclusion": "Un retour est prévu.",
            },
        )(),
    )
    monkeypatch.setattr(
        "daemon_core.held_atlas_sovereign.verify_l0_evidence_coverage",
        lambda **kwargs: (False, "La destination n'est pas dans la citation."),
    )

    resp = client.post(
        "/openclaw/tool",
        json={
            "user_id": "u",
            "name": "daemon_recall_l0",
            "arguments": {"need": "destination du retour familial"},
            "library_preflight_calls": 1,
        },
    )

    result = resp.json()["result"]
    assert result["status"] == "partial"
    assert result["evidence_status"] == "partial_exact_evidence"
    assert result["relation_status"] == "ambiguous"
    assert result["evidence"] == "Ah si nous rentrons"
    assert result["conclusion"] == ""
    assert result["confidence"] == 0.0
    assert "reason" not in result


def test_canary_ingest_hands_exact_reads_to_shared_after_turn(monkeypatch):
    l0 = []
    client = _client(monkeypatch, l0)
    scheduled = []
    continuity = []

    monkeypatch.setattr(
        openclaw,
        "_schedule_sovereign_after_turn",
        lambda request: scheduled.append(request),
    )
    monkeypatch.setattr(
        openclaw,
        "_observe_continuity",
        lambda *args, **kwargs: continuity.append((args, kwargs)),
    )

    resp = client.post(
        "/openclaw/ingest",
        json={
            "user_id": "u",
            "role": "assistant",
            "content": "réponse",
            "last_user_message": "question",
            "selected_souvenirs": ["theme/hot"],
            "route": {"mode": "canonical"},
            "voice_path": "canonical",
            "sovereign_library": False,
            "canary_trace_id": "trace",
            "source_turn_started_at": 123.0,
            "scene_scope": "openclaw__u",
            "read_souvenirs": ["theme/read"],
        },
    )

    assert resp.status_code == 200
    assert len(scheduled) == 1
    turn = scheduled[0]
    assert turn.route is None
    assert turn.read_souvenirs == ("theme/read",)
    assert turn.selected_souvenirs == ("theme/hot",)
    assert turn.memory_souvenirs == ("theme/read", "theme/hot")
    assert turn.source_trace_id == "trace"
    assert turn.scene_scope == "openclaw__u"
    assert continuity[0][1]["voice_path"] == "canonical"


@pytest.mark.parametrize("storage_unavailable", [False, True])
def test_canary_ingest_uses_durable_spool_with_exact_l0_refs(
    monkeypatch,
    storage_unavailable,
):
    from daemon_core.memory.writer import GroundZeroReference

    l0 = []
    client = _client(monkeypatch, l0)
    monkeypatch.setenv("DAEMON_CORE_AFTER_TURN_SPOOL", "on")
    user_ref = GroundZeroReference(
        path="levels/L0_ground_zero/conversations/u.md",
        offset=0,
        length=10,
        sha256="a" * 64,
        role="user",
        turn_id="trace",
    )
    assistant_ref = GroundZeroReference(
        path="levels/L0_ground_zero/conversations/u.md",
        offset=10,
        length=12,
        sha256="b" * 64,
        role="assistant",
        turn_id="trace",
    )
    monkeypatch.setattr(
        openclaw,
        "_safe_append",
        lambda *args, **kwargs: (True, assistant_ref),
    )
    queued = []

    class Spool:
        def handoff(self, job):
            if storage_unavailable:
                raise OSError("synthetic storage unavailable")
            queued.append(job)
            return SimpleNamespace(created=True, state="pending")

    monkeypatch.setattr(
        "daemon_core.after_turn_spool.AfterTurnSpool",
        lambda: Spool(),
    )
    monkeypatch.setattr(
        "integrations.openclaw_plugin.origin.resolve_delivery_origin",
        lambda session_id: {
            "channel": "telegram",
            "to": "telegram:12345",
            "account_id": "default",
        }
        if session_id == "session-1"
        else {},
    )

    response = client.post(
        "/openclaw/ingest",
        json={
            "user_id": "u",
            "role": "assistant",
            "content": "réponse",
            "last_user_message": "question",
            "selected_souvenirs": ["theme/hot"],
            "voice_path": "canonical",
            "canary_trace_id": "trace",
            "source_turn_started_at": 123.0,
            "scene_scope": "openclaw__u",
            "read_souvenirs": ["theme/read"],
            "session_id": "session-1",
            "user_l0_ref": user_ref.as_dict(),
        },
    )

    if storage_unavailable:
        assert response.status_code == 503
        assert response.json()["detail"] == "after_turn_handoff_unavailable"
        assert not queued
        return
    assert response.status_code == 200
    assert response.json()["reason"] == (
        "Cycle après-tour confié à la file durable."
    )
    assert len(queued) == 1
    assert queued[0].host == "openclaw"
    assert queued[0].tool_read_souvenirs == ("theme/read",)
    assert queued[0].origin == {
        "platform": "openclaw",
        "session_id": "session-1",
        "voice_path": "canonical",
        "channel": "telegram",
        "to": "telegram:12345",
        "account_id": "default",
    }


def test_openclaw_scene_revision_uses_after_turn_library_state(monkeypatch):
    from daemon_core.after_turn import AfterTurnRequest

    scheduled = []
    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    monkeypatch.setattr(
        "daemon_core.scene_model.schedule_scene_model_revision",
        lambda **kwargs: scheduled.append(kwargs) or True,
    )
    provider = object()
    request = AfterTurnRequest(
        user_id="u",
        user_message="question",
        assistant_response="réponse",
        scene_scope="openclaw__u",
        source_trace_id="trace",
        source_turn_started_at=123.0,
        library_active=True,
    )

    openclaw._revise_scene_model(request, memory_provider=provider)

    assert scheduled[0]["provider"] is provider
    assert scheduled[0]["source_trace_id"] == "trace"


def test_openclaw_shared_after_turn_preserves_promoted_order(monkeypatch):
    from daemon_core.after_turn import AfterTurnRequest

    order = []

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    class Slot:
        def acquire(self, *, blocking):
            assert blocking is False
            order.append("acquire")
            return True

        def release(self):
            order.append("release")

    monkeypatch.setattr(openclaw.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(openclaw, "_memory_slot", Slot())
    monkeypatch.setattr(
        openclaw,
        "_run_memory_update",
        lambda **kwargs: order.append(
            ("memory", tuple(kwargs["selected_souvenirs"]))
        ),
    )
    monkeypatch.setattr(
        openclaw,
        "_revise_scene_model",
        lambda request: order.append("scene"),
    )

    barrier = openclaw._schedule_sovereign_after_turn(
        AfterTurnRequest(
            user_id="u",
            user_message="question",
            assistant_response="réponse",
            scene_scope="openclaw__u",
            source_trace_id="trace",
            source_turn_started_at=123.0,
            selected_souvenirs=("theme/hot",),
            read_souvenirs=("theme/read",),
            library_active=True,
        )
    )

    assert barrier.succeeded is True
    assert order == [
        "acquire",
        ("memory", ("theme/read", "theme/hot")),
        "scene",
        "release",
    ]


def test_openclaw_shared_after_turn_releases_slot_when_memory_fails(monkeypatch):
    from daemon_core.after_turn import AfterTurnRequest

    order = []

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    class Slot:
        def acquire(self, *, blocking):
            return True

        def release(self):
            order.append("release")

    monkeypatch.setattr(openclaw.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(openclaw, "_memory_slot", Slot())
    monkeypatch.setattr(
        openclaw,
        "_run_memory_update",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("memory down")),
    )
    monkeypatch.setattr(
        openclaw,
        "_revise_scene_model",
        lambda request: order.append("scene"),
    )

    barrier = openclaw._schedule_sovereign_after_turn(
        AfterTurnRequest(
            user_id="u",
            user_message="correction",
            assistant_response="réponse",
            source_trace_id="trace",
            source_turn_started_at=123.0,
            scene_scope="openclaw__u",
            library_active=True,
        ),
    )

    assert barrier.succeeded is False
    assert order == ["release"]
