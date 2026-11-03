import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

agent_module = sys.modules.setdefault("agent", types.ModuleType("agent"))
memory_provider_module = types.ModuleType("agent.memory_provider")
memory_provider_module.MemoryProvider = type("MemoryProvider", (), {})
sys.modules["agent.memory_provider"] = memory_provider_module
setattr(agent_module, "memory_provider", memory_provider_module)

spec = importlib.util.spec_from_file_location(
    "daemon_memory_trace_test", ROOT / "integrations" / "hermes_plugin" / "__init__.py"
)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class HarnessStub:
    def update_memory_after_response(self, **kwargs):
        return None


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


def _provider(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    monkeypatch.setenv("DAEMON_CORE_LATENCY_LOG", str(tmp_path / "latency.jsonl"))
    monkeypatch.setattr("daemon_core.memory.writer.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.memory.reader.MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.sovereign.write_scene_verdict", lambda *a, **k: None)
    provider = plugin.DaemonCoreMemoryProvider()
    provider._harness = HarnessStub()
    provider._user_id = "garden"
    provider._writes_enabled = True
    provider._platform = "telegram"
    provider._assemble_context = lambda query, **kwargs: (
        "context",
        type("Route", (), {"context_depth": "light", "scene_register": "relationnel"})(),
        [],
    )
    return provider, memory_root


def _l0_text(memory_root: Path) -> str:
    files = list((memory_root / "levels" / "L0_ground_zero" / "conversations").glob("garden_*.md"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


def test_initialize_does_not_schedule_fake_model_warmup(monkeypatch):
    harness = HarnessStub()
    monkeypatch.setattr("daemon_core.loop.DaemonCore", lambda: harness)

    def forbidden_thread(*args, **kwargs):
        raise AssertionError("initialize ne doit plus lancer de warmup")

    monkeypatch.setattr(plugin.threading, "Thread", forbidden_thread)
    provider = plugin.DaemonCoreMemoryProvider()

    provider.initialize("session", user_id="garden", platform="telegram")

    assert provider._harness is harness


def test_runtime_bootstrap_rejects_an_empty_namespace_package(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "src"
    (source / "daemon_core").mkdir(parents=True)
    monkeypatch.setenv("DAEMON_CORE_SRC", str(source))

    assert plugin._ensure_daemon_core_importable() is False


def test_runtime_bootstrap_rejects_a_loaded_module_from_another_source(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "src"
    package = source / "daemon_core"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# package\n", encoding="utf-8")
    (package / "loop.py").write_text("# loop\n", encoding="utf-8")
    monkeypatch.setenv("DAEMON_CORE_SRC", str(source))

    assert plugin._ensure_daemon_core_importable() is False


def test_initialize_fails_loudly_when_runtime_contract_is_invalid(monkeypatch):
    monkeypatch.setattr(plugin, "_ensure_daemon_core_importable", lambda: False)

    with pytest.raises(RuntimeError, match="runtime contract"):
        plugin.DaemonCoreMemoryProvider().initialize("session")


def test_hermes_provider_is_not_an_after_turn_worker_owner():
    provider = plugin.DaemonCoreMemoryProvider()

    assert not hasattr(provider, "_ensure_durable_after_turn_worker")
    assert not hasattr(provider, "_after_turn_worker")


def test_l0_recall_tool_schema_is_feature_gated(monkeypatch):
    provider = plugin.DaemonCoreMemoryProvider()
    monkeypatch.delenv("DAEMON_CORE_L0_RECALL_TOOL", raising=False)

    assert provider.get_tool_schemas() == []

    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    schemas = provider.get_tool_schemas()

    assert [schema["name"] for schema in schemas] == ["daemon_recall_l0"]
    assert schemas[0]["parameters"]["required"] == ["need"]
    assert "temporal_context" not in schemas[0]["parameters"]["properties"]
    assert schemas[0]["parameters"]["properties"]["lookup_kind"]["enum"] == [
        "personal_fact",
        "conversation_chronology",
    ]


def test_exact_souvenir_read_contract_matches_cross_host_fixture(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    provider = plugin.DaemonCoreMemoryProvider()
    schema = next(
        item
        for item in provider.get_tool_schemas()
        if item["name"] == "daemon_read_souvenirs"
    )
    properties = schema["parameters"]["properties"]
    scene = properties["scene"]
    scene_properties = scene["properties"]
    handling = scene_properties["handling"]
    handling_properties = handling["properties"]
    actual = {
        "description": schema["description"],
        "paths_description": properties["paths"]["description"],
        "collection_path_description": properties["collection_path"][
            "description"
        ],
        "collection_offset_description": properties["collection_offset"][
            "description"
        ],
        "scene_description": scene["description"],
        "established_description": scene_properties["established"][
            "description"
        ],
        "uncertain_description": scene_properties["uncertain"]["description"],
        "need_description": scene_properties["need"]["description"],
        "handling_description": handling["description"],
        "mode_description": handling_properties["mode"]["description"],
        "plan_description": handling_properties["plan"]["description"],
    }
    expected = json.loads(
        (ROOT / "tests" / "fixtures" / "souvenir_read_tool_contract.json")
        .read_text(encoding="utf-8")
    )

    assert actual == expected


def test_l0_recall_contract_matches_cross_host_fixture(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    provider = plugin.DaemonCoreMemoryProvider()
    schema = next(
        item
        for item in provider.get_tool_schemas()
        if item["name"] == "daemon_recall_l0"
    )
    properties = schema["parameters"]["properties"]
    actual = {
        "description": schema["description"],
        "lookup_kind_description": properties["lookup_kind"]["description"],
        "need_description": properties["need"]["description"],
    }
    expected = json.loads(
        (ROOT / "tests" / "fixtures" / "l0_recall_tool_contract.json")
        .read_text(encoding="utf-8")
    )

    assert actual == expected


def test_exact_souvenir_read_tool_is_candidate_gated_and_tracks_real_reads(
    tmp_path,
    monkeypatch,
):
    import daemon_core.held_atlas_sovereign as held_atlas

    provider, _ = _provider(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_HELD_ATLAS_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_HELD_ATLAS_USER_IDS", "garden")
    observed_read = {}

    def read(paths, **kwargs):
        observed_read["paths"] = paths
        observed_read.update(kwargs)
        return held_atlas.ExactSouvenirRead(
            souvenirs=(("theme/famille", "# Famille\n\n- Fait exact."),),
            rejected_paths=("theme/inconnue",),
            rendered_chars=42,
        )

    monkeypatch.setattr(
        held_atlas,
        "read_exact_active_souvenirs",
        read,
    )

    schemas = provider.get_tool_schemas()
    raw = provider.handle_tool_call(
        "daemon_read_souvenirs",
        {
            "paths": ["theme/famille", "theme/inconnue"],
            "collection_path": "theme/famille",
            "collection_offset": 0,
            "scene": {
                "established": ["Alex parle de sa famille."],
                "uncertain": ['La personne visée reste à confirmer.'],
                "need": "Identifier les proches réellement concernés.",
                "handling": {
                    "mode": "clarify",
                    "plan": "De qui parles-tu précisément ?",
                },
            },
        },
    )
    result = __import__("json").loads(raw)

    assert [schema["name"] for schema in schemas] == [
        "daemon_read_souvenirs",
        "daemon_recall_l0",
    ]
    assert schemas[0]["parameters"]["properties"]["paths"]["maxItems"] == 8
    assert schemas[0]["parameters"]["properties"]["collection_offset"][
        "minimum"
    ] == 0
    assert "collection_path" in schemas[0]["parameters"]["properties"]
    assert schemas[0]["parameters"]["required"] == ["paths", "scene"]
    assert schemas[0]["parameters"]["properties"]["scene"]["required"] == [
        "established",
        "uncertain",
        "need",
        "handling",
    ]
    assert schemas[0]["parameters"]["properties"]["scene"]["properties"][
        "handling"
    ]["properties"]["mode"]["enum"] == [
        "direct",
        "clarify",
        "condition",
    ]
    mode_description = schemas[0]["parameters"]["properties"]["scene"]["properties"][
        "handling"
    ]["properties"]["mode"]["description"]
    plan_description = schemas[0]["parameters"]["properties"]["scene"]["properties"][
        "handling"
    ]["properties"]["plan"]["description"]
    established_description = schemas[0]["parameters"]["properties"]["scene"][
        "properties"
    ]["established"]["description"]
    uncertain_description = schemas[0]["parameters"]["properties"]["scene"][
        "properties"
    ]["uncertain"]["description"]
    assert 'waits without composing' in mode_description
    assert 'then waits without composing' in plan_description
    assert 'neither downgrades nor removes' in established_description
    assert 'an unknown about one property' in uncertain_description
    assert 'The scope itself is uncertain' in uncertain_description
    assert "offer" not in mode_description
    assert "offer" not in plan_description
    assert "nouveau sujet" not in mode_description
    assert result["status"] == "partial"
    assert result["capsules"][0]["content"].endswith("Fait exact.")
    assert result["scene_assessment"]["uncertain"] == [
        'La personne visée reste à confirmer.'
    ]
    assert result["scene_assessment"]["handling"]["mode"] == "clarify"
    assert 'natural question' in result["coverage_checkpoint"]
    assert observed_read["collection_path"] == "theme/famille"
    assert observed_read["collection_offset"] == 0
    assert provider._turn_read_souvenirs == ["theme/famille"]
    assert provider._turn_souvenir_read_calls == 1


def test_exact_souvenir_read_is_not_blocked_by_provisional_direct_handling(
    tmp_path,
    monkeypatch,
):
    import daemon_core.held_atlas_sovereign as held_atlas

    provider, _ = _provider(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_HELD_ATLAS_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_HELD_ATLAS_USER_IDS", "garden")
    monkeypatch.setattr(
        held_atlas,
        "read_exact_active_souvenirs",
        lambda paths, **_kwargs: held_atlas.ExactSouvenirRead(
            souvenirs=(
                (
                    "theme/famille/famille_elargie",
                    "# Famille\n\n- La composition exacte est établie ici.",
                ),
            ),
            rejected_paths=(),
            rendered_chars=54,
        ),
    )

    result = __import__("json").loads(
        provider.handle_tool_call(
            "daemon_read_souvenirs",
            {
                "paths": ["theme/famille/famille_elargie"],
                "scene": {
                    "established": ["Un groupe familial est présent."],
                    "uncertain": ["Sa composition exacte reste à établir."],
                    "need": "Retrouver la composition exacte du groupe.",
                    "handling": {
                        "mode": "direct",
                        "plan": "Répondre si la lecture résout la composition.",
                    },
                },
            },
        )
    )

    assert result["status"] == "ok"
    assert result["capsules"][0]["path"] == "theme/famille/famille_elargie"
    assert result["scene_assessment"]["handling"]["mode"] == "direct"
    assert 'proposed before reading does not resolve' in result["coverage_checkpoint"]
    assert provider._turn_read_souvenirs == ["theme/famille/famille_elargie"]


def test_professional_calendar_bridge_is_not_registered(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")

    assert "read_professional_calendar" not in {
        schema["name"] for schema in provider.get_tool_schemas()
    }
    result = json.loads(
        provider.handle_tool_call("read_professional_calendar", {})
    )
    assert result == {"status": "error", "reason": "Unknown tool."}


def test_canonical_memory_tools_are_registered_without_personal_allowlist(
    monkeypatch,
):
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_MODE", "block")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS", "off")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_LIBRARY", "off")
    monkeypatch.setenv("DAEMON_CORE_HELD_ATLAS_VOICE", "off")
    monkeypatch.setattr("daemon_core.loop.DaemonCore", HarnessStub)

    provider = plugin.DaemonCoreMemoryProvider()

    # MemoryManager.add_provider() runs before initialize(): the tool name
    # must already exist so Hermes can build its immutable routing index.
    assert [schema["name"] for schema in provider.get_tool_schemas()] == [
        "daemon_read_souvenirs",
        "daemon_recall_l0",
    ]

    provider.initialize("session", user_id="other", platform="telegram")

    assert [schema["name"] for schema in provider.get_tool_schemas()] == [
        "daemon_read_souvenirs",
        "daemon_recall_l0",
    ]

    canary = plugin.DaemonCoreMemoryProvider()
    canary.initialize("session", user_id="garden", platform="telegram")
    assert [schema["name"] for schema in canary.get_tool_schemas()] == [
        "daemon_read_souvenirs",
        "daemon_recall_l0",
    ]


def test_retired_library_flags_cannot_activate_canonical_tools(monkeypatch):
    monkeypatch.delenv("DAEMON_CORE_SOVEREIGN_VOICE", raising=False)
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_LIBRARY", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_LIBRARY_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_HELD_ATLAS_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_HELD_ATLAS_USER_IDS", "garden")

    provider = plugin.DaemonCoreMemoryProvider()
    provider._initialized = True
    provider._user_id = "garden"
    provider._platform = "telegram"

    assert provider.get_tool_schemas() == []


def test_standalone_l0_tool_uses_only_its_own_allowlist(monkeypatch):
    monkeypatch.delenv("DAEMON_CORE_SOVEREIGN_VOICE", raising=False)
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    monkeypatch.delenv("DAEMON_CORE_L0_RECALL_USER_IDS", raising=False)
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS_USER_IDS", "garden")

    assert plugin._l0_recall_tool_enabled("garden", "telegram") is False


def test_l0_recall_preflights_and_returns_unread_durable_souvenirs(
    tmp_path,
    monkeypatch,
):
    import daemon_core.held_atlas_sovereign as held_atlas

    provider, _ = _provider(tmp_path, monkeypatch)
    provider._harness.routing_provider = object()
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_LIBRARY", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_LIBRARY_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_USER_IDS", "garden")
    monkeypatch.setattr(
        held_atlas,
        "build_exact_memory_atlas",
        lambda **_kwargs: held_atlas.ExactMemoryAtlas(
            text="- theme/jeux/manuels",
            souvenir_names=("theme/jeux/manuels",),
            rendered_chars=22,
            sha256="atlas",
        ),
    )
    monkeypatch.setattr(
        held_atlas,
        "judge_unread_souvenirs_before_l0",
        lambda *args, **kwargs: held_atlas.SouvenirLibraryPreflight(
            paths=("theme/jeux/manuels",),
            reason="Source durable plausible.",
            confidence=0.98,
        ),
    )
    monkeypatch.setattr(
        held_atlas,
        "read_exact_active_souvenirs",
        lambda paths, **_kwargs: held_atlas.ExactSouvenirRead(
            souvenirs=(("theme/jeux/manuels", "# Manuels\n\n- D&D et Cthulhu."),),
            rejected_paths=(),
            rendered_chars=34,
        ),
    )
    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_forensic_recovery",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("L0 must stay cold while durable sources remain")
        ),
    )

    result = __import__("json").loads(
        provider.handle_tool_call(
            "daemon_recall_l0",
            {"need": "Quelles autres reliques ont été sauvées ?"},
        )
    )

    assert result["status"] == "capsule_sources"
    assert result["l0_searched"] is False
    assert result["capsules"][0]["path"] == "theme/jeux/manuels"
    assert provider._turn_read_souvenirs == ["theme/jeux/manuels"]


def test_l0_recall_tool_returns_only_validated_forensic_evidence(
    tmp_path,
    monkeypatch,
):
    provider, _ = _provider(tmp_path, monkeypatch)
    provider._harness.routing_provider = object()
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_USER_IDS", "garden")
    captured = {}

    def recover(**kwargs):
        captured.update(kwargs)
        return type(
            "Recovery",
            (),
            {
                "status": "found",
                "source_file": "garden_2026-07-01.md",
                "evidence": "Nous rentrons dimanche soir à Lyon.",
                "reason": "Citation utilisateur exacte.",
                "confidence": 0.98,
                "relation_status": "established",
                "conclusion": "Alex et Sam rentrent dimanche soir à Lyon.",
            },
        )()

    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_forensic_recovery",
        recover,
    )
    monkeypatch.setattr(plugin, "_verify_l0_evidence_coverage", lambda **kwargs: (True, ""))

    raw = provider.handle_tool_call(
        "daemon_recall_l0",
        {
            "need": "date du retour à Lyon de Alex et Sam",
            "temporal_context": "vacances de juillet 2026",
        },
    )
    result = __import__("json").loads(raw)

    assert result["status"] == "found"
    assert result["evidence"] == "Nous rentrons dimanche soir à Lyon."
    assert result["relation_status"] == "established"
    assert result["conclusion"] == "Alex et Sam rentrent dimanche soir à Lyon."
    assert result["source_file"] == "garden_2026-07-01.md"
    assert result["evidence_kind"] == "personal_fact"
    assert 'only' in result["response_constraint"]
    assert "reason" not in result
    assert captured["user_id"] == "garden"
    assert captured["recovery_needs"][0].description.startswith("date du retour")
    assert captured["message"] == (
        "Recall need: date du retour à Lyon de Alex et Sam"
    )
    assert "vacances de juillet 2026" not in captured["message"]


def test_l0_chronology_bypasses_souvenir_preflight_and_returns_exact_passage(
    tmp_path,
    monkeypatch,
):
    import daemon_core.held_atlas_sovereign as held_atlas

    provider, _ = _provider(tmp_path, monkeypatch)
    provider._harness.routing_provider = object()
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_LIBRARY", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_LIBRARY_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_USER_IDS", "garden")
    provider._pending = (
        "question",
        None,
        [],
        True,
        "",
        "trace-hermes-tool",
        0.0,
        "telegram__garden",
        None,
    )
    observed = []
    monkeypatch.setattr(
        "daemon_core.evidence_observability.observe_evidence_tool_result",
        lambda trace_id, **kwargs: observed.append((trace_id, kwargs)),
    )
    monkeypatch.setattr(
        held_atlas,
        "judge_unread_souvenirs_before_l0",
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
                "source_file": "garden_2026-07-29.md",
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

    result = __import__("json").loads(
        provider.handle_tool_call(
            "daemon_recall_l0",
            {
                "need": "le sujet discuté juste avant Magic",
                "temporal_context": "plus tôt dans cette conversation",
                "lookup_kind": "conversation_chronology",
            },
        )
    )

    assert result["evidence_status"] == "exact_evidence_found"
    assert result["relation_status"] == "established"
    assert result["conclusion"] == "Le sujet précédent était la chaleur."
    assert 'only' in result["response_constraint"]
    assert result["evidence_kind"] == "conversation_chronology"
    assert result["source_file"] == "garden_2026-07-29.md"
    assert "rester au frais" in result["evidence"]
    assert provider._turn_library_preflight_calls == 0
    assert observed[0][0] == "trace-hermes-tool"
    assert observed[0][1]["tool_name"] == "daemon_recall_l0"
    assert observed[0][1]["result"]["status"] == "found"


def test_l0_chronology_not_found_is_fail_closed_on_hermes(
    tmp_path,
    monkeypatch,
):
    provider, _ = _provider(tmp_path, monkeypatch)
    provider._harness.routing_provider = object()
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_USER_IDS", "garden")
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

    result = __import__("json").loads(
        provider.handle_tool_call(
            "daemon_recall_l0",
            {
                "need": "le sujet discuté avant ce pivot",
                "lookup_kind": "conversation_chronology",
            },
        )
    )

    assert result["status"] == "not_found"
    assert result["evidence"] == ""
    assert result["conclusion"] == ""
    assert result["relation_status"] == "not_established"
    assert result["response_constraint"].startswith(
        'No usable exact evidence'
    )
    assert "reason" not in result


def test_l0_chronology_invalid_evidence_is_fail_closed_on_hermes(
    tmp_path,
    monkeypatch,
):
    provider, _ = _provider(tmp_path, monkeypatch)
    provider._harness.routing_provider = object()
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_USER_IDS", "garden")
    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_chronology_recovery",
        lambda **kwargs: type(
            "Recovery",
            (),
            {
                "status": "invalid_evidence",
                "source_file": "garden_2026-07-29.md",
                "evidence": "",
                "reason": "Plage non continue.",
                "confidence": 0.0,
                "relation_status": "ambiguous",
                "conclusion": "",
            },
        )(),
    )

    result = __import__("json").loads(
        provider.handle_tool_call(
            "daemon_recall_l0",
            {
                "need": "le sujet discuté avant ce pivot",
                "lookup_kind": "conversation_chronology",
            },
        )
    )

    assert result["status"] == "invalid_evidence"
    assert result["evidence_status"] == "invalid_evidence"
    assert result["evidence"] == ""
    assert result["source_file"] == ""
    assert result["relation_status"] == "not_established"
    assert result["conclusion"] == ""
    assert "reason" not in result


def test_l0_chronology_provider_error_is_a_material_boundary_on_hermes(
    tmp_path,
    monkeypatch,
):
    provider, _ = _provider(tmp_path, monkeypatch)
    provider._harness.routing_provider = object()
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_USER_IDS", "garden")
    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_chronology_recovery",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
    )

    result = __import__("json").loads(
        provider.handle_tool_call(
            "daemon_recall_l0",
            {
                "need": "le sujet discuté avant ce pivot",
                "lookup_kind": "conversation_chronology",
            },
        )
    )

    assert result["status"] == "error"
    assert result["evidence_status"] == "error"
    assert result["evidence_kind"] == "conversation_chronology"
    assert result["error_code"] == "forensic_lookup_failed"
    assert result["evidence"] == ""
    assert result["conclusion"] == ""
    assert 'Do not assert' in result["response_constraint"]
    assert "reason" not in result


def test_l0_recall_tool_returns_structured_counterevidence_without_reason_leak(
    tmp_path,
    monkeypatch,
):
    provider, _ = _provider(tmp_path, monkeypatch)
    provider._harness.routing_provider = object()
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_USER_IDS", "garden")
    provider._turn_library_preflight_calls = 2
    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_forensic_recovery",
        lambda **kwargs: type(
            "Recovery",
            (),
            {
                "status": "found",
                "source_file": "garden_2026-07-19.md",
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
        )(),
    )
    monkeypatch.setattr(
        plugin,
        "_verify_l0_evidence_coverage",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("counterevidence needs no positive coverage pass")
        ),
    )

    result = __import__("json").loads(
        provider.handle_tool_call(
            "daemon_recall_l0",
            {
                "need": "lieu prévu pour regarder la finale",
                "temporal_context": "retour de Bordeaux en juillet 2026",
            },
        )
    )

    assert result["status"] == "found"
    assert result["relation_status"] == "not_established"
    assert result["evidence_status"] == "exact_evidence_found"
    assert result["response_constraint"].startswith('Do not confirm')
    assert "aéroport" in result["conclusion"]
    assert "reason" not in result


def test_l0_recall_tool_downgrades_incomplete_exact_quote_to_partial(
    tmp_path,
    monkeypatch,
):
    provider, _ = _provider(tmp_path, monkeypatch)
    provider._harness.routing_provider = object()
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_USER_IDS", "garden")
    monkeypatch.setattr(
        "daemon_core.memory.forensics.attempt_l0_forensic_recovery",
        lambda **kwargs: type(
            "Recovery",
            (),
            {
                "status": "found",
                "source_file": "garden_2026-07-01.md",
                "evidence": "Ah si nous rentrons",
                "reason": 'Retour confirmé, destination absente.',
                "confidence": 0.78,
                "relation_status": "established",
                "conclusion": "Un retour est prévu.",
            },
        )(),
    )
    monkeypatch.setattr(
        plugin,
        "_verify_l0_evidence_coverage",
        lambda **kwargs: (False, "La destination demandée n'est pas dans la citation."),
    )

    result = __import__("json").loads(
        provider.handle_tool_call(
            "daemon_recall_l0",
            {"need": "destination du retour de Alex et Sam"},
        )
    )

    assert result["status"] == "partial"
    assert result["evidence"] == "Ah si nous rentrons"
    assert result["evidence_status"] == "partial_exact_evidence"
    assert result["relation_status"] == "ambiguous"
    assert result["conclusion"] == ""
    assert "reason" not in result
    assert result["confidence"] == 0.0


def test_l0_recall_tool_is_denied_outside_canary_allowlist(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_TOOL", "on")
    monkeypatch.setenv("DAEMON_CORE_L0_RECALL_USER_IDS", "someone-else")

    result = __import__("json").loads(
        provider.handle_tool_call("daemon_recall_l0", {"need": "un fait"})
    )

    assert result["status"] == "unavailable"


def test_prefetch_persists_user_before_model_response(tmp_path, monkeypatch):
    provider, memory_root = _provider(tmp_path, monkeypatch)

    provider.prefetch("Ne tourne pas en rond.")

    text = _l0_text(memory_root)
    assert text.count("Ne tourne pas en rond.") == 1
    assert "— assistant" not in text


def test_prefetch_retry_is_idempotent_and_sync_only_adds_assistant(tmp_path, monkeypatch):
    provider, memory_root = _provider(tmp_path, monkeypatch)
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)

    provider.prefetch("Ne tourne pas en rond.")
    provider.prefetch("Ne tourne pas en rond.")
    provider.sync_turn("Ne tourne pas en rond.", "D'accord, je change de terrain.")

    text = _l0_text(memory_root)
    assert text.count("Ne tourne pas en rond.") == 1
    assert text.count("D'accord, je change de terrain.") == 1
    assert text.index("— user") < text.index("— assistant")


def test_sync_strips_degraded_transport_notice_before_l0(tmp_path, monkeypatch):
    from daemon_core.runtime_degradation import DEGRADED_RESPONSE_NOTICE

    provider, memory_root = _provider(tmp_path, monkeypatch)
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)

    provider.prefetch("Tu te souviens ?")
    provider.sync_turn(
        "Tu te souviens ?",
        f"{DEGRADED_RESPONSE_NOTICE}\n\nJe ne peux pas le vérifier.",
    )

    text = _l0_text(memory_root)
    assert DEGRADED_RESPONSE_NOTICE not in text
    assert text.count("Je ne peux pas le vérifier.") == 1


