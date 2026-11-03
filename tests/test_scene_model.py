import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from daemon_core import scene_model
from daemon_core.providers.base import LLMProvider
from daemon_core.scene_model import (
    MAX_CURRENT_TOPICS,
    MAX_SCENE_DEPENDENCIES,
    MAX_SCENE_ITEMS,
    SceneDependency,
    SceneCurrentTopic,
    SceneItem,
    SceneSnapshot,
    SceneSource,
    SceneTemporalBoundary,
    build_scene_bootstrap_messages,
    build_scene_model_messages,
    build_scene_sources,
    current_topics_canary_enabled,
    parse_scene_bootstrap_selection,
    parse_scene_dependency_patch,
    parse_scene_patch,
    read_scene_model,
    render_scene_model_for_voice,
    render_unrepresented_active_context_for_voice,
    run_scene_model_revision,
    scene_model_dialogue_delta,
    scene_model_shadow_enabled,
    scene_model_voice_enabled,
    schedule_scene_model_revision,
)


class ScriptedProvider(LLMProvider):
    model = "test-terra"
    reasoning_effort = "low"

    def __init__(
        self,
        raw: str,
        dependency_raw: str | None = None,
    ) -> None:
        self.raw = raw
        self.dependency_raw = dependency_raw or _dependency_raw()
        self.prompts: list[list[dict]] = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.prompts.append(messages)
        return _combined_raw(self.raw, self.dependency_raw)


class BlockingProvider(ScriptedProvider):
    def __init__(self, raw: str) -> None:
        super().__init__(raw)
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.prompts.append(messages)
        self.entered.set()
        assert self.release.wait(timeout=2)
        return _combined_raw(self.raw, self.dependency_raw)


class SequenceProvider(LLMProvider):
    model = "test-terra"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = iter(outputs)
        self.prompts: list[list[dict]] = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.prompts.append(messages)
        return next(self.outputs)


SOURCE = SceneSource(
    source_id="src-l0_turn-user",
    kind="l0_turn",
    locator="2026-07-20T09:00:00 — user",
    sha256="abc",
    excerpt="Je pars demain matin et quelque chose reste à préparer.",
)
SOUVENIR_SOURCE = SceneSource(
    source_id="src-capsule-project",
    kind="capsule",
    locator="theme/projets/exemple",
    sha256="def",
    excerpt="Un projet encore actif.",
)


def _raw(
    *,
    changes=None,
    hot_memories=None,
    released_memories=None,
    current_topic_changes=None,
    reading="Une transition proche structure encore la scène.",
):
    normalized_changes = changes
    if normalized_changes is None:
        normalized_changes = [
            {
                "op": "add",
                "id": "",
                "text": "Un départ proche laisse une préparation inachevée.",
                "epistemic": "established",
                "sources": [SOURCE.source_id],
                "reason": "",
            }
        ]
    normalized_changes = [
        {**change, "temporal_boundaries": change.get("temporal_boundaries", [])}
        for change in normalized_changes
    ]
    payload = {
            "reading": reading,
            "conversation": "L'échange reste ouvert sans mouvement prescrit.",
            "changes": normalized_changes,
            "hot_memories": hot_memories or [],
            "released_memories": released_memories or [],
            "dependency_changes": [],
            "source_dispositions": [],
        }
    if current_topic_changes is not None:
        payload["current_topic_changes"] = current_topic_changes
    return json.dumps(payload, ensure_ascii=False)


def _dependency_raw(*, changes=None, reading="Une transition proche structure encore la scène."):
    normalized_changes = [
        {**change, "temporal_boundaries": change.get("temporal_boundaries", [])}
        for change in (changes or [])
    ]
    return json.dumps(
        {
            "reading": reading,
            "changes": normalized_changes,
        },
        ensure_ascii=False,
    )


def _combined_raw(scene_raw: str, dependency_raw: str) -> str:
    scene = json.loads(scene_raw)
    dependencies = json.loads(dependency_raw)
    scene["dependency_changes"] = dependencies.get("changes", [])
    scene["source_dispositions"] = dependencies.get(
        "source_dispositions",
        [],
    )
    return json.dumps(scene, ensure_ascii=False)


def _context(previous=None, include_souvenir=False):
    sources = {SOURCE.source_id: SOURCE}
    if include_souvenir:
        sources[SOUVENIR_SOURCE.source_id] = SOUVENIR_SOURCE
    return {
        "previous": previous,
        "sources": sources,
        "allowed_souvenirs": ["theme/projets/exemple"] if include_souvenir else [],
        "souvenir_contents": {
            "theme/projets/exemple": "Un projet encore actif."
        }
        if include_souvenir
        else {},
    }


def _patch_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(scene_model, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(
        scene_model,
        "SHADOW_LOG",
        tmp_path / "exports" / "scene_model_shadow.jsonl",
    )


def _run(tmp_path, monkeypatch, provider=None, **kwargs):
    _patch_paths(tmp_path, monkeypatch)
    return run_scene_model_revision(
        "garden",
        "telegram__garden",
        provider or ScriptedProvider(_raw()),
        source_trace_id=kwargs.pop("source_trace_id", "turn-1"),
        source_turn_started_at=kwargs.pop("source_turn_started_at", 10.0),
        context=kwargs.pop("context", _context()),
        **kwargs,
    )


def test_revision_roundtrip_preserves_sources_and_readable_body(tmp_path, monkeypatch):
    provider = ScriptedProvider(_raw())

    snapshot = _run(tmp_path, monkeypatch, provider=provider)

    assert snapshot is not None
    assert snapshot.revision == 1
    assert snapshot.items[0].epistemic == "established"
    assert snapshot.items[0].sources == (SOURCE.source_id,)
    assert snapshot.sources == (SOURCE,)
    assert read_scene_model("garden", "telegram__garden") == snapshot
    stored = scene_model.scene_model_path(
        "garden", "telegram__garden"
    ).read_text(encoding="utf-8")
    assert stored.startswith("---\nschema_version: 3")
    assert "# Scene model" in stored
    assert "## Lecture de la scène" in stored
    assert '"op":"add|keep|revise|retire"' in provider.prompts[0][0]["content"]


def test_shared_world_starts_from_the_freshest_valid_host_scene(
    tmp_path,
    monkeypatch,
):
    _patch_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_SHARED_WORLD_STATE", "canary")
    telegram = run_scene_model_revision(
        "garden",
        "telegram__garden",
        ScriptedProvider(_raw()),
        source_trace_id="telegram-turn",
        source_turn_started_at=10.0,
        context=_context(),
    )
    openclaw = run_scene_model_revision(
        "garden",
        "openclaw__garden",
        ScriptedProvider(_raw(reading="La scène OpenClaw est la plus récente.")),
        source_trace_id="openclaw-turn",
        source_turn_started_at=20.0,
        context=_context(),
    )

    migrated = scene_model.ensure_shared_scene_model("garden", "world")

    assert telegram is not None
    assert openclaw is not None
    assert migrated == replace(openclaw, scene_scope="world")
    assert read_scene_model("garden", "world") == migrated
    assert scene_model.scene_model_path("garden", "world").stat().st_mode & 0o777 == 0o600


def test_existing_shared_world_is_not_reseeded_from_a_later_legacy_scene(
    tmp_path,
    monkeypatch,
):
    _patch_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_SHARED_WORLD_STATE", "canary")
    first = run_scene_model_revision(
        "garden",
        "telegram__garden",
        ScriptedProvider(_raw()),
        source_trace_id="telegram-turn",
        source_turn_started_at=10.0,
        context=_context(),
    )
    shared = scene_model.ensure_shared_scene_model("garden", "world")
    later = run_scene_model_revision(
        "garden",
        "openclaw__garden",
        ScriptedProvider(_raw(reading="Une scène legacy plus tardive.")),
        source_trace_id="openclaw-turn",
        source_turn_started_at=20.0,
        context=_context(),
    )

    assert first is not None
    assert later is not None
    assert scene_model.ensure_shared_scene_model("garden", "world") == shared
    assert read_scene_model("garden", "world").source_trace_id == "telegram-turn"


def test_shared_world_does_not_revive_an_invalidated_legacy_scene(
    tmp_path,
    monkeypatch,
):
    _patch_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_SHARED_WORLD_STATE", "canary")
    legacy = run_scene_model_revision(
        "garden",
        "telegram__garden",
        ScriptedProvider(_raw()),
        source_trace_id="legacy-turn",
        source_turn_started_at=10.0,
        context=_context(),
    )
    monkeypatch.setattr(scene_model.time, "time", lambda: 20.0)
    scene_model.invalidate_scene_models("garden", reason="test")

    assert legacy is not None
    assert scene_model.ensure_shared_scene_model("garden", "world") is None
    assert not scene_model.scene_model_path("garden", "world").exists()


def test_revision_persists_source_accounting_for_voice_residuals(
    tmp_path,
    monkeypatch,
):
    active_context = """# Contexte actif

## Sujets actifs
- Une mission reste prioritaire aujourd'hui.
"""
    active_source = next(
        source
        for source in build_scene_sources(
            recent_dialogue="",
            active_context=active_context,
        ).values()
        if source.kind == "active_context"
    )
    grounding = _raw(
        changes=[
            {
                "op": "add",
                "id": "",
                "text": "Une mission reste prioritaire aujourd'hui.",
                "epistemic": "established",
                "sources": [SOURCE.source_id, active_source.source_id],
                "reason": "",
            }
        ]
    )
    dependency_payload = json.loads(_dependency_raw())
    dependency_payload["source_dispositions"] = [
        {
            "source": active_source.source_id,
            "condition": "represented",
            "dependency": "none",
            "evidence_sources": [],
            "reason": "La condition ne laisse aucune dépendance ouverte.",
        }
    ]
    context = {
        "previous": None,
        "sources": {
            SOURCE.source_id: SOURCE,
            active_source.source_id: active_source,
        },
        "allowed_souvenirs": [],
        "souvenir_contents": {},
        "source_accounting_ids": [active_source.source_id],
    }

    snapshot = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(
            grounding,
            json.dumps(dependency_payload),
        ),
        context=context,
    )

    assert snapshot is not None
    assert snapshot.source_dispositions == (
        scene_model.SceneSourceDisposition(
            source_id=active_source.source_id,
            condition="represented",
            dependency="none",
            reason="La condition ne laisse aucune dépendance ouverte.",
        ),
    )
    assert read_scene_model("garden", "telegram__garden") == snapshot
    assert render_unrepresented_active_context_for_voice(
        snapshot,
        active_context,
    ) == ""


