"""``watcher_cursor`` 表读写——「心已读书签」持久化（earlier milestone 建表 / N-4）。

cursor = 「心已读到哪条」的单一书签。**路 X / earlier milestone**：运行期唯一写者是
``sense_io``（tick 内读完 cursor 后消息即推进表）；watcher 只读表 cursor 判是否
唤醒，仅 ``prime()`` 首启时写一次 baseline。落库意义 = daemon 崩溃重启后从书签
位置继续，不从头重放历史：

- sense_io 读完非空消息 ``set`` 推进书签到本页末行。
- 重启时 ``get`` 恢复——库有书签从该位置继续；库无书签（首次启动）则
watcher.prime 设到表尾 baseline（不重放历史，保 N-1 契约）。

游标三元组 ``(ts_ms, seq, id)`` 与 :class:`MessageCursor` 一致；``seq`` 可能是
归一哨兵 ``-1``（同毫秒无 seq 时靠 id tiebreak）。一个 ``session_key`` 一行（PK）。

═══ 风格 ═══

与 ``messages.py`` / ``thoughts.py`` 一致——函数式 + conn 显式传入：

- 读函数直接 ``conn.execute`` 返回（只读 SELECT 不需事务）
- 写函数分 ``_set_impl``（无事务，调用方提供边界）+ thin wrapper（``with conn:``）
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def get(conn: sqlite3.Connection, *, session_key: str) -> tuple[int, int, int] | None:
    """读某 session 的「心已读书签」 ``(ts_ms, seq, id)``；未持久化返 ``None``。"""
    row = conn.execute(
        "SELECT ts_ms, seq, id FROM watcher_cursor WHERE session_key = ?",
        (session_key,),
    ).fetchone()
    if not row:
        return None
    return (int(row[0]), int(row[1]), int(row[2]))


def _set_impl(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    ts_ms: int,
    seq: int,
    id: int,
) -> None:
    """持久化「心已读书签」（无事务，调用方提供边界）。

    一个 ``session_key`` 一行：``INSERT ... ON CONFLICT`` 覆盖更新。游标单调
    前进由写者侧保证（路 X：sense_io 读完只推到本页末行，单调增大），本层不校验单调。
    """
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO watcher_cursor (session_key, ts_ms, seq, id, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(session_key) DO UPDATE SET "
        "ts_ms = excluded.ts_ms, seq = excluded.seq, id = excluded.id, "
        "updated_at = excluded.updated_at",
        (session_key, ts_ms, seq, id, now_iso),
    )


def set_cursor(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    ts_ms: int,
    seq: int,
    id: int,
) -> None:
    """持久化「心已读书签」（含事务的 thin wrapper）。"""
    with conn:
        _set_impl(conn, session_key=session_key, ts_ms=ts_ms, seq=seq, id=id)