def test_durable_sync_enqueues_exact_l0_references(
    tmp_path,
    monkeypatch,
):
    from daemon_core.memory.writer import read_ground_zero_reference

    provider, memory_root = _provider(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_AFTER_TURN_SPOOL", "on")
    queued = []

    class Spool:
        def handoff(self, job):
            queued.append(job)
            return types.SimpleNamespace(
                created=True,
                state="pending",
            )

    monkeypatch.setattr(
        "daemon_core.after_turn_spool.AfterTurnSpool",
        lambda: Spool(),
    )

    provider.prefetch("Question durable")
    provider.sync_turn("Question durable", "Réponse durable")

    assert len(queued) == 1
    assert read_ground_zero_reference(queued[0].user_l0) == (
        "Question durable"
    )
    assert read_ground_zero_reference(queued[0].assistant_l0) == (
        "Réponse durable"
    )
    text = _l0_text(memory_root)
    assert text.count("Question durable") == 1
    assert text.count("Réponse durable") == 1


def test_durable_sync_carries_voice_hot_souvenirs_into_after_turn(
    tmp_path,
    monkeypatch,
):
    import daemon_core.turn_handoff as turn_handoff

    provider, _ = _provider(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_AFTER_TURN_SPOOL", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setattr(turn_handoff, "write_turn_handoff", lambda *_args: None)
    monkeypatch.setattr(
        turn_handoff,
        "read_turn_hot_souvenirs",
        lambda user_id, trace_id: ("theme/voice-hot",),
    )
    queued = []

    class Spool:
        def handoff(self, job):
            queued.append(job)
            return types.SimpleNamespace(created=True, state="pending")

    monkeypatch.setattr(
        "daemon_core.after_turn_spool.AfterTurnSpool",
        lambda: Spool(),
    )
    provider.prefetch("Question avec mémoire chaude")
    provider._turn_read_souvenirs = ["theme/explicit-read"]
    provider.sync_turn(
        "Question avec mémoire chaude",
        "Réponse située.",
    )

    assert len(queued) == 1
    assert queued[0].library_active is True
    assert queued[0].read_souvenirs == (
        "theme/explicit-read",
        "theme/voice-hot",
    )
    assert queued[0].tool_read_souvenirs == ("theme/explicit-read",)


def test_durable_handoff_failure_preserves_l0_without_volatile_acknowledgement(
    tmp_path,
    monkeypatch,
):
    provider, memory_root = _provider(tmp_path, monkeypatch)
    calls = []

    class Harness(HarnessStub):
        def update_memory_after_response(self, **kwargs):
            calls.append(kwargs)
            return None

    class Spool:
        def handoff(self, job):
            raise OSError("disk full")

    provider._harness = Harness()
    monkeypatch.setattr(
        "daemon_core.after_turn_spool.AfterTurnSpool",
        lambda: Spool(),
    )
    monkeypatch.setenv("DAEMON_CORE_AFTER_TURN_SPOOL", "on")
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)

    provider.prefetch("Question avec repli")
    provider.sync_turn("Question avec repli", "Réponse avec repli")

    text = _l0_text(memory_root)
    assert text.count("Question avec repli") == 1
    assert text.count("Réponse avec repli") == 1
    assert len(calls) == 0


def test_sync_without_prefetch_preserves_unknown_souvenir_selection(
    tmp_path,
    monkeypatch,
):
    provider, _ = _provider(tmp_path, monkeypatch)
    captured = {}

    class CapturingHarness(HarnessStub):
        def update_memory_after_response(self, **kwargs):
            captured.update(kwargs)
            return None

    provider._harness = CapturingHarness()
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)

    provider.sync_turn("Question sans prefetch", "Réponse")

    assert captured["route"] is None
    assert captured["selected_souvenirs"] is None