def test_later_scene_revision_cannot_regress_dialogue_watermark(
    tmp_path,
    monkeypatch,
):
    newer_watermark = SceneSource(
        source_id="src-l0-newer-watermark",
        kind="l0_turn",
        locator="2026-08-29T16:04:39 — assistant",
        sha256="newer-watermark",
        excerpt="Une réponse plus récente a déjà été absorbée.",
    )
    first = _run(
        tmp_path,
        monkeypatch,
        source_trace_id="hermes-later-dialogue",
        source_turn_started_at=10.0,
        context={
            "previous": None,
            "sources": {
                SOURCE.source_id: SOURCE,
                newer_watermark.source_id: newer_watermark,
            },
            "allowed_souvenirs": [],
            "souvenir_contents": {},
        },
    )
    assert first is not None
    assert first.dialogue_watermark_locator == newer_watermark.locator

    older_watermark = SceneSource(
        source_id="src-l0-older-watermark",
        kind="l0_turn",
        locator="2026-08-29T16:01:11 — user",
        sha256="older-watermark",
        excerpt="Une confirmation plus ancienne reste utile.",
    )
    second = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(
            _raw(
                changes=[
                    {
                        "op": "keep",
                        "id": first.items[0].item_id,
                    }
                ]
            )
        ),
        source_trace_id="openclaw-later-job",
        source_turn_started_at=20.0,
        context={
            "previous": first,
            "sources": {
                SOURCE.source_id: SOURCE,
                older_watermark.source_id: older_watermark,
            },
            "allowed_souvenirs": [],
            "souvenir_contents": {},
        },
    )

    assert second is not None
    assert second.revision == first.revision + 1
    assert second.dialogue_watermark_source_id == newer_watermark.source_id
    assert second.dialogue_watermark_locator == newer_watermark.locator


def test_scene_reader_rejects_an_unproved_superseded_disposition(
    tmp_path,
    monkeypatch,
):
    active_context = """# Contexte actif

## Sujets actifs
- Une mission reste prioritaire aujourd'hui.
"""
    active_source = next(
        source
        for source in build_scene_sources(
            recent_dialogue="",
            active_context=active_context,
        ).values()
        if source.kind == "active_context"
    )
    grounding = _raw(
        changes=[
            {
                "op": "add",
                "id": "",
                "text": "Une mission reste prioritaire aujourd'hui.",
                "epistemic": "established",
                "sources": [SOURCE.source_id, active_source.source_id],
                "reason": "",
            }
        ]
    )
    dependency_payload = json.loads(_dependency_raw())
    dependency_payload["source_dispositions"] = [
        {
            "source": active_source.source_id,
            "condition": "represented",
            "dependency": "none",
            "evidence_sources": [],
            "reason": "La condition ne laisse aucune dépendance ouverte.",
        }
    ]
    context = {
        "previous": None,
        "sources": {
            SOURCE.source_id: SOURCE,
            active_source.source_id: active_source,
        },
        "allowed_souvenirs": [],
        "souvenir_contents": {},
        "source_accounting_ids": [active_source.source_id],
    }
    snapshot = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(grounding, json.dumps(dependency_payload)),
        context=context,
    )
    assert snapshot is not None

    path = scene_model.scene_model_path("garden", "telegram__garden")
    raw = path.read_text(encoding="utf-8")
    path.write_text(
        raw.replace(
            "condition: represented",
            "condition: superseded",
            1,
        ),
        encoding="utf-8",
    )

    assert read_scene_model("garden", "telegram__garden") is None


def test_revision_ignores_recovery_accounting_outside_the_prompt_contract(
    tmp_path,
    monkeypatch,
):
    context = {
        **_context(),
        "dialogue_turn_count": 2,
        "dialogue_total_turns": 5,
        "dialogue_truncated": True,
    }

    snapshot = _run(tmp_path, monkeypatch, context=context)

    assert snapshot is not None
    assert snapshot.revision == 1


def test_scene_invalidation_cutoff_keeps_other_scope_hidden(
    tmp_path,
    monkeypatch,
):
    first = _run(tmp_path, monkeypatch, source_turn_started_at=10.0)
    other = run_scene_model_revision(
        "garden",
        "openclaw__garden",
        ScriptedProvider(_raw()),
        source_trace_id="turn-openclaw-before-repair",
        source_turn_started_at=11.0,
        context=_context(),
    )
    marker = scene_model.invalidate_scene_models(
        "garden",
        reason="durable_memory_repair",
    )

    assert first is not None
    assert other is not None
    assert marker.exists()
    assert read_scene_model("garden", "telegram__garden") is None
    assert read_scene_model("garden", "openclaw__garden") is None

    kept_change = {
        "op": "keep",
        "id": first.items[0].item_id,
    }
    invalidated_at = json.loads(marker.read_text(encoding="utf-8"))[
        "invalidated_at"
    ]

    rebuilt = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(_raw(changes=[kept_change])),
        source_trace_id="turn-after-repair",
        source_turn_started_at=invalidated_at + 1.0,
        context=_context(previous=first),
    )

    assert rebuilt is not None
    assert rebuilt.revision == 2
    assert rebuilt.source_trace_id == "turn-after-repair"
    assert marker.exists()
    assert read_scene_model("garden", "telegram__garden") == rebuilt
    assert read_scene_model("garden", "openclaw__garden") is None

    other_kept_change = {
        "op": "keep",
        "id": other.items[0].item_id,
    }
    other_rebuilt = run_scene_model_revision(
        "garden",
        "openclaw__garden",
        ScriptedProvider(_raw(changes=[other_kept_change])),
        source_trace_id="turn-openclaw-after-repair",
        source_turn_started_at=invalidated_at + 2.0,
        context=_context(previous=other),
    )

    assert other_rebuilt is not None
    assert marker.exists()
    assert read_scene_model("garden", "telegram__garden") == rebuilt
    assert read_scene_model("garden", "openclaw__garden") == other_rebuilt

    monkeypatch.setattr(
        scene_model.time,
        "time",
        lambda: invalidated_at + 3.0,
    )
    assert scene_model.invalidate_scene_models(
        "garden",
        reason="later_durable_memory_repair",
    ) == marker
    assert read_scene_model("garden", "telegram__garden") is None
    assert read_scene_model("garden", "openclaw__garden") is None


def test_scene_started_before_invalidation_cannot_become_visible(
    tmp_path,
    monkeypatch,
):
    first = _run(tmp_path, monkeypatch, source_turn_started_at=10.0)
    marker = scene_model.invalidate_scene_models(
        "garden",
        reason="durable_memory_repair",
    )
    invalidated_at = json.loads(marker.read_text(encoding="utf-8"))[
        "invalidated_at"
    ]
    kept_change = {
        "op": "keep",
        "id": first.items[0].item_id,
    }

    stale = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(_raw(changes=[kept_change])),
        source_trace_id="turn-before-repair",
        source_turn_started_at=invalidated_at - 1.0,
        context=_context(previous=first),
    )

    assert stale is None
    assert marker.exists()
    assert read_scene_model("garden", "telegram__garden") is None
    assert scene_model._read_scene_model_path(
        scene_model.scene_model_path("garden", "telegram__garden")
    ) == first


def test_invalidated_scene_remains_available_as_internal_revision_basis(
    tmp_path,
    monkeypatch,
):
    first = _run(tmp_path, monkeypatch, source_turn_started_at=10.0)
    scene_model.invalidate_scene_models(
        "garden",
        reason="durable_memory_repair",
    )
    monkeypatch.setattr(
        scene_model,
        "scene_model_dialogue_delta",
        lambda *_: "",
    )
    monkeypatch.setattr(
        scene_model,
        "load_recent_dialogue",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(scene_model, "load_user_agenda", lambda *_: "")
    monkeypatch.setattr(scene_model, "load_active_context", lambda *_: "")
    monkeypatch.setattr(scene_model, "load_immediate_focus", lambda *_: "")

    context = scene_model.capture_scene_model_context(
        "garden",
        "telegram__garden",
        [],
    )

    assert read_scene_model("garden", "telegram__garden") is None
    assert context["previous"] == first
    assert SOURCE.source_id in context["sources"]


def test_l0_accounting_consumes_ordinary_verdicts_but_reopens_mutable_closures(
    tmp_path,
    monkeypatch,
):
    superseded_source = SceneSource(
        source_id="src-l0-superseded",
        kind="l0_turn",
        locator="2026-08-08T08:00:00+02:00 — assistant",
        sha256="superseded",
        excerpt="Une ancienne condition ensuite résolue.",
    )
    initial_sources = build_scene_sources(
        recent_dialogue="",
        agenda="# Agenda\n\n- Version A de l'agenda.",
        now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
    )
    agenda_source = next(
        source for source in initial_sources.values() if source.kind == "agenda"
    )
    sources = {
        SOURCE.source_id: SOURCE,
        superseded_source.source_id: superseded_source,
        agenda_source.source_id: agenda_source,
    }
    dependency_payload = json.loads(_dependency_raw())
    dependency_payload["source_dispositions"] = [
        {
            "source": SOURCE.source_id,
            "condition": "represented",
            "dependency": "none",
            "evidence_sources": [],
            "reason": "La condition reste représentée.",
        },
        {
            "source": superseded_source.source_id,
            "condition": "superseded",
            "dependency": "none",
            "evidence_sources": [agenda_source.source_id],
            "reason": "L'agenda courant établit sa résolution.",
        },
        {
            "source": agenda_source.source_id,
            "condition": "nonoperative",
            "dependency": "none",
            "evidence_sources": [],
            "reason": "Cette unité ne décrit aucune condition ouverte.",
        },
    ]
    snapshot = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(_raw(), json.dumps(dependency_payload)),
        context={
            "previous": None,
            "sources": sources,
            "allowed_souvenirs": [],
            "souvenir_contents": {},
            "source_accounting_ids": list(sources),
        },
    )

    assert snapshot is not None
    assert all(
        disposition.source_id != SOURCE.source_id
        for disposition in snapshot.source_dispositions
    )
    assert any(
        disposition.source_id == superseded_source.source_id
        for disposition in snapshot.source_dispositions
    )

    prior_agenda_id = agenda_source.source_id
    for agenda_text in ("Version B de l'agenda.", "Version C de l'agenda."):
        next_sources = build_scene_sources(
            recent_dialogue="",
            agenda=f"# Agenda\n\n- {agenda_text}",
            previous=snapshot,
            now=datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc),
        )
        agenda_source = next(
            source for source in next_sources.values() if source.kind == "agenda"
        )
        accounting = scene_model._source_accounting_ids(
            previous=snapshot,
            sources=next_sources,
            bootstrap_source_ids=set(),
        )

        assert prior_agenda_id not in next_sources
        assert SOURCE.source_id in next_sources
        assert set(accounting) == {
            superseded_source.source_id,
            agenda_source.source_id,
        }
        certificate = scene_model.SceneSourceDisposition(
            source_id=superseded_source.source_id,
            condition="superseded",
            dependency="none",
            evidence_sources=(agenda_source.source_id,),
            reason="L'agenda courant établit sa résolution.",
        )
        snapshot = replace(
            snapshot,
            sources=(SOURCE, superseded_source, agenda_source),
            source_dispositions=(certificate,),
        )
        prior_agenda_id = agenda_source.source_id


