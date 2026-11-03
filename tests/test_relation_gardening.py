from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from daemon_core.after_turn_executor import execute_after_turn_job
from daemon_core.after_turn_spool import AfterTurnJob
from daemon_core.dreaming import run_dreaming
from daemon_core.memory import relation_gardening, writer
from daemon_core.memory.relation_dreaming import RELATION_DREAMING_SCHEMA
from daemon_core.memory.relation_gardening import (
    pending_relation_gardening_event_ids,
    read_relation_gardening_event,
    record_relation_gardening_event,
    relation_gardening_decision_path,
    relation_gardening_enabled,
    relation_gardening_receipt_path,
    run_pending_relation_gardening_dry_run,
    run_relation_gardening_event_dry_run,
)
from daemon_core.memory.writer import GroundZeroReference, append_ground_zero
from daemon_core.providers.base import LLMProvider


USER_ID = "gardening-user"
BOOKS = "theme/loisirs/livres_a_recuperer"
SECOND = "theme/loisirs/coffrets_oasis"
BROAD = "theme/famille/demenagement_parents"
COLLECTION = "theme/loisirs/objets_sauves"


class GardeningProvider(LLMProvider):
    def __init__(
        self,
        repairs: set[tuple[str, str]] | None = None,
        *,
        failures: int = 0,
    ) -> None:
        self.repairs = repairs or set()
        self.failures = failures
        self.calls = 0

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("provider unavailable")
        items = json.loads(messages[-1]["content"].split("\n\n", 1)[1])
        decisions = []
        for item in items:
            pair = (item["source"], item["target"])
            repair = pair in self.repairs
            decisions.append(
                {
                    "item_id": item["item_id"],
                    "disposition": "repair" if repair else "reject",
                    "action": "upsert" if repair else "none",
                    "reason": (
                        "La parole utilisateur etablit cette appartenance."
                        if repair
                        else "Aucune appartenance n'est etablie."
                    ),
                }
            )
        return json.dumps(
            {
                "schema": RELATION_DREAMING_SCHEMA,
                "decisions": decisions,
            }
        )


class DreamingAndGardeningProvider(LLMProvider):
    def __init__(
        self,
        repairs: set[tuple[str, str]] | None = None,
        *,
        dreaming_updates: list[dict] | None = None,
        fail_gardening: bool = False,
    ) -> None:
        self.repairs = repairs or set()
        self.dreaming_updates = dreaming_updates or []
        self.fail_gardening = fail_gardening
        self.calls: list[str] = []

    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        if "DreamingAgent" in messages[0]["content"]:
            self.calls.append("dreaming")
            return json.dumps(
                {
                    "reason": "Entretien ordinaire.",
                    "confidence": 1.0,
                    "report_md": "Entretien principal termine.",
                    "unresolved_conflicts": [],
                    "updates": self.dreaming_updates,
                }
            )
        self.calls.append("relation_gardening")
        if self.fail_gardening:
            raise RuntimeError("relation judge unavailable")
        items = json.loads(messages[-1]["content"].split("\n\n", 1)[1])
        decisions = []
        for item in items:
            repair = (item["source"], item["target"]) in self.repairs
            decisions.append(
                {
                    "item_id": item["item_id"],
                    "disposition": "repair" if repair else "reject",
                    "action": "upsert" if repair else "none",
                    "reason": (
                        "La preuve directe etablit cette appartenance."
                        if repair
                        else "La preuve directe ne suffit pas."
                    ),
                }
            )
        return json.dumps(
            {
                "schema": RELATION_DREAMING_SCHEMA,
                "decisions": decisions,
            }
        )


def _write_souvenir(memory_root: Path, name: str, content: str) -> Path:
    path = memory_root / "capsules" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _vault(tmp_path: Path, *, closed: bool = False) -> Path:
    memory_root = tmp_path / "memory"
    _write_souvenir(
        memory_root,
        BOOKS,
        "# Livres a recuperer\n\nSujet: Cinq livres personnels a recuperer.\n\n"
        "## Faits\n- Les cinq livres font partie des objets a sauver.\n",
    )
    _write_souvenir(
        memory_root,
        SECOND,
        "# Coffrets Oasis\n\nSujet: Deux coffrets collector Oasis.\n\n"
        "## Faits\n- Les coffrets font partie des objets a sauver.\n",
    )
    _write_souvenir(
        memory_root,
        BROAD,
        "# Demenagement des parents\n\nSujet: Contexte general du demenagement.\n",
    )
    _write_souvenir(
        memory_root,
        COLLECTION,
        "---\ncollection:\n"
        f"  coverage: {'closed' if closed else 'open'}\n"
        "---\n# Objets sauves\n\nSujet: Ensemble des objets a sauver.\n\n"
        "## Faits\n- Les livres et coffrets appartiennent a cet ensemble.\n",
    )
    return memory_root


