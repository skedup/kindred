"""``thought`` 表读写。

接口围绕 09 §3.2 schema：
- ``insert_thoughts(thoughts, source_tick_id)`` —— T3.persist.write_memory 唯一入口
- ``query_active_thoughts(now, limit)`` —— **历史/运维按需查询**未过期 thoughts，**不接进 tick
  热路径**（tick 运行时真相源是 ``state_latest.interior.thoughts``，在 state 跨 tick 滚动）

设计：
- thought 表是**历史镜像/查询层**，保留所有历史、**不物理 DELETE**——过期的留作历史档案
- ``query_active_thoughts`` 用 ``WHERE expire_at IS NULL OR expire_at > :now`` 过滤；其 NULL
  语义只属查询层兼容，**不代表 Layer 4 runtime 有永久念头**（apply_thought_diff 已给所有
  null 念头兜底 TTL，见 graph/tick/_thought_diff.py）
"""

from __future__ import annotations

import sqlite3
from typing import Any

from kindred.db.connection import count
from kindred.state.interior import Thought


def _insert_thoughts_impl(
    conn: sqlite3.Connection,
    *,
    thoughts: list[Thought],
    ts: str,
    source_tick_id: int | None = None,
) -> list[int]:
    """批量写入 thoughts；返回 rowid 列表。

    **事务不在本函数。**与 :func:`kindred.db.ticks._insert_tick_impl` 同处理：
    调用者（thin wrapper / facade）提供事务边界，避免嵌套 ``with conn:``
    在 sqlite3 DEFERRED 模式下隐含 commit。

    参数含义参见 :func:`insert_thoughts` thin wrapper。
    """
    if not thoughts:
        return []

    rowids: list[int] = []
    for t in thoughts:
        cur = conn.execute(
            """
            INSERT INTO thought (
                ts, description, mood_w, expire_at, tag, source_tick_id
            ) VALUES (
                :ts, :description, :mood_w, :expire_at, :tag, :source_tick_id
            )
            """,
            {
                "ts": ts,
                "description": t.description,
                "mood_w": t.mood_w,
                "expire_at": t.expire_at,
                "tag": t.tag,
                "source_tick_id": source_tick_id,
            },
        )
        rowid = cur.lastrowid
        if rowid is None:
            raise RuntimeError("INSERT INTO thought returned no lastrowid")
        rowids.append(rowid)
    return rowids


def insert_thoughts(
    conn: sqlite3.Connection,
    *,
    thoughts: list[Thought],
    ts: str,
    source_tick_id: int | None = None,
) -> list[int]:
    """批量写入 thoughts；返回 rowid 列表。

    本函数是 thin wrapper，在外面提供 ``with conn:`` 事务边界，真实现在
    :func:`_insert_thoughts_impl`。这里保留 backward-compat——已有的
    ``insert_thoughts(conn, ...)`` 调用点不需修改。

    新代码推荐 ``KindredDB.insert_thoughts(thoughts, ts=...)``，事务由
    ``db.transaction()`` 统一控制。

    Parameters
    ----------
    thoughts
        ``Thought`` model 列表（state.interior.thoughts）。
    ts
        统一写入时间戳（一般是 source_tick.ts）。
    source_tick_id
        软关联 tick.id（09 §3.2 不加外键）。
    """
    if not thoughts:
        return []

    # 同事务写入：DEFERRED 模式下必须 ``with conn:`` 才会 COMMIT。
    # 不包会在调用方 close() 时丢掉所有写入（autocommit 陷阱）。
    with conn:
        return _insert_thoughts_impl(
            conn,
            thoughts=thoughts,
            ts=ts,
            source_tick_id=source_tick_id,
        )


def query_active_thoughts(
    conn: sqlite3.Connection,
    *,
    now: str,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """查询未过期的 thoughts（09 §3.2：``expire_at IS NULL OR expire_at > now``）。

    按 ts DESC 排序。返回 dict 列表（含全部 thought 表字段）。
    """
    if limit <= 0:
        return []
    cur = conn.execute(
        """
        SELECT * FROM thought
        WHERE expire_at IS NULL OR expire_at > :now
        ORDER BY ts DESC
        LIMIT :limit
        """,
        {"now": now, "limit": limit},
    )
    return [dict(row) for row in cur.fetchall()]


def count_thoughts(conn: sqlite3.Connection) -> int:
    """thought 总行数（运维 / 测试用）。thin wrapper。"""
    return count(conn, "thought")