def test_invalid_grounding_gets_one_structural_retry(tmp_path, monkeypatch):
    provider = SequenceProvider(["not-json", _raw()])

    snapshot = _run(tmp_path, monkeypatch, provider=provider)

    assert snapshot is not None
    assert len(provider.prompts) == 2
    assert provider.prompts[1][-2] == {
        "role": "assistant",
        "content": "not-json",
    }
    assert 'structural contract' in provider.prompts[1][-1]["content"]
    assert 'Rejection code : `scene:invalid_json`' in provider.prompts[1][-1]["content"]
    assert 'each `established` change' in provider.prompts[1][-1]["content"]
    assert 'each temporal boundary' in provider.prompts[1][-1]["content"]
    event = json.loads(scene_model.SHADOW_LOG.read_text(encoding="utf-8"))
    assert event["grounding_attempts"] == 2
    assert event["grounding_rejections"] == ["invalid_json"]
    assert event["dependency_attempts"] == 0
    assert event["projection_attempts"] == 2


def test_double_grounding_failure_records_both_rejections(tmp_path, monkeypatch):
    snapshot = _run(
        tmp_path,
        monkeypatch,
        provider=SequenceProvider(["not-json", "still-not-json"]),
    )

    assert snapshot is None
    event = json.loads(scene_model.SHADOW_LOG.read_text(encoding="utf-8"))
    assert event["grounding_rejections"] == ["invalid_json", "invalid_json"]
    assert event["dependency_rejections"] == []


def test_invalid_dependencies_get_one_structural_retry(tmp_path, monkeypatch):
    invalid = json.loads(_raw())
    invalid.pop("dependency_changes")
    provider = SequenceProvider([json.dumps(invalid), _raw()])

    snapshot = _run(tmp_path, monkeypatch, provider=provider)

    assert snapshot is not None
    assert len(provider.prompts) == 2
    assert provider.prompts[1][-2] == {
        "role": "assistant",
        "content": json.dumps(invalid),
    }
    assert 'structural contract' in provider.prompts[1][-1]["content"]
    assert "dependencies:root_or_changes_invalid" in provider.prompts[1][-1]["content"]
    event = json.loads(scene_model.SHADOW_LOG.read_text(encoding="utf-8"))
    assert event["grounding_attempts"] == 2
    assert event["dependency_attempts"] == 0
    assert event["dependency_rejections"] == ["root_or_changes_invalid"]


def test_dependency_retry_receives_all_repairable_failures(
    tmp_path,
    monkeypatch,
):
    clock = SceneSource(
        source_id="src-clock-now",
        kind="clock",
        locator="current_time",
        sha256="clock",
        excerpt="2026-07-20T09:00:00+02:00",
    )
    other = SceneSource(
        source_id="src-active_context-other",
        kind="active_context",
        locator="contexte_actif.md#Sujet/other",
        sha256="other",
        excerpt="Une autre condition présente.",
    )
    invalid = json.loads(
        _dependency_raw(
            changes=[
                {
                    "op": "add",
                    "id": "",
                    "text": "Une préparation reste ouverte.",
                    "status": "open",
                    "horizon": "avant demain",
                    "sources": [SOURCE.source_id],
                    "temporal_boundaries": [
                        {
                            "at": "2026-07-21T09:00:00+02:00",
                            "after": "L'échéance est passée ; l'issue reste inconnue.",
                        }
                    ],
                    "reason": "",
                }
            ]
        )
    )
    invalid["source_dispositions"] = [
        {
            "source": other.source_id,
            "condition": "represented",
            "dependency": "none",
            "evidence_sources": [],
            "reason": "Cette condition ne laisse rien d'ouvert.",
        }
    ]
    repaired = json.loads(json.dumps(invalid))
    repaired["changes"][0]["sources"].append(clock.source_id)
    repaired["source_dispositions"][0] = {
        "source": other.source_id,
        "condition": "nonoperative",
        "dependency": "none",
        "evidence_sources": [],
        "reason": "Cette unité ne décrit aucune condition présente.",
    }
    provider = SequenceProvider(
        [
            _combined_raw(_raw(), json.dumps(invalid)),
            _combined_raw(_raw(), json.dumps(repaired)),
        ]
    )
    context = _context()
    context["sources"].update(
        {clock.source_id: clock, other.source_id: other}
    )
    context["source_accounting_ids"] = [other.source_id]

    snapshot = _run(
        tmp_path,
        monkeypatch,
        provider=provider,
        context=context,
    )

    assert snapshot is not None
    assert len(provider.prompts) == 2
    retry_guidance = provider.prompts[1][-1]["content"]
    assert (
        "temporal_clock_source_missing;represented_condition_not_grounded"
        in retry_guidance
    )
    event = json.loads(scene_model.SHADOW_LOG.read_text(encoding="utf-8"))
    assert event["dependency_attempts"] == 0
    assert event["projection_attempts"] == 2
    assert event["dependency_rejections"] == [
        "temporal_clock_source_missing;represented_condition_not_grounded"
    ]


def test_dependency_roundtrip_is_separate_from_scene_items(tmp_path, monkeypatch):
    dependency_raw = _dependency_raw(
        changes=[
            {
                "op": "add",
                "id": "",
                "text": 'Une préparation reste à confirmer avant le départ.',
                "status": "uncertain",
                "horizon": "avant le départ proche",
                "sources": [SOURCE.source_id],
                "reason": "",
            }
        ]
    )

    snapshot = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(_raw(), dependency_raw),
    )

    assert snapshot is not None
    assert len(snapshot.items) == 1
    assert len(snapshot.dependencies) == 1
    assert snapshot.dependencies[0].status == "uncertain"
    assert snapshot.dependencies[0].horizon == "avant le départ proche"
    assert read_scene_model("garden", "telegram__garden") == snapshot
    rendered = render_scene_model_for_voice(snapshot)
    assert "## Dépendances ouvertes" in rendered
    assert "[uncertain; horizon: avant le départ proche]" in rendered


def test_single_pass_deposits_dependency_without_challenge(tmp_path, monkeypatch):
    dependency_raw = _dependency_raw(
        changes=[
            {
                "op": "add",
                "id": "",
                "text": "Une capacité ordinaire reste sans support.",
                "status": "uncertain",
                "horizon": "avant le départ",
                "sources": [SOURCE.source_id],
                "reason": "",
            }
        ]
    )

    snapshot = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(
            _raw(),
            dependency_raw,
        ),
    )

    assert snapshot is not None
    assert len(snapshot.items) == 1
    assert len(snapshot.dependencies) == 1
    assert snapshot.dependencies[0].text == "Une capacité ordinaire reste sans support."
    assert snapshot.dependencies[0].sources == (SOURCE.source_id,)
    event = json.loads(scene_model.SHADOW_LOG.read_text(encoding="utf-8"))
    assert event["projection_attempts"] == 1
    assert event["challenge_attempts"] == 0
    assert event["challenge_additions"] == 0


def test_previous_dependency_must_be_kept_revised_or_retired():
    previous = SceneSnapshot(
        scene_scope="primary",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        dependencies=(
            SceneDependency(
                dependency_id="dependency-1",
                text="Une préparation reste ouverte.",
                status="open",
                horizon="avant demain",
                sources=(SOURCE.source_id,),
            ),
        ),
        sources=(SOURCE,),
    )

    rejection_reasons = []
    assert parse_scene_dependency_patch(
        _dependency_raw(),
        previous=previous,
        allowed_sources={SOURCE.source_id},
        rejection_reasons=rejection_reasons,
    ) is None
    assert rejection_reasons == ["previous_dependencies_unaccounted"]
    retired = parse_scene_dependency_patch(
        _dependency_raw(
            changes=[
                {
                    "op": "retire",
                    "id": "dependency-1",
                    "text": "",
                    "status": "",
                    "horizon": "",
                    "sources": [],
                    "reason": "Une source établit sa résolution.",
                }
            ]
        ),
        previous=previous,
        allowed_sources={SOURCE.source_id},
    )

    assert retired is not None
    assert retired.changes[0].operation == "retire"


def test_dependency_budget_is_independent_from_saturated_scene_items(
    tmp_path,
    monkeypatch,
):
    item_changes = [
        {
            "op": "add",
            "id": "",
            "text": f"Condition présente {index}",
            "epistemic": "established",
            "sources": [SOURCE.source_id],
            "reason": "",
        }
        for index in range(MAX_SCENE_ITEMS)
    ]
    dependency_raw = _dependency_raw(
        changes=[
            {
                "op": "add",
                "id": "",
                "text": "Une dépendance reste ouverte malgré la saturation descriptive.",
                "status": "open",
                "horizon": "avant la transition",
                "sources": [SOURCE.source_id],
                "reason": "",
            }
        ]
    )

    snapshot = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(
            _raw(changes=item_changes),
            dependency_raw,
        ),
    )

    assert snapshot is not None
    assert len(snapshot.items) == MAX_SCENE_ITEMS
    assert len(snapshot.dependencies) == 1


def test_previous_item_must_be_kept_revised_or_retired():
    previous = SceneSnapshot(
        scene_scope="primary",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        items=(
            SceneItem(
                item_id="scene-1",
                text="Quelque chose reste actif.",
                epistemic="established",
                sources=(SOURCE.source_id,),
            ),
        ),
        sources=(SOURCE,),
    )

    rejection_reasons = []
    silent_drop = parse_scene_patch(
        _raw(changes=[]),
        previous=previous,
        allowed_sources={SOURCE.source_id},
        authoritative_sources={SOURCE.source_id},
        allowed_souvenirs=[],
        rejection_reasons=rejection_reasons,
    )
    retired = parse_scene_patch(
        _raw(
            changes=[
                {
                    "op": "retire",
                    "id": "scene-1",
                    "text": "",
                    "epistemic": "",
                    "sources": [],
                    "reason": "La source établit maintenant sa résolution.",
                }
            ]
        ),
        previous=previous,
        allowed_sources={SOURCE.source_id},
        authoritative_sources={SOURCE.source_id},
        allowed_souvenirs=[],
    )

    assert silent_drop is None
    assert rejection_reasons == ["previous_items_unaccounted"]
    assert retired is not None


def test_patch_rejects_unknown_source_and_unlabelled_inference():
    unknown = json.loads(_raw())
    unknown["changes"][0]["sources"] = ["src-inconnue"]
    unlabelled = json.loads(_raw())
    unlabelled["changes"][0]["epistemic"] = "fact"

    rejection_reasons = []
    assert parse_scene_patch(
        json.dumps(unknown),
        previous=None,
        allowed_sources={SOURCE.source_id},
        authoritative_sources={SOURCE.source_id},
        allowed_souvenirs=[],
        rejection_reasons=rejection_reasons,
    ) is None
    assert rejection_reasons == ["source_unavailable"]
    assert parse_scene_patch(
        json.dumps(unlabelled),
        previous=None,
        allowed_sources={SOURCE.source_id},
        authoritative_sources={SOURCE.source_id},
        allowed_souvenirs=[],
    ) is None

    new_inference = json.loads(_raw())
    new_inference["changes"][0]["epistemic"] = "inferred"
    assert parse_scene_patch(
        json.dumps(new_inference),
        previous=None,
        allowed_sources={SOURCE.source_id},
        authoritative_sources={SOURCE.source_id},
        allowed_souvenirs=[],
    ) is None


