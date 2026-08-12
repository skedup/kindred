"""HistorySync: pull main-session chat.history into the messages table (earlier milestone).

The heart daemon calls :meth:`HistorySync.sync_once` right before each watcher
poll. It pulls the session's recent ``chat.history`` over the Gateway WS,
converts each entry via :func:`build_from_gateway` (toolResult skip / role map /
text_summary compaction),
and upserts into ``main_session_messages`` (idempotent ``INSERT OR IGNORE``).
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
from typing import TYPE_CHECKING

from kindred.db import build_from_gateway

if TYPE_CHECKING:
    from kindred.adapters.openclaw.gateway import GatewayClient
    from kindred.db import KindredDB
    from kindred.openclaw import OpenClawWire

logger = logging.getLogger(__name__)

# Per-poll pull size. The watcher is an incremental cursor stream; re-pulled old
# rows are IGNORE'd by upsert, so this only needs to exceed the messages that
# can arrive between two ~1s polls. 30 is ample (first-party predecessor default).
_HISTORY_LIMIT = 30


class HistorySync:
    """Pulls chat.history → upserts into the messages table, failure-safe."""

    def __init__(
        self,
        client: GatewayClient,
        db: KindredDB,
        *,
        wire: OpenClawWire,
        limit: int = _HISTORY_LIMIT,
    ) -> None:
        self._client = client
        self._db = db
        self._wire = wire
        self._limit = limit

    def sync_once(self) -> int:
        """Pull once and upsert; return rows newly inserted (0 on any failure).

        Never raises — every failure path (network error, malformed payload,
        db error) is logged and returns 0 so the caller's heartbeat continues.
        """
        try:
            result = self._client.fetch_chat_history(
                self._wire.transcript_session, limit=self._limit
            )
        except Exception as exc:  # pragma: no cover - defensive; client returns dict
            logger.warning("history sync: fetch raised error_type=%s", type(exc).__name__)
            return 0

        if "error" in result:
            logger.warning("history sync: gateway request failed")
            return 0
        if (
            result.get("sessionKey") != self._wire.transcript_session
            or result.get("sessionInfoKey") != self._wire.transcript_session
        ):
            logger.warning("history sync: canonical session identity mismatch")
            return 0

        raw_messages = result.get("messages") or []
        cached_at = _now_iso()
        try:
            built = [
                msg
                for raw in raw_messages
                if isinstance(raw, dict)
                and (
                    msg := build_from_gateway(
                        raw,
                        session_key=self._wire.transcript_session,
                        cached_at=cached_at,
                    )
                )
                is not None
            ]
        except Exception as exc:  # pragma: no cover - build is defensive already
            logger.warning("history sync: build failed error_type=%s", type(exc).__name__)
            return 0

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