def test_same_message_on_new_hermes_turn_is_not_deduplicated(tmp_path, monkeypatch):
    provider, memory_root = _provider(tmp_path, monkeypatch)

    provider.on_turn_start(1, "Oui")
    provider.prefetch("Oui")
    # Simulate an interrupted turn: no sync_turn. Hermes still announces the
    # next real turn before prefetch, even when the text is identical.
    provider.on_turn_start(2, "Oui")
    provider.prefetch("Oui")

    assert _l0_text(memory_root).count("\n\nOui\n") == 2


def test_canonical_voice_skips_prefetch_radar_but_keeps_l0_and_handoff(
    tmp_path,
    monkeypatch,
):
    import daemon_core.turn_handoff as turn_handoff

    provider, memory_root = _provider(tmp_path, monkeypatch)
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    provider._assemble_context = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("radar must stay outside the critical path")
    )
    captured = {}
    monkeypatch.setattr(
        turn_handoff,
        "write_turn_handoff",
        lambda user_id, message, trace_id, started_at: captured.update(
            user_id=user_id,
            message=message,
            trace_id=trace_id,
            started_at=started_at,
        ),
    )

    block = provider.prefetch("Question rapide")

    assert block == ""
    assert provider._pending[1] is None
    assert provider._pending[2] == []
    assert captured["user_id"] == "garden"
    assert captured["message"] == "Question rapide"
    assert captured["trace_id"]
    assert _l0_text(memory_root).count("Question rapide") == 1