def test_derived_state_cannot_establish_a_fact_without_an_exact_authority():
    active_context = SceneSource(
        source_id="src-active_context-family",
        kind="active_context",
        locator="contexte_actif.md#sujets-actifs-1",
        sha256="active",
        excerpt="La petite famille comprend Olivier, Andréa et Timothé.",
    )
    assistant_turn = SceneSource(
        source_id="src-l0_turn-assistant",
        kind="l0_turn",
        locator="2026-07-29T09:48:00 — assistant",
        sha256="assistant",
        excerpt="La petite famille comprend Olivier, Andréa et Timothé.",
    )
    sources = {
        active_context.source_id: active_context,
        assistant_turn.source_id: assistant_turn,
        SOURCE.source_id: SOURCE,
    }
    authoritative = scene_model.authoritative_scene_source_ids(sources)

    derived_only = json.loads(_raw())
    derived_only["changes"][0]["sources"] = [active_context.source_id]
    assistant_only = json.loads(_raw())
    assistant_only["changes"][0]["sources"] = [assistant_turn.source_id]
    uncertain = json.loads(_raw())
    uncertain["changes"][0]["sources"] = [active_context.source_id]
    uncertain["changes"][0]["epistemic"] = "uncertain"
    confirmed = json.loads(_raw())
    confirmed["changes"][0]["sources"] = [
        assistant_turn.source_id,
        SOURCE.source_id,
    ]

    rejection_reasons = []
    assert parse_scene_patch(
        json.dumps(derived_only),
        previous=None,
        allowed_sources=set(sources),
        authoritative_sources=authoritative,
        allowed_souvenirs=[],
        rejection_reasons=rejection_reasons,
    ) is None
    assert rejection_reasons == ["established_authority_missing"]
    assert parse_scene_patch(
        json.dumps(assistant_only),
        previous=None,
        allowed_sources=set(sources),
        authoritative_sources=authoritative,
        allowed_souvenirs=[],
    ) is None
    assert parse_scene_patch(
        json.dumps(uncertain),
        previous=None,
        allowed_sources=set(sources),
        authoritative_sources=authoritative,
        allowed_souvenirs=[],
    ) is not None
    assert parse_scene_patch(
        json.dumps(confirmed),
        previous=None,
        allowed_sources=set(sources),
        authoritative_sources=authoritative,
        allowed_souvenirs=[],
    ) is not None


def test_scene_prompt_names_projection_sources_as_orientation_only():
    messages = build_scene_model_messages(
        previous=None,
        sources={SOURCE.source_id: SOURCE},
        allowed_souvenirs=[],
        souvenir_contents={},
    )

    assert 'can never on their own support' in messages[0]["content"]
    assert 'unconfirmed assistant L0 turn' in messages[0]["content"]
    assert 'confirmed' in messages[0]["content"]
    assert 'never qualifies another action' in messages[0]["content"]


def test_dependency_prompt_preserves_distinct_stage_horizons():
    messages = build_scene_model_messages(
        previous=None,
        sources={SOURCE.source_id: SOURCE},
        allowed_souvenirs=[],
        souvenir_contents={},
    )

    assert 'preparation time' in messages[0]["content"]
    assert 'does not become the time of the next stage' in messages[0]["content"]


def test_scene_prompts_name_previous_units_that_cannot_be_kept():
    stale_source = SceneSource(
        source_id="src-agenda-stale",
        kind="agenda",
        locator="agenda.md#item-1",
        sha256="stale",
        excerpt="Une ancienne échéance.",
    )
    derived_source = SceneSource(
        source_id="src-active_context-current",
        kind="active_context",
        locator="contexte_actif.md#sujet-1",
        sha256="derived",
        excerpt="Une projection actuelle de la condition.",
    )
    item = SceneItem(
        item_id="scene-stale",
        text="Une ancienne condition.",
        epistemic="established",
        sources=(
            stale_source.source_id,
            SOURCE.source_id,
            derived_source.source_id,
        ),
    )
    dependency = SceneDependency(
        dependency_id="dependency-stale",
        text="Une ancienne dépendance.",
        status="uncertain",
        horizon="avant l'échéance",
        sources=(stale_source.source_id,),
    )
    previous = SceneSnapshot(
        scene_scope="primary",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        items=(item,),
        dependencies=(dependency,),
        sources=(stale_source, SOURCE, derived_source),
    )

    grounding = build_scene_model_messages(
        previous=previous,
        sources={
            SOURCE.source_id: SOURCE,
            derived_source.source_id: derived_source,
        },
        allowed_souvenirs=[],
        souvenir_contents={},
    )
    assert '"scene-stale"' in grounding[1]["content"]
    assert '"sources_still_available"' in grounding[1]["content"]
    assert f'"{SOURCE.source_id}"' in grounding[1]["content"]
    assert f'"{derived_source.source_id}"' in grounding[1]["content"]
    assert f'"{stale_source.source_id}"' in grounding[1]["content"]
    assert '"dependency-stale"' in grounding[1]["content"]
    assert '`keep` forbidden' in grounding[1]["content"]
    assert item.text in grounding[1]["content"]
    assert "## Sources conservées" not in grounding[1]["content"]
    assert stale_source.locator not in grounding[1]["content"]
    assert SOURCE.locator in grounding[1]["content"]
    assert '"dependency-stale"' in grounding[1]["content"]


def test_scene_source_catalog_bounds_excerpts_without_dropping_sources():
    sources = {
        f"src-long-{index}": SceneSource(
            source_id=f"src-long-{index}",
            kind="l0_turn",
            locator=f"2026-07-29T19:{index:02d}:00 — user",
            sha256=f"sha-{index}",
            excerpt=(
                f"head-{index} "
                + ("matière " * 700)
                + f"tail-{index}"
            ),
        )
        for index in range(12)
    }

    rendered = scene_model._render_source_catalog(sources)

    assert len(rendered) <= scene_model.MAX_SOURCE_CATALOG_CHARS
    assert '[... bounded source excerpt ...]' in rendered
    for index in range(12):
        assert f"### src-long-{index}\n" in rendered
        assert f"head-{index}" in rendered
        assert f"tail-{index}" in rendered


def test_large_legacy_l0_catalog_fits_then_is_consumed():
    old_sources = tuple(
        SceneSource(
            source_id=f"src-l0_turn-{index:012x}",
            kind="l0_turn",
            locator=f"2026-08-09T12:{index % 60:02d}:00+02:00 — user",
            sha256=f"{index:064x}",
            excerpt=f"Tour exact {index} encore à réviser.",
        )
        for index in range(205)
    )

    rendered = scene_model._render_source_catalog(
        {source.source_id: source for source in old_sources}
    )

    assert len(rendered) <= scene_model.MAX_SOURCE_CATALOG_CHARS
    assert "sha256:" not in rendered
    assert all(source.source_id in rendered for source in old_sources)

    previous = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-205",
        source_turn_started_at=205.0,
        revised_at=206.0,
        revision=205,
        provider={},
        sources=old_sources,
        source_dispositions=tuple(
            scene_model.SceneSourceDisposition(
                source_id=source.source_id,
                condition=("superseded" if index == 0 else "nonoperative"),
                dependency="none",
                evidence_sources=(
                    (old_sources[1].source_id,) if index == 0 else ()
                ),
                reason="Ce tour est déjà comptabilisé.",
            )
            for index, source in enumerate(old_sources)
        ),
    )
    next_sources = build_scene_sources(
        recent_dialogue="",
        previous=previous,
        now=datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc),
    )

    assert all(source.kind != "l0_turn" for source in next_sources.values())
    assert len(next_sources) == 1


def test_hot_memory_cannot_disappear_silently(tmp_path, monkeypatch):
    first_raw = _raw(
        hot_memories=[
            {"name": "theme/projets/exemple", "reason": "Encore active."}
        ]
    )
    first = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(first_raw),
        context=_context(include_souvenir=True),
    )
    assert first is not None

    kept_change = {
        "op": "keep",
        "id": first.items[0].item_id,
        "text": first.items[0].text,
        "epistemic": first.items[0].epistemic,
        "sources": list(first.items[0].sources),
        "reason": "",
    }
    missing = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(_raw(changes=[kept_change])),
        source_trace_id="turn-2",
        source_turn_started_at=20.0,
        context=_context(previous=first, include_souvenir=True),
    )
    released = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(
            _raw(
                changes=[kept_change],
                released_memories=[
                    {
                        "name": "theme/projets/exemple",
                        "reason": "La scène a explicitement changé.",
                    }
                ],
            )
        ),
        source_trace_id="turn-2",
        source_turn_started_at=20.0,
        context=_context(previous=first, include_souvenir=True),
    )

    assert missing is None
    assert released is not None
    assert released.hot_souvenirs == []
    assert released.released_memories[0].name == "theme/projets/exemple"


def test_late_revision_cannot_overwrite_newer_scene(tmp_path, monkeypatch):
    _patch_paths(tmp_path, monkeypatch)
    old_provider = BlockingProvider(_raw())
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            run_scene_model_revision(
                "garden",
                "telegram__garden",
                old_provider,
                source_trace_id="old",
                source_turn_started_at=10.0,
                context=_context(),
            )
        )
    )
    worker.start()
    assert old_provider.entered.wait(timeout=2)

    newer = run_scene_model_revision(
        "garden",
        "telegram__garden",
        ScriptedProvider(_raw()),
        source_trace_id="new",
        source_turn_started_at=20.0,
        context=_context(),
    )
    old_provider.release.set()
    worker.join(timeout=2)

    assert newer is not None
    assert results == [None]
    assert read_scene_model("garden", "telegram__garden").source_trace_id == "new"


def test_recovery_can_continue_the_same_source_on_its_exact_previous_revision(
    tmp_path,
    monkeypatch,
):
    first = _run(
        tmp_path,
        monkeypatch,
        source_trace_id="recovery-turn",
        source_turn_started_at=10.0,
    )
    assert first is not None

    rejected = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(
            _raw(changes=[{"op": "keep", "id": first.items[0].item_id}])
        ),
        source_trace_id="recovery-turn",
        source_turn_started_at=10.0,
        context=_context(previous=first),
    )
    assert rejected is None

    continued = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(
            _raw(changes=[{"op": "keep", "id": first.items[0].item_id}])
        ),
        source_trace_id="recovery-turn",
        source_turn_started_at=10.0,
        context=_context(previous=first),
        allow_same_source_revision=True,
    )

    assert continued is not None
    assert continued.revision == 2
    assert continued.source_trace_id == "recovery-turn"
    assert continued.source_turn_started_at == 10.0
    assert continued.items == first.items


def test_scene_sources_preserve_role_and_time_for_identical_text():
    dialogue = """## 2026-07-20T09:00:00 — user

D'accord.

## 2026-07-20T09:00:01 — assistant

D'accord.
"""
    sources = build_scene_sources(
        recent_dialogue=dialogue,
        now=datetime(2026, 7, 20, 9, 1, tzinfo=timezone.utc),
    )
    turns = [source for source in sources.values() if source.kind == "l0_turn"]

    assert len(turns) == 2
    assert turns[0].source_id != turns[1].source_id
    assert {source.locator.rsplit(" — ", 1)[-1] for source in turns} == {
        "user",
        "assistant",
    }