def _references(
    monkeypatch,
    memory_root: Path,
    *,
    turn_id: str = "turn-1",
    message: str = (
        "Mes cinq livres a recuperer font partie de l'ensemble des objets "
        "que je veux sauver avant le demenagement de mes parents."
    ),
) -> tuple[GroundZeroReference, GroundZeroReference]:
    monkeypatch.setattr(writer, "MEMORY_ROOT", memory_root)
    user_l0 = append_ground_zero(
        USER_ID,
        "user",
        message,
        turn_id=turn_id,
        return_reference=True,
    )
    assistant_l0 = append_ground_zero(
        USER_ID,
        "assistant",
        "Je comprends l'ensemble auquel ils appartiennent.",
        turn_id=turn_id,
        return_reference=True,
    )
    assert isinstance(user_l0, GroundZeroReference)
    assert isinstance(assistant_l0, GroundZeroReference)
    return user_l0, assistant_l0


def _capture(
    memory_root: Path,
    user_l0: GroundZeroReference,
    assistant_l0: GroundZeroReference,
    *,
    turn_id: str = "turn-1",
    source_posture: str = "direct_fact",
    changed: tuple[str, ...] = (),
    context: tuple[str, ...] = (BOOKS, COLLECTION),
):
    return record_relation_gardening_event(
        user_id=USER_ID,
        source_trace_id=turn_id,
        source_turn_started_at=1.0,
        user_l0=user_l0,
        assistant_l0=assistant_l0,
        source_posture=source_posture,
        changed_souvenirs=changed,
        context_souvenirs=context,
        memory_root=memory_root,
        require_enabled=False,
    )


