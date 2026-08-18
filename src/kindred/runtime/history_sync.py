"""HistorySync: pull a normalized Mouth transcript into the messages table.

The heart daemon calls :meth:`HistorySync.sync_once` right before each watcher
poll. The configured host source validates and normalizes its transcript; this
module adds the local cache timestamp and upserts into ``main_session_messages``
(idempotent ``INSERT OR IGNORE``).
The watcher then polls the now-populated table.

Why this exists separate from the daemon: single responsibility + isolated
failure containment. The **most critical** rule (MEMORY R3): a pull failure must
never crash the heartbeat. ``sync_once`` swallows every error into a logged
no-op so the daemon keeps breathing and the watcher keeps polling whatever the
table already holds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kindred.db import MainSessionMessage
from kindred.mouth_host.runtime import TranscriptPullError, TranscriptSource

if TYPE_CHECKING:
    from kindred.db import KindredDB

logger = logging.getLogger(__name__)


class HistorySync:
    """Pulls normalized transcript messages into the local table, failure-safe."""

    def __init__(
        self,
        source: TranscriptSource | Any,
        db: KindredDB,
        *,
        wire: Any | None = None,
        limit: int = 30,
    ) -> None:
        if wire is not None:
            # Protected public constructor; production composition injects a source.
            from kindred.openclaw.runtime import OpenClawTranscriptSource

            source = OpenClawTranscriptSource(source, wire, limit=limit)  # type: ignore[arg-type]
        self._source = source
        self._db = db

    def sync_once(self) -> int:
        """Pull once and upsert; return rows newly inserted (0 on any failure).

        Never raises — every failure path (network error, malformed payload,
        db error) is logged and returns 0 so the caller's heartbeat continues.
        """
        try:
            batch = self._source.pull()
        except TranscriptPullError as exc:
            logger.warning("history sync: pull failed reason=%s", exc.reason)
            return 0
        except Exception as exc:
            logger.warning("history sync: pull failed error_type=%s", type(exc).__name__)
            return 0
        cached_at = _now_iso()
        built = [
            MainSessionMessage(
                id=None,
                session_key=batch.identity,
                msg_id=message.msg_id,
                seq=message.seq,
                role=message.role,
                content=None,
                text_summary=message.text_summary,
                ts_ms=message.ts_ms,
                cached_at=cached_at,
            )
            for message in batch.messages
        ]

        if not built:
            return 0

        try:
            with self._db.transaction():
                inserted = self._db.upsert_messages(built)
        except Exception as exc:
            logger.warning("history sync: upsert failed error_type=%s", type(exc).__name__)
            return 0

        if inserted:
            logger.info("history sync: %d new message(s) into table", inserted)
        return inserted


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