def test_scene_clock_source_exposes_the_calendar_weekday():
    sources = build_scene_sources(
        recent_dialogue="",
        now=datetime(
            2026,
            7,
            25,
            10,
            0,
            tzinfo=ZoneInfo("Europe/Paris"),
        ),
    )
    clock = next(source for source in sources.values() if source.kind == "clock")

    assert clock.excerpt == (
        "2026-07-25T10:00:00+02:00 "
        "(weekday=Saturday, timezone=Europe/Paris)"
    )


def test_structured_memory_statements_are_independent_sources():
    sources = build_scene_sources(
        recent_dialogue="",
        agenda="""# Agenda

- Demain : un départ est prévu.
""",
        active_context='# Contexte actif\n\n## Sujets actifs\n- Un séjour commence demain.\n- Une ressource domestique reste indisponible.\n\n## Questions ouvertes\n- Une visite reste à confirmer.\n',
        now=datetime(2026, 7, 20, 9, 1, tzinfo=timezone.utc),
    )
    active = [
        source for source in sources.values() if source.kind == "active_context"
    ]

    assert len(active) == 3
    assert len({source.source_id for source in active}) == 3
    resource = next(
        source for source in active if "ressource domestique" in source.excerpt
    )
    assert "## Sujets actifs" in resource.excerpt
    assert "séjour commence" not in resource.excerpt
    assert "visite reste" not in resource.excerpt
    accounting = scene_model._source_accounting_ids(
        previous=None,
        sources=sources,
        bootstrap_source_ids=set(),
    )
    assert {
        source.source_id
        for source in sources.values()
        if source.kind in {"active_context", "agenda"}
    }.issubset(accounting)


def test_voice_receives_only_active_context_units_absent_from_scene_items():
    active_context = """# Contexte actif

## Sujets actifs
- Les 6 et 7 août, Client Alpha est la mission prioritaire.
- Le 6 août, un sync Chronodrive a lieu à midi.
- La propale Helexia est à remettre le 7 août.
"""
    sources = build_scene_sources(
        recent_dialogue="",
        active_context=active_context,
    )
    active = {
        source.excerpt: source
        for source in sources.values()
        if source.kind == "active_context"
    }
    client_alpha = next(source for excerpt, source in active.items() if "Client Alpha" in excerpt)
    chronodrive = next(
        source for excerpt, source in active.items() if "Chronodrive" in excerpt
    )
    helexia = next(source for excerpt, source in active.items() if "Helexia" in excerpt)
    snapshot = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        items=(
            SceneItem(
                item_id="scene-helexia",
                text="La propale Helexia est à remettre le 7 août.",
                epistemic="established",
                sources=(helexia.source_id,),
            ),
        ),
        dependencies=(
            SceneDependency(
                dependency_id="dependency-client_alpha",
                text="La charge Client Alpha reste à articuler avec les autres missions.",
                status="open",
                horizon="les 6 et 7 août",
                sources=(client_alpha.source_id,),
            ),
        ),
        sources=(client_alpha, chronodrive, helexia),
    )

    residual = render_unrepresented_active_context_for_voice(
        snapshot,
        active_context,
    )

    assert "Client Alpha" in residual
    assert "Chronodrive" in residual
    assert "Helexia" not in residual


def test_active_context_residual_is_empty_when_every_unit_is_in_scene_items():
    active_context = """# Contexte actif

## Sujets actifs
- Une mission reste prioritaire aujourd'hui.
"""
    source = next(
        source
        for source in build_scene_sources(
            recent_dialogue="",
            active_context=active_context,
        ).values()
        if source.kind == "active_context"
    )
    snapshot = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        items=(
            SceneItem(
                item_id="scene-priority",
                text="Une mission reste prioritaire aujourd'hui.",
                epistemic="established",
                sources=(source.source_id,),
            ),
        ),
        sources=(source,),
    )

    assert render_unrepresented_active_context_for_voice(
        snapshot,
        active_context,
    ) == ""


def test_nonoperative_disposition_cannot_hide_exact_active_context():
    active_context = """# Contexte actif

## Sujets actifs
- Une mission reste prioritaire aujourd'hui.
"""
    source = next(
        source
        for source in build_scene_sources(
            recent_dialogue="",
            active_context=active_context,
        ).values()
        if source.kind == "active_context"
    )
    snapshot = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        sources=(source,),
        source_dispositions=(
            scene_model.SceneSourceDisposition(
                source_id=source.source_id,
                condition="nonoperative",
                dependency="none",
                reason="Le jugement de scène ne l'a pas retenue.",
            ),
        ),
    )

    assert "mission reste prioritaire" in (
        render_unrepresented_active_context_for_voice(
            snapshot,
            active_context,
        )
    )


def test_previous_souvenir_source_is_not_kept_after_memory_repair():
    previous = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        sources=(SOUVENIR_SOURCE,),
    )

    sources = build_scene_sources(
        recent_dialogue="",
        previous=previous,
    )

    assert SOUVENIR_SOURCE.source_id not in sources


def test_mutable_sources_follow_current_document_while_l0_evidence_survives():
    stale_agenda = SceneSource(
        source_id="src-agenda-stale",
        kind="agenda",
        locator="agenda.md#Agenda/stale",
        sha256="stale",
        excerpt="- Lundi | présence exceptionnelle chez le client.",
    )
    historical_l0 = SceneSource(
        source_id="src-l0-historical",
        kind="l0_turn",
        locator="2026-07-25T10:58:25 — user",
        sha256="historical",
        excerpt="Lundi, exceptionnellement, je vais chez un client.",
    )
    previous = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        sources=(stale_agenda, historical_l0),
    )

    sources = build_scene_sources(
        recent_dialogue="",
        agenda="# Agenda\n\n- À compter d'août | rythme client à confirmer.",
        previous=previous,
    )

    assert stale_agenda.source_id not in sources
    assert historical_l0.source_id not in sources
    assert any(
        source.kind == "agenda" and "À compter d'août" in source.excerpt
        for source in sources.values()
    )

    current_agenda = next(
        source for source in sources.values() if source.kind == "agenda"
    )
    previous_with_l0_evidence = replace(
        previous,
        sources=(current_agenda, historical_l0),
        source_dispositions=(
            scene_model.SceneSourceDisposition(
                source_id=current_agenda.source_id,
                condition="superseded",
                dependency="none",
                evidence_sources=(historical_l0.source_id,),
                reason="Le tour exact remplace cette unité.",
            ),
        ),
    )
    sources_with_evidence = build_scene_sources(
        recent_dialogue="",
        agenda="# Agenda\n\n- À compter d'août | rythme client à confirmer.",
        previous=previous_with_l0_evidence,
    )

    assert historical_l0.source_id in sources_with_evidence


def test_bootstrap_selection_returns_exact_chosen_turns():
    dialogue = """## 2026-07-20T08:00:00 — user

Une ancienne situation reste peut-être ouverte.

## 2026-07-20T09:00:00 — assistant

Un autre échange est terminé.
"""
    sources = build_scene_sources(recent_dialogue=dialogue)
    chosen = next(iter(sources))
    provider = ScriptedProvider(
        json.dumps({"selected": [chosen], "reason": "Encore actuel."})
    )

    selected, judgment = scene_model.select_scene_bootstrap_sources(
        provider,
        dialogue,
    )

    assert judgment.source_ids == (chosen,)
    assert tuple(selected) == (chosen,)
    assert selected[chosen].excerpt == sources[chosen].excerpt
    assert "ancienne situation" in selected[chosen].excerpt


def test_bootstrap_selection_reduces_large_catalogs_in_bounded_stages(
    monkeypatch,
):
    dialogue = "\n\n".join(
        f"## 2026-07-20T08:00:0{index} — user\n\nSituation {index}."
        for index in range(7)
    )
    candidate_ids = tuple(scene_model._dialogue_sources(dialogue))
    monkeypatch.setattr(
        scene_model,
        "MAX_BOOTSTRAP_SELECTION_CANDIDATES",
        3,
    )
    monkeypatch.setattr(
        scene_model,
        "MAX_BOOTSTRAP_CHUNK_SELECTED_TURNS",
        1,
    )
    provider = SequenceProvider(
        [
            json.dumps({"selected": [candidate_ids[1]], "reason": "Lot 1."}),
            json.dumps({"selected": [candidate_ids[4]], "reason": "Lot 2."}),
            json.dumps({"selected": [candidate_ids[6]], "reason": "Lot 3."}),
            json.dumps(
                {"selected": [candidate_ids[4]], "reason": "Arbitrage global."}
            ),
        ]
    )

    selected, judgment = scene_model.select_scene_bootstrap_sources(
        provider,
        dialogue,
    )

    assert tuple(selected) == (candidate_ids[4],)
    assert judgment.source_ids == (candidate_ids[4],)
    assert judgment.reason == "Arbitrage global."
    assert len(provider.prompts) == 4
    assert all(
        prompt[1]["content"].count("### src-l0_turn-") <= 3
        for prompt in provider.prompts
    )


def test_bootstrap_selection_rejects_unknown_source():
    assert parse_scene_bootstrap_selection(
        json.dumps({"selected": ["unknown"], "reason": ""}),
        allowed_sources={SOURCE.source_id},
    ) is None


def test_dependency_patch_rejects_overflow_instead_of_truncating():
    changes = [
        {
            "op": "add",
            "id": "",
            "text": f"Dépendance {index}",
            "status": "open",
            "horizon": "avant la transition",
            "sources": [SOURCE.source_id],
            "reason": "",
        }
        for index in range(MAX_SCENE_DEPENDENCIES + 1)
    ]

    assert parse_scene_dependency_patch(
        _dependency_raw(changes=changes),
        previous=None,
        allowed_sources={SOURCE.source_id},
    ) is None


def test_dependency_source_accounting_rejects_silent_omission():
    other = SceneSource(
        source_id="src-active_context-other",
        kind="active_context",
        locator="contexte_actif.md#Sujet/other",
        sha256="b" * 64,
        excerpt="Une autre condition présente.",
    )
    raw = _dependency_raw(
        changes=[
            {
                "op": "add",
                "id": "",
                "text": "Une autre résolution reste ouverte.",
                "status": "open",
                "horizon": "avant demain",
                "sources": [other.source_id],
                "reason": "",
            }
        ]
    )
    payload = json.loads(raw)
    payload["source_dispositions"] = [
        {
            "source": SOURCE.source_id,
            "condition": "represented",
            "dependency": "none",
            "evidence_sources": [],
            "reason": "Cette condition ne laisse rien d'ouvert.",
        },
        {
            "source": other.source_id,
            "condition": "represented",
            "dependency": "represented",
            "evidence_sources": [],
            "reason": "",
        },
    ]

    parsed = parse_scene_dependency_patch(
        json.dumps(payload),
        previous=None,
        allowed_sources={SOURCE.source_id, other.source_id},
        required_source_accounting={SOURCE.source_id, other.source_id},
    )
    assert parsed is not None
    assert len(parsed.source_dispositions) == 2

    payload["source_dispositions"].pop()
    assert parse_scene_dependency_patch(
        json.dumps(payload),
        previous=None,
        allowed_sources={SOURCE.source_id, other.source_id},
        required_source_accounting={SOURCE.source_id, other.source_id},
    ) is None