def test_held_atlas_sync_does_not_revise_suspended_held_thought(
    tmp_path,
    monkeypatch,
):
    import daemon_core.held_thought as held_thought

    provider, _ = _provider(tmp_path, monkeypatch)
    provider._harness.memory_provider = object()
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_HELD_ATLAS_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_HELD_ATLAS_USER_IDS", "garden")
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        held_thought,
        "run_held_thought_revision",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Held Thought is suspended from the Hermes runtime")
        ),
        raising=False,  # Keep the guard for the retired API.
    )

    provider.on_turn_start(1, "Parlons de Sam")
    provider.prefetch("Parlons de Sam")
    provider._turn_read_souvenirs = ["theme/famille/lectures_sam"]
    provider.sync_turn("Parlons de Sam", "Oui, partons de ses lectures.")

def test_scene_model_shadow_revises_after_l0_without_reaching_the_voice(
    tmp_path,
    monkeypatch,
):
    import daemon_core.scene_model as scene_model

    provider, memory_root = _provider(tmp_path, monkeypatch)
    provider._harness.memory_provider = object()
    monkeypatch.setenv("DAEMON_CORE_SCENE_MODEL", "shadow")
    monkeypatch.setenv("DAEMON_CORE_SCENE_MODEL_USER_IDS", "garden")
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)
    captured = {}

    def schedule(*args, **kwargs):
        captured.update(kwargs)
        captured["l0"] = _l0_text(memory_root)
        return True

    monkeypatch.setattr(scene_model, "schedule_scene_model_revision", schedule)

    provider.on_turn_start(1, "Une situation vient de changer")
    provider.prefetch("Une situation vient de changer")
    provider._turn_read_souvenirs = ["theme/contexte/exemple"]
    provider.sync_turn(
        "Une situation vient de changer",
        "Je comprends ce changement.",
    )

    assert captured["read_souvenirs"] == ["theme/contexte/exemple"]
    assert "Une situation vient de changer" in captured["l0"]
    assert "Je comprends ce changement." in captured["l0"]


