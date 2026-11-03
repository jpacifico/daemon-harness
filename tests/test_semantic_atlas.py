import hashlib
import io
import json
import sqlite3
import math
import threading
from pathlib import Path

import pytest

from daemon_core.held_atlas_sovereign import build_exact_memory_atlas
from daemon_core.semantic_atlas import (
    CoalescedSemanticAtlasPrewarmer,
    MODEL_KEEP_ALIVE,
    RUNTIME_EMBEDDING_TIMEOUT_SECONDS,
    TOP12_CATALOG_NOTICE,
    OllamaEmbeddingProvider,
    RankedPath,
    SemanticAtlasUnavailable,
    _select_top12,
    prewarm_semantic_atlas_cache,
    select_semantic_atlas_paths,
    semantic_atlas_cache_refresh_enabled,
    semantic_atlas_canary_enabled,
)


def test_ollama_embedding_request_keeps_model_warm(monkeypatch) -> None:
    provider = OllamaEmbeddingProvider(model="test-model", model_digest="digest")
    assert provider.timeout_s == RUNTIME_EMBEDDING_TIMEOUT_SECONDS == 10.0
    captured = {}

    def fake_request(endpoint, payload=None):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {"embeddings": [[1.0, 0.0]]}

    monkeypatch.setattr(provider, "_request", fake_request)

    assert provider.embed(["texte"]) == [[1.0, 0.0]]
    assert captured == {
        "endpoint": "/api/embed",
        "payload": {
            "model": "test-model",
            "input": ["texte"],
            "truncate": False,
            "keep_alive": MODEL_KEEP_ALIVE,
        },
    }


class FakeEmbeddingProvider:
    model = "fake"
    model_digest = "fake-digest"

    def __init__(self) -> None:
        self.document_calls = 0
        self.query_calls = 0

    def verify_model(self) -> None:
        return None

    def embed(self, texts, *, query=False):
        if query:
            self.query_calls += 1
        else:
            self.document_calls += 1
        return [_vector(text) for text in texts]


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    values = [float(item + 1) for item in digest[:8]]
    norm = math.sqrt(sum(item * item for item in values))
    return [item / norm for item in values]


def _memory(root: Path, count: int = 15) -> None:
    for index in range(count):
        path = root / "capsules" / "theme" / f"capsule_{index:02d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nsubjects:\n  - Personne\nfacets:\n  - test\n---\n\n"
            f"# Capsule {index}\n\nFait distinct {index}.",
            encoding="utf-8",
        )


def _ranking(prefix: str) -> tuple[RankedPath, ...]:
    return tuple(
        RankedPath(path=f"{prefix}/{index}", score=100.0 - index)
        for index in range(20)
    )