def test_source_accounting_cannot_claim_an_uncited_scene_condition():
    payload = json.loads(_dependency_raw())
    payload["source_dispositions"] = [
        {
            "source": SOURCE.source_id,
            "condition": "represented",
            "dependency": "none",
            "evidence_sources": [],
            "reason": "La condition ne laisse rien d'ouvert.",
        }
    ]

    rejection_reasons = []
    assert parse_scene_dependency_patch(
        json.dumps(payload),
        previous=None,
        allowed_sources={SOURCE.source_id},
        required_source_accounting={SOURCE.source_id},
        grounded_condition_sources=set(),
        rejection_reasons=rejection_reasons,
    ) is None
    assert rejection_reasons == ["represented_condition_not_grounded"]


def test_dependency_patch_reports_independent_repairable_failures_together():
    clock = SceneSource(
        source_id="src-clock-now",
        kind="clock",
        locator="current_time",
        sha256="clock",
        excerpt="2026-07-20T09:00:00+02:00",
    )
    payload = json.loads(
        _dependency_raw(
            changes=[
                {
                    "op": "add",
                    "id": "",
                    "text": "Une préparation reste ouverte.",
                    "status": "open",
                    "horizon": "avant demain",
                    "sources": [SOURCE.source_id],
                    "temporal_boundaries": [
                        {
                            "at": "2026-07-21T09:00:00+02:00",
                            "after": "L'échéance est passée ; l'issue reste inconnue.",
                        }
                    ],
                    "reason": "",
                }
            ]
        )
    )
    payload["source_dispositions"] = [
        {
            "source": SOURCE.source_id,
            "condition": "represented",
            "dependency": "represented",
            "evidence_sources": [],
            "reason": "",
        }
    ]

    rejection_reasons = []
    assert parse_scene_dependency_patch(
        json.dumps(payload),
        previous=None,
        allowed_sources={SOURCE.source_id, clock.source_id},
        temporal_source_ids={clock.source_id},
        required_source_accounting={SOURCE.source_id},
        grounded_condition_sources=set(),
        rejection_reasons=rejection_reasons,
    ) is None
    assert rejection_reasons == [
        "temporal_clock_source_missing;represented_condition_not_grounded"
    ]

    messages = scene_model._scene_stage_retry_messages(
        [{"role": "system", "content": "jugement"}],
        "dépendances",
        rejected_raw=json.dumps(payload),
        rejection_reason=rejection_reasons[0],
    )
    guidance = messages[-1]["content"]
    assert 'Rejection codes' in guidance
    assert '`clock` source' in guidance
    assert 'no grounded item cites it' in guidance


def test_dialogue_delta_starts_after_explicit_watermark():
    dialogue = """## 2026-07-20T09:00:00 — user

Premier tour absorbé.

## 2026-07-20T09:00:01 — assistant

Réponse encore non absorbée.
"""
    sources = build_scene_sources(recent_dialogue=dialogue)
    watermark = next(iter(sources.values()))
    snapshot = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        dialogue_watermark_source_id=watermark.source_id,
        dialogue_watermark_locator=watermark.locator,
        sources=(watermark,),
    )

    delta = scene_model_dialogue_delta(snapshot, dialogue)

    assert delta is not None
    assert "Premier tour absorbé" not in delta
    assert "Réponse encore non absorbée" in delta


def test_dialogue_delta_refuses_an_unprovable_gap():
    snapshot = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        dialogue_watermark_source_id="missing",
    )

    assert scene_model_dialogue_delta(
        snapshot,
        "## 2026-07-20T09:00:01 — assistant\n\nTour récent.",
    ) is None


def test_scene_context_recovery_uses_the_oldest_delta_prefix(monkeypatch):
    absorbed = "## 2026-08-03T10:00:00+02:00 — user\n\nAbsorbé."
    delta_chunks = [
        (
            f"## 2026-08-03T10:00:0{index + 1}+02:00 — assistant\n\n"
            f"Delta {index}."
        )
        for index in range(5)
    ]
    dialogue = "\n\n".join((absorbed, *delta_chunks))
    source = next(
        source
        for source in build_scene_sources(
            recent_dialogue=absorbed
        ).values()
        if source.kind == "l0_turn"
    )
    previous = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-0",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        dialogue_watermark_source_id=source.source_id,
        dialogue_watermark_locator=source.locator,
        sources=(source,),
    )
    monkeypatch.setattr(scene_model, "read_scene_model", lambda *_args: previous)
    monkeypatch.setattr(
        scene_model,
        "load_recent_dialogue",
        lambda *_args, **_kwargs: dialogue,
    )
    for loader in (
        "load_user_agenda",
        "load_active_context",
        "load_immediate_focus",
    ):
        monkeypatch.setattr(scene_model, loader, lambda *_args: "")

    context = scene_model.capture_scene_model_context(
        "garden",
        "telegram__garden",
        [],
        dialogue_turn_limit=2,
    )

    excerpts = [
        source.excerpt
        for source in context["sources"].values()
        if source.kind == "l0_turn"
    ]
    assert context["dialogue_total_turns"] == 5
    assert context["dialogue_turn_count"] == 2
    assert context["dialogue_truncated"] is True
    assert any("Delta 0." in excerpt for excerpt in excerpts)
    assert any("Delta 1." in excerpt for excerpt in excerpts)
    assert not any("Delta 2." in excerpt for excerpt in excerpts)


def test_context_rebases_unprovable_watermark_from_bounded_l0(monkeypatch):
    old_source = SceneSource(
        source_id="src-l0-old",
        kind="l0_turn",
        locator="2026-07-20T08:00:00 — user",
        sha256="old",
        excerpt="Une ancienne condition reste ouverte.",
    )
    previous = SceneSnapshot(
        scene_scope="openclaw__garden",
        source_trace_id="turn-old",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        dialogue_watermark_source_id=old_source.source_id,
        dialogue_watermark_locator=old_source.locator,
        sources=(old_source,),
    )
    recovery_dialogue = """## 2026-07-30T08:00:00 — user

Une condition intermédiaire compte encore.

## 2026-07-30T09:00:00 — user

Voici la situation la plus récente.

## 2026-07-30T09:00:01 — assistant

Je la prends en compte.
"""
    recent_dialogue = """## 2026-07-30T09:00:00 — user

Voici la situation la plus récente.

## 2026-07-30T09:00:01 — assistant

Je la prends en compte.
"""
    recovery_sources = build_scene_sources(
        recent_dialogue=recovery_dialogue
    )
    selected_id = next(
        source_id
        for source_id, source in recovery_sources.items()
        if "intermédiaire" in source.excerpt
    )
    unprocessed_id = next(
        source_id
        for source_id, source in recovery_sources.items()
        if "Je la prends en compte" in source.excerpt
    )
    provider = ScriptedProvider(
        json.dumps(
            {
                "selected": [selected_id, unprocessed_id],
                "reason": "La condition reste actuelle.",
            }
        )
    )
    dialogue_reads = []

    def load_dialogue(_user_id, *, max_turns):
        dialogue_reads.append(max_turns)
        if max_turns == scene_model.SCENE_MODEL_DELTA_SCAN_TURNS:
            return recovery_dialogue
        assert max_turns == scene_model.SCENE_MODEL_DIALOGUE_TURNS
        return recent_dialogue

    monkeypatch.setattr(scene_model, "read_scene_model", lambda *_: previous)
    monkeypatch.setattr(scene_model, "load_recent_dialogue", load_dialogue)
    monkeypatch.setattr(scene_model, "load_user_agenda", lambda *_: "")
    monkeypatch.setattr(scene_model, "load_active_context", lambda *_: "")
    monkeypatch.setattr(scene_model, "load_immediate_focus", lambda *_: "")

    context = scene_model.capture_scene_model_context(
        "garden",
        "openclaw__garden",
        [],
        provider=provider,
        dialogue_turn_limit=1,
    )

    assert context["previous"] == previous
    assert old_source.source_id not in context["sources"]
    assert selected_id in context["sources"]
    assert unprocessed_id not in context["sources"]
    assert context["dialogue_total_turns"] == 2
    assert context["dialogue_turn_count"] == 1
    assert context["dialogue_truncated"] is True
    assert any(
        "situation la plus récente" in source.excerpt
        for source in context["sources"].values()
    )
    l0_sources = [
        source
        for source in context["sources"].values()
        if source.kind == "l0_turn"
    ]
    assert max(source.locator for source in l0_sources).startswith(
        "2026-07-30T09:00:00"
    )
    assert dialogue_reads == [
        scene_model.SCENE_MODEL_DELTA_SCAN_TURNS,
        scene_model.SCENE_MODEL_DIALOGUE_TURNS,
    ]
    assert len(provider.prompts) == 1


