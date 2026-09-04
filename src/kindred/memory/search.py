"""Read-only lexical, vector, and hybrid retrieval over the episode index."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from kindred.db.connection import connect_readonly
from kindred.memory.contracts import (
    EpisodeSource,
    MemoryFilters,
    MemoryHit,
    MemorySearchRequest,
    MemorySearchResponse,
    MemorySearchTrace,
    SourceStatus,
)
from kindred.memory.embedding import (
    EmbeddingError,
    EmbeddingProvider,
    normalize_embedding,
    unpack_embedding,
)
from kindred.memory.index import INDEX_SCHEMA_VERSION, IndexSpec
from kindred.memory.normalization import normalize_text


class MemorySearchError(RuntimeError):
    """The derived memory index cannot safely serve a search."""


@dataclass(frozen=True, slots=True)
class LexicalSearchConfig:
    candidate_k: int = 10
    rerank_k: int = 30
    trigram_coverage_threshold: float = 0.30
    bm25_evidence_threshold: float = 5.0
    hit_overhead_tokens: int = 32

    def __post_init__(self) -> None:
        for integer_value, field_name in (
            (self.candidate_k, "candidate_k"),
            (self.rerank_k, "rerank_k"),
            (self.hit_overhead_tokens, "hit_overhead_tokens"),
        ):
            if (
                isinstance(integer_value, bool)
                or not isinstance(integer_value, int)
                or integer_value < 1
            ):
                raise ValueError(f"{field_name} must be a positive integer")
        if self.rerank_k < self.candidate_k:
            raise ValueError("rerank_k must be greater than or equal to candidate_k")
        for numeric_value, field_name in (
            (self.trigram_coverage_threshold, "trigram_coverage_threshold"),
            (self.bm25_evidence_threshold, "bm25_evidence_threshold"),
        ):
            if (
                isinstance(numeric_value, bool)
                or not isinstance(numeric_value, (int, float))
                or numeric_value <= 0
            ):
                raise ValueError(f"{field_name} must be positive")


@dataclass(frozen=True, slots=True)
class LexicalSearchDiagnostics:
    response: MemorySearchResponse
    exact_candidate_tick_ids: tuple[int, ...]
    lexical_candidate_tick_ids: tuple[int, ...]
    reranked_tick_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class VectorSearchConfig:
    candidate_k: int = 10
    rerank_k: int = 30
    cosine_threshold: float = 0.55
    hit_overhead_tokens: int = 32

    def __post_init__(self) -> None:
        for integer_value, field_name in (
            (self.candidate_k, "candidate_k"),
            (self.rerank_k, "rerank_k"),
            (self.hit_overhead_tokens, "hit_overhead_tokens"),
        ):
            if (
                isinstance(integer_value, bool)
                or not isinstance(integer_value, int)
                or integer_value < 1
            ):
                raise ValueError(f"{field_name} must be a positive integer")
        if self.rerank_k < self.candidate_k:
            raise ValueError("rerank_k must be greater than or equal to candidate_k")
        if (
            isinstance(self.cosine_threshold, bool)
            or not isinstance(self.cosine_threshold, (int, float))
            or not 0.0 <= self.cosine_threshold <= 1.0
        ):
            raise ValueError("cosine_threshold must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class HybridSearchConfig:
    candidate_k: int = 10
    rerank_k: int = 30
    trigram_coverage_threshold: float = 0.30
    bm25_evidence_threshold: float = 5.0
    cosine_threshold: float = 0.55
    fallback_bm25_threshold: float = 3.5
    fallback_cosine_threshold: float = 0.38
    rrf_k: int = 60
    hit_overhead_tokens: int = 32

    def __post_init__(self) -> None:
        LexicalSearchConfig(
            candidate_k=self.candidate_k,
            rerank_k=self.rerank_k,
            trigram_coverage_threshold=self.trigram_coverage_threshold,
            bm25_evidence_threshold=self.bm25_evidence_threshold,
            hit_overhead_tokens=self.hit_overhead_tokens,
        )
        VectorSearchConfig(
            candidate_k=self.candidate_k,
            rerank_k=self.rerank_k,
            cosine_threshold=self.cosine_threshold,
            hit_overhead_tokens=self.hit_overhead_tokens,
        )
        if (
            isinstance(self.fallback_cosine_threshold, bool)
            or not isinstance(self.fallback_cosine_threshold, (int, float))
            or not 0.0 <= self.fallback_cosine_threshold <= self.cosine_threshold
        ):
            raise ValueError("fallback_cosine_threshold must be between 0 and cosine_threshold")
        if (
            isinstance(self.fallback_bm25_threshold, bool)
            or not isinstance(self.fallback_bm25_threshold, (int, float))
            or not 0.0 < self.fallback_bm25_threshold <= self.bm25_evidence_threshold
        ):
            raise ValueError(
                "fallback_bm25_threshold must be between 0 and bm25_evidence_threshold"
            )
        if isinstance(self.rrf_k, bool) or not isinstance(self.rrf_k, int) or self.rrf_k < 1:
            raise ValueError("rrf_k must be a positive integer")


@dataclass(frozen=True, slots=True)
class VectorSearchDiagnostics:
    response: MemorySearchResponse
    vector_candidate_tick_ids: tuple[int, ...]
    reranked_tick_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class HybridSearchDiagnostics:
    response: MemorySearchResponse
    lexical_candidate_tick_ids: tuple[int, ...]
    vector_candidate_tick_ids: tuple[int, ...]
    rrf_ranked_tick_ids: tuple[int, ...]
    reranked_tick_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _IndexState:
    index_version: str
    indexed_through_tick_id: int


@dataclass(slots=True)
class _Candidate:
    source_tick_id: int
    document_id: str
    occurred_at: str
    text: str
    normalized_text: str
    activity: str
    location: str
    mood_description: str
    significance: int
    exact_phrase: bool = False
    trigram_coverage: float = 0.0
    bm25_evidence: float = 0.0
    bm25_rank: int | None = None
    cosine_similarity: float = 0.0
    lexical_rank: int | None = None
    vector_rank: int | None = None
    cross_channel_support: bool = False
    temporal_rank: int | None = None
    rrf_score: float = 0.0
    normalized_rrf: float = 0.0
    score: float = 0.0


_DOCUMENT_COLUMNS = (
    "d.source_tick_id, d.document_id, d.occurred_at, d.text, d.normalized_text, "
    "d.activity, d.location, d.mood_description, d.significance"
)


def _query_trigrams(normalized_query: str) -> tuple[str, ...]:
    if len(normalized_query) < 3:
        return ()
    return tuple(
        dict.fromkeys(
            normalized_query[index : index + 3] for index in range(len(normalized_query) - 2)
        )
    )


def _fts_literal(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _filter_sql(filters: MemoryFilters) -> tuple[str, dict[str, object]]:
    clauses: list[str] = []
    params: dict[str, object] = {}
    for value, column, name, operator in (
        (filters.occurred_after, "d.occurred_at", "occurred_after", ">="),
        (filters.occurred_before, "d.occurred_at", "occurred_before", "<="),
        (filters.activity, "d.activity", "activity", "="),
    ):
        if value is not None:
            clauses.append(f"{column} {operator} :{name}")
            params[name] = value
    if filters.location is not None:
        clauses.append(
            "(d.location = :location OR d.location_city = :location "
            "OR d.location_address = :location)"
        )
        params["location"] = filters.location
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def _candidate_from_row(row: sqlite3.Row) -> _Candidate:
    return _Candidate(
        source_tick_id=row["source_tick_id"],
        document_id=row["document_id"],
        occurred_at=row["occurred_at"],
        text=row["text"],
        normalized_text=row["normalized_text"],
        activity=row["activity"],
        location=row["location"],
        mood_description=row["mood_description"],
        significance=row["significance"],
    )


def _read_index_state(conn: sqlite3.Connection, spec: IndexSpec) -> _IndexState:
    metadata = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM memory_index_metadata")
    }
    required = {"schema_version", "index_version", "indexed_through_tick_id"}
    if required - set(metadata):
        raise MemorySearchError("memory index metadata is incomplete; run kindred memory sync")
    if metadata["schema_version"] != str(INDEX_SCHEMA_VERSION):
        raise MemorySearchError("memory index schema is incompatible; rebuild required")
    if metadata["index_version"] != spec.version:
        raise MemorySearchError("memory index projection is incompatible; rebuild required")
    try:
        watermark = int(metadata["indexed_through_tick_id"])
    except ValueError as exc:
        raise MemorySearchError("memory index watermark is invalid; rebuild required") from exc
    if watermark < 0:
        raise MemorySearchError("memory index watermark is invalid; rebuild required")
    return _IndexState(index_version=metadata["index_version"], indexed_through_tick_id=watermark)


def _preliminary_score(candidate: _Candidate) -> float:
    bm25_rank_score = 0.0 if candidate.bm25_rank is None else 1.0 / candidate.bm25_rank
    return (
        0.65 * candidate.trigram_coverage
        + 0.20 * bm25_rank_score
        + 0.15 * float(candidate.exact_phrase)
    )


def _is_answerable(candidate: _Candidate, config: LexicalSearchConfig) -> bool:
    return (
        candidate.exact_phrase
        or candidate.trigram_coverage >= config.trigram_coverage_threshold
        or candidate.bm25_evidence >= config.bm25_evidence_threshold
    )


def _candidate_reasons(candidate: _Candidate, config: LexicalSearchConfig) -> tuple[str, ...]:
    reasons: list[str] = []
    if candidate.exact_phrase:
        reasons.append("EXACT_PHRASE")
    if candidate.trigram_coverage >= config.trigram_coverage_threshold:
        reasons.append("TRIGRAM_COVERAGE")
    if candidate.bm25_evidence >= config.bm25_evidence_threshold:
        reasons.append("BM25_EVIDENCE")
    if candidate.temporal_rank is not None:
        reasons.append("TEMPORAL_MATCH")
    return tuple(reasons)


class SqliteMemorySearch:
    """A small lexical baseline that never writes the index or canonical source."""

    def __init__(
        self,
        index_path: Path,
        *,
        spec: IndexSpec,
        source: EpisodeSource | None = None,
        config: LexicalSearchConfig | None = None,
    ) -> None:
        self._index_path = index_path
        self._spec = spec
        self._source = source
        self._config = config or LexicalSearchConfig()

    def search(self, request: MemorySearchRequest) -> MemorySearchResponse:
        return self.search_with_diagnostics(request).response

    def search_with_diagnostics(self, request: MemorySearchRequest) -> LexicalSearchDiagnostics:
        try:
            conn = connect_readonly(self._index_path)
        except sqlite3.Error as exc:
            raise MemorySearchError("memory index is unavailable; run kindred memory sync") from exc
        try:
            state = _read_index_state(conn, self._spec)
            normalized_query = normalize_text(request.query)
            exact_ids, candidates = self._read_candidates(
                conn,
                normalized_query=normalized_query,
                filters=request.filters,
                candidate_k=self._config.candidate_k,
            )
        except MemorySearchError:
            raise
        except sqlite3.Error as exc:
            raise MemorySearchError("memory index is unavailable; run kindred memory sync") from exc
        finally:
            conn.close()

        query_trigrams = _query_trigrams(normalized_query)
        for candidate in candidates.values():
            candidate.exact_phrase = normalized_query in candidate.normalized_text
            if query_trigrams:
                matches = sum(trigram in candidate.normalized_text for trigram in query_trigrams)
                candidate.trigram_coverage = matches / len(query_trigrams)

        lexical_candidates = sorted(
            candidates.values(),
            key=lambda candidate: (
                _preliminary_score(candidate),
                candidate.significance,
                candidate.occurred_at,
                candidate.source_tick_id,
            ),
            reverse=True,
        )[: self._config.candidate_k]
        eligible = [
            candidate for candidate in lexical_candidates if _is_answerable(candidate, self._config)
        ][: self._config.rerank_k]
        self._assign_temporal_ranks(eligible, request)
        ranked = self._rerank(eligible, request)
        hits, estimated_tokens, budget_exhausted = self._select_hits(ranked, request)
        source_max, source_status, stale, degraded_reasons = self._freshness(state)
        trace_reasons: list[str] = []
        if not eligible:
            trace_reasons.append("NO_RELEVANT_EPISODE")
        if budget_exhausted:
            trace_reasons.append("BUDGET_EXHAUSTED")
        response = MemorySearchResponse(
            results=hits,
            trace=MemorySearchTrace(
                active_channels=("lexical",),
                degraded_reasons=degraded_reasons,
                candidate_k=self._config.candidate_k,
                rerank_k=self._config.rerank_k,
                returned_k=len(hits),
                abstained=not eligible,
                budget_exhausted=budget_exhausted,
                reasons=tuple(trace_reasons),
                estimated_tokens=estimated_tokens,
                index_version=state.index_version,
                indexed_through_tick_id=state.indexed_through_tick_id,
                source_max_episode_tick_id=source_max,
                stale=stale,
                source_status=source_status,
            ),
        )
        return LexicalSearchDiagnostics(
            response=response,
            exact_candidate_tick_ids=exact_ids,
            lexical_candidate_tick_ids=tuple(
                candidate.source_tick_id for candidate in lexical_candidates
            ),
            reranked_tick_ids=tuple(candidate.source_tick_id for candidate in ranked),
        )

    @staticmethod
    def _read_candidates(
        conn: sqlite3.Connection,
        *,
        normalized_query: str,
        filters: MemoryFilters,
        candidate_k: int,
    ) -> tuple[tuple[int, ...], dict[int, _Candidate]]:
        filter_sql, params = _filter_sql(filters)
        params.update(query=normalized_query, candidate_k=candidate_k)
        exact_rows = conn.execute(
            f"SELECT {_DOCUMENT_COLUMNS} FROM memory_document d "
            f"WHERE instr(d.normalized_text, :query) > 0{filter_sql} "
            "ORDER BY d.significance DESC, d.occurred_at DESC, d.source_tick_id DESC "
            "LIMIT :candidate_k",
            params,
        ).fetchall()
        candidates = {row["source_tick_id"]: _candidate_from_row(row) for row in exact_rows}
        exact_ids = tuple(row["source_tick_id"] for row in exact_rows)
        query_trigrams = _query_trigrams(normalized_query)
        if not query_trigrams:
            return exact_ids, candidates

        fts_query = " OR ".join(_fts_literal(trigram) for trigram in query_trigrams)
        fts_params = {**params, "fts_query": fts_query}
        fts_rows = conn.execute(
            f"SELECT {_DOCUMENT_COLUMNS}, bm25(memory_document_fts) AS bm25_score "
            "FROM memory_document_fts "
            "JOIN memory_document d ON d.source_tick_id = memory_document_fts.rowid "
            f"WHERE memory_document_fts MATCH :fts_query{filter_sql} "
            "ORDER BY bm25(memory_document_fts), d.source_tick_id DESC LIMIT :candidate_k",
            fts_params,
        ).fetchall()
        for rank, row in enumerate(fts_rows, start=1):
            candidate = candidates.setdefault(row["source_tick_id"], _candidate_from_row(row))
            candidate.bm25_rank = rank
            candidate.bm25_evidence = max(0.0, -float(row["bm25_score"]))
        return exact_ids, candidates

    @staticmethod
    def _assign_temporal_ranks(candidates: list[_Candidate], request: MemorySearchRequest) -> None:
        if request.time_order == "relevance":
            return
        ordered = sorted(
            candidates,
            key=lambda candidate: (candidate.occurred_at, candidate.source_tick_id),
            reverse=request.time_order == "latest",
        )
        for rank, candidate in enumerate(ordered, start=1):
            candidate.temporal_rank = rank

    def _rerank(
        self, candidates: list[_Candidate], request: MemorySearchRequest
    ) -> list[_Candidate]:
        for candidate in candidates:
            bm25_rank_score = 0.0 if candidate.bm25_rank is None else 1.0 / candidate.bm25_rank
            temporal_fit = 0.0 if candidate.temporal_rank is None else 1.0 / candidate.temporal_rank
            candidate.score = (
                0.60 * candidate.trigram_coverage
                + 0.20 * bm25_rank_score
                + 0.15 * float(candidate.exact_phrase)
                + 0.05 * temporal_fit
            )

        if request.time_order == "relevance":
            ordered = sorted(
                candidates,
                key=lambda candidate: (candidate.occurred_at, candidate.source_tick_id),
                reverse=True,
            )
            ordered.sort(key=lambda candidate: candidate.significance, reverse=True)
        else:
            ordered = sorted(
                candidates,
                key=lambda candidate: (candidate.occurred_at, candidate.source_tick_id),
                reverse=request.time_order == "latest",
            )
            ordered.sort(key=lambda candidate: candidate.temporal_rank or len(candidates) + 1)
        ordered.sort(key=lambda candidate: candidate.score, reverse=True)
        return ordered

    def _select_hits(
        self, candidates: list[_Candidate], request: MemorySearchRequest
    ) -> tuple[tuple[MemoryHit, ...], int, bool]:
        hits: list[MemoryHit] = []
        estimated_tokens = 0
        budget_exhausted = False
        for candidate in candidates[: request.top_k]:
            remaining = request.token_budget - estimated_tokens
            text_budget = remaining - self._config.hit_overhead_tokens
            if text_budget < 1:
                budget_exhausted = True
                break
            text = candidate.text
            reasons = list(_candidate_reasons(candidate, self._config))
            if len(text) > text_budget:
                text = text[: max(0, text_budget - 1)] + "…"
                reasons.append("TEXT_TRUNCATED")
                budget_exhausted = True
            bm25_rank_score = 0.0 if candidate.bm25_rank is None else 1.0 / candidate.bm25_rank
            temporal_fit = 0.0 if candidate.temporal_rank is None else 1.0 / candidate.temporal_rank
            hits.append(
                MemoryHit(
                    id=candidate.document_id,
                    source_tick_id=candidate.source_tick_id,
                    text=text,
                    occurred_at=candidate.occurred_at,
                    activity=candidate.activity,
                    location=candidate.location,
                    mood_description=candidate.mood_description,
                    significance=candidate.significance,
                    score=candidate.score,
                    score_breakdown={
                        "trigram_coverage": candidate.trigram_coverage,
                        "bm25_evidence": candidate.bm25_evidence,
                        "bm25_rank_score": bm25_rank_score,
                        "exact_phrase": float(candidate.exact_phrase),
                        "temporal_fit": temporal_fit,
                    },
                    reasons=tuple(reasons),
                )
            )
            estimated_tokens += self._config.hit_overhead_tokens + len(text)
            if budget_exhausted:
                break
        if len(hits) < min(request.top_k, len(candidates)):
            budget_exhausted = True
        return tuple(hits), estimated_tokens, budget_exhausted

    def _freshness(
        self, state: _IndexState
    ) -> tuple[int | None, SourceStatus, bool | None, tuple[str, ...]]:
        if self._source is None:
            return None, "unavailable", None, ("SOURCE_UNAVAILABLE",)
        try:
            source_max = self._source.max_tick_id()
        except Exception:
            return None, "unavailable", None, ("SOURCE_UNAVAILABLE",)
        stale = source_max is not None and state.indexed_through_tick_id < source_max
        return source_max, "available", stale, ()


class SqliteVectorMemorySearch:
    """Exact cosine scan over the small, derived Episode vector corpus."""

    def __init__(
        self,
        index_path: Path,
        *,
        spec: IndexSpec,
        embedding_provider: EmbeddingProvider,
        source: EpisodeSource | None = None,
        config: VectorSearchConfig | None = None,
    ) -> None:
        profile = embedding_provider.profile
        if (
            profile.revision != spec.embedding_revision
            or profile.dimension != spec.embedding_dimension
            or profile.normalized != spec.embedding_normalized
        ):
            raise ValueError("embedding provider does not match the index spec")
        if spec.embedding_dimension is None or spec.embedding_normalized is not True:
            raise ValueError("vector search requires a normalized embedding index spec")
        self._index_path = index_path
        self._spec = spec
        self._embedding_provider = embedding_provider
        self._source = source
        self._config = config or VectorSearchConfig()

    def search(self, request: MemorySearchRequest) -> MemorySearchResponse:
        return self.search_with_diagnostics(request).response

    def search_with_diagnostics(self, request: MemorySearchRequest) -> VectorSearchDiagnostics:
        try:
            conn = connect_readonly(self._index_path)
        except sqlite3.Error as exc:
            raise MemorySearchError("memory index is unavailable; run kindred memory sync") from exc
        try:
            state = _read_index_state(conn, self._spec)
            rows = self._read_rows(conn, filters=request.filters)
        except MemorySearchError:
            raise
        except sqlite3.Error as exc:
            raise MemorySearchError("memory index is unavailable; run kindred memory sync") from exc
        finally:
            conn.close()

        candidates: list[_Candidate] = []
        if rows:
            try:
                query_vector = normalize_embedding(
                    self._embedding_provider.embed_query(request.query),
                    dimension=self._embedding_provider.profile.dimension,
                )
                for row in rows:
                    document_vector = unpack_embedding(
                        row["embedding"],
                        dimension=self._embedding_provider.profile.dimension,
                    )
                    candidate = _candidate_from_row(row)
                    candidate.cosine_similarity = sum(
                        left * right
                        for left, right in zip(query_vector, document_vector, strict=True)
                    )
                    candidates.append(candidate)
            except EmbeddingError as exc:
                raise MemorySearchError(
                    "vector embedding is unavailable or invalid; rebuild may be required"
                ) from exc

        vector_candidates = sorted(
            candidates,
            key=lambda candidate: (
                candidate.cosine_similarity,
                candidate.significance,
                candidate.occurred_at,
                candidate.source_tick_id,
            ),
            reverse=True,
        )[: self._config.candidate_k]
        eligible = [
            candidate
            for candidate in vector_candidates
            if candidate.cosine_similarity >= self._config.cosine_threshold
        ][: self._config.rerank_k]
        SqliteMemorySearch._assign_temporal_ranks(eligible, request)
        ranked = self._rerank(eligible, request)
        hits, estimated_tokens, budget_exhausted = self._select_hits(ranked, request)
        source_max, source_status, stale, degraded_reasons = self._freshness(state)
        trace_reasons: list[str] = []
        if not eligible:
            trace_reasons.append("NO_RELEVANT_EPISODE")
        if budget_exhausted:
            trace_reasons.append("BUDGET_EXHAUSTED")
        response = MemorySearchResponse(
            results=hits,
            trace=MemorySearchTrace(
                active_channels=("vector",),
                degraded_reasons=degraded_reasons,
                candidate_k=self._config.candidate_k,
                rerank_k=self._config.rerank_k,
                returned_k=len(hits),
                abstained=not eligible,
                budget_exhausted=budget_exhausted,
                reasons=tuple(trace_reasons),
                estimated_tokens=estimated_tokens,
                index_version=state.index_version,
                indexed_through_tick_id=state.indexed_through_tick_id,
                source_max_episode_tick_id=source_max,
                stale=stale,
                source_status=source_status,
            ),
        )
        return VectorSearchDiagnostics(
            response=response,
            vector_candidate_tick_ids=tuple(
                candidate.source_tick_id for candidate in vector_candidates
            ),
            reranked_tick_ids=tuple(candidate.source_tick_id for candidate in ranked),
        )

    @staticmethod
    def _read_rows(
        conn: sqlite3.Connection,
        *,
        filters: MemoryFilters,
    ) -> list[sqlite3.Row]:
        filter_sql, params = _filter_sql(filters)
        return conn.execute(
            f"SELECT {_DOCUMENT_COLUMNS}, d.embedding FROM memory_document d "
            f"WHERE d.embedding IS NOT NULL{filter_sql} ORDER BY d.source_tick_id",
            params,
        ).fetchall()

    @staticmethod
    def _rerank(candidates: list[_Candidate], request: MemorySearchRequest) -> list[_Candidate]:
        for candidate in candidates:
            temporal_fit = 0.0 if candidate.temporal_rank is None else 1.0 / candidate.temporal_rank
            candidate.score = 0.95 * candidate.cosine_similarity + 0.05 * temporal_fit
        return sorted(
            candidates,
            key=lambda candidate: (
                candidate.score,
                candidate.significance,
                candidate.occurred_at,
                candidate.source_tick_id,
            ),
            reverse=True,
        )

    def _select_hits(
        self,
        candidates: list[_Candidate],
        request: MemorySearchRequest,
    ) -> tuple[tuple[MemoryHit, ...], int, bool]:
        hits: list[MemoryHit] = []
        estimated_tokens = 0
        budget_exhausted = False
        for candidate in candidates[: request.top_k]:
            remaining = request.token_budget - estimated_tokens
            text_budget = remaining - self._config.hit_overhead_tokens
            if text_budget < 1:
                budget_exhausted = True
                break
            text = candidate.text
            reasons = ["VECTOR_SIMILARITY"]
            if candidate.temporal_rank is not None:
                reasons.append("TEMPORAL_MATCH")
            if len(text) > text_budget:
                text = text[: max(0, text_budget - 1)] + "…"
                reasons.append("TEXT_TRUNCATED")
                budget_exhausted = True
            temporal_fit = 0.0 if candidate.temporal_rank is None else 1.0 / candidate.temporal_rank
            hits.append(
                MemoryHit(
                    id=candidate.document_id,
                    source_tick_id=candidate.source_tick_id,
                    text=text,
                    occurred_at=candidate.occurred_at,
                    activity=candidate.activity,
                    location=candidate.location,
                    mood_description=candidate.mood_description,
                    significance=candidate.significance,
                    score=candidate.score,
                    score_breakdown={
                        "cosine_similarity": candidate.cosine_similarity,
                        "temporal_fit": temporal_fit,
                    },
                    reasons=tuple(reasons),
                )
            )
            estimated_tokens += self._config.hit_overhead_tokens + len(text)
            if budget_exhausted:
                break
        if len(hits) < min(request.top_k, len(candidates)):
            budget_exhausted = True
        return tuple(hits), estimated_tokens, budget_exhausted

    def _freshness(
        self, state: _IndexState
    ) -> tuple[int | None, SourceStatus, bool | None, tuple[str, ...]]:
        if self._source is None:
            return None, "unavailable", None, ("SOURCE_UNAVAILABLE",)
        try:
            source_max = self._source.max_tick_id()
        except Exception:
            return None, "unavailable", None, ("SOURCE_UNAVAILABLE",)
        stale = source_max is not None and state.indexed_through_tick_id < source_max
        return source_max, "available", stale, ()


class SqliteHybridMemorySearch:
    """Fuse eligible lexical and vector candidates with deterministic RRF."""

    def __init__(
        self,
        index_path: Path,
        *,
        spec: IndexSpec,
        embedding_provider: EmbeddingProvider,
        source: EpisodeSource | None = None,
        config: HybridSearchConfig | None = None,
    ) -> None:
        profile = embedding_provider.profile
        if (
            profile.revision != spec.embedding_revision
            or profile.dimension != spec.embedding_dimension
            or profile.normalized != spec.embedding_normalized
        ):
            raise ValueError("embedding provider does not match the index spec")
        if spec.embedding_dimension is None or spec.embedding_normalized is not True:
            raise ValueError("hybrid search requires a normalized embedding index spec")
        self._index_path = index_path
        self._spec = spec
        self._embedding_provider = embedding_provider
        self._source = source
        self._config = config or HybridSearchConfig()
        self._lexical_config = LexicalSearchConfig(
            candidate_k=self._config.candidate_k,
            rerank_k=self._config.rerank_k,
            trigram_coverage_threshold=self._config.trigram_coverage_threshold,
            bm25_evidence_threshold=self._config.bm25_evidence_threshold,
            hit_overhead_tokens=self._config.hit_overhead_tokens,
        )

    def search(self, request: MemorySearchRequest) -> MemorySearchResponse:
        return self.search_with_diagnostics(request).response

    def search_with_diagnostics(self, request: MemorySearchRequest) -> HybridSearchDiagnostics:
        try:
            conn = connect_readonly(self._index_path)
        except sqlite3.Error as exc:
            raise MemorySearchError("memory index is unavailable; run kindred memory sync") from exc
        try:
            state = _read_index_state(conn, self._spec)
            normalized_query = normalize_text(request.query)
            _, lexical_by_id = SqliteMemorySearch._read_candidates(
                conn,
                normalized_query=normalized_query,
                filters=request.filters,
                candidate_k=self._config.candidate_k,
            )
            vector_rows = SqliteVectorMemorySearch._read_rows(conn, filters=request.filters)
        except MemorySearchError:
            raise
        except sqlite3.Error as exc:
            raise MemorySearchError("memory index is unavailable; run kindred memory sync") from exc
        finally:
            conn.close()

        query_trigrams = _query_trigrams(normalized_query)
        for candidate in lexical_by_id.values():
            candidate.exact_phrase = normalized_query in candidate.normalized_text
            if query_trigrams:
                matches = sum(trigram in candidate.normalized_text for trigram in query_trigrams)
                candidate.trigram_coverage = matches / len(query_trigrams)
        lexical_candidates = sorted(
            lexical_by_id.values(),
            key=lambda candidate: (
                _preliminary_score(candidate),
                candidate.significance,
                candidate.occurred_at,
                candidate.source_tick_id,
            ),
            reverse=True,
        )[: self._config.candidate_k]
        # Keep BM25-only matches in the lexical baseline, but do not let that
        # broad evidence alone admit another candidate into the hybrid union.
        lexical_eligible = [
            candidate
            for candidate in lexical_candidates
            if candidate.exact_phrase
            or candidate.trigram_coverage >= self._lexical_config.trigram_coverage_threshold
        ]
        vector_candidates: list[_Candidate] = []
        if vector_rows:
            try:
                query_vector = normalize_embedding(
                    self._embedding_provider.embed_query(request.query),
                    dimension=self._embedding_provider.profile.dimension,
                )
                for row in vector_rows:
                    document_vector = unpack_embedding(
                        row["embedding"],
                        dimension=self._embedding_provider.profile.dimension,
                    )
                    candidate = _candidate_from_row(row)
                    candidate.cosine_similarity = sum(
                        left * right
                        for left, right in zip(query_vector, document_vector, strict=True)
                    )
                    vector_candidates.append(candidate)
            except EmbeddingError as exc:
                raise MemorySearchError(
                    "vector embedding is unavailable or invalid; rebuild may be required"
                ) from exc
        vector_candidates = sorted(
            vector_candidates,
            key=lambda candidate: (
                candidate.cosine_similarity,
                candidate.significance,
                candidate.occurred_at,
                candidate.source_tick_id,
            ),
            reverse=True,
        )[: self._config.candidate_k]
        vector_eligible = [
            candidate
            for candidate in vector_candidates
            if candidate.cosine_similarity >= self._config.cosine_threshold
        ]

        fused: dict[int, _Candidate] = {}
        vector_by_id = {candidate.source_tick_id: candidate for candidate in vector_candidates}
        lexical_candidate_by_id = {
            candidate.source_tick_id: candidate for candidate in lexical_candidates
        }
        for rank, candidate in enumerate(lexical_eligible, start=1):
            candidate.lexical_rank = rank
            if vector_candidate := vector_by_id.get(candidate.source_tick_id):
                candidate.cosine_similarity = vector_candidate.cosine_similarity
            fused[candidate.source_tick_id] = candidate
        for rank, candidate in enumerate(vector_eligible, start=1):
            candidate.vector_rank = rank
            existing = fused.get(candidate.source_tick_id) or lexical_candidate_by_id.get(
                candidate.source_tick_id
            )
            if existing is None:
                existing = candidate
            else:
                existing.vector_rank = rank
                existing.cosine_similarity = candidate.cosine_similarity
            fused[candidate.source_tick_id] = existing

        # Only rescue an otherwise empty result when both raw top-k channels
        # independently provide moderate evidence for the same Episode.
        if not fused:
            vector_ranks = {
                candidate.source_tick_id: rank
                for rank, candidate in enumerate(vector_candidates, start=1)
            }
            for lexical_rank, candidate in enumerate(lexical_candidates, start=1):
                vector_candidate = vector_by_id.get(candidate.source_tick_id)
                if (
                    candidate.bm25_evidence >= self._config.fallback_bm25_threshold
                    and vector_candidate is not None
                    and vector_candidate.cosine_similarity >= self._config.fallback_cosine_threshold
                ):
                    candidate.lexical_rank = lexical_rank
                    candidate.vector_rank = vector_ranks[candidate.source_tick_id]
                    candidate.cosine_similarity = vector_candidate.cosine_similarity
                    candidate.cross_channel_support = True
                    fused[candidate.source_tick_id] = candidate

        candidates = list(fused.values())
        SqliteMemorySearch._assign_temporal_ranks(candidates, request)
        ranked = self._rerank(candidates, request)[: self._config.rerank_k]
        rrf_ranked = sorted(
            candidates,
            key=lambda candidate: (
                candidate.rrf_score,
                candidate.significance,
                candidate.occurred_at,
                candidate.source_tick_id,
            ),
            reverse=True,
        )[: self._config.rerank_k]
        hits, estimated_tokens, budget_exhausted = self._select_hits(ranked, request)
        source_max, source_status, stale, degraded_reasons = self._freshness(state)
        trace_reasons: list[str] = []
        if not candidates:
            trace_reasons.append("NO_RELEVANT_EPISODE")
        if budget_exhausted:
            trace_reasons.append("BUDGET_EXHAUSTED")
        response = MemorySearchResponse(
            results=hits,
            trace=MemorySearchTrace(
                active_channels=("lexical", "vector"),
                degraded_reasons=degraded_reasons,
                candidate_k=self._config.candidate_k,
                rerank_k=self._config.rerank_k,
                returned_k=len(hits),
                abstained=not candidates,
                budget_exhausted=budget_exhausted,
                reasons=tuple(trace_reasons),
                estimated_tokens=estimated_tokens,
                index_version=state.index_version,
                indexed_through_tick_id=state.indexed_through_tick_id,
                source_max_episode_tick_id=source_max,
                stale=stale,
                source_status=source_status,
            ),
        )
        return HybridSearchDiagnostics(
            response=response,
            lexical_candidate_tick_ids=tuple(
                candidate.source_tick_id for candidate in lexical_candidates
            ),
            vector_candidate_tick_ids=tuple(
                candidate.source_tick_id for candidate in vector_candidates
            ),
            rrf_ranked_tick_ids=tuple(candidate.source_tick_id for candidate in rrf_ranked),
            reranked_tick_ids=tuple(candidate.source_tick_id for candidate in ranked),
        )

    def _rerank(
        self, candidates: list[_Candidate], request: MemorySearchRequest
    ) -> list[_Candidate]:
        theoretical_max = 2.0 / (self._config.rrf_k + 1)
        for candidate in candidates:
            lexical_rrf = (
                0.0
                if candidate.lexical_rank is None
                else 1.0 / (self._config.rrf_k + candidate.lexical_rank)
            )
            vector_rrf = (
                0.0
                if candidate.vector_rank is None
                else 1.0 / (self._config.rrf_k + candidate.vector_rank)
            )
            candidate.rrf_score = lexical_rrf + vector_rrf
            candidate.normalized_rrf = candidate.rrf_score / theoretical_max
            temporal_fit = 0.0 if candidate.temporal_rank is None else 1.0 / candidate.temporal_rank
            candidate.score = (
                0.85 * candidate.normalized_rrf
                + 0.10 * float(candidate.exact_phrase)
                + 0.05 * temporal_fit
            )

        if request.time_order == "relevance":
            return sorted(
                candidates,
                key=lambda candidate: (
                    candidate.score,
                    candidate.significance,
                    candidate.occurred_at,
                    candidate.source_tick_id,
                ),
                reverse=True,
            )
        ordered = sorted(
            candidates,
            key=lambda candidate: (candidate.occurred_at, candidate.source_tick_id),
            reverse=request.time_order == "latest",
        )
        ordered.sort(key=lambda candidate: candidate.temporal_rank or len(candidates) + 1)
        ordered.sort(key=lambda candidate: candidate.score, reverse=True)
        return ordered

    def _select_hits(
        self, candidates: list[_Candidate], request: MemorySearchRequest
    ) -> tuple[tuple[MemoryHit, ...], int, bool]:
        hits: list[MemoryHit] = []
        estimated_tokens = 0
        budget_exhausted = False
        for candidate in candidates[: request.top_k]:
            remaining = request.token_budget - estimated_tokens
            text_budget = remaining - self._config.hit_overhead_tokens
            if text_budget < 1:
                budget_exhausted = True
                break
            text = candidate.text
            reasons = list(_candidate_reasons(candidate, self._lexical_config))
            if candidate.cosine_similarity >= self._config.cosine_threshold:
                reasons.append("VECTOR_SIMILARITY")
            if candidate.cross_channel_support:
                reasons.append("CROSS_CHANNEL_SUPPORT")
            reasons.append("RRF_FUSION")
            if len(text) > text_budget:
                text = text[: max(0, text_budget - 1)] + "…"
                reasons.append("TEXT_TRUNCATED")
                budget_exhausted = True
            lexical_rrf = (
                0.0
                if candidate.lexical_rank is None
                else 1.0 / (self._config.rrf_k + candidate.lexical_rank)
            )
            vector_rrf = (
                0.0
                if candidate.vector_rank is None
                else 1.0 / (self._config.rrf_k + candidate.vector_rank)
            )
            temporal_fit = 0.0 if candidate.temporal_rank is None else 1.0 / candidate.temporal_rank
            hits.append(
                MemoryHit(
                    id=candidate.document_id,
                    source_tick_id=candidate.source_tick_id,
                    text=text,
                    occurred_at=candidate.occurred_at,
                    activity=candidate.activity,
                    location=candidate.location,
                    mood_description=candidate.mood_description,
                    significance=candidate.significance,
                    score=candidate.score,
                    score_breakdown={
                        "rrf_score": candidate.rrf_score,
                        "normalized_rrf": candidate.normalized_rrf,
                        "lexical_rrf": lexical_rrf,
                        "vector_rrf": vector_rrf,
                        "trigram_coverage": candidate.trigram_coverage,
                        "bm25_evidence": candidate.bm25_evidence,
                        "cosine_similarity": candidate.cosine_similarity,
                        "exact_phrase": float(candidate.exact_phrase),
                        "temporal_fit": temporal_fit,
                    },
                    reasons=tuple(reasons),
                )
            )
            estimated_tokens += self._config.hit_overhead_tokens + len(text)
            if budget_exhausted:
                break
        if len(hits) < min(request.top_k, len(candidates)):
            budget_exhausted = True
        return tuple(hits), estimated_tokens, budget_exhausted

    def _freshness(
        self, state: _IndexState
    ) -> tuple[int | None, SourceStatus, bool | None, tuple[str, ...]]:
        if self._source is None:
            return None, "unavailable", None, ("SOURCE_UNAVAILABLE",)
        try:
            source_max = self._source.max_tick_id()
        except Exception:
            return None, "unavailable", None, ("SOURCE_UNAVAILABLE",)
        stale = source_max is not None and state.indexed_through_tick_id < source_max
        return source_max, "available", stale, ()


__all__ = [
    "HybridSearchConfig",
    "HybridSearchDiagnostics",
    "LexicalSearchConfig",
    "LexicalSearchDiagnostics",
    "MemorySearchError",
    "SqliteHybridMemorySearch",
    "SqliteMemorySearch",
    "SqliteVectorMemorySearch",
    "VectorSearchConfig",
    "VectorSearchDiagnostics",
]
