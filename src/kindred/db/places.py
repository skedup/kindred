"""``place_visits`` 表读写 —— PlaceStore 到访事件索引（第四趴 / L3）。

定位（docs/discussions/2026-07-01-location-place-store.md）：

- **tick 表是真相源**；本表是从 ``tick.act_result.location_arrival`` 派生的
  读优化镜像（``episode_recall`` 同款副表模式），冲突时以 tick 为准。
- 写入点归 T3：``_insert_tick_impl`` 在 tick 落库同事务内调
  :func:`derive_place_visit` + :func:`_insert_place_visit_impl`；
  Provider / act 节点不直接写 DB。
- 读取只做 ``place_key`` 批量 exact lookup（:func:`get_visit_stats`），
  不做向量检索、不扫全表——act 上下文准备保持轻量（第四趴 §6）。
- 可重建：:func:`rebuild_place_visits` 用**同一个**派生函数重放 tick 历史，
  在线派生写与离线重建结果一致（第四趴 §8，测试锁住）。

写入触发条件（第四趴 §2，收窄到 L2 的 arrival 事件）：

1. ``act_result.committed == True``；
2. ``act_result.location_arrival`` 存在且带非空 ``place_key`` / ``name`` / ``source``
   （L2 起 act 节点只在真实更新 ``state.location`` 时才记录该事件，且已按计划归一化）。

没有 arrival 事件就不写——即使 ``state.location`` 有地点名（cold start / 手工
state / 停留同地的后续 tick），都不猜测成到访历史。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from kindred.state._base import StrictBase
from kindred.state._types import IsoDatetime, NonBlankStr

_LOG = logging.getLogger(__name__)


# ─── 类型 ───────────────────────────────────────────────────────────


class PlaceVisitParams(StrictBase):
    """一行 ``place_visits`` 的写入参数（派生自 committed 的 arrival 事件）。"""

    place_key: NonBlankStr
    tick_id: int
    arrived_at: IsoDatetime
    name: NonBlankStr
    address: str | None = None
    type: str | None = None
    source: NonBlankStr
    activity_name: str | None = None
    candidate_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class PlaceVisitStats:
    """某 ``place_key`` 的到访聚合（pre-act 本地经验渲染用）。

    从 ``place_visits`` 现查现算（COUNT + MAX），不落聚合表——数据量小，
    先不做 ``places`` 聚合缓存（第四趴开放问题：查询收益验证后再补）。
    """

    visit_count: int
    last_visited_at: str
    last_activity_name: str | None


# ─── SQL ────────────────────────────────────────────────────────────

SQL_INSERT_PLACE_VISIT = """
INSERT INTO place_visits (
    place_key, tick_id, arrived_at,
    name, address, type, source,
    activity_name, candidate_snapshot, created_at
) VALUES (
    :place_key, :tick_id, :arrived_at,
    :name, :address, :type, :source,
    :activity_name, :candidate_snapshot, :created_at
)
"""


# ─── 派生（在线写与离线重建共用的单一真相源）─────────────────────────


def derive_place_visit(
    *,
    tick_id: int,
    act_result: dict[str, Any] | None,
    location: dict[str, Any] | None,
    activity: dict[str, Any] | None,
) -> PlaceVisitParams | None:
    """从一个 tick 的 ``act_result`` + state 层派生到访行；不满足条件返 ``None``。

    输入取自 tick 行（在线时是 ``TickWriteParams``，重建时是历史行 JSON），
    派生逻辑单点——在线派生写与 rebuild 必须走同一函数（第四趴 §8）。

    宽容边界：历史数据里 arrival 事件形状不合法（缺 place_key 等）时返 ``None``
    并记 warning，不 raise——副表派生不该让 tick 写入 / 重建崩掉。
    """
    if not isinstance(act_result, dict) or act_result.get("committed") is not True:
        return None
    arrival = act_result.get("location_arrival")
    if not isinstance(arrival, dict):
        return None

    place_key = _text(arrival.get("place_key"))
    name = _text(arrival.get("name")) or (_text(location.get("name")) if location else "")
    source = _text(arrival.get("source"))
    arrived_at = _text(location.get("arrived_at")) if isinstance(location, dict) else ""
    if not place_key or not name or not source or not arrived_at:
        _LOG.warning(
            "place_visits: tick_id=%s 有 location_arrival 但字段不全"
            "（place_key/name/source/arrived_at 缺失），跳过派生。",
            tick_id,
        )
        return None

    snapshot = arrival.get("candidate_snapshot")
    activity_name = _text(activity.get("name")) if isinstance(activity, dict) else ""
    try:
        return PlaceVisitParams(
            place_key=place_key,
            tick_id=tick_id,
            arrived_at=arrived_at,
            name=name,
            address=_text(arrival.get("address")) or None,
            type=_text(arrival.get("type")) or None,
            source=source,
            activity_name=activity_name or None,
            candidate_snapshot=snapshot if isinstance(snapshot, dict) else None,
        )
    except ValueError as exc:
        _LOG.warning("place_visits: tick_id=%s arrival 派生校验失败，跳过：%s", tick_id, exc)
        return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# ─── 写入（事务由调用方提供）────────────────────────────────────────


def _insert_place_visit_impl(conn: sqlite3.Connection, params: PlaceVisitParams) -> int:
    """INSERT 一行到访；返回 rowid。事务边界由调用方（``_insert_tick_impl`` /
    rebuild）提供，本函数不开事务（与 ``_maybe_insert_episode_recall`` 同款）。

    ``created_at`` 写 ``arrived_at``（**确定性镜像时间**，非行创建墙钟）：本表是
    可重建镜像，rebuild 必须逐字节复现在线写入的行（一致性测试锁住）——写墙钟会让
    同批输入两次派生产出不同行。要查行真实落库时刻回 tick 表（tick_id 软关联）。
    """
    cur = conn.execute(
        SQL_INSERT_PLACE_VISIT,
        {
            "place_key": params.place_key,
            "tick_id": params.tick_id,
            "arrived_at": params.arrived_at,
            "name": params.name,
            "address": params.address,
            "type": params.type,
            "source": params.source,
            "activity_name": params.activity_name,
            "candidate_snapshot": (
                json.dumps(params.candidate_snapshot, ensure_ascii=False)
                if params.candidate_snapshot is not None
                else None
            ),
            "created_at": params.arrived_at,
        },
    )
    rowid = cur.lastrowid
    if rowid is None:  # pragma: no cover - sqlite INSERT 总有 lastrowid
        raise RuntimeError("INSERT INTO place_visits returned no lastrowid")
    return rowid


def maybe_insert_place_visit(
    conn: sqlite3.Connection,
    *,
    tick_id: int,
    act_result: dict[str, Any] | None,
    location: dict[str, Any] | None,
    activity: dict[str, Any] | None,
) -> int | None:
    """tick 落库同事务的副写入口：派生 + INSERT；无到访返 ``None``。

    与 ``_maybe_insert_episode_recall`` 同位：由 ``_insert_tick_impl`` 在
    ``with conn:`` / ``db.transaction()`` 边界内调用。
    """
    params = derive_place_visit(
        tick_id=tick_id,
        act_result=act_result,
        location=location,
        activity=activity,
    )
    if params is None:
        return None
    return _insert_place_visit_impl(conn, params)


# ─── 读取（pre-act exact lookup）────────────────────────────────────


def get_visit_stats(
    conn: sqlite3.Connection,
    place_keys: list[str],
) -> dict[str, PlaceVisitStats]:
    """按 ``place_key`` 批量查到访聚合；没来过的 key 不在返回 dict 里。

    只做 exact lookup（IN 查询 + GROUP BY，走 ``idx_place_visits_key_arrived``），
    第四趴 §6 读取约束：不做相似度检索、不扫全表、不生成摘要。
    """
    keys = [k for k in place_keys if isinstance(k, str) and k]
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    # 「最近一次」用窗口函数按 (arrived_at, tick_id) 显式排序取第一行——tie-break
    # 明确（同 place_key 同 arrived_at 的多次到访按 tick_id 大者算最近），不依赖
    # SQLite bare-column-with-MAX 的隐式取行；一次查询免 N+1（earlier review 建议 3）。
    rows = conn.execute(
        f"""
        SELECT place_key, visit_count, last_visited_at, last_activity_name
        FROM (
            SELECT place_key,
                   COUNT(*) OVER (PARTITION BY place_key)  AS visit_count,
                   arrived_at                              AS last_visited_at,
                   activity_name                           AS last_activity_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY place_key
                       ORDER BY arrived_at DESC, tick_id DESC
                   ) AS recency_rank
            FROM place_visits
            WHERE place_key IN ({placeholders})
        )
        WHERE recency_rank = 1
        """,  # noqa: S608 - placeholders 是按 key 数量生成的 '?'，值全走参数绑定
        keys,
    ).fetchall()
    return {
        row["place_key"]: PlaceVisitStats(
            visit_count=int(row["visit_count"]),
            last_visited_at=str(row["last_visited_at"]),
            last_activity_name=row["last_activity_name"],
        )
        for row in rows
    }


# ─── 重建（第四趴 §8：派生镜像必须可从 tick 历史重建）────────────────


def rebuild_place_visits(conn: sqlite3.Connection) -> int:
    """清空 ``place_visits`` 并重放全部 tick 历史重建；返回重建行数。

    与在线派生共用 :func:`derive_place_visit`——同一批输入下结果一致
    （测试锁住）。事务边界由调用方提供（facade / 测试里包 transaction）。
    """
    conn.execute("DELETE FROM place_visits")
    rows = conn.execute(
        "SELECT id, act_result, location, activity FROM tick ORDER BY id"
    ).fetchall()
    rebuilt = 0
    for row in rows:
        act_result = _json_or_none_loads(row["act_result"])
        location = _json_or_none_loads(row["location"])
        activity = _json_or_none_loads(row["activity"])
        if (
            maybe_insert_place_visit(
                conn,
                tick_id=int(row["id"]),
                act_result=act_result,
                location=location,
                activity=activity,
            )
            is not None
        ):
            rebuilt += 1
    _LOG.info("place_visits rebuilt: %d rows from %d ticks", rebuilt, len(rows))
    return rebuilt


def _json_or_none_loads(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "PlaceVisitParams",
    "PlaceVisitStats",
    "derive_place_visit",
    "get_visit_stats",
    "maybe_insert_place_visit",
    "rebuild_place_visits",
]
