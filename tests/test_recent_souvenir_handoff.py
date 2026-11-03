"""Synthetic exact-source lifecycle; no provider, channel or personal memory."""

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from daemon_core import after_turn_spool as queue
from daemon_core import held_atlas_sovereign as library
from daemon_core.canonical_voice import compose_canonical_voice_messages
from daemon_core.memory import reader, writer
from daemon_core.voice_brief import build_dialogue_coverage
from test_canonical_voice import _freeze_inputs


@pytest.fixture
def case(monkeypatch, tmp_path):
    memory = tmp_path / "memory"
    for module in (writer, reader, queue, library):
        monkeypatch.setattr(module, "MEMORY_ROOT", memory)
    monkeypatch.setenv("DAEMON_CORE_AFTER_TURN_SPOOL", "on")
    monkeypatch.setenv("DAEMON_CORE_AFTER_TURN_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.delenv("DAEMON_CORE_CONTINUITY_RECEIPTS", raising=False)
    monkeypatch.delenv("DAEMON_CORE_SHARED_WORLD_STATE", raising=False)
    monkeypatch.delenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12", raising=False)
    now = datetime.fromisoformat("2026-09-26T09:00:00+02:00").timestamp()

    def job(
        turn="first",
        *,
        host="hermes",
        scope="telegram__u",
        when=now - 10,
        paths=("theme/habit",),
        user="u",
    ):
        return queue.AfterTurnJob(
            turn_id=turn,
            host=host,
            user_id=user,
            scene_scope=scope,
            source_turn_started_at=when,
            user_l0=writer.append_ground_zero(
                user,
                "user",
                "Open my source " + turn,
                turn_id=turn,
                occurred_at=when,
                return_reference=True,
            ),
            assistant_l0=writer.append_ground_zero(
                user,
                "assistant",
                "Source read " + turn,
                turn_id=turn,
                occurred_at=when + 1,
                return_reference=True,
            ),
            read_souvenirs=paths,
            tool_read_souvenirs=paths,
            library_active=True,
        )

    def ids():
        return build_dialogue_coverage(
            scan_dialogue="",
            scene_active=False,
            scene_delta="",
            recent_dialogue=reader.load_recent_dialogue("u", max_turns=24),
        ).recent_turn_ids

    def reads(*, scope="telegram__u", user="u", at=now, root=memory, eligible=None):
        return queue.AfterTurnSpool().read_recent_souvenir_sources(
            user,
            scope,
            memory_root=root,
            recent_turn_ids=ids() if eligible is None else eligible,
            now=at,
        )

    def souvenir(
        name="theme/habit", body="# Synthetic habit\n\nMira usually chooses mint tea."
    ):
        path = memory / "capsules" / (name + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    return SimpleNamespace(
        memory=memory,
        now=now,
        job=job,
        ids=ids,
        reads=reads,
        souvenir=souvenir,
        spool=queue.AfterTurnSpool(),
    )


@pytest.mark.parametrize(
    "state",
    ["pending", "running", "failed", "scene_pending", "scene_running", "scene_failed"],
)
def test_survives_each_live_queue_state_and_restart(case, state):
    job = case.job()
    if state.startswith("scene_"):
        projection = queue.SceneProjectionJob.from_turn(job)
        original = case.spool.enqueue_projection(projection).path
        destination = case.spool._projection_path(state, projection)
    else:
        original = case.spool.enqueue(job).path
        destination = case.spool._job_path(state, job)
    destination.parent.mkdir(parents=True, exist_ok=True)
    original.replace(destination)
    assert case.reads() == ((case.ids()[-1], ("theme/habit",)),)


def test_transfer_deduplicates_and_does_not_renew_injected_sources(case):
    original = case.job()
    pending = case.spool.enqueue(original).path
    projection = queue.SceneProjectionJob.from_turn(original)
    scene = case.spool.enqueue_projection(projection).path
    expected = case.reads()
    assert len(expected) == 1
    pending.unlink()
    assert case.reads() == expected
    injected = replace(case.job("injected", when=case.now - 3), tool_read_souvenirs=())
    case.spool.enqueue(injected)
    case.spool.enqueue_projection(queue.SceneProjectionJob.from_turn(injected))
    assert case.reads() == expected
    scene.unlink()
    assert case.reads() == ()


def test_old_jobs_keep_after_turn_inventory_without_retroactive_tool_provenance(case):
    value = case.job().as_dict()
    value.pop("tool_read_capsules")
    job = queue.AfterTurnJob.from_dict(value)
    assert job.read_souvenirs == ("theme/habit",)
    assert not job.tool_read_souvenirs
    case.spool.enqueue(job)
    assert case.reads() == ()


def test_projection_merge_preserves_each_exact_origin_without_renewal(case):
    first = queue.SceneProjectionJob.from_turn(case.job())
    second = queue.SceneProjectionJob.from_turn(
        case.job("second", when=case.now - 3, paths=("theme/other",))
    )
    merged = queue._merge_scene_projections(first, second)
    assert queue.SceneProjectionJob.from_dict(merged.as_dict()) == merged
    assert len(merged.souvenir_read_sources) == 2
    case.spool.enqueue_projection(merged)
    assert [paths for _, paths in case.reads()] == [("theme/other",), ("theme/habit",)]
    assert case.reads(eligible=(case.ids()[-1],)) == (
        (case.ids()[-1], ("theme/other",)),
    )


@pytest.mark.parametrize(
    "reason",
    [
        "scope",
        "user",
        "namespace",
        "history",
        "expired",
        "future",
        "corrupt_l0",
        "missing_l0",
        "corrupt_job",
        "missing_marker",
        "large_job",
        "too_many_jobs",
    ],
)
def test_ineligible_sources_are_read_only_noops(case, reason, tmp_path):
    job = case.job()
    path = case.spool.enqueue(job).path
    kwargs = {}
    if reason == "scope":
        kwargs["scope"] = "openclaw__u"
    elif reason == "user":
        kwargs["user"] = "different"
    elif reason == "namespace":
        kwargs["root"] = tmp_path / "different-memory"
    elif reason == "history":
        kwargs["eligible"] = ("turn-not-visible",)
    elif reason == "expired":
        kwargs["at"] = case.now + queue.MAX_RECENT_SOUVENIR_AGE_SECONDS
    elif reason == "future":
        kwargs["at"] = case.now - 20
    elif reason == "corrupt_l0":
        (case.memory / job.assistant_l0.path).write_text("changed")
    elif reason == "missing_l0":
        (case.memory / job.assistant_l0.path).unlink()
    elif reason == "corrupt_job":
        path.write_text("[]")
    elif reason == "missing_marker":
        (case.spool.root / "runtime.json").unlink()
    elif reason == "large_job":
        path.write_text(" " * (queue.MAX_RECENT_SOUVENIR_DOCUMENT_BYTES + 1))
    elif reason == "too_many_jobs":
        for index in range(queue.MAX_RECENT_SOUVENIR_DOCUMENTS):
            (path.parent / f"duplicate-{index}.json").write_bytes(path.read_bytes())
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert case.reads(**kwargs) == ()
    after = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_shared_scope_is_explicit_and_user_is_always_exact(case):
    case.spool.enqueue(case.job(scope="world"))
    case.spool.enqueue(
        case.job("other-host", host="openclaw", scope="world", paths=("theme/other",))
    )
    assert {p for _, paths in case.reads(scope="world") for p in paths} == {
        "theme/habit",
        "theme/other",
    }
    assert not case.reads(scope="telegram__u")


def _compose_setup(case, monkeypatch, *, covered=False, scene_paths=None):
    exact_reader = library.read_exact_active_souvenirs
    scene = (
        SimpleNamespace(
            revised_at=case.now - 1, hot_souvenirs=scene_paths or (), revision=1
        )
        if covered or scene_paths
        else None
    )
    _freeze_inputs(monkeypatch, scene=scene)
    monkeypatch.setattr(library, "read_exact_active_souvenirs", exact_reader)
    monkeypatch.setattr(library, "load_recent_dialogue", reader.load_recent_dialogue)
    monkeypatch.setattr(reader, "load_prior_dialogue", lambda *a, **kw: "")
    monkeypatch.setattr(
        library,
        "inspect_current_scene_model",
        lambda *a, **kw: library.CurrentSceneModelView(
            scene,
            "" if covered else reader.load_recent_dialogue("u", 24),
            "ready" if scene else "absent",
        ),
    )


@pytest.mark.parametrize(
    "platform,host,scope",
    [("telegram", "hermes", "telegram__u"), ("openclaw", "openclaw", "openclaw__u")],
)
def test_real_composer_rereads_changed_bodies_and_omits_deleted_ones(
    case, monkeypatch, platform, host, scope
):
    case.spool.enqueue(case.job(host=host, scope=scope))
    path = case.souvenir()
    _compose_setup(case, monkeypatch)

    def compose():
        return compose_canonical_voice_messages(
            "u", "Maybe a stop there.", platform=platform, message_received_at=case.now
        )

    messages, context = compose()
    assert context.hot_memory.souvenir_names == ("theme/habit",)
    assert "Mira usually chooses mint tea." in messages[0]["content"]
    path.write_text("# Synthetic habit\n\nMira now prefers rooibos.")
    messages, _ = compose()
    assert "Mira now prefers rooibos." in messages[0]["content"]
    assert "Mira usually chooses mint tea." not in messages[0]["content"]
    path.unlink()
    assert compose()[1].hot_memory.souvenir_names == ()


def test_scene_coverage_releases_pending_sources_and_restores_base_prompt(
    case, monkeypatch
):
    case.spool.enqueue(case.job())
    case.souvenir()
    _compose_setup(case, monkeypatch, covered=True)

    def compose():
        return compose_canonical_voice_messages(
            "u", "Unrelated topic.", message_received_at=case.now
        )

    with_queue, context = compose()
    assert context.hot_memory.souvenir_names == ()
    monkeypatch.setenv("DAEMON_CORE_AFTER_TURN_SPOOL", "off")
    assert compose()[0] == with_queue


def test_existing_scene_hot_souvenirs_keep_priority_and_total_count_bound(
    case, monkeypatch
):
    paths = tuple(f"theme/source-{i}" for i in range(10))
    for name in paths:
        case.souvenir(name)
    case.spool.enqueue(case.job(paths=paths))
    _compose_setup(case, monkeypatch, scene_paths=(paths[-1],))
    _, context = compose_canonical_voice_messages(
        "u", "Continue.", message_received_at=case.now
    )
    assert len(context.hot_memory.souvenirs) == 8
    assert context.hot_memory.souvenir_names == (paths[-1], *paths[:7])


def test_oversized_supplement_cannot_break_composition(case, monkeypatch):
    case.spool.enqueue(case.job())
    case.souvenir(body="x" * 50_001)
    _compose_setup(case, monkeypatch)
    messages, context = compose_canonical_voice_messages(
        "u", "Continue.", message_received_at=case.now
    )
    assert context.hot_memory.souvenir_names == ()
    assert "x" * 100 not in messages[0]["content"]


def test_queue_is_snapshotted_before_scene_consumes_it(case, monkeypatch):
    pending = case.spool.enqueue(case.job()).path
    case.souvenir()
    _compose_setup(case, monkeypatch)

    def inspect(*a, **kw):
        pending.unlink()
        return library.CurrentSceneModelView(None, "", "absent")

    monkeypatch.setattr(library, "inspect_current_scene_model", inspect)
    assert compose_canonical_voice_messages(
        "u", "Continue.", message_received_at=case.now
    )[1].hot_memory.souvenir_names == ("theme/habit",)