def test_canary_is_personal_and_available_to_both_canonical_hosts(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12", "canary")
    monkeypatch.setenv("DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS", "garden")

    assert semantic_atlas_canary_enabled("garden", "telegram") is True
    assert semantic_atlas_canary_enabled("garden", "openclaw") is True
    assert semantic_atlas_canary_enabled("other", "telegram") is False
    assert semantic_atlas_canary_enabled("garden", "other") is False
    assert semantic_atlas_canary_enabled("garden", None) is False
    assert semantic_atlas_cache_refresh_enabled("garden") is True


def test_selector_preserves_three_leaders_from_each_view() -> None:
    channels = {
        "need_body": _ranking("need-body"),
        "need_routing": _ranking("need-routing"),
        "dialogue_body": _ranking("dialogue-body"),
        "dialogue_routing": _ranking("dialogue-routing"),
    }

    selected = _select_top12(channels)

    assert len(selected) == 12
    assert set(selected) == {
        f"{prefix}/{index}"
        for prefix in (
            "need-body",
            "need-routing",
            "dialogue-body",
            "dialogue-routing",
        )
        for index in range(3)
    }


def test_cold_runtime_cache_falls_back_before_embedding_documents(tmp_path) -> None:
    _memory(tmp_path)
    provider = FakeEmbeddingProvider()

    with pytest.raises(SemanticAtlasUnavailable, match="embedding_cache_cold"):
        select_semantic_atlas_paths(
            "question",
            "dialogue",
            tmp_path,
            provider=provider,
            cache_path=tmp_path / "cache.sqlite3",
        )

    assert provider.document_calls == 0
    assert provider.query_calls == 0


def test_prewarmed_cache_serves_runtime_and_repairs_one_change(tmp_path) -> None:
    _memory(tmp_path)
    cache = tmp_path / "cache.sqlite3"
    provider = FakeEmbeddingProvider()
    prewarm = prewarm_semantic_atlas_cache(
        tmp_path,
        provider=provider,
        cache_path=cache,
    )

    assert prewarm["capsules"] == 15
    assert prewarm["embedded"] == 30
    assert cache.stat().st_mode & 0o777 == 0o600

    runtime = FakeEmbeddingProvider()
    first = select_semantic_atlas_paths(
        "question",
        "dialogue",
        tmp_path,
        provider=runtime,
        cache_path=cache,
    )
    assert len(first.paths) == 12
    assert first.cache_misses == 0
    assert runtime.document_calls == 0
    assert runtime.query_calls == 1

    changed = tmp_path / "capsules" / "theme" / "capsule_00.md"
    changed.write_text(changed.read_text(encoding="utf-8") + "\nRévision.", encoding="utf-8")
    second = select_semantic_atlas_paths(
        "question",
        "dialogue",
        tmp_path,
        provider=runtime,
        cache_path=cache,
    )
    assert second.cache_misses == 1
    assert runtime.document_calls == 1


def test_semantic_shortlist_can_retrieve_a_past_chronicle(tmp_path) -> None:
    _memory(tmp_path)
    chronicle = (
        tmp_path
        / "capsules"
        / "theme"
        / "chroniques"
        / "2026-08-04.md"
    )
    chronicle.parent.mkdir(parents=True, exist_ok=True)
    chronicle.write_text(
        "---\n"
        "memory_type: episodic_archive\n"
        "truth_scope: past_only\n"
        "subjects:\n  - Travail\n"
        "facets:\n  - historique\n"
        "---\n\n"
        "# Journée du 4 août\n\nLa proposition Helexia a été préparée.",
        encoding="utf-8",
    )

    class KeywordProvider(FakeEmbeddingProvider):
        def embed(self, texts, *, query=False):
            if query:
                self.query_calls += 1
            else:
                self.document_calls += 1
            return [
                [1.0, 0.0]
                if "helexia" in str(text).casefold()
                else [0.0, 1.0]
                for text in texts
            ]

    cache = tmp_path / "cache.sqlite3"
    prewarm_semantic_atlas_cache(
        tmp_path,
        provider=KeywordProvider(),
        cache_path=cache,
    )
    selection = select_semantic_atlas_paths(
        "Helexia, la suite du projet",
        "J'ai dû t'en parler.",
        tmp_path,
        provider=KeywordProvider(),
        cache_path=cache,
    )

    assert len(selection.paths) == 12
    assert set(dict(selection.channel_heads)) == {
        "need_body",
        "need_routing",
        "dialogue_body",
        "dialogue_routing",
    }
    assert "theme/chroniques/2026-08-04" in selection.paths


def test_prewarm_loads_model_even_when_vector_cache_is_complete(tmp_path) -> None:
    _memory(tmp_path, count=4)
    cache = tmp_path / "cache.sqlite3"
    prewarm_semantic_atlas_cache(
        tmp_path,
        provider=FakeEmbeddingProvider(),
        cache_path=cache,
    )
    provider = FakeEmbeddingProvider()

    result = prewarm_semantic_atlas_cache(
        tmp_path,
        provider=provider,
        cache_path=cache,
    )

    assert result["embedded"] == 0
    assert result["model_warmed"] is True
    assert provider.document_calls == 0
    assert provider.query_calls == 1


def test_runtime_query_does_not_duplicate_a_timed_out_request(tmp_path) -> None:
    _memory(tmp_path, count=15)
    cache = tmp_path / "cache.sqlite3"
    prewarm_semantic_atlas_cache(
        tmp_path,
        provider=FakeEmbeddingProvider(),
        cache_path=cache,
    )

    class SlowProvider(FakeEmbeddingProvider):
        def embed(self, texts, *, query=False):
            if query:
                self.query_calls += 1
                raise SemanticAtlasUnavailable(
                    "embedding_service_unavailable"
                )
            return super().embed(texts, query=query)

    provider = SlowProvider()
    with pytest.raises(
        SemanticAtlasUnavailable,
        match="embedding_service_unavailable",
    ):
        select_semantic_atlas_paths(
            "question",
            "dialogue",
            tmp_path,
            provider=provider,
            cache_path=cache,
        )

    assert provider.query_calls == 1


def test_runtime_query_does_not_retry_a_non_transient_failure(tmp_path) -> None:
    _memory(tmp_path, count=15)
    cache = tmp_path / "cache.sqlite3"
    prewarm_semantic_atlas_cache(
        tmp_path,
        provider=FakeEmbeddingProvider(),
        cache_path=cache,
    )

    class BrokenProvider(FakeEmbeddingProvider):
        def embed(self, texts, *, query=False):
            if query:
                self.query_calls += 1
                raise SemanticAtlasUnavailable("embedding_response_invalid")
            return super().embed(texts, query=query)

    provider = BrokenProvider()
    with pytest.raises(
        SemanticAtlasUnavailable,
        match="embedding_response_invalid",
    ):
        select_semantic_atlas_paths(
            "question",
            "dialogue",
            tmp_path,
            provider=provider,
            cache_path=cache,
        )

    assert provider.query_calls == 1


def test_after_turn_prewarmer_coalesces_souvenirs_and_fills_only_misses(
    tmp_path,
) -> None:
    _memory(tmp_path, count=5)
    cache = tmp_path / "cache.sqlite3"
    prewarm_semantic_atlas_cache(
        tmp_path,
        provider=FakeEmbeddingProvider(),
        cache_path=cache,
    )
    for index in (0, 1):
        path = tmp_path / "capsules" / "theme" / f"capsule_{index:02d}.md"
        path.write_text(
            path.read_text(encoding="utf-8") + f"\nRévision {index}.",
            encoding="utf-8",
        )

    class MemoryResult:
        def __init__(self, names):
            self.updated_souvenirs = names

    class Execution:
        def __init__(self, names):
            self.memory_result = MemoryResult(names)

    provider = FakeEmbeddingProvider()
    prewarmer = CoalescedSemanticAtlasPrewarmer(
        tmp_path,
        provider=provider,
        cache_path=cache,
        start_pending=False,
    )

    result = prewarmer.process_batch(
        (
            Execution(("theme/capsule_00",)),
            Execution(("theme/capsule_01", "theme/capsule_00")),
        )
    )
    idle = prewarmer.process_batch(())

    assert result.status == "completed"
    assert result.embedded == 2
    assert result.requested_souvenirs == (
        "theme/capsule_00",
        "theme/capsule_01",
    )
    assert provider.document_calls == 1
    assert idle.status == "idle"
    assert provider.document_calls == 1


def test_after_turn_prewarmer_converges_on_startup_without_a_new_mutation(
    tmp_path,
) -> None:
    _memory(tmp_path, count=4)
    cache = tmp_path / "cache.sqlite3"
    prewarm_semantic_atlas_cache(
        tmp_path,
        provider=FakeEmbeddingProvider(),
        cache_path=cache,
    )
    new_souvenir = tmp_path / "capsules" / "theme" / "capsule_04.md"
    new_souvenir.write_text(
        "---\nsubjects:\n  - Personne\nfacets:\n  - test\n---\n\n"
        "# Capsule 4\n\nAjout avant redémarrage.",
        encoding="utf-8",
    )
    provider = FakeEmbeddingProvider()
    prewarmer = CoalescedSemanticAtlasPrewarmer(
        tmp_path,
        provider=provider,
        cache_path=cache,
    )

    result = prewarmer.process_batch(())

    assert result.status == "completed"
    assert result.embedded == 2
    assert result.requested_souvenirs == ()
    assert provider.document_calls == 2


def test_coalesced_prewarm_repairs_a_bulk_change_before_the_next_voice_turn(
    tmp_path,
) -> None:
    _memory(tmp_path, count=8)
    cache = tmp_path / "cache.sqlite3"
    prewarm_semantic_atlas_cache(
        tmp_path,
        provider=FakeEmbeddingProvider(),
        cache_path=cache,
    )
    changed_names = []
    for index in range(5):
        name = f"theme/capsule_{index:02d}"
        changed_names.append(name)
        path = tmp_path / "capsules" / f"{name}.md"
        path.write_text(
            path.read_text(encoding="utf-8") + f"\nRévision {index}.",
            encoding="utf-8",
        )

    with pytest.raises(SemanticAtlasUnavailable, match="embedding_cache_cold"):
        select_semantic_atlas_paths(
            "question",
            "dialogue",
            tmp_path,
            provider=FakeEmbeddingProvider(),
            cache_path=cache,
        )

    result = type("Result", (), {"updated_souvenirs": tuple(changed_names)})()
    prewarmer = CoalescedSemanticAtlasPrewarmer(
        tmp_path,
        provider=FakeEmbeddingProvider(),
        cache_path=cache,
        start_pending=False,
    )
    warmed = prewarmer.process_batch((result,))
    selection = select_semantic_atlas_paths(
        "question",
        "dialogue",
        tmp_path,
        provider=FakeEmbeddingProvider(),
        cache_path=cache,
    )

    assert warmed.status == "completed"
    assert warmed.embedded == 5
    assert selection.cache_misses == 0


def test_after_turn_prewarmer_retries_without_losing_the_request(
    tmp_path,
) -> None:
    _memory(tmp_path, count=4)
    cache = tmp_path / "cache.sqlite3"
    prewarm_semantic_atlas_cache(
        tmp_path,
        provider=FakeEmbeddingProvider(),
        cache_path=cache,
    )
    changed = tmp_path / "capsules" / "theme" / "capsule_00.md"
    changed.write_text(
        changed.read_text(encoding="utf-8") + "\nRévision.",
        encoding="utf-8",
    )

    class FlakyProvider(FakeEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self.failures = 1

        def embed(self, texts, *, query=False):
            if not query and self.failures:
                self.failures -= 1
                raise RuntimeError("temporary")
            return super().embed(texts, query=query)

    now = [10.0]
    provider = FlakyProvider()
    prewarmer = CoalescedSemanticAtlasPrewarmer(
        tmp_path,
        provider=provider,
        cache_path=cache,
        retry_seconds=5.0,
        clock=lambda: now[0],
        start_pending=False,
    )

    first = prewarmer.process_batch((type("Result", (), {
        "updated_souvenirs": ("theme/capsule_00",),
    })(),))
    blocked = prewarmer.process_batch(())
    now[0] = 15.0
    recovered = prewarmer.process_batch(())

    assert first.status == "failed"
    assert blocked.status == "backoff"
    assert recovered.status == "completed"
    assert recovered.embedded == 1
    assert recovered.requested_souvenirs == ("theme/capsule_00",)


def test_background_embedding_does_not_hold_the_sqlite_cache_lock(
    tmp_path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_cache = tmp_path / "first.sqlite3"
    second_cache = tmp_path / "second.sqlite3"
    _memory(first_root, count=4)
    _memory(second_root, count=4)
    prewarm_semantic_atlas_cache(
        first_root,
        provider=FakeEmbeddingProvider(),
        cache_path=first_cache,
    )
    prewarm_semantic_atlas_cache(
        second_root,
        provider=FakeEmbeddingProvider(),
        cache_path=second_cache,
    )
    changed = first_root / "capsules" / "theme" / "capsule_00.md"
    changed.write_text(
        changed.read_text(encoding="utf-8") + "\nRévision.",
        encoding="utf-8",
    )
    embedding_started = threading.Event()
    release_embedding = threading.Event()
    second_finished = threading.Event()

    class BlockingProvider(FakeEmbeddingProvider):
        def embed(self, texts, *, query=False):
            if not query:
                embedding_started.set()
                assert release_embedding.wait(timeout=2.0)
            return super().embed(texts, query=query)

    def warm_second_cache() -> None:
        prewarm_semantic_atlas_cache(
            second_root,
            provider=FakeEmbeddingProvider(),
            cache_path=second_cache,
        )
        second_finished.set()

    first_thread = threading.Thread(
        target=lambda: prewarm_semantic_atlas_cache(
            first_root,
            provider=BlockingProvider(),
            cache_path=first_cache,
        )
    )
    second_thread = threading.Thread(target=warm_second_cache)
    first_thread.start()
    assert embedding_started.wait(timeout=1.0)
    second_thread.start()
    completed_while_embedding = second_finished.wait(timeout=1.0)
    release_embedding.set()
    first_thread.join(timeout=2.0)
    second_thread.join(timeout=2.0)

    assert completed_while_embedding is True
    assert first_thread.is_alive() is False
    assert second_thread.is_alive() is False


def test_filtered_exact_atlas_keeps_only_selected_paths_and_notice(tmp_path) -> None:
    _memory(tmp_path, count=4)

    atlas = build_exact_memory_atlas(
        tmp_path,
        selected_souvenir_names=("theme/capsule_02", "theme/capsule_00"),
        catalog_notice=TOP12_CATALOG_NOTICE,
    )

    assert atlas.souvenir_names == ("theme/capsule_02", "theme/capsule_00")
    assert atlas.text.startswith(TOP12_CATALOG_NOTICE)
    assert "theme/capsule_02" in atlas.text
    assert "theme/capsule_01" not in atlas.text



def test_background_initialization_has_its_own_request_deadline(tmp_path, monkeypatch):
    import daemon_core.semantic_atlas as atlas

    _memory(tmp_path, count=5)
    requests = []

    def slow_local_embedding(request, timeout):
        requests.append((request.full_url, timeout))
        if request.full_url.endswith("/api/tags"):
            payload = {"models": [{"name": atlas.DEFAULT_MODEL,
                                   "digest": atlas.DEFAULT_MODEL_DIGEST}]}
        else:
            # The observed cold batch takes longer than the former five seconds.
            if timeout < 6.2:
                raise TimeoutError("cold batch exceeds request deadline")
            inputs = json.loads(request.data)["input"]
            payload = {"embeddings": [_vector(text) for text in inputs]}
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(atlas.urllib.request, "urlopen", slow_local_embedding)
    cache = tmp_path / "cache.sqlite3"
    result = CoalescedSemanticAtlasPrewarmer(tmp_path, cache_path=cache).process_batch(())

    assert result.status == "completed"
    assert result.embedded == 10
    assert all(6.2 < timeout <= 30.0 for _, timeout in requests)
    requests.clear()
    selection = select_semantic_atlas_paths(
        "question", "dialogue", tmp_path,
        provider=OllamaEmbeddingProvider(), cache_path=cache,
    )
    assert selection.cache_misses == 0
    assert all(timeout == RUNTIME_EMBEDDING_TIMEOUT_SECONDS for _, timeout in requests)


@pytest.mark.parametrize("failure", ["timeout", "incomplete"])
def test_cold_prewarm_resumes_persisted_batches_after_restart(tmp_path, failure):
    _memory(tmp_path, count=40)
    cache = tmp_path / "cache.sqlite3"

    class InterruptedProvider(FakeEmbeddingProvider):
        def embed(self, texts, *, query=False):
            if query:
                return super().embed(texts, query=query)
            vectors = []
            for offset in range(0, len(texts), 32):
                self.document_calls += 1
                if self.document_calls == 2:
                    if failure == "timeout":
                        raise SemanticAtlasUnavailable("embedding_service_unavailable")
                    return vectors  # The second batch was not completely returned.
                vectors.extend(_vector(text) for text in texts[offset:offset + 32])
            return vectors

    first = CoalescedSemanticAtlasPrewarmer(
        tmp_path, cache_path=cache, provider=InterruptedProvider(),
    ).process_batch(())
    assert first.status == "failed"
    with sqlite3.connect(cache) as database:
        assert database.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 32

    class CountingProvider(FakeEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self.document_texts = []

        def embed(self, texts, *, query=False):
            if not query:
                self.document_texts.extend(texts)
            return super().embed(texts, query=query)

    resumed = CountingProvider()
    complete = CoalescedSemanticAtlasPrewarmer(
        tmp_path, cache_path=cache, provider=resumed,
    ).process_batch(())
    assert complete.status == "completed"
    assert complete.embedded == 48  # Eight remaining bodies and forty routing views.
    assert len(resumed.document_texts) == 48
    selection = select_semantic_atlas_paths(
        "question", "dialogue", tmp_path, provider=resumed, cache_path=cache,
    )
    assert selection.cache_misses == 0


def test_empty_library_is_ready_without_contacting_embedding_service(tmp_path):
    class OfflineProvider(FakeEmbeddingProvider):
        def verify_model(self):
            raise AssertionError("Empty memory needs no embedding service")

        def embed(self, texts, *, query=False):
            raise AssertionError("Empty memory needs no embeddings")

    cache = tmp_path / "cache.sqlite3"
    worker = CoalescedSemanticAtlasPrewarmer(
        tmp_path, provider=OfflineProvider(), cache_path=cache,
    )
    result = worker.process_batch()
    assert result.status == "completed"
    assert result.souvenirs == result.embedded == 0
    assert result.model_warmed is False
    assert not cache.exists()
    assert worker.process_batch().status == "idle"
    with pytest.raises(SemanticAtlasUnavailable, match="no_active_capsules"):
        select_semantic_atlas_paths("question", "", tmp_path,
                                    provider=OfflineProvider(), cache_path=cache)


def test_first_note_after_empty_start_is_indexed_and_survives_restart(tmp_path):
    from types import SimpleNamespace

    cache = tmp_path / "cache.sqlite3"
    provider = FakeEmbeddingProvider()
    worker = CoalescedSemanticAtlasPrewarmer(tmp_path, provider=provider, cache_path=cache)
    assert worker.process_batch().status == "completed"
    _memory(tmp_path, count=1)
    result = worker.process_batch([SimpleNamespace(
        memory_result=SimpleNamespace(updated_souvenirs=["theme/capsule_00"]),
    )])
    assert result.status == "completed"
    assert result.souvenirs == 1
    assert result.embedded == 2
    selected = select_semantic_atlas_paths("question", "", tmp_path,
                                           provider=provider, cache_path=cache)
    assert selected.paths == ("theme/capsule_00",)
    assert selected.cache_misses == 0
    restarted = CoalescedSemanticAtlasPrewarmer(tmp_path, provider=provider, cache_path=cache)
    assert restarted.process_batch().embedded == 0
    (tmp_path / "capsules/theme/capsule_00.md").unlink()
    restarted.request(["theme/capsule_00"])
    assert restarted.process_batch().status == "completed"
    assert restarted.process_batch().status == "idle"
