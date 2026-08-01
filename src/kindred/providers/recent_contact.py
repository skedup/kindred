"""近期联系事实的 tick 主动 pull 投影。"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from math import floor
from typing import TYPE_CHECKING, Final, Literal

from kindred.db.messages import ROLE_MY_HEART, ROLE_MY_VOICE, ROLE_PARTNER
from kindred.state.tick import RecentContactContext

if TYPE_CHECKING:
    from kindred.db.facade import KindredDB


RECENT_CONTACT_VISIBILITY_SECONDS: Final[int] = 2 * 60 * 60

_ACTOR_BY_ROLE: Final[dict[str, Literal["partner", "kindred"]]] = {
    ROLE_PARTNER: "partner",
    ROLE_MY_VOICE: "kindred",
    ROLE_MY_HEART: "kindred",
}
_LOG = logging.getLogger(__name__)


class RecentContactProvider:
    """从当前已读消息投影最近一次双方表达，不推进 cursor。"""

    def __init__(self, db: KindredDB, session_key: str) -> None:
        self._db = db
        self._session_key = session_key

    def observe(self, *, now: datetime) -> RecentContactContext:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now 必须包含时区信息")

        try:
            cursor = self._db.get_watcher_cursor(session_key=self._session_key)
        except sqlite3.Error:
            _LOG.warning("recent_contact_available=false visible=false")
            return RecentContactContext(available=False)

        if cursor is None:
            return RecentContactContext(available=True)

        now_ms = floor(now.timestamp() * 1000)
        try:
            expression = self._db.get_latest_visible_contact_expression(
                session_key=self._session_key,
                cursor=cursor,
                now_ms=now_ms,
            )
        except sqlite3.Error:
            _LOG.warning("recent_contact_available=false visible=false")
            return RecentContactContext(available=False)

        if expression is None:
            return RecentContactContext(available=True)

        ts_ms, role = expression
        age_ms = now_ms - ts_ms
        if age_ms >= RECENT_CONTACT_VISIBILITY_SECONDS * 1000:
            return RecentContactContext(available=True)
        return RecentContactContext(
            available=True,
            recent_exchange_age_seconds=age_ms // 1000,
            recent_actor=_ACTOR_BY_ROLE[role],
        )
