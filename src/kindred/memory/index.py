"""Derived SQLite storage for the episode memory corpus.

The canonical Kindred database remains authoritative. This module owns only a
rebuildable projection and never imports Mouth or Heart runtime code.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from kindred.memory.contracts import EpisodeDocument
from kindred.memory.embedding import EmbeddingProvider, pack_embedding
from kindred.memory.normalization import NORMALIZATION_REVISION, normalize_text
from kindred.memory.source import PROJECTION_REVISION

INDEX_SCHEMA_VERSION: Final[int] = 1

_SCHEMA_SQL = """
PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS memory_index_metadata (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_document (
    source_tick_id        INTEGER PRIMARY KEY CHECK (source_tick_id > 0),
    document_id           TEXT NOT NULL UNIQUE,
    occurred_at           TEXT NOT NULL,
    text                  TEXT NOT NULL,
    normalized_text       TEXT NOT NULL,
    activity              TEXT NOT NULL,
    activity_description  TEXT,
    location              TEXT NOT NULL,
    location_city         TEXT,
    location_address      TEXT,
    mood_description      TEXT NOT NULL,
    significance          INTEGER NOT NULL CHECK (significance BETWEEN 7 AND 10),
    content_hash          TEXT NOT NULL,
    embedding_revision    TEXT,
    embedding_dimension   INTEGER CHECK (embedding_dimension IS NULL OR embedding_dimension > 0),
    embedding_normalized  INTEGER CHECK (
        embedding_normalized IS NULL OR embedding_normalized IN (0, 1)
    ),
    embedding             BLOB,
    CHECK (document_id = 'episode:' || source_tick_id),
    CHECK (
        (embedding IS NULL AND embedding_revision IS NULL
            AND embedding_dimension IS NULL AND embedding_normalized IS NULL)
        OR
        (embedding IS NOT NULL AND embedding_revision IS NOT NULL
            AND embedding_dimension IS NOT NULL AND embedding_normalized IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_memory_document_occurred_at
    ON memory_document(occurred_at);
CREATE INDEX IF NOT EXISTS idx_memory_document_activity
    ON memory_document(activity);
CREATE INDEX IF NOT EXISTS idx_memory_document_location
    ON memory_document(location);
CREATE INDEX IF NOT EXISTS idx_memory_document_location_city
    ON memory_document(location_city);
CREATE INDEX IF NOT EXISTS idx_memory_document_location_address
    ON memory_document(location_address);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_document_fts USING fts5(
    normalized_text,
    content = 'memory_document',
    content_rowid = 'source_tick_id',
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS memory_document_ai AFTER INSERT ON memory_document BEGIN
    INSERT INTO memory_document_fts(rowid, normalized_text)
    VALUES (new.source_tick_id, new.normalized_text);
END;

CREATE TRIGGER IF NOT EXISTS memory_document_ad AFTER DELETE ON memory_document BEGIN
    INSERT INTO memory_document_fts(memory_document_fts, rowid, normalized_text)
    VALUES ('delete', old.source_tick_id, old.normalized_text);
END;

CREATE TRIGGER IF NOT EXISTS memory_document_au AFTER UPDATE ON memory_document BEGIN
    INSERT INTO memory_document_fts(memory_document_fts, rowid, normalized_text)
    VALUES ('delete', old.source_tick_id, old.normalized_text);
    INSERT INTO memory_document_fts(rowid, normalized_text)
    VALUES (new.source_tick_id, new.normalized_text);
END;
"""


class MemoryIndexError(RuntimeError):
    """Base error for an unavailable or invalid derived memory index."""


class IndexVersionMismatch(MemoryIndexError):
    """The existing derived index uses an incompatible schema or projection contract."""


class IndexDataError(MemoryIndexError):
    """The derived index metadata or append-only input is inconsistent."""


def _nonblank(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """Every input that can change document text or vector compatibility."""

    activity_catalog_digest: str
    projection_revision: str = PROJECTION_REVISION
    normalization_revision: str = NORMALIZATION_REVISION
    embedding_revision: str | None = None
    embedding_dimension: int | None = None
    embedding_normalized: bool | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "activity_catalog_digest",
            "projection_revision",
            "normalization_revision",
        ):
            _nonblank(getattr(self, field_name), field_name)
        embedding_fields = (
            self.embedding_revision,
            self.embedding_dimension,
            self.embedding_normalized,
        )
        if any(value is not None for value in embedding_fields) and not all(
            value is not None for value in embedding_fields
        ):
            raise ValueError("embedding index spec must set revision, dimension, and normalized")
        if self.embedding_dimension is not None and (
            isinstance(self.embedding_dimension, bool)
            or not isinstance(self.embedding_dimension, int)
            or self.embedding_dimension < 1
        ):
            raise ValueError("embedding_dimension must be a positive integer")
        if self.embedding_revision is not None:
            _nonblank(self.embedding_revision, "embedding_revision")
        if self.embedding_normalized is not None and self.embedding_normalized is not True:
            raise ValueError("only normalized embeddings are supported")

    @property
    def version(self) -> str:
        payload = json.dumps(
            {
                "activity_catalog_digest": self.activity_catalog_digest,
                "embedding_dimension": self.embedding_dimension,
                "embedding_normalized": self.embedding_normalized,
                "embedding_revision": self.embedding_revision,
                "normalization_revision": self.normalization_revision,
                "projection_revision": self.projection_revision,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class IndexState:
    schema_version: int
    index_version: str
    indexed_through_tick_id: int
    document_count: int


_SPEC_METADATA_KEYS: Final[tuple[str, ...]] = (
    "schema_version",
    "index_version",
    "projection_revision",
    "normalization_revision",
    "activity_catalog_digest",
    "embedding_revision",
    "embedding_dimension",
    "embedding_normalized",
    "indexed_through_tick_id",
)


def _spec_metadata(spec: IndexSpec) -> dict[str, str]:
    return {
        "schema_version": str(INDEX_SCHEMA_VERSION),
        "index_version": spec.version,
        "projection_revision": spec.projection_revision,
        "normalization_revision": spec.normalization_revision,
        "activity_catalog_digest": spec.activity_catalog_digest,
        "embedding_revision": spec.embedding_revision or "",
        "embedding_dimension": str(spec.embedding_dimension or ""),
        "embedding_normalized": (
            "" if spec.embedding_normalized is None else str(int(spec.embedding_normalized))
        ),
        "indexed_through_tick_id": "0",
    }


def _document_hash(document: EpisodeDocument) -> str:
    payload = json.dumps(
        {
            "activity": document.activity,
            "activity_description": document.activity_description,
            "id": document.id,
            "location": document.location,
            "location_address": document.location_address,
            "location_city": document.location_city,
            "mood_description": document.mood_description,
            "occurred_at": document.occurred_at,
            "significance": document.significance,
            "source_tick_id": document.source_tick_id,
            "text": document.text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_batch(
    documents: Iterable[EpisodeDocument],
    *,
    after_tick_id: int,
    indexed_through_tick_id: int,
) -> tuple[EpisodeDocument, ...]:
    for value, field_name in (
        (after_tick_id, "after_tick_id"),
        (indexed_through_tick_id, "indexed_through_tick_id"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise IndexDataError(f"{field_name} must be a non-negative integer")
    batch = tuple(documents)
    if indexed_through_tick_id < after_tick_id:
        raise IndexDataError("indexed watermark cannot move backwards")
    previous = after_tick_id
    for document in batch:
        if document.source_tick_id <= previous:
            raise IndexDataError("episode documents must be unique and strictly ordered by tick id")
        previous = document.source_tick_id
    if batch:
        if batch[-1].source_tick_id != indexed_through_tick_id:
            raise IndexDataError("watermark must equal the final document tick id")
    elif indexed_through_tick_id != after_tick_id:
        raise IndexDataError("an empty batch cannot advance the episode watermark")
    return batch


def _document_rows(
    documents: Iterable[EpisodeDocument],
    *,
    spec: IndexSpec,
    embedding_provider: EmbeddingProvider | None,
) -> list[dict[str, object]]:
    batch = tuple(documents)
    if spec.embedding_revision is None:
        vectors: tuple[tuple[float, ...] | None, ...] = (None,) * len(batch)
    else:
        if embedding_provider is None:
            raise IndexDataError("embedding provider is required to write a vector index")
        embedded = embedding_provider.embed_documents(tuple(document.text for document in batch))
        if len(embedded) != len(batch):
            raise IndexDataError("embedding provider returned the wrong document count")
        vectors = embedded
    rows: list[dict[str, object]] = []
    for document, vector in zip(batch, vectors, strict=True):
        rows.append(
            {
                "source_tick_id": document.source_tick_id,
                "document_id": document.id,
                "occurred_at": document.occurred_at,
                "text": document.text,
                "normalized_text": normalize_text(document.text),
                "activity": document.activity,
                "activity_description": document.activity_description,
                "location": document.location,
                "location_city": document.location_city,
                "location_address": document.location_address,
                "mood_description": document.mood_description,
                "significance": document.significance,
                "content_hash": _document_hash(document),
                "embedding_revision": spec.embedding_revision,
                "embedding_dimension": spec.embedding_dimension,
                "embedding_normalized": (
                    None if spec.embedding_normalized is None else int(spec.embedding_normalized)
                ),
                "embedding": (
                    None
                    if vector is None or spec.embedding_dimension is None
                    else pack_embedding(vector, dimension=spec.embedding_dimension)
                ),
            }
        )
    return rows


class MemoryIndex:
    """Lifecycle and append-only sync for one derived memory SQLite file."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        spec: IndexSpec,
        embedding_provider: EmbeddingProvider | None,
    ) -> None:
        self._conn = conn
        self._spec = spec
        self._embedding_provider = embedding_provider
        self._rebuild_required = False

    @classmethod
    @contextmanager
    def open(
        cls,
        index_path: Path,
        *,
        spec: IndexSpec,
        embedding_provider: EmbeddingProvider | None = None,
        allow_rebuild: bool = False,
    ) -> Iterator[MemoryIndex]:
        """Open an index, permitting projection drift only for an immediate rebuild.

        Schema-version drift remains fail-closed because an in-place rebuild cannot
        safely assume that the existing tables match the current schema.
        """

        index_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(index_path, isolation_level="DEFERRED")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            conn.executescript(_SCHEMA_SQL)
            if embedding_provider is not None:
                profile = embedding_provider.profile
                if (
                    profile.revision != spec.embedding_revision
                    or profile.dimension != spec.embedding_dimension
                    or profile.normalized != spec.embedding_normalized
                ):
                    raise ValueError("embedding provider does not match the index spec")
            instance = cls(conn, spec, embedding_provider)
            instance._bind_spec(allow_rebuild=allow_rebuild)
            yield instance
        finally:
            conn.close()

    def _metadata(self) -> dict[str, str]:
        return {
            row["key"]: row["value"]
            for row in self._conn.execute("SELECT key, value FROM memory_index_metadata")
        }

    def _bind_spec(self, *, allow_rebuild: bool) -> None:
        metadata = self._metadata()
        if not metadata:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM memory_document").fetchone()
            if row is None or row["n"] != 0:
                raise IndexDataError("unversioned memory index contains documents")
            with self._conn:
                self._conn.executemany(
                    "INSERT INTO memory_index_metadata(key, value) VALUES (?, ?)",
                    _spec_metadata(self._spec).items(),
                )
            return
        missing = set(_SPEC_METADATA_KEYS) - set(metadata)
        if missing:
            raise IndexDataError(f"memory index metadata is incomplete: {sorted(missing)}")
        if metadata["schema_version"] != str(INDEX_SCHEMA_VERSION):
            raise IndexVersionMismatch("memory index schema version is incompatible")
        if metadata["index_version"] != self._spec.version:
            if allow_rebuild:
                self._rebuild_required = True
                return
            raise IndexVersionMismatch(
                "memory index projection version is incompatible; rebuild required"
            )
        expected = _spec_metadata(self._spec)
        for key in set(_SPEC_METADATA_KEYS) - {"indexed_through_tick_id"}:
            if metadata[key] != expected[key]:
                raise IndexDataError("memory index metadata does not match its index version")

    def state(self) -> IndexState:
        metadata = self._metadata()
        missing = set(_SPEC_METADATA_KEYS) - set(metadata)
        if missing:
            raise IndexDataError(f"memory index metadata is incomplete: {sorted(missing)}")
        try:
            schema_version = int(metadata["schema_version"])
            indexed_through_tick_id = int(metadata["indexed_through_tick_id"])
        except ValueError as exc:
            raise IndexDataError("memory index metadata contains a non-integer value") from exc
        if indexed_through_tick_id < 0:
            raise IndexDataError("memory index watermark must not be negative")
        row = self._conn.execute("SELECT COUNT(*) AS n FROM memory_document").fetchone()
        if row is None or not isinstance(row["n"], int):
            raise IndexDataError("memory index document count is unavailable")
        return IndexState(
            schema_version=schema_version,
            index_version=metadata["index_version"],
            indexed_through_tick_id=indexed_through_tick_id,
            document_count=row["n"],
        )

    def _insert_rows(self, rows: list[dict[str, object]]) -> None:
        self._conn.executemany(
            "INSERT INTO memory_document("
            "source_tick_id, document_id, occurred_at, text, normalized_text, activity, "
            "activity_description, location, location_city, location_address, "
            "mood_description, significance, content_hash"
            ", embedding_revision, embedding_dimension, embedding_normalized, embedding"
            ") VALUES ("
            ":source_tick_id, :document_id, :occurred_at, :text, :normalized_text, :activity, "
            ":activity_description, :location, :location_city, :location_address, "
            ":mood_description, :significance, :content_hash"
            ", :embedding_revision, :embedding_dimension, :embedding_normalized, :embedding"
            ")",
            rows,
        )

    def _set_watermark(self, tick_id: int) -> None:
        self._conn.execute(
            "UPDATE memory_index_metadata SET value = :value WHERE key = 'indexed_through_tick_id'",
            {"value": str(tick_id)},
        )

    def sync(
        self,
        documents: Iterable[EpisodeDocument],
        *,
        indexed_through_tick_id: int,
    ) -> IndexState:
        if self._rebuild_required:
            raise IndexVersionMismatch(
                "memory index projection version is incompatible; rebuild required"
            )
        current = self.state()
        batch = _validate_batch(
            documents,
            after_tick_id=current.indexed_through_tick_id,
            indexed_through_tick_id=indexed_through_tick_id,
        )
        if not batch:
            return current
        rows = _document_rows(
            batch,
            spec=self._spec,
            embedding_provider=self._embedding_provider,
        )
        with self._conn:
            self._insert_rows(rows)
            self._set_watermark(indexed_through_tick_id)
        return self.state()

    def rebuild(
        self,
        documents: Iterable[EpisodeDocument],
        *,
        indexed_through_tick_id: int,
    ) -> IndexState:
        """Atomically replace all derived documents under the current ``IndexSpec``."""

        batch = _validate_batch(
            documents,
            after_tick_id=0,
            indexed_through_tick_id=indexed_through_tick_id,
        )
        rows = _document_rows(
            batch,
            spec=self._spec,
            embedding_provider=self._embedding_provider,
        )
        with self._conn:
            self._conn.execute("DELETE FROM memory_document")
            self._insert_rows(rows)
            metadata = _spec_metadata(self._spec)
            metadata["indexed_through_tick_id"] = str(indexed_through_tick_id)
            self._conn.execute("DELETE FROM memory_index_metadata")
            self._conn.executemany(
                "INSERT INTO memory_index_metadata(key, value) VALUES (?, ?)",
                metadata.items(),
            )
        self._rebuild_required = False
        return self.state()


__all__ = [
    "INDEX_SCHEMA_VERSION",
    "IndexDataError",
    "IndexSpec",
    "IndexState",
    "IndexVersionMismatch",
    "MemoryIndex",
    "MemoryIndexError",
]