def test_scene_model_shadow_waits_for_memory_curation(
    tmp_path,
    monkeypatch,
):
    import daemon_core.scene_model as scene_model

    provider, _ = _provider(tmp_path, monkeypatch)
    order = []

    class OrderedHarness(HarnessStub):
        memory_provider = object()

        def update_memory_after_response(self, **kwargs):
            order.append("memory")
            return None

    provider._harness = OrderedHarness()
    monkeypatch.setenv("DAEMON_CORE_SCENE_MODEL", "shadow")
    monkeypatch.setenv("DAEMON_CORE_SCENE_MODEL_USER_IDS", "garden")
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        scene_model,
        "schedule_scene_model_revision",
        lambda *args, **kwargs: order.append("scene") or True,
    )

    provider.on_turn_start(1, "Une correction")
    provider.prefetch("Une correction")
    provider.sync_turn("Une correction", "Je corrige la scène.")

    assert order == ["memory", "scene"]


def test_scene_model_shadow_runs_without_held_thought(
    tmp_path,
    monkeypatch,
):
    import daemon_core.held_thought as held_thought
    import daemon_core.scene_model as scene_model

    provider, _ = _provider(tmp_path, monkeypatch)
    provider._harness.memory_provider = object()
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS", "on")
    monkeypatch.setenv("DAEMON_CORE_SINGLE_PASS_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_SOVEREIGN_PLATFORMS", "telegram")
    monkeypatch.setenv("DAEMON_CORE_HELD_ATLAS_VOICE", "on")
    monkeypatch.setenv("DAEMON_CORE_HELD_ATLAS_USER_IDS", "garden")
    monkeypatch.setenv("DAEMON_CORE_SCENE_MODEL", "shadow")
    monkeypatch.setenv("DAEMON_CORE_SCENE_MODEL_USER_IDS", "garden")
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)
    order = []

    def schedule_scene(*args, **kwargs):
        order.append("scene")
        return True

    monkeypatch.setattr(
        held_thought,
        "run_held_thought_revision",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Held Thought is suspended from the Hermes runtime")
        ),
        raising=False,  # Keep the guard for the retired API.
    )
    monkeypatch.setattr(scene_model, "schedule_scene_model_revision", schedule_scene)

    provider.on_turn_start(1, "Une scène")
    provider.prefetch("Une scène")
    provider.sync_turn("Une scène", "Une réponse")

    assert order == ["scene"]


