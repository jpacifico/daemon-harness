"""Local, derived semantic shortlist for the exact Markdown Atlas.

The vectors are only a routing index.  They never become evidence and the
voice must still read an exact souvenir body before using a personal fact.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from daemon_core.single_pass_sovereign import load_searchable_memory_entries
from daemon_core.sovereign_library import frontmatter_routing_metadata
from daemon_core.platform_support import create_private_file, protect_private_path
from daemon_core.embedding_defaults import DEFAULT_MODEL, DEFAULT_MODEL_DIGEST


SEMANTIC_ATLAS_ENV = "DAEMON_CORE_ATLAS_EMBEDDING_TOP12"
SEMANTIC_ATLAS_USER_IDS_ENV = "DAEMON_CORE_ATLAS_EMBEDDING_TOP12_USER_IDS"
SEMANTIC_ATLAS_CACHE_ENV = "DAEMON_CORE_ATLAS_EMBEDDING_CACHE"
SEMANTIC_ATLAS_MODEL_ENV = "DAEMON_CORE_ATLAS_EMBEDDING_MODEL"
SEMANTIC_ATLAS_URL_ENV = "DAEMON_CORE_ATLAS_EMBEDDING_URL"
SEMANTIC_ATLAS_HOST_PLATFORMS = frozenset({"telegram", "openclaw"})
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
TOP12_LIMIT = 12
VIEW_QUOTA = 3
RRF_CONSTANT = 60
BODY_MAX_CHARS = 8_000
MAX_RUNTIME_CACHE_MISSES = 4
MODEL_KEEP_ALIVE = "24h"
RUNTIME_EMBEDDING_TIMEOUT_SECONDS = 10.0
# Cold document batches run off the response path and can exceed five seconds.
RUNTIME_PREWARM_TIMEOUT_SECONDS = 30.0
EMBEDDING_BATCH_SIZE = 32
PREWARM_RETRY_SECONDS = 30.0
MODEL_WARMUP_TEXT = "Semantic Atlas readiness probe."
QUERY_INSTRUCTION = (
    "Retrieve exact personal memory sources that may provide evidence required "
    "by this request, including indirectly involved people or constraints."
)
TOP12_CATALOG_NOTICE = (
    'The following paths are source hypotheses ordered by estimated relevance, never facts. Only the exact body returned by `daemon_read_souvenirs` can support the response.'
)
CHANNEL_NAMES = (
    "need_body",
    "need_routing",
    "dialogue_body",
    "dialogue_routing",
)

logger = logging.getLogger(__name__)
_cache_lock = threading.Lock()
_provider_lock = threading.Lock()
_runtime_provider: OllamaEmbeddingProvider | None = None
_prewarmers_lock = threading.Lock()
_prewarmers: dict[tuple[str, str, str, str], "CoalescedSemanticAtlasPrewarmer"] = {}


class SemanticAtlasUnavailable(RuntimeError):
    """The canary cannot safely produce its shortlist for this turn."""


class EmbeddingProvider(Protocol):
    model: str
    model_digest: str

    def verify_model(self) -> None: ...

    def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]: ...


@dataclass(frozen=True)
class SemanticDocument:
    path: str
    body: str
    routing: str


@dataclass(frozen=True)
class RankedPath:
    path: str
    score: float


@dataclass(frozen=True)
class SemanticAtlasSelection:
    paths: tuple[str, ...]
    channel_heads: tuple[tuple[str, str], ...]
    duration_s: float
    corpus_fingerprint: str
    cache_misses: int


@dataclass(frozen=True)
class SemanticAtlasPrewarmResult:
    status: str
    embedded: int = 0
    souvenirs: int = 0
    model_warmed: bool = False
    requested_souvenirs: tuple[str, ...] = ()
    fingerprint: str = ""
    error_type: str = ""
    duration_s: float = 0.0


class CoalescedSemanticAtlasPrewarmer:
    """Refresh the derived index once after a drained after-turn batch.

    The after-turn worker calls :meth:`process_batch` only after it has drained
    every currently ready memory job.  Multiple souvenir mutations therefore
    converge through one incremental cache fill.  A failed fill remains
    pending and is retried by the worker's existing idle polling loop.
    """

    def __init__(
        self,
        memory_root: Path,
        *,
        provider: EmbeddingProvider | None = None,
        cache_path: Path | None = None,
        retry_seconds: float = PREWARM_RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        start_pending: bool = True,
    ) -> None:
        self.memory_root = Path(memory_root)
        self.provider = provider or _default_provider(
            timeout_s=RUNTIME_PREWARM_TIMEOUT_SECONDS
        )
        self.cache_path = cache_path or default_cache_path()
        self.retry_seconds = max(1.0, float(retry_seconds))
        self.clock = clock
        self._lock = threading.Lock()
        self._pending = bool(start_pending)
        self._running = False
        self._generation = 1 if start_pending else 0
        self._next_attempt_at = 0.0
        self._requested_souvenirs: set[str] = set()

    def request(self, souvenir_names: Sequence[str]) -> bool:
        names = tuple(_normalise_souvenir_name(value) for value in souvenir_names)
        names = tuple(value for value in names if value)
        if not names:
            return False
        with self._lock:
            self._requested_souvenirs.update(names)
            self._generation += 1
            self._pending = True
            self._next_attempt_at = 0.0
        return True

    def process_batch(
        self,
        executions: Sequence[object] = (),
    ) -> SemanticAtlasPrewarmResult:
        changed = _updated_souvenirs_from_executions(executions)
        if changed:
            self.request(changed)
        return self.flush_if_pending()

    def flush_if_pending(
        self,
        *,
        force: bool = False,
    ) -> SemanticAtlasPrewarmResult:
        now = self.clock()
        with self._lock:
            requested = tuple(sorted(self._requested_souvenirs))
            if not self._pending:
                return SemanticAtlasPrewarmResult(status="idle")
            if self._running:
                return SemanticAtlasPrewarmResult(
                    status="running",
                    requested_souvenirs=requested,
                )
            if not force and now < self._next_attempt_at:
                return SemanticAtlasPrewarmResult(
                    status="backoff",
                    requested_souvenirs=requested,
                )
            self._running = True
            generation = self._generation

        try:
            warmed = prewarm_semantic_atlas_cache(
                self.memory_root,
                provider=self.provider,
                cache_path=self.cache_path,
            )
        except Exception as exc:
            with self._lock:
                self._running = False
                self._pending = True
                self._next_attempt_at = self.clock() + self.retry_seconds
            logger.warning(
                "Semantic Atlas prewarm deferred | error=%s",
                exc.__class__.__name__,
            )
            return SemanticAtlasPrewarmResult(
                status="failed",
                requested_souvenirs=requested,
                error_type=exc.__class__.__name__,
            )

        with self._lock:
            self._running = False
            if self._generation == generation:
                self._pending = False
                self._requested_souvenirs.clear()
            self._next_attempt_at = 0.0
        result = SemanticAtlasPrewarmResult(
            status="completed",
            embedded=int(warmed["embedded"]),
            souvenirs=int(warmed["capsules"]),
            model_warmed=bool(warmed["model_warmed"]),
            requested_souvenirs=requested,
            fingerprint=str(warmed["fingerprint"]),
            duration_s=float(warmed["duration_s"]),
        )
        logger.info(
            "Semantic Atlas prewarm completed | souvenirs=%d | embedded=%d | "
            "model_warmed=%s | coalesced_changes=%d | elapsed=%.3fs | corpus=%s",
            result.souvenirs,
            result.embedded,
            result.model_warmed,
            len(result.requested_souvenirs),
            result.duration_s,
            result.fingerprint[:16],
        )
        return result


class OllamaEmbeddingProvider:
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        model_digest: str = DEFAULT_MODEL_DIGEST,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = RUNTIME_EMBEDDING_TIMEOUT_SECONDS,
        batch_size: int = EMBEDDING_BATCH_SIZE,
    ) -> None:
        self.model = model
        self.model_digest = model_digest
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.batch_size = batch_size
        self._verified = False

    def verify_model(self) -> None:
        if self._verified:
            return
        payload = self._request("/api/tags")
        digest = ""
        for item in payload.get("models", []):
            if item.get("name") == self.model or item.get("model") == self.model:
                digest = str(item.get("digest", ""))
                break
        if digest != self.model_digest:
            raise SemanticAtlasUnavailable("embedding_model_digest_mismatch")
        self._verified = True

    def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        prepared = [
            _render_query(text) if query else str(text)
            for text in texts
        ]
        vectors: list[list[float]] = []
        for offset in range(0, len(prepared), self.batch_size):
            batch = prepared[offset : offset + self.batch_size]
            payload = self._request(
                "/api/embed",
                {
                    "model": self.model,
                    "input": batch,
                    "truncate": False,
                    "keep_alive": MODEL_KEEP_ALIVE,
                },
            )
            embeddings = payload.get("embeddings")
            if not isinstance(embeddings, list) or len(embeddings) != len(batch):
                raise SemanticAtlasUnavailable("embedding_batch_incomplete")
            vectors.extend(_normalise_vector(value) for value in embeddings)
        return vectors

    def _request(self, endpoint: str, payload: dict | None = None) -> dict:
        request = urllib.request.Request(
            self.base_url + endpoint,
            data=(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if payload is not None
                else None
            ),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                result = json.load(response)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            raise SemanticAtlasUnavailable("embedding_service_unavailable") from exc
        if not isinstance(result, dict):
            raise SemanticAtlasUnavailable("embedding_response_invalid")
        return result


def semantic_atlas_canary_enabled(user_id: str, platform: str | None) -> bool:
    return bool(
        platform in SEMANTIC_ATLAS_HOST_PLATFORMS
        and semantic_atlas_cache_refresh_enabled(user_id)
    )


def semantic_atlas_cache_refresh_enabled(user_id: str) -> bool:
    if os.getenv(SEMANTIC_ATLAS_ENV, "off").strip().casefold() != "canary":
        return False
    allowed = {
        value.strip()
        for value in os.getenv(SEMANTIC_ATLAS_USER_IDS_ENV, "").split(",")
        if value.strip()
    }
    return bool(user_id and user_id in allowed)


def coalesced_semantic_atlas_prewarmer(
    user_id: str,
    memory_root: Path,
) -> CoalescedSemanticAtlasPrewarmer | None:
    """Return the one process-local prewarmer for a derived cache."""

    if not semantic_atlas_cache_refresh_enabled(user_id):
        return None
    root = Path(memory_root).expanduser().resolve()
    cache = default_cache_path().expanduser().resolve()
    model = os.getenv(SEMANTIC_ATLAS_MODEL_ENV, DEFAULT_MODEL).strip() or DEFAULT_MODEL
    base_url = (
        os.getenv(SEMANTIC_ATLAS_URL_ENV, DEFAULT_BASE_URL).strip()
        or DEFAULT_BASE_URL
    ).rstrip("/")
    key = (str(root), str(cache), model, base_url)
    with _prewarmers_lock:
        prewarmer = _prewarmers.get(key)
        if prewarmer is None:
            prewarmer = CoalescedSemanticAtlasPrewarmer(
                root,
                cache_path=cache,
            )
            _prewarmers[key] = prewarmer
        return prewarmer


def select_semantic_atlas_paths(
    message: str,
    recent_dialogue: str,
    memory_root: Path,
    *,
    provider: EmbeddingProvider | None = None,
    cache_path: Path | None = None,
    allow_cache_build: bool = False,
) -> SemanticAtlasSelection:
    started = time.perf_counter()
    documents = _build_documents(memory_root)
    if not documents:
        raise SemanticAtlasUnavailable("no_active_capsules")
    provider = provider or _default_provider()
    provider.verify_model()
    cache_path = cache_path or default_cache_path()
    body_vectors, body_misses = _cached_vectors(
        documents,
        view="body",
        texts=[item.body for item in documents],
        provider=provider,
        cache_path=cache_path,
        allow_cache_build=allow_cache_build,
    )
    routing_vectors, routing_misses = _cached_vectors(
        documents,
        view="routing",
        texts=[item.routing for item in documents],
        provider=provider,
        cache_path=cache_path,
        allow_cache_build=allow_cache_build,
    )
    query_vectors = provider.embed((message, recent_dialogue), query=True)
    if len(query_vectors) != 2:
        raise SemanticAtlasUnavailable("embedding_queries_incomplete")
    paths = [item.path for item in documents]
    channels = {
        "need_body": _rank(paths, body_vectors, query_vectors[0]),
        "need_routing": _rank(paths, routing_vectors, query_vectors[0]),
        "dialogue_body": _rank(paths, body_vectors, query_vectors[1]),
        "dialogue_routing": _rank(paths, routing_vectors, query_vectors[1]),
    }
    selected = _select_top12(channels)
    return SemanticAtlasSelection(
        paths=selected,
        channel_heads=tuple(
            (name, ranked[0].path)
            for name, ranked in channels.items()
            if ranked
        ),
        duration_s=round(time.perf_counter() - started, 6),
        corpus_fingerprint=_corpus_fingerprint(documents),
        cache_misses=body_misses + routing_misses,
    )


def prewarm_semantic_atlas_cache(
    memory_root: Path,
    *,
    provider: EmbeddingProvider | None = None,
    cache_path: Path | None = None,
    timeout_s: float = 180.0,
) -> dict[str, object]:
    started = time.perf_counter()
    documents = _build_documents(memory_root)
    provider = provider or _default_provider(timeout_s=timeout_s)
    cache_path = cache_path or default_cache_path()
    if not documents:
        # An empty library is ready, not a failed cache. The first mutation
        # will request a fresh batch through the existing after-turn worker.
        return {
            "capsules": 0,
            "embedded": 0,
            "model_warmed": False,
            "fingerprint": _corpus_fingerprint(documents),
            "cache_path": str(cache_path),
            "model": provider.model,
            "model_digest": provider.model_digest,
            "duration_s": round(time.perf_counter() - started, 6),
        }
    provider.verify_model()
    _body, body_misses = _cached_vectors(
        documents,
        view="body",
        texts=[item.body for item in documents],
        provider=provider,
        cache_path=cache_path,
        allow_cache_build=True,
    )
    _routing, routing_misses = _cached_vectors(
        documents,
        view="routing",
        texts=[item.routing for item in documents],
        provider=provider,
        cache_path=cache_path,
        allow_cache_build=True,
    )
    warmup_vectors = provider.embed((MODEL_WARMUP_TEXT,), query=True)
    if len(warmup_vectors) != 1:
        raise SemanticAtlasUnavailable("embedding_warmup_incomplete")
    return {
        "capsules": len(documents),
        "embedded": body_misses + routing_misses,
        "model_warmed": True,
        "fingerprint": _corpus_fingerprint(documents),
        "cache_path": str(cache_path),
        "model": provider.model,
        "model_digest": provider.model_digest,
        "duration_s": round(time.perf_counter() - started, 6),
    }


def default_cache_path() -> Path:
    configured = os.getenv(SEMANTIC_ATLAS_CACHE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return (
        Path.home()
        / "Library"
        / "Caches"
        / "daemon-harness"
        / "atlas-embedding-top12.sqlite3"
    )


def _default_provider(
    *,
    timeout_s: float = RUNTIME_EMBEDDING_TIMEOUT_SECONDS,
) -> OllamaEmbeddingProvider:
    global _runtime_provider

    model = os.getenv(SEMANTIC_ATLAS_MODEL_ENV, DEFAULT_MODEL).strip() or DEFAULT_MODEL
    base_url = (
        os.getenv(SEMANTIC_ATLAS_URL_ENV, DEFAULT_BASE_URL).strip()
        or DEFAULT_BASE_URL
    )
    if timeout_s != RUNTIME_EMBEDDING_TIMEOUT_SECONDS:
        return OllamaEmbeddingProvider(
            model=model,
            base_url=base_url,
            timeout_s=timeout_s,
        )
    with _provider_lock:
        if (
            _runtime_provider is None
            or _runtime_provider.model != model
            or _runtime_provider.base_url != base_url.rstrip("/")
        ):
            _runtime_provider = OllamaEmbeddingProvider(
                model=model,
                base_url=base_url,
                timeout_s=timeout_s,
            )
        return _runtime_provider


def _build_documents(memory_root: Path) -> tuple[SemanticDocument, ...]:
    entries, _expired = load_searchable_memory_entries(memory_root)
    documents = []
    for path, content in entries:
        subjects, facets = frontmatter_routing_metadata(content)
        documents.append(
            SemanticDocument(
                path=path,
                body=_strip_frontmatter(content)[:BODY_MAX_CHARS],
                routing=_render_routing(path, subjects, facets),
            )
        )
    return tuple(documents)


def _updated_souvenirs_from_executions(
    executions: Sequence[object],
) -> tuple[str, ...]:
    changed: set[str] = set()
    for execution in executions:
        memory_result: Any = getattr(execution, "memory_result", execution)
        for raw in getattr(memory_result, "updated_souvenirs", ()) or ():
            name = _normalise_souvenir_name(raw)
            if name:
                changed.add(name)
    return tuple(sorted(changed))


def _normalise_souvenir_name(value: object) -> str:
    name = (
        str(value)
        .strip()
        .replace("\\", "/")
        .removeprefix("memory/")
        .removeprefix("capsules/")
        .removesuffix(".md")
    )
    path = Path(name)
    if (
        not name.startswith("theme/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return ""
    return path.as_posix()


def _cached_vectors(
    documents: Sequence[SemanticDocument],
    *,
    view: str,
    texts: Sequence[str],
    provider: EmbeddingProvider,
    cache_path: Path,
    allow_cache_build: bool,
) -> tuple[list[list[float]], int]:
    keys = [
        _embedding_key(provider.model_digest, view, text)
        for text in texts
    ]
    cached = _read_cached_vectors(cache_path, keys)
    missing_indexes = [index for index, key in enumerate(keys) if key not in cached]
    if missing_indexes and (
        not allow_cache_build
        and len(missing_indexes) > MAX_RUNTIME_CACHE_MISSES
    ):
        raise SemanticAtlasUnavailable(
            f"embedding_cache_cold:{view}:{len(missing_indexes)}"
        )
    for offset in range(0, len(missing_indexes), EMBEDDING_BATCH_SIZE):
        batch = missing_indexes[offset : offset + EMBEDDING_BATCH_SIZE]
        # Commit each successful batch before asking Ollama for the next one.
        # A timeout or worker restart must not discard completed cold indexing.
        # SQLite remains unlocked during inference, so warm voice reads continue.
        new_vectors = provider.embed([texts[index] for index in batch])
        if len(new_vectors) != len(batch):
            raise SemanticAtlasUnavailable("embedding_cache_fill_incomplete")
        additions = []
        for index, vector in zip(batch, new_vectors):
            key = keys[index]
            cached[key] = vector
            additions.append((key, vector))
        _write_cached_vectors(cache_path, additions)
    vectors = [cached[key] for key in keys]
    dimension = len(vectors[0]) if vectors else 0
    if dimension < 1 or any(len(vector) != dimension for vector in vectors):
        raise SemanticAtlasUnavailable("embedding_cache_dimension_mismatch")
    return vectors, len(missing_indexes)


def _read_cached_vectors(
    cache_path: Path,
    keys: Sequence[str],
) -> dict[str, list[float]]:
    with _cache_database(cache_path) as database:
        cached: dict[str, list[float]] = {}
        for offset in range(0, len(keys), 500):
            batch = keys[offset : offset + 500]
            placeholders = ",".join("?" for _item in batch)
            rows = database.execute(
                f"SELECT cache_key, dimension, vector_json FROM embeddings "
                f"WHERE cache_key IN ({placeholders})",
                batch,
            ).fetchall()
            for key, dimension, raw in rows:
                cached[str(key)] = _decode_cached_vector(raw, int(dimension))
        return cached


def _write_cached_vectors(
    cache_path: Path,
    additions: Sequence[tuple[str, Sequence[float]]],
) -> None:
    if not additions:
        return
    with _cache_database(cache_path) as database:
        database.executemany(
            "INSERT OR REPLACE INTO embeddings "
            "(cache_key, dimension, vector_json) VALUES (?, ?, ?)",
            (
                (
                    key,
                    len(vector),
                    json.dumps(vector, separators=(",", ":")),
                )
                for key, vector in additions
            ),
        )
        database.commit()


class _cache_database:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.database: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "nt":
            # SQLite sidecars inherit the directory ACL, not the database ACL.
            protect_private_path(self.cache_path.parent, directory=True)
        else:
            try:
                self.cache_path.parent.chmod(0o700)
            except OSError:
                pass
        _cache_lock.acquire()
        database = None
        try:
            if os.name == "nt":
                try:
                    fd = create_private_file(self.cache_path)
                except FileExistsError:
                    protect_private_path(self.cache_path)
                else:
                    os.close(fd)
            database = sqlite3.connect(self.cache_path, timeout=5.0)
            database.execute("PRAGMA busy_timeout = 5000")
            database.execute(
                "CREATE TABLE IF NOT EXISTS embeddings ("
                "cache_key TEXT PRIMARY KEY, dimension INTEGER NOT NULL, "
                "vector_json TEXT NOT NULL)"
            )
            database.commit()
            self.cache_path.chmod(0o600)
        except Exception:
            if database is not None:
                database.close()
            _cache_lock.release()
            raise
        self.database = database
        return database

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self.database is not None:
                self.database.close()
        finally:
            _cache_lock.release()


def _decode_cached_vector(raw: str, dimension: int) -> list[float]:
    try:
        value = json.loads(raw)
        vector = [float(item) for item in value]
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SemanticAtlasUnavailable("embedding_cache_corrupt") from exc
    if len(vector) != dimension or dimension < 1:
        raise SemanticAtlasUnavailable("embedding_cache_corrupt")
    return vector


def _rank(
    paths: Sequence[str],
    vectors: Sequence[Sequence[float]],
    query: Sequence[float],
) -> tuple[RankedPath, ...]:
    if len(paths) != len(vectors):
        raise SemanticAtlasUnavailable("embedding_path_count_mismatch")
    dimension = len(query)
    if dimension < 1 or any(len(vector) != dimension for vector in vectors):
        raise SemanticAtlasUnavailable("embedding_query_dimension_mismatch")
    ranked = [
        RankedPath(path, sum(left * right for left, right in zip(vector, query)))
        for path, vector in zip(paths, vectors)
    ]
    ranked.sort(key=lambda item: (-item.score, item.path))
    return tuple(ranked)


def _select_top12(
    channels: dict[str, Sequence[RankedPath]],
) -> tuple[str, ...]:
    if any(name not in channels for name in CHANNEL_NAMES):
        raise ValueError("all four semantic channels are required")
    selected: list[str] = []
    for name in CHANNEL_NAMES:
        for item in channels[name][:VIEW_QUOTA]:
            if item.path not in selected:
                selected.append(item.path)
    consensus: dict[str, float] = {}
    for name in CHANNEL_NAMES:
        for rank, item in enumerate(channels[name], start=1):
            consensus[item.path] = consensus.get(item.path, 0.0) + 1.0 / (
                RRF_CONSTANT + rank
            )
    for path, _score in sorted(consensus.items(), key=lambda item: (-item[1], item[0])):
        if len(selected) >= TOP12_LIMIT:
            break
        if path not in selected:
            selected.append(path)
    return tuple(selected[:TOP12_LIMIT])


def _normalise_vector(value: object) -> list[float]:
    if not isinstance(value, list) or not value:
        raise SemanticAtlasUnavailable("embedding_vector_invalid")
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise SemanticAtlasUnavailable("embedding_vector_invalid") from exc
    norm = math.sqrt(sum(item * item for item in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise SemanticAtlasUnavailable("embedding_vector_invalid")
    return [item / norm for item in vector]


def _embedding_key(model_digest: str, view: str, text: str) -> str:
    return hashlib.sha256(
        (model_digest + "\0" + view + "\0" + text).encode("utf-8")
    ).hexdigest()


def _corpus_fingerprint(documents: Sequence[SemanticDocument]) -> str:
    digest = hashlib.sha256()
    for item in documents:
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.body.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.routing.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _render_query(value: str) -> str:
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery: {str(value).strip()}"


def _strip_frontmatter(content: str) -> str:
    lines = str(content).strip().splitlines()
    if not lines or lines[0].strip() != "---":
        return "\n".join(lines).strip()
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :]).strip()
    return "\n".join(lines).strip()


def _render_routing(
    path: str,
    subjects: Sequence[str],
    facets: Sequence[str],
) -> str:
    readable = " > ".join(
        part.replace("_", " ").replace("-", " ") for part in path.split("/")
    )
    return (
        "Personal memory route.\n"
        f"Path: {readable}.\n"
        f"Subjects: {', '.join(subjects) if subjects else 'none'}.\n"
        f"Facets: {', '.join(facets) if facets else 'none'}."
    )


__all__ = [
    "CoalescedSemanticAtlasPrewarmer",
    "SemanticAtlasSelection",
    "SemanticAtlasPrewarmResult",
    "SemanticAtlasUnavailable",
    "TOP12_CATALOG_NOTICE",
    "coalesced_semantic_atlas_prewarmer",
    "default_cache_path",
    "prewarm_semantic_atlas_cache",
    "select_semantic_atlas_paths",
    "semantic_atlas_cache_refresh_enabled",
    "semantic_atlas_canary_enabled",
]
