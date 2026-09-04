"""Host-neutral contracts for Kindred's optional episode memory source."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from kindred.memory.normalization import normalize_text

TimeOrder = Literal["relevance", "earliest", "latest"]
SourceStatus = Literal["available", "unavailable"]


def _require_nonblank(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


@dataclass(frozen=True, slots=True)
class EpisodeDocument:
    """A deterministic projection of one canonical high-significance tick."""

    id: str
    source_tick_id: int
    text: str
    occurred_at: str
    activity: str
    activity_description: str | None
    location: str
    location_city: str | None
    location_address: str | None
    mood_description: str
    significance: int

    def __post_init__(self) -> None:
        if isinstance(self.source_tick_id, bool) or self.source_tick_id < 1:
            raise ValueError("source_tick_id must be positive")
        if self.id != f"episode:{self.source_tick_id}":
            raise ValueError("id must equal episode:<source_tick_id>")
        if isinstance(self.significance, bool) or not 7 <= self.significance <= 10:
            raise ValueError("episode significance must be between 7 and 10")
        for field_name in ("text", "occurred_at", "activity", "location", "mood_description"):
            _require_nonblank(getattr(self, field_name), field_name)


class EpisodeSource(Protocol):
    """Read-only source contract consumed by the explicit indexer."""

    def read_after(self, after_tick_id: int, *, limit: int = 500) -> tuple[EpisodeDocument, ...]:
        """Return episodes in ascending canonical tick order."""

    def max_tick_id(self) -> int | None:
        """Return the latest canonical episode tick id, or ``None`` for an empty corpus."""


@dataclass(frozen=True, slots=True)
class MemoryFilters:
    occurred_after: str | None = None
    occurred_before: str | None = None
    activity: str | None = None
    location: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("occurred_after", "occurred_before", "activity", "location"):
            value = getattr(self, field_name)
            if value is not None:
                _require_nonblank(value, field_name)


@dataclass(frozen=True, slots=True)
class MemorySearchRequest:
    query: str
    top_k: int = 3
    token_budget: int = 800
    time_order: TimeOrder = "relevance"
    filters: MemoryFilters = field(default_factory=MemoryFilters)

    def __post_init__(self) -> None:
        normalized_query = normalize_text(self.query)
        if not 1 <= len(normalized_query) <= 512:
            raise ValueError("normalized query must contain 1..512 Unicode characters")
        if isinstance(self.top_k, bool) or not 1 <= self.top_k <= 5:
            raise ValueError("top_k must be between 1 and 5")
        if isinstance(self.token_budget, bool) or not 128 <= self.token_budget <= 1200:
            raise ValueError("token_budget must be between 128 and 1200")
        if self.time_order not in ("relevance", "earliest", "latest"):
            raise ValueError("unsupported time_order")


@dataclass(frozen=True, slots=True)
class MemoryHit:
    id: str
    source_tick_id: int
    text: str
    occurred_at: str
    activity: str | None
    location: str | None
    mood_description: str | None
    significance: int
    score: float
    score_breakdown: Mapping[str, float]
    reasons: tuple[str, ...]
    source: Literal["kindred_episode"] = "kindred_episode"


@dataclass(frozen=True, slots=True)
class MemorySearchTrace:
    active_channels: tuple[str, ...]
    degraded_reasons: tuple[str, ...]
    candidate_k: int
    rerank_k: int
    returned_k: int
    abstained: bool
    budget_exhausted: bool
    reasons: tuple[str, ...]
    estimated_tokens: int
    index_version: str
    indexed_through_tick_id: int
    source_max_episode_tick_id: int | None
    stale: bool | None
    source_status: SourceStatus
    budget_kind: Literal["estimated_tokens"] = "estimated_tokens"


@dataclass(frozen=True, slots=True)
class MemorySearchResponse:
    results: tuple[MemoryHit, ...]
    trace: MemorySearchTrace


class MemorySearch(Protocol):
    """The only contract a Mouth Host adapter needs from the retrieval core."""

    def search(self, request: MemorySearchRequest) -> MemorySearchResponse:
        """Search the derived episode index without mutating canonical state."""


__all__ = [
    "EpisodeDocument",
    "EpisodeSource",
    "MemoryFilters",
    "MemoryHit",
    "MemorySearch",
    "MemorySearchRequest",
    "MemorySearchResponse",
    "MemorySearchTrace",
    "SourceStatus",
    "TimeOrder",
]