def _semantic_bytes(memory_root: Path) -> dict[str, bytes]:
    roots = [
        memory_root / "capsules",
        memory_root / "levels" / "L0_ground_zero",
    ]
    return {
        path.relative_to(memory_root).as_posix(): path.read_bytes()
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_dry_run_requires_one_explicit_identity(monkeypatch):
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING", "dry-run")
    monkeypatch.delenv(
        "DAEMON_CORE_RELATION_GARDENING_USER_IDS",
        raising=False,
    )
    assert relation_gardening_enabled(USER_ID) is False

    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING_USER_IDS", USER_ID)
    assert relation_gardening_enabled(USER_ID) is True
    assert relation_gardening_enabled("another-user") is False

    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING", "active")
    assert relation_gardening_enabled(USER_ID) is True
    assert relation_gardening_enabled("another-user") is False

    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING_USER_IDS", "*")
    assert relation_gardening_enabled(USER_ID) is False


def test_capture_is_idempotent_and_copies_no_semantic_content(
    monkeypatch,
    tmp_path,
):
    memory_root = _vault(tmp_path)
    user_l0, assistant_l0 = _references(monkeypatch, memory_root)
    first = _capture(memory_root, user_l0, assistant_l0)
    replay = _capture(
        memory_root,
        user_l0,
        assistant_l0,
        source_posture="none",
        context=(),
    )

    assert first.status == "created"
    assert replay.status == "duplicate"
    event = read_relation_gardening_event(
        USER_ID,
        first.event_id,
        memory_root=memory_root,
    )
    rendered = json.dumps(event)
    assert "Mes cinq livres" not in rendered
    assert "Je comprends" not in rendered
    assert event["context_capsules"] == [BOOKS, COLLECTION]


@pytest.mark.parametrize("host", ["hermes", "openclaw"])
@pytest.mark.parametrize("mode", ["dry-run", "active"])
def test_shared_after_turn_executor_only_captures_for_later_dreaming(
    monkeypatch,
    tmp_path,
    host,
    mode,
):
    memory_root = _vault(tmp_path)
    turn_id = f"turn-{host}"
    user_l0, assistant_l0 = _references(
        monkeypatch,
        memory_root,
        turn_id=turn_id,
    )
    monkeypatch.setattr(relation_gardening, "MEMORY_ROOT", memory_root)
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING", mode)
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING_USER_IDS", USER_ID)
    monkeypatch.setenv("DAEMON_CORE_MEMORY_RELATION_PUBLICATION", "off")
    monkeypatch.setattr(
        "daemon_core.scene_model.scene_model_shadow_enabled",
        lambda _user_id: False,
    )
    provider = GardeningProvider({(BOOKS, COLLECTION)})

    class Harness:
        memory_provider = provider

        def update_memory_after_response(self, **_kwargs):
            return SimpleNamespace(
                source_posture="direct_fact",
                updated_souvenirs=[],
                relation_shadow_event_id="",
                changed_paths=[],
                unchanged_paths=[],
                rejected_paths=[],
                consulted_souvenirs=[BOOKS, COLLECTION],
            )

    job = AfterTurnJob(
        turn_id=turn_id,
        host=host,
        user_id=USER_ID,
        source_turn_started_at=1.0,
        scene_scope=f"telegram__{USER_ID}",
        user_l0=user_l0,
        assistant_l0=assistant_l0,
        selected_souvenirs=(BOOKS,),
        read_souvenirs=(BOOKS, COLLECTION),
        library_active=False,
        origin={"platform": "telegram"},
    )

    execute_after_turn_job(job, harness=Harness())
    execute_after_turn_job(job, harness=Harness())

    event_root = (
        memory_root
        / "levels"
        / "relation_gardening"
        / USER_ID
        / "events"
    )
    events = list(event_root.glob("*.json"))
    assert len(events) == 1
    assert json.loads(events[0].read_text(encoding="utf-8"))[
        "changed_capsules"
    ] == []
    assert provider.calls == 0


def test_dreaming_runs_pending_relation_gardening_in_dry_run(
    monkeypatch,
    tmp_path,
):
    memory_root = _vault(tmp_path)
    user_l0, assistant_l0 = _references(monkeypatch, memory_root)
    capture = _capture(memory_root, user_l0, assistant_l0)
    before = _semantic_bytes(memory_root)
    monkeypatch.setattr(relation_gardening, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING", "dry-run")
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING_USER_IDS", USER_ID)
    provider = DreamingAndGardeningProvider({(BOOKS, COLLECTION)})

    result = run_dreaming(USER_ID, provider=provider, dry_run=True)

    assert provider.calls == ["dreaming", "relation_gardening"]
    assert len(result.relation_gardening_results) == 1
    assert result.relation_gardening_results[0].event_id == capture.event_id
    assert result.relation_gardening_results[0].status == "planned"
    assert "Jardinage relationnel (dry-run)" in result.report_md
    assert _semantic_bytes(memory_root) == before


def test_dreaming_does_not_call_relation_gardening_when_disabled(
    monkeypatch,
    tmp_path,
):
    memory_root = _vault(tmp_path)
    user_l0, assistant_l0 = _references(monkeypatch, memory_root)
    _capture(memory_root, user_l0, assistant_l0)
    monkeypatch.setattr(relation_gardening, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    monkeypatch.delenv("DAEMON_CORE_RELATION_GARDENING", raising=False)
    monkeypatch.delenv(
        "DAEMON_CORE_RELATION_GARDENING_USER_IDS",
        raising=False,
    )
    provider = DreamingAndGardeningProvider({(BOOKS, COLLECTION)})

    result = run_dreaming(USER_ID, provider=provider, dry_run=True)

    assert provider.calls == ["dreaming"]
    assert result.relation_gardening_results == []
    assert pending_relation_gardening_event_ids(
        USER_ID,
        memory_root=memory_root,
    )


def test_relation_gardening_failure_prevents_normal_dreaming_write(
    monkeypatch,
    tmp_path,
):
    memory_root = _vault(tmp_path)
    user_root = memory_root / "users" / USER_ID
    user_root.mkdir(parents=True)
    context_path = user_root / "context.md"
    context_path.write_text("# Context\n\n- Existing fact.\n", encoding="utf-8")
    user_l0, assistant_l0 = _references(monkeypatch, memory_root)
    capture = _capture(memory_root, user_l0, assistant_l0)
    monkeypatch.setattr(relation_gardening, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr("daemon_core.dreaming.MEMORY_ROOT", memory_root)
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING", "dry-run")
    monkeypatch.setenv("DAEMON_CORE_RELATION_GARDENING_USER_IDS", USER_ID)
    provider = DreamingAndGardeningProvider(
        dreaming_updates=[
            {
                "path": f"memory/users/{USER_ID}/context.md",
                "action": "update",
                "reason": "Proposed normal Dreaming repair.",
                "content_md": "# Context\n\n- Existing fact.\n- New fact.\n",
            }
        ],
        fail_gardening=True,
    )

    with pytest.raises(RuntimeError, match="relation judge unavailable"):
        run_dreaming(USER_ID, provider=provider, dry_run=False)

    assert provider.calls == ["dreaming", "relation_gardening"]
    assert context_path.read_text(encoding="utf-8") == (
        "# Context\n\n- Existing fact.\n"
    )
    assert pending_relation_gardening_event_ids(
        USER_ID,
        memory_root=memory_root,
    ) == (capture.event_id,)
    assert not relation_gardening_receipt_path(
        USER_ID,
        capture.event_id,
        memory_root=memory_root,
    ).exists()


def test_simple_missing_membership_is_planned_without_semantic_write(
    monkeypatch,
    tmp_path,
):
    memory_root = _vault(tmp_path)
    user_l0, assistant_l0 = _references(monkeypatch, memory_root)
    before = _semantic_bytes(memory_root)
    capture = _capture(memory_root, user_l0, assistant_l0)
    provider = GardeningProvider({(BOOKS, COLLECTION)})

    first = run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=capture.event_id,
        provider=provider,
        memory_root=memory_root,
    )
    second = run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=capture.event_id,
        provider=provider,
        memory_root=memory_root,
    )

    assert first.status == "planned"
    assert first.candidate_count == 1
    assert first.repair_count == 1
    assert first.planned_count == 1
    assert first.provider_calls == 1
    assert second.replayed is True
    assert second.provider_calls == 0
    assert provider.calls == 1
    assert _semantic_bytes(memory_root) == before
    assert relation_gardening_decision_path(
        USER_ID,
        capture.event_id,
        memory_root=memory_root,
    ).is_file()
    assert relation_gardening_receipt_path(
        USER_ID,
        capture.event_id,
        memory_root=memory_root,
    ).is_file()


def test_two_members_are_judged_and_planned_in_one_complete_batch(
    monkeypatch,
    tmp_path,
):
    memory_root = _vault(tmp_path)
    user_l0, assistant_l0 = _references(
        monkeypatch,
        memory_root,
        message=(
            "Mes cinq livres et mes deux coffrets Oasis font tous partie "
            "des objets que je veux sauver."
        ),
    )
    before = _semantic_bytes(memory_root)
    capture = _capture(
        memory_root,
        user_l0,
        assistant_l0,
        context=(BOOKS, SECOND, COLLECTION),
    )
    provider = GardeningProvider(
        {(BOOKS, COLLECTION), (SECOND, COLLECTION)}
    )

    result = run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=capture.event_id,
        provider=provider,
        memory_root=memory_root,
    )

    assert result.status == "planned"
    assert result.candidate_count == 2
    assert result.decision_count == 2
    assert result.repair_count == 2
    assert result.planned_count == 2
    assert provider.calls == 1
    assert _semantic_bytes(memory_root) == before


@pytest.mark.parametrize(
    ("posture", "reason"),
    [
        ("reported_memory_claim", "source_not_direct_fact"),
        ("none", "source_not_direct_fact"),
    ],
)
def test_non_direct_source_is_terminal_without_model(
    monkeypatch,
    tmp_path,
    posture,
    reason,
):
    memory_root = _vault(tmp_path)
    user_l0, assistant_l0 = _references(monkeypatch, memory_root)
    capture = _capture(
        memory_root,
        user_l0,
        assistant_l0,
        source_posture=posture,
    )
    provider = GardeningProvider({(BOOKS, COLLECTION)})

    result = run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=capture.event_id,
        provider=provider,
        memory_root=memory_root,
    )

    assert result.status == "skipped"
    assert result.reason_code == reason
    assert result.provider_calls == 0
    assert provider.calls == 0


