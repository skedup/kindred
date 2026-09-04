"""Explicit, host-neutral orchestration for updating the derived episode index."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from kindred.memory.contracts import EpisodeDocument, EpisodeSource
from kindred.memory.index import IndexState, MemoryIndex

SyncMode = Literal["incremental", "rebuild"]


class MemorySyncError(RuntimeError):
    """The source cannot make safe forward progress during an explicit sync."""


@dataclass(frozen=True, slots=True)
class MemorySyncResult:
    mode: SyncMode
    before: IndexState
    after: IndexState
    indexed_documents: int
    source_batches: int
    source_max_episode_tick_id: int | None

    @property
    def stale(self) -> bool:
        source_max = self.source_max_episode_tick_id
        return source_max is not None and self.after.indexed_through_tick_id < source_max


def _read_documents(
    source: EpisodeSource,
    *,
    after_tick_id: int,
    batch_size: int,
) -> tuple[tuple[EpisodeDocument, ...], int, int]:
    documents: list[EpisodeDocument] = []
    cursor = after_tick_id
    batches = 0
    while True:
        batch = source.read_after(cursor, limit=batch_size)
        if not batch:
            return tuple(documents), cursor, batches
        next_cursor = batch[-1].source_tick_id
        if next_cursor <= cursor:
            raise MemorySyncError("episode source did not advance its tick watermark")
        documents.extend(batch)
        cursor = next_cursor
        batches += 1


def sync_episode_index(
    source: EpisodeSource,
    index: MemoryIndex,
    *,
    rebuild: bool = False,
    batch_size: int = 500,
) -> MemorySyncResult:
    """Read all pending episodes, then update the derived index in one transaction."""

    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= 1000
    ):
        raise ValueError("batch_size must be an integer between 1 and 1000")

    before = index.state()
    start_tick_id = 0 if rebuild else before.indexed_through_tick_id
    documents, final_tick_id, source_batches = _read_documents(
        source,
        after_tick_id=start_tick_id,
        batch_size=batch_size,
    )
    source_max_tick_id = source.max_tick_id()
    if final_tick_id > 0 and (source_max_tick_id is None or source_max_tick_id < final_tick_id):
        raise MemorySyncError("canonical episode watermark moved backwards; rebuild required")

    if rebuild:
        after = index.rebuild(documents, indexed_through_tick_id=final_tick_id)
        mode: SyncMode = "rebuild"
    else:
        after = index.sync(documents, indexed_through_tick_id=final_tick_id)
        mode = "incremental"

    return MemorySyncResult(
        mode=mode,
        before=before,
        after=after,
        indexed_documents=len(documents),
        source_batches=source_batches,
        source_max_episode_tick_id=source_max_tick_id,
    )


__all__ = [
    "MemorySyncError",
    "MemorySyncResult",
    "SyncMode",
    "sync_episode_index",
]