def test_revision_scheduler_serializes_and_coalesces_rapid_turns(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def revise(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=2)
        return None

    monkeypatch.setattr(scene_model, "run_scene_model_revision", revise)
    key = ("garden", "telegram__garden")
    with scene_model._SCENE_REVISION_LOCK:
        scene_model._SCENE_REVISION_PENDING.pop(key, None)
        scene_model._SCENE_REVISION_RUNNING.discard(key)

    provider = ScriptedProvider(_raw())
    assert schedule_scene_model_revision(
        *key,
        provider,
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        read_souvenirs=["theme/one"],
    )
    assert entered.wait(timeout=2)
    assert schedule_scene_model_revision(
        *key,
        provider,
        source_trace_id="turn-2",
        source_turn_started_at=2.0,
        read_souvenirs=["theme/two"],
    )
    assert schedule_scene_model_revision(
        *key,
        provider,
        source_trace_id="turn-3",
        source_turn_started_at=3.0,
        read_souvenirs=["theme/three"],
    )
    release.set()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with scene_model._SCENE_REVISION_LOCK:
            if key not in scene_model._SCENE_REVISION_RUNNING:
                break
        time.sleep(0.01)

    assert [call["source_trace_id"] for call in calls] == ["turn-1", "turn-3"]
    assert calls[1]["read_souvenirs"] == ["theme/two", "theme/three"]


def test_prompt_is_domain_agnostic():
    messages = build_scene_model_messages(**_context())
    system = messages[0]["content"].casefold()

    for forbidden in ("frigo", "repas", "restaurant", "famille", "train"):
        assert forbidden not in system
    assert 'no situation category\nis predefined' in system
    assert 'a conduct plan' in system
    assert 'it prescribes no' in system
    assert 'independent evidence units' in system
    assert "temporal_boundaries" in system
    assert 'iso\ndate with timezone' in system
    assert '`clock` source' in system

    assert 'a dependency is' in system
    assert 'still-open state or capability' in system
    assert 'preparation time' in system

    bootstrap_system = build_scene_bootstrap_messages(
        {SOURCE.source_id: SOURCE}
    )[0]["content"].casefold()
    for forbidden in ("frigo", "repas", "restaurant", "famille", "train"):
        assert forbidden not in bootstrap_system
    assert 'no domain category\nor vocabulary' in bootstrap_system

    assert 'challenge your own result' in system
    assert 'new evidence closes an old dependency' in system


def test_dependency_pass_adds_only_sourced_open_state():
    patch = parse_scene_patch(
        _raw(),
        previous=None,
        allowed_sources={SOURCE.source_id},
        authoritative_sources={SOURCE.source_id},
        allowed_souvenirs=[],
    )
    raw = json.dumps(
        {
            "reading": "La situation laisse une capacité ordinaire incertaine.",
            "changes": [
                {
                    "op": "add",
                    "id": "",
                    "text": "Une capacité ordinaire ne peut plus être tenue pour acquise.",
                    "status": "uncertain",
                    "horizon": "avant la transition proche",
                    "sources": [SOURCE.source_id],
                    "temporal_boundaries": [],
                    "reason": "",
                }
            ],
        }
    )

    dependencies = parse_scene_dependency_patch(
        raw,
        previous=None,
        allowed_sources={SOURCE.source_id},
    )

    assert patch is not None
    assert dependencies is not None
    assert dependencies.changes[0].status == "uncertain"
    assert dependencies.changes[0].horizon == "avant la transition proche"
    assert dependencies.changes[0].sources == (SOURCE.source_id,)
    assert dependencies.reading == "La situation laisse une capacité ordinaire incertaine."

    unknown = json.loads(raw)
    unknown["changes"][0]["sources"] = ["src-inconnue"]
    assert parse_scene_dependency_patch(
        json.dumps(unknown),
        previous=None,
        allowed_sources={SOURCE.source_id},
    ) is None


def test_patch_rejects_overflow_instead_of_truncating():
    changes = [
        {
            "op": "add",
            "id": "",
            "text": f"Élément {index}",
            "epistemic": "established",
            "sources": [SOURCE.source_id],
            "reason": "",
        }
        for index in range(MAX_SCENE_ITEMS + 1)
    ]

    assert parse_scene_patch(
        _raw(changes=changes),
        previous=None,
        allowed_sources={SOURCE.source_id},
        authoritative_sources={SOURCE.source_id},
        allowed_souvenirs=[],
    ) is None


def test_patch_rejects_an_overlong_scene_text_instead_of_truncating_it():
    changes = [
        {
            "op": "add",
            "id": "",
            "text": "x" * 701,
            "epistemic": "established",
            "sources": [SOURCE.source_id],
            "reason": "",
        }
    ]

    assert parse_scene_patch(
        _raw(changes=changes),
        previous=None,
        allowed_sources={SOURCE.source_id},
        allowed_souvenirs=[],
        authoritative_sources=set(),
    ) is None


def test_dependency_patch_rejects_overlong_text_instead_of_truncating_it():
    changes = [
        {
            "op": "add",
            "id": "",
            "text": "x" * 701,
            "status": "uncertain",
            "horizon": "2026-08-05",
            "sources": [SOURCE.source_id],
            "reason": "",
        }
    ]

    assert parse_scene_dependency_patch(
        _dependency_raw(changes=changes),
        previous=None,
        allowed_sources={SOURCE.source_id},
    ) is None


def test_patch_rejects_an_overlong_boundary_state_instead_of_truncating_it():
    changes = [
        {
            "op": "add",
            "id": "",
            "text": "Un départ est prévu.",
            "epistemic": "established",
            "sources": [SOURCE.source_id],
            "temporal_boundaries": [
                {
                    "at": "2026-08-05T00:00:00+02:00",
                    "after": "x" * 701,
                }
            ],
            "reason": "",
        }
    ]

    assert parse_scene_patch(
        _raw(changes=changes),
        previous=None,
        allowed_sources={SOURCE.source_id},
        allowed_souvenirs=[],
        authoritative_sources=set(),
    ) is None


def test_shadow_requires_explicit_user_allowlist(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_SCENE_MODEL", "shadow")
    monkeypatch.setenv("DAEMON_CORE_SCENE_MODEL_USER_IDS", "garden,other")

    assert scene_model_shadow_enabled("garden") is True
    assert scene_model_shadow_enabled("unknown") is False
    assert scene_model_voice_enabled("garden") is False

    monkeypatch.setenv("DAEMON_CORE_SCENE_MODEL", "canary")
    assert scene_model_shadow_enabled("garden") is True
    assert scene_model_voice_enabled("garden") is True
    assert scene_model_voice_enabled("unknown") is False

    monkeypatch.setenv("DAEMON_CORE_SCENE_MODEL", "off")
    assert scene_model_shadow_enabled("garden") is False


def test_current_topics_canary_requires_an_explicit_user(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_CURRENT_TOPICS", "canary")
    monkeypatch.setenv(
        "DAEMON_CORE_CURRENT_TOPICS_USER_IDS",
        "garden,other",
    )

    assert current_topics_canary_enabled("garden") is True
    assert current_topics_canary_enabled("unknown") is False

    monkeypatch.setenv("DAEMON_CORE_CURRENT_TOPICS_USER_IDS", "*")
    assert current_topics_canary_enabled("garden") is False

    monkeypatch.setenv("DAEMON_CORE_CURRENT_TOPICS", "on")
    monkeypatch.setenv("DAEMON_CORE_CURRENT_TOPICS_USER_IDS", "garden")
    assert current_topics_canary_enabled("garden") is False


def test_current_topics_prompt_is_absent_off_and_bounded_on():
    baseline = build_scene_model_messages(
        previous=None,
        sources={SOURCE.source_id: SOURCE},
        allowed_souvenirs=[],
        souvenir_contents={},
    )
    candidate = build_scene_model_messages(
        previous=None,
        sources={SOURCE.source_id: SOURCE},
        allowed_souvenirs=[],
        souvenir_contents={},
        current_topics_enabled=True,
    )

    assert "current_topic_changes" not in baseline[0]["content"]
    assert "current_topic_changes" in candidate[0]["content"]
    assert 'three topics' in candidate[0]["content"]
    assert 'mental space' in candidate[0]["content"]
    assert 'A topic may remain summarized' in candidate[0]["content"]
    assert 'retaining a topic never suffices' in candidate[0]["content"]
    assert "garde-la aussi dans `hot_memories`" not in candidate[0]["content"]
    assert 'Previous current topics' in candidate[1]["content"]


def test_current_topics_roundtrip_and_voice_visibility_are_canary_scoped(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DAEMON_CORE_CURRENT_TOPICS", "canary")
    monkeypatch.setenv("DAEMON_CORE_CURRENT_TOPICS_USER_IDS", "garden")
    provider = ScriptedProvider(
        _raw(
            current_topic_changes=[
                {
                    "op": "add",
                    "id": "",
                    "text": "Un départ proche reste une matière vivante.",
                    "sources": [SOURCE.source_id],
                    "reason": "",
                }
            ]
        )
    )

    snapshot = _run(tmp_path, monkeypatch, provider=provider)

    assert snapshot is not None
    assert len(snapshot.current_topics) == 1
    assert snapshot.current_topics[0].sources == (SOURCE.source_id,)
    assert snapshot.current_topics[0].topic_id.startswith("topic-1-1-")
    assert read_scene_model("garden", "telegram__garden") == snapshot
    assert "current_topic_changes" in provider.prompts[0][0]["content"]
    assert "## Sujets du moment" not in render_scene_model_for_voice(snapshot)
    visible = render_scene_model_for_voice(
        snapshot,
        include_current_topics=True,
    )
    assert "## Sujets du moment" in visible
    assert "[established] Un départ proche reste une matière vivante." in visible
    assert snapshot.current_topics[0].topic_id not in visible


def test_invalid_current_topics_fail_open_without_blocking_scene(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DAEMON_CORE_CURRENT_TOPICS", "canary")
    monkeypatch.setenv("DAEMON_CORE_CURRENT_TOPICS_USER_IDS", "garden")
    provider = ScriptedProvider(
        _raw(
            current_topic_changes=[
                {
                    "op": "add",
                    "id": "",
                    "text": "Le projet reste vivant.",
                    "sources": [SOUVENIR_SOURCE.source_id],
                    "reason": "",
                }
            ]
        )
    )

    snapshot = _run(
        tmp_path,
        monkeypatch,
        provider=provider,
        context=_context(include_souvenir=True),
    )

    assert snapshot is not None
    assert len(snapshot.items) == 1
    assert snapshot.current_topics == ()
    event = json.loads(scene_model.SHADOW_LOG.read_text(encoding="utf-8"))
    assert event["current_topics_valid"] is False
    assert event["current_topics_rejection"] == (
        "current_topic_user_authority_missing"
    )


def test_current_topics_release_is_explicit_and_budget_is_fail_open():
    previous = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        current_topics=(
            SceneCurrentTopic(
                topic_id="topic-1",
                text="Les lunettes restent à réparer.",
                sources=(SOURCE.source_id,),
            ),
        ),
        sources=(SOURCE,),
    )
    released = parse_scene_patch(
        _raw(
            changes=[],
            current_topic_changes=[
                {
                    "op": "release",
                    "id": "topic-1",
                    "text": "",
                    "sources": [],
                    "reason": "Alex dit que les lunettes sont réparées.",
                }
            ],
        ),
        previous=previous,
        allowed_sources={SOURCE.source_id},
        allowed_souvenirs=[],
        authoritative_sources={SOURCE.source_id},
        current_topics_enabled=True,
        user_turn_sources={SOURCE.source_id},
    )
    too_many = parse_scene_patch(
        _raw(
            changes=[],
            current_topic_changes=[
                {
                    "op": "add",
                    "id": "",
                    "text": f"Matière vivante {index}.",
                    "sources": [SOURCE.source_id],
                    "reason": "",
                }
                for index in range(MAX_CURRENT_TOPICS + 1)
            ],
        ),
        previous=None,
        allowed_sources={SOURCE.source_id},
        allowed_souvenirs=[],
        authoritative_sources={SOURCE.source_id},
        current_topics_enabled=True,
        user_turn_sources={SOURCE.source_id},
    )

    assert released is not None
    assert released.current_topics_valid is True
    assert released.current_topic_changes[0].operation == "release"
    assert too_many is not None
    assert too_many.current_topics_valid is False
    assert too_many.current_topic_changes == ()
    assert too_many.current_topics_rejection == "current_topics_budget_exceeded"


def test_current_topic_exact_source_survives_the_next_scene_delta():
    previous = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        current_topics=(
            SceneCurrentTopic(
                topic_id="topic-1",
                text="Les lunettes restent à réparer.",
                sources=(SOURCE.source_id,),
            ),
        ),
        sources=(SOURCE,),
    )

    sources = build_scene_sources(
        recent_dialogue="",
        previous=previous,
    )

    assert SOURCE.source_id in sources


def test_voice_render_keeps_epistemic_status_without_internal_source_ids():
    snapshot = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-1",
        source_turn_started_at=10.0,
        revised_at=20.0,
        revision=1,
        provider={},
        reading="Une transition reste ouverte.",
        conversation="L'échange garde une prise.",
        items=(
            SceneItem(
                item_id="scene-1",
                text='Une présence reste à confirmer.',
                epistemic="uncertain",
                sources=(SOURCE.source_id,),
            ),
        ),
        sources=(SOURCE,),
    )

    rendered = render_scene_model_for_voice(snapshot)

    assert '[uncertain] Une présence reste à confirmer.' in rendered
    assert "Une transition reste ouverte." in rendered
    assert SOURCE.source_id not in rendered
    assert "scene-1" not in rendered


def test_alarm_boundary_is_projected_after_four_hours_of_silence():
    boundary = SceneTemporalBoundary(
        at="2026-07-21T05:40:52+02:00",
        after=(
            "L'heure prévue du réveil est passée ; le fait que Alex soit "
            "levé n'est pas établi."
        ),
    )
    snapshot = SceneSnapshot(
        scene_scope="telegram__garden",
        source_trace_id="turn-alarm",
        source_turn_started_at=datetime.fromisoformat(
            "2026-07-21T02:40:57+02:00"
        ).timestamp(),
        revised_at=datetime.fromisoformat(
            "2026-07-21T02:41:41+02:00"
        ).timestamp(),
        revision=18,
        provider={},
        reading="Alex va dormir environ trois heures avant de se lever.",
        conversation="La nuit se termine provisoirement.",
        items=(
            SceneItem(
                item_id="scene-alarm",
                text="Un réveil est prévu le 21 juillet 2026 vers 05:40 CEST.",
                epistemic="established",
                sources=(SOURCE.source_id,),
                temporal_boundaries=(boundary,),
            ),
        ),
        sources=(SOURCE,),
    )

    before = render_scene_model_for_voice(
        snapshot,
        now=datetime.fromisoformat("2026-07-21T05:30:00+02:00"),
    )
    after = render_scene_model_for_voice(
        snapshot,
        now=datetime.fromisoformat("2026-07-21T06:36:30+02:00"),
    )

    assert "Un réveil est prévu" in before
    assert "prochain seuil: 2026-07-21T05:40:52+02:00" in before
    assert "Un réveil est prévu" not in after
    assert "L'heure prévue du réveil est passée" in after
    assert "le fait que Alex soit levé n'est pas établi" in after
    assert "Alex va dormir environ trois heures" not in after
    assert "[uncertain; seuil franchi: 2026-07-21T05:40:52+02:00]" in after


def test_temporal_boundary_roundtrip_keeps_clock_provenance(tmp_path, monkeypatch):
    clock = SceneSource(
        source_id="src-clock-now",
        kind="clock",
        locator="current_time",
        sha256="clock",
        excerpt="2026-07-21T02:41:00+02:00",
    )
    raw = _raw(
        changes=[
            {
                "op": "add",
                "id": "",
                "text": "Un réveil est prévu le 21 juillet 2026 vers 05:40 CEST.",
                "epistemic": "established",
                "sources": [SOURCE.source_id, clock.source_id],
                "temporal_boundaries": [
                    {
                        "at": "2026-07-21T05:40:52+02:00",
                        "after": "L'heure prévue du réveil est passée ; l'issue reste inconnue.",
                    }
                ],
                "reason": "",
            }
        ]
    )
    context = _context()
    context["sources"][clock.source_id] = clock

    snapshot = _run(
        tmp_path,
        monkeypatch,
        provider=ScriptedProvider(raw),
        context=context,
    )

    assert snapshot is not None
    assert snapshot.items[0].temporal_boundaries == (
        SceneTemporalBoundary(
            at="2026-07-21T05:40:52+02:00",
            after="L'heure prévue du réveil est passée ; l'issue reste inconnue.",
        ),
    )
    assert clock in snapshot.sources
    assert read_scene_model("garden", "telegram__garden") == snapshot


def test_temporal_boundary_requires_timezone_and_clock_source():
    clock_id = "src-clock-now"
    base_change = {
        "op": "add",
        "id": "",
        "text": "Un réveil est prévu à une heure absolue.",
        "epistemic": "established",
        "sources": [SOURCE.source_id],
        "temporal_boundaries": [
            {
                "at": "2026-07-21T05:40:52",
                "after": "L'heure prévue est passée ; l'issue reste inconnue.",
            }
        ],
        "reason": "",
    }
    timezone_rejections = []
    assert parse_scene_patch(
        _raw(changes=[base_change]),
        previous=None,
        allowed_sources={SOURCE.source_id, clock_id},
        authoritative_sources={SOURCE.source_id, clock_id},
        allowed_souvenirs=[],
        temporal_source_ids={clock_id},
        rejection_reasons=timezone_rejections,
    ) is None
    assert timezone_rejections == ["temporal_boundaries_invalid"]

    aware_change = {
        **base_change,
        "temporal_boundaries": [
            {
                "at": "2026-07-21T05:40:52+02:00",
                "after": "L'heure prévue est passée ; l'issue reste inconnue.",
            }
        ],
    }
    clock_rejections = []
    assert parse_scene_patch(
        _raw(changes=[aware_change]),
        previous=None,
        allowed_sources={SOURCE.source_id, clock_id},
        authoritative_sources={SOURCE.source_id, clock_id},
        allowed_souvenirs=[],
        temporal_source_ids={clock_id},
        rejection_reasons=clock_rejections,
    ) is None
    assert clock_rejections == ["temporal_clock_source_missing"]


def test_keep_preserves_scene_item_without_rewriting_its_provenance():
    clock = SceneSource(
        source_id="src-clock-previous",
        kind="clock",
        locator="current_time",
        sha256="clock",
        excerpt="2026-07-21T02:41:00+02:00",
    )
    boundary = SceneTemporalBoundary(
        at="2026-07-21T05:40:52+02:00",
        after="L'heure prévue est passée ; l'issue reste inconnue.",
    )
    item = SceneItem(
        item_id="scene-previous",
        text="Un réveil est prévu le 21 juillet 2026 vers 05:40 CEST.",
        epistemic="established",
        sources=(SOURCE.source_id, clock.source_id),
        temporal_boundaries=(boundary,),
    )
    previous = SceneSnapshot(
        scene_scope="primary",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        items=(item,),
        sources=(SOURCE, clock),
    )
    raw = json.dumps(
        {
            "reading": "La condition reste active.",
            "conversation": "",
            "changes": [
                {
                    "op": "keep",
                    "id": item.item_id,
                    "text": "Une copie altérée qui doit être ignorée.",
                    "sources": [SOURCE.source_id],
                }
            ],
            "hot_memories": [],
            "released_memories": [],
        }
    )

    patch = parse_scene_patch(
        raw,
        previous=previous,
        allowed_sources={SOURCE.source_id, clock.source_id},
        authoritative_sources={SOURCE.source_id, clock.source_id},
        allowed_souvenirs=[],
        temporal_source_ids={clock.source_id},
    )

    assert patch is not None
    assert patch.changes[0].text == item.text
    assert patch.changes[0].epistemic == item.epistemic
    assert patch.changes[0].sources == item.sources
    assert patch.changes[0].temporal_boundaries == item.temporal_boundaries


def test_keep_rejects_scene_item_whose_previous_source_is_no_longer_available():
    stale_source = SceneSource(
        source_id="src-agenda-stale",
        kind="agenda",
        locator="agenda.md#item-1",
        sha256="stale",
        excerpt="La présentation est prévue le matin.",
    )
    item = SceneItem(
        item_id="scene-stale",
        text="La présentation est prévue le matin.",
        epistemic="established",
        sources=(stale_source.source_id,),
    )
    previous = SceneSnapshot(
        scene_scope="primary",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        items=(item,),
        sources=(stale_source,),
    )

    patch = parse_scene_patch(
        json.dumps(
            {
                "reading": "La situation doit être révisée.",
                "conversation": "",
                "changes": [{"op": "keep", "id": item.item_id}],
                "hot_memories": [],
                "released_memories": [],
            }
        ),
        previous=previous,
        allowed_sources={SOURCE.source_id},
        authoritative_sources={SOURCE.source_id},
        allowed_souvenirs=[],
    )

    assert patch is None


def test_keep_preserves_dependency_without_rewriting_its_provenance():
    clock = SceneSource(
        source_id="src-clock-previous",
        kind="clock",
        locator="current_time",
        sha256="clock",
        excerpt="2026-07-21T02:41:00+02:00",
    )
    boundary = SceneTemporalBoundary(
        at="2026-07-21T05:40:52+02:00",
        after="L'heure prévue est passée ; l'issue reste inconnue.",
    )
    dependency = SceneDependency(
        dependency_id="dependency-previous",
        text="Le résultat du réveil reste à établir.",
        status="uncertain",
        horizon="21 juillet 2026 vers 05:40 CEST",
        sources=(SOURCE.source_id, clock.source_id),
        temporal_boundaries=(boundary,),
    )
    previous = SceneSnapshot(
        scene_scope="primary",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        dependencies=(dependency,),
        sources=(SOURCE, clock),
    )
    raw = json.dumps(
        {
            "reading": "La dépendance reste ouverte.",
            "changes": [
                {
                    "op": "keep",
                    "id": dependency.dependency_id,
                    "text": "Une copie altérée qui doit être ignorée.",
                    "sources": [SOURCE.source_id],
                }
            ],
        }
    )

    patch = parse_scene_dependency_patch(
        raw,
        previous=previous,
        allowed_sources={SOURCE.source_id, clock.source_id},
        temporal_source_ids={clock.source_id},
    )

    assert patch is not None
    assert patch.changes[0].text == dependency.text
    assert patch.changes[0].status == dependency.status
    assert patch.changes[0].horizon == dependency.horizon
    assert patch.changes[0].sources == dependency.sources
    assert patch.changes[0].temporal_boundaries == dependency.temporal_boundaries


def test_keep_rejects_dependency_whose_previous_source_is_no_longer_available():
    stale_source = SceneSource(
        source_id="src-agenda-stale",
        kind="agenda",
        locator="agenda.md#item-1",
        sha256="stale",
        excerpt="L'horaire reste à confirmer.",
    )
    dependency = SceneDependency(
        dependency_id="dependency-stale",
        text="L'horaire de présentation reste à confirmer.",
        status="uncertain",
        horizon="avant la présentation",
        sources=(stale_source.source_id,),
    )
    previous = SceneSnapshot(
        scene_scope="primary",
        source_trace_id="turn-1",
        source_turn_started_at=1.0,
        revised_at=2.0,
        revision=1,
        provider={},
        dependencies=(dependency,),
        sources=(stale_source,),
    )

    patch = parse_scene_dependency_patch(
        json.dumps(
            {
                "reading": "La dépendance doit être révisée.",
                "changes": [
                    {"op": "keep", "id": dependency.dependency_id}
                ],
            }
        ),
        previous=previous,
        allowed_sources={SOURCE.source_id},
    )

    assert patch is None