def test_closed_collection_calls_no_model(monkeypatch, tmp_path):
    memory_root = _vault(tmp_path, closed=True)
    user_l0, assistant_l0 = _references(monkeypatch, memory_root)
    capture = _capture(memory_root, user_l0, assistant_l0)
    provider = GardeningProvider({(BOOKS, COLLECTION)})

    result = run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=capture.event_id,
        provider=provider,
        memory_root=memory_root,
    )

    assert result.status == "skipped"
    assert result.reason_code == "no_open_collection_in_context"
    assert provider.calls == 0


def test_candidate_overflow_blocks_whole_event_without_model(
    monkeypatch,
    tmp_path,
):
    memory_root = _vault(tmp_path)
    extra = []
    for index in range(8):
        name = f"theme/test/source_{index}"
        _write_souvenir(memory_root, name, f"# Source {index}\n")
        extra.append(name)
    user_l0, assistant_l0 = _references(monkeypatch, memory_root)
    capture = _capture(
        memory_root,
        user_l0,
        assistant_l0,
        context=(BOOKS, *extra, COLLECTION),
    )
    provider = GardeningProvider()

    result = run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=capture.event_id,
        provider=provider,
        memory_root=memory_root,
    )

    assert result.status == "blocked"
    assert result.reason_code == "relation_candidate_overflow"
    assert provider.calls == 0