def _route(*candidates):
    return type("Route", (), {
        "mode": "contextual",
        "candidate_souvenirs": list(candidates),
    })()


def test_selection_reuse_rejects_candidate_subset_from_previous_scene(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "daemon_core.memory.reader.souvenir_domain_names",
        lambda: {"theme/famille", "theme/loisirs"},
    )
    provider._last_scene = (
        frozenset({"theme/famille", "theme/loisirs"}),
        ["theme/loisirs/lectures_sam"],
        time.time(),
    )

    assert provider._reused_selection(_route("theme/famille")) is None
    assert provider._last_scene is None


def test_selection_reuse_always_rezooms_domains(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "daemon_core.memory.reader.souvenir_domain_names",
        lambda: {"theme/famille"},
    )
    provider._last_scene = (
        frozenset({"theme/famille"}),
        ["theme/famille/famille_region_voisine"],
        time.time(),
    )

    assert provider._reused_selection(_route("theme/famille")) is None


def test_selection_reuse_accepts_only_identical_loaded_leaves(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "daemon_core.memory.reader.souvenir_domain_names", lambda: {"theme/famille"}
    )
    leaf = "theme/famille/famille_region_voisine"
    provider._last_scene = (frozenset({leaf}), [leaf], time.time())

    assert provider._reused_selection(_route(leaf)) == [leaf]


def test_selection_reuse_is_invalidated_by_light_turn(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path, monkeypatch)
    leaf = "theme/famille/famille_region_voisine"
    provider._last_scene = (frozenset({leaf}), [leaf], time.time())
    light_route = type("Route", (), {"mode": "fast", "candidate_souvenirs": []})()

    assert provider._reused_selection(light_route) is None
    assert provider._last_scene is None