def test_altered_l0_is_blocked_without_model(monkeypatch, tmp_path):
    memory_root = _vault(tmp_path)
    user_l0, assistant_l0 = _references(monkeypatch, memory_root)
    capture = _capture(memory_root, user_l0, assistant_l0)
    (memory_root / user_l0.path).write_text("altered\n", encoding="utf-8")
    provider = GardeningProvider({(BOOKS, COLLECTION)})

    result = run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=capture.event_id,
        provider=provider,
        memory_root=memory_root,
    )

    assert result.status == "blocked"
    assert result.reason_code == "l0_provenance_invalid"
    assert provider.calls == 0


def test_provider_failure_leaves_event_pending(monkeypatch, tmp_path):
    memory_root = _vault(tmp_path)
    user_l0, assistant_l0 = _references(monkeypatch, memory_root)
    capture = _capture(memory_root, user_l0, assistant_l0)
    provider = GardeningProvider({(BOOKS, COLLECTION)}, failures=1)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        run_relation_gardening_event_dry_run(
            user_id=USER_ID,
            event_id=capture.event_id,
            provider=provider,
            memory_root=memory_root,
        )

    assert pending_relation_gardening_event_ids(
        USER_ID,
        memory_root=memory_root,
    ) == (capture.event_id,)
    recovered = run_pending_relation_gardening_dry_run(
        user_id=USER_ID,
        provider=provider,
        memory_root=memory_root,
        require_enabled=False,
    )
    assert recovered[0].status == "planned"
    assert provider.calls == 2


def test_failure_after_decision_reuses_it_without_second_model_call(
    monkeypatch,
    tmp_path,
):
    memory_root = _vault(tmp_path)
    user_l0, assistant_l0 = _references(monkeypatch, memory_root)
    capture = _capture(memory_root, user_l0, assistant_l0)
    provider = GardeningProvider({(BOOKS, COLLECTION)})
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary publication failure")
        return original(*args, **kwargs)

    original = relation_gardening.apply_relation_mutation_batch
    monkeypatch.setattr(
        relation_gardening,
        "apply_relation_mutation_batch",
        fail_once,
    )

    with pytest.raises(RuntimeError, match="temporary publication failure"):
        run_relation_gardening_event_dry_run(
            user_id=USER_ID,
            event_id=capture.event_id,
            provider=provider,
            memory_root=memory_root,
        )
    assert relation_gardening_decision_path(
        USER_ID,
        capture.event_id,
        memory_root=memory_root,
    ).is_file()
    assert not relation_gardening_receipt_path(
        USER_ID,
        capture.event_id,
        memory_root=memory_root,
    ).exists()

    replay = run_relation_gardening_event_dry_run(
        user_id=USER_ID,
        event_id=capture.event_id,
        provider=provider,
        memory_root=memory_root,
    )

    assert replay.status == "planned"
    assert replay.provider_calls == 0
    assert provider.calls == 1
