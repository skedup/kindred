"""``tick`` 表读写。

接口围绕 02 §3.10 schema：

- ``insert_tick(...)`` —— T3.persist.write_state 唯一 INSERT 入口。
  当 ``significance >= 7`` 时**同事务**顺手 INSERT
  ``episode_recall(tick_id, cooldown=100)``（参见 09 §4.4.4.2 / 14 §3.5）。
- ``get_state_latest()`` —— state_latest 视图（T1.sense.io 读 prev_state）。
- ``get_episodes(limit)`` —— episode 视图（significance>=7，09 Layer C）。

设计：

- state 8 层从 ``State`` model dump 成 JSON dict 后逐列灌入。
- act_decision / act_result 接受 dict（None 时存 NULL）。
- 读出时 JSON 列经 ``json.loads`` 还原成 dict；NOT NULL 与 nullable 分开处理。
- 不依赖 sqlite3.Row 的列序——按列名访问。

不在本节范围（graph 节点的事）：

- bundle 渲染。
- 从 ``State`` 直接构造 insert_tick 调用的便捷方法。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from kindred.db._types import TickWriteParams
from kindred.db.artifacts import maybe_insert_artifact_commits
from kindred.db.connection import count
from kindred.db.places import maybe_insert_place_visit
from kindred.state.state import State

# ─── 常量 ───────────────────────────────────────────────────────────


STATE_LAYERS: tuple[str, ...] = (
    "interior",
    "embodiment",
    "bag",
    "activity",
    "location",
    "time",
    "environment",
    "presence",
)
"""state 8 层顺序。与 02 §3.1 字段顺序契约一致。

**公开跨模块常量**：``graph.tick.sense_io`` 从 tick 表整行 result 中 pick 8 层 state
需同一顺序。单一真相源——不重复字面量，import 同一 ``STATE_LAYERS``。

**未来重构**：8 层是 state 概念的字段顺序契约，理论上属 ``state/`` 层而非 ``db/`` 层。
follow-up 小 PR 会迁到 ``state/_layers.py`` 或从 ``State.model_fields`` 推导，反向依赖
（"db 拥有 state schema"）自然消除。

文档漂移记录：02 §3.10.1 示例 SQL 列顺序与本列表不同，已在
``docs/archive/discussions/2026-06/2026-06-02-spec-bug-3.10-column-order.md`` 留指针；
应以本列表（= state.py 运行时 model_fields 顺序）为准，
下一个文档 sync MR 会修齐 02 示例。
"""


_NULLABLE_JSON_COLUMNS: tuple[str, ...] = ("act_decision", "act_result")
"""允许 NULL 的 JSON 列（仅 act_decision / act_result）：
读出时需 isinstance(str) guard。state 各层在 schema 里是 NOT NULL，
应该裸走 json.loads——任何 None 都是 schema 漂移信号，必须立即报。"""

EPISODE_THRESHOLD: int = 7
"""significance >= 7 进 episode 视图（= 09 Layer C 候选池）。与 schema.sql 同步。"""

_STREAM_COLS = "id, ts, note, significance, act_decision"
"""web /stream + /episodes 专用轻量列（不含 act_result / 8 层 state）。review N-3。"""

_INTERIOR_HISTORY_COLS = "id, ts, trigger_source, interior, activity, note, significance"
"""web /interior/history 专用窄列；不读取其余 6 层 state 或 act JSON。"""

_VISUAL_STATE_REQUIRED_COLS = (
    "id",
    "ts",
    *STATE_LAYERS,
    *_NULLABLE_JSON_COLUMNS,
)
"""Visual snapshot 读取 ``state_latest`` 时实际依赖的最小 schema。"""

EPISODE_RECALL_INITIAL_COOLDOWN: int = 100
"""新 episode 插入 episode_recall 时的初始冷却度（09 §4.4.4.2）。"""

EPISODE_RECALL_DECAY: int = 50
"""flush_bundle 选中高光后每次 cooldown 扣减量（09 §4.4.4：100→50→0，闪回约 2 次后耗尽）。"""


# SQL 文本提到模块常量，使 insert_tick 主体看到的是“业务逻辑”而不是 SQL 字串。
SQL_INSERT_TICK = """
INSERT INTO tick (
    ts, trigger_source, triggered_at,
    interior, embodiment, bag, activity,
    location, time, environment, presence,
    note, significance, act_decision, act_result
) VALUES (
    :ts, :trigger_source, :triggered_at,
    :interior, :embodiment, :bag, :activity,
    :location, :time, :environment, :presence,
    :note, :significance, :act_decision, :act_result
)
"""

SQL_INSERT_EPISODE_RECALL = """
INSERT INTO episode_recall (tick_id, cooldown, last_recalled_ts)
VALUES (:tick_id, :cooldown, NULL)
"""


# ─── 私有 helpers ───────────────────────────────────────────────────


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """sqlite3.Row → dict，并把 JSON 列从 str 反序列化。

    NOT NULL 与 nullable 列分开处理（E-6）：
    - state 各层（STATE_LAYERS）是 schema NOT NULL：裸走 json.loads，
      如果某列意外是 None（schema 漂移 / 脏数据）会立即报 TypeError，
      不会静默传到下游 State.model_validate 报个晦涩的 pydantic 错。
    - nullable 列（_NULLABLE_JSON_COLUMNS）：仅在 str 时 loads，允许 None。

    哲学同 “extra=forbid 满天飞”——schema 约束在 Python 读路径上也强校验。
    """
    out: dict[str, Any] = dict(row)
    for col in STATE_LAYERS:
        # NOT NULL：json.loads(None) 会 TypeError，这是好事。
        out[col] = json.loads(out[col])
    for col in _NULLABLE_JSON_COLUMNS:
        if isinstance(out[col], str):
            out[col] = json.loads(out[col])
    return out


# insert_tick 拆出的 3 个私有 helper（让主函数从 100 行缩到 ~20 行）。


def _serialize_state_layers(state: State) -> dict[str, str]:
    """State 转为 {layer_name: json_string}（model_dump_json 一步到位）。"""
    return {layer: getattr(state, layer).model_dump_json() for layer in STATE_LAYERS}


def _maybe_insert_episode_recall(
    conn: sqlite3.Connection, tick_id: int, significance: int | None
) -> None:
    """sig 达阈时同事务 INSERT episode_recall。

    sig=None / sig<阈值 都不写；sig 传入是调用方责任。本 helper 不开事务——
    调用处 ``with conn:`` 保证与主 INSERT 同事务。
    """
    if significance is None or significance < EPISODE_THRESHOLD:
        return
    conn.execute(
        SQL_INSERT_EPISODE_RECALL,
        {"tick_id": tick_id, "cooldown": EPISODE_RECALL_INITIAL_COOLDOWN},
    )


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    """nullable JSON 列的统一序列化路径。"""
    return json.dumps(value, ensure_ascii=False) if value is not None else None


# ─── 写入 ───────────────────────────────────────────────────────────


def _insert_tick_impl(conn: sqlite3.Connection, params: TickWriteParams) -> int:
    """写入一行 tick + （可选）episode_recall 副表；返回 rowid。

    **事务则不在本函数。**调用者需自行给定事务边界：

    - thin wrapper :func:`insert_tick` 在外面套 ``with conn:``
    - facade ``KindredDB.insert_tick`` 在 ``db.transaction()`` 上下文里调用

    这样避免“嵌套 with conn:”的隐含问题——sqlite3 原生不支持嵌套
    事务，嵌套 ``with conn:`` 的内层 ``__exit__`` 会提前 commit/rollback，
    会造成后续写入被错误上下文覆盖。

    当 ``params.significance >= 7`` 时顺手 INSERT
    ``episode_recall(tick_id, cooldown=100)``——避免“tick 写了但 episode_recall
    没写”的半状态。两表中任一 INSERT 失败都会被调用者的事务边界 rollback。

    Returns
    -------
    int
        新 tick 的 ``id``。T3.persist.write_memory 会拿这个 id 作为
        ``thought.source_tick_id``。
    """
    # 校验已在 TickWriteParams 实例化时完成（fail-fast in ingress）。
    layer_jsons = _serialize_state_layers(params.state)

    cur = conn.execute(
        SQL_INSERT_TICK,
        {
            "ts": params.state.time.iso,
            "trigger_source": params.trigger_source,
            "triggered_at": params.triggered_at,
            **layer_jsons,
            "note": params.note,
            "significance": params.significance,
            "act_decision": _json_or_none(params.act_decision),
            "act_result": _json_or_none(params.act_result),
        },
    )
    rowid = cur.lastrowid
    if rowid is None:
        raise RuntimeError("INSERT INTO tick returned no lastrowid")

    # 同事务副写：sig >= 阈值 → episode_recall。
    _maybe_insert_episode_recall(conn, rowid, params.significance)

    # 同事务副写：committed 的 location_arrival → place_visits（PlaceStore，L3）。
    # 与 episode_recall 同款副表模式：任一 INSERT 失败整体 rollback，不留半状态。
    maybe_insert_place_visit(
        conn,
        tick_id=rowid,
        act_result=params.act_result,
        location=params.state.location.model_dump(),
        activity=params.state.activity.model_dump(),
    )

    # 同事务副写：本 tick committed Artifact → artifact_commit Host 读模型。
    maybe_insert_artifact_commits(
        conn,
        tick_id=rowid,
        created_at=params.state.time.iso,
        activity=params.state.activity.model_dump(),
        act_result=params.act_result,
    )

    return rowid


def insert_tick(
    conn: sqlite3.Connection,
    *,
    state: State,
    trigger_source: str,
    triggered_at: str,
    note: str | None = None,
    significance: int | None = None,
    act_decision: dict[str, Any] | None = None,
    act_result: dict[str, Any] | None = None,
) -> int:
    """写入一行 tick；返回 rowid。

    这是 thin wrapper——实际实现在 :func:`_insert_tick_impl`。本函数仅为
    backward-compat，且负责提供事务边界（``with conn:``）——事务不由
    ``_insert_tick_impl`` 自身提供，避免与 facade ``KindredDB.transaction()``
    冲突（嵌套 ``with conn:`` 会被 sqlite3 隐含 commit，造成后续写入被错误
    上下文覆盖）。

    - 已有的过程式测试 / 外部调用点不需修改
    - 新代码（graph 节点 / cli）推荐直接使用
      ``KindredDB.insert_tick(params)``——事务边界由 ``db.transaction()`` 统一控制。

    参数详见 :class:`kindred.db._types.TickWriteParams`。
    """
    params = TickWriteParams(
        state=state,
        trigger_source=trigger_source,  # type: ignore[arg-type]
        triggered_at=triggered_at,
        note=note,
        significance=significance,
        act_decision=act_decision,
        act_result=act_result,
    )
    with conn:
        return _insert_tick_impl(conn, params)


# ─── 读取 ───────────────────────────────────────────────────────────


def get_state_latest(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """从 ``state_latest`` 视图读取最新一行（14 §3.1 T1.sense.io 入口）。

    Returns
    -------
    dict 或 None（库为空时）
        含 8 层 state JSON 列已反序列化为 dict。
    """
    row = conn.execute("SELECT * FROM state_latest").fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def validate_visual_state_schema(conn: sqlite3.Connection) -> None:
    """Validate only the schema invariants required by the visual snapshot.

    ``schema_version`` proves that the migration marker was written, but a
    read-only observer must still fail closed if its view is missing columns.
    ``state_latest`` remains the read authority; re-checking its result against
    ``MAX(tick.id)`` on every request would duplicate that view's responsibility
    and allow concurrent commits to create a false mismatch.
    """
    columns = ", ".join(_VISUAL_STATE_REQUIRED_COLS)
    conn.execute(f"SELECT {columns} FROM state_latest LIMIT 0")  # noqa: S608


def get_motion_instance_start_id(
    conn: sqlite3.Connection,
    *,
    latest_tick_id: int,
) -> int | None:
    """Return the first tick id in the latest contiguous action occurrence.

    The occurrence signature is exactly ``(activity.started_at, activity.step)``.
    Rows are traversed by committed ``id DESC`` order, never timestamp order.  A
    malformed or missing row returns ``None`` so the caller can conservatively use
    ``latest_tick_id`` instead of accidentally merging two distinct occurrences.

    ``step=None`` is a valid signature component for bootstrap and legacy state.
    """
    if latest_tick_id < 1:
        return None
    rows = conn.execute(
        "SELECT id, activity FROM tick WHERE id <= :latest_tick_id ORDER BY id DESC",
        {"latest_tick_id": latest_tick_id},
    )
    expected_signature: tuple[str, str | None] | None = None
    occurrence_start = latest_tick_id
    saw_latest = False
    for row in rows:
        row_id = row["id"]
        if not isinstance(row_id, int):
            return None
        if not saw_latest:
            if row_id != latest_tick_id:
                return None
            saw_latest = True
        try:
            activity = json.loads(row["activity"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(activity, dict):
            return None
        started_at = activity.get("started_at")
        step = activity.get("step")
        if not isinstance(started_at, str) or not started_at:
            return None
        if step is not None and (not isinstance(step, str) or not step):
            return None
        signature = (started_at, step)
        if expected_signature is None:
            expected_signature = signature
        elif signature != expected_signature:
            break
        occurrence_start = row_id
    return occurrence_start if saw_latest else None


def get_recent_ticks(conn: sqlite3.Connection, *, limit: int = 5) -> list[dict[str, Any]]:
    """读最近 N 个 tick 的**连续**轨迹（Layer A，docs 14 §1.4 第 1 行）。

    与 ``get_episodes``（``episode`` 视图，significance>=7 高光过滤，Layer C）
    不同：本查询**不过滤 significance**，取最近连续几条，给 sense.llm
    「刚经历了什么」的连续感（L2，节点本地 read 渲染进 prompt）。

    返回按 ts DESC、id DESC（最新在前；同秒时以自增 id 稳定裁决）。limit<=0 返空。

    **调用者契约**：同 get_episodes——显式 ORDER BY 才是可靠合约。

    **轻量查询**：prompt 轨迹消费 ts/note/significance/act_decision/act_result
    + activity（stuck warning 检测连续卡同一 activity+step 用，sense_llm
    ``_render_stuck_warning``），所以**只 SELECT 这几列**，不走 ``_row_to_dict``
    反序列化 8 层 state JSON（interior/embodiment/...）。docs 14 §1.4（第 849 行）
    写的也是摘要列查询。对 act_decision/act_result/activity 做 JSON decode。
    """
    if limit <= 0:
        return []
    cur = conn.execute(
        "SELECT id, ts, note, significance, act_decision, act_result, activity "
        "FROM tick ORDER BY ts DESC, id DESC LIMIT :limit",
        {"limit": limit},
    )
    return [_row_to_summary_dict(row) for row in cur.fetchall()]


def get_recent_activity_rows(
    conn: sqlite3.Connection,
    *,
    until: str,
    limit: int = 512,
) -> list[dict[str, Any]]:
    """读取 ``until`` 前 24 小时的 ``ts/activity``；供 Sense 折叠生活 run。"""
    if limit <= 0:
        return []
    rows = conn.execute(
        "SELECT ts, activity FROM tick "
        "WHERE json_valid(activity) "
        "AND json_type(activity) = 'object' "
        "AND julianday(ts) >= julianday(:until) - 1 "
        "AND julianday(ts) <= julianday(:until) "
        "ORDER BY ts DESC, id DESC LIMIT :limit",
        {"until": until, "limit": min(limit, 512)},
    ).fetchall()
    return [{"ts": row["ts"], "activity": json.loads(row["activity"])} for row in rows]


def get_activity_send_statuses(
    conn: sqlite3.Connection,
    *,
    activity_name: str,
    started_at: str,
) -> dict[str, str]:
    """读取精确 Activity run 的 artifact 发送状态；确认送达优先于结果未知。"""
    rows = conn.execute(
        "SELECT "
        "json_extract(act_result, '$.outbound_delivery.artifact_ref') AS artifact_ref, "
        "json_extract(act_result, '$.outbound_delivery.delivered') AS delivered, "
        "json_extract(act_result, '$.outbound_delivery.evidence') AS evidence "
        "FROM tick "
        "WHERE json_extract(activity, '$.name') = :activity_name "
        "AND json_extract(activity, '$.started_at') = :started_at",
        {"activity_name": activity_name, "started_at": started_at},
    ).fetchall()
    statuses: dict[str, str] = {}
    unknown_evidence = {
        "current_tick_send_to_user_unknown",
        "recent_send_to_user_unknown",
    }
    for row in rows:
        ref = row["artifact_ref"]
        if not isinstance(ref, str) or not ref:
            continue
        if row["delivered"] == 1:
            statuses[ref] = "delivered"
        elif row["evidence"] in unknown_evidence and statuses.get(ref) != "delivered":
            statuses[ref] = "attempted_unknown"
    return statuses


#: get_recent_ticks 摘要行需 decode 的 JSON 列：nullable 的 act_decision/
#: act_result + NOT NULL 的 activity（stuck warning 读 activity.name/step）。
#: 都用 isinstance(str) guard，对 None / 已解析值幂等。
_SUMMARY_JSON_COLUMNS: tuple[str, ...] = (*_NULLABLE_JSON_COLUMNS, "activity")


def _row_to_summary_dict(row: sqlite3.Row) -> dict[str, Any]:
    """轨迹摘要行 → dict：decode act_decision/act_result/activity。

    区别于 ``_row_to_dict``：本函数用于 get_recent_ticks 的轻量列子集
    （id/ts/note/significance/act_decision/act_result/activity），**不碰其余
    7 层 state**。各 JSON 列仅在 str 时 loads（同 _row_to_dict 对 nullable
    的处理；activity 虽 NOT NULL 也走同一 guard，对 None 幂等防御）。
    """
    out: dict[str, Any] = dict(row)
    for col in _SUMMARY_JSON_COLUMNS:
        if isinstance(out.get(col), str):
            out[col] = json.loads(out[col])
    return out


def get_ticks_page(
    conn: sqlite3.Connection,
    *,
    limit: int,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    """生命流分页：按 ``id`` DESC 取一页轻量 tick 摘要（web ``/stream`` 用）。

    **为什么 cursor 用 id 而不是 ts**：``id`` 是 AUTOINCREMENT 主键，严格单调
    递增且唯一；``ts`` 是 ISO8601 文本，同一分钟 / 心跳重跑可能撞同值，
    用作游标会漏行 / 重复行。按 id 翻页稳定且走主键索引。

    - ``before_id=None``：从最新一页开始（``id`` 最大的 N 条）。
    - ``before_id=k``：取 ``id < k`` 的 N 条（上一页尾部继续往前翻）。

    返回按 ``id`` DESC（最新在前）。``limit<=0`` 返空。

    **专用轻量列 + 专用 mapper**（review N-3）：/stream contract 不消费
    ``act_result``，所以本查询**不** SELECT 它，也不复用 ``get_recent_ticks``
    的 ``_row_to_summary_dict``（那个会多解 ``act_result``）。否则 DB reader 的
    读取形状会和 prompt 轨迹继续绑定，「轻量列」边界变含糊。走独立的
    ``_row_to_stream_dict``，只取 /stream 真正需要的列。
    """
    if limit <= 0:
        return []
    if before_id is None:
        cur = conn.execute(
            f"SELECT {_STREAM_COLS} FROM tick ORDER BY id DESC LIMIT :limit",
            {"limit": limit},
        )
    else:
        cur = conn.execute(
            f"SELECT {_STREAM_COLS} FROM tick WHERE id < :before_id ORDER BY id DESC LIMIT :limit",
            {"before_id": before_id, "limit": limit},
        )
    return [_row_to_stream_dict(row) for row in cur.fetchall()]


def get_interior_history(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    """读取最近 N 个 tick 的 interior 与 hover 摘要，按 id DESC 返回。"""
    if limit <= 0:
        return []
    cur = conn.execute(
        f"SELECT {_INTERIOR_HISTORY_COLS} FROM tick ORDER BY id DESC LIMIT :limit",
        {"limit": limit},
    )
    rows: list[dict[str, Any]] = []
    for row in cur.fetchall():
        item = dict(row)
        for column in ("interior", "activity"):
            if isinstance(item.get(column), str):
                item[column] = json.loads(item[column])
        rows.append(item)
    return rows


def get_episodes_page(
    conn: sqlite3.Connection,
    *,
    limit: int,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    """高光闪回分页：按 ``id`` DESC 取一页 ``significance >= 7`` 的轻量 tick
    （web ``/episodes`` 用）。

    与 ``get_ticks_page`` 同构（同一 cursor 策略 / 同一轻量列 / 同一 mapper），
    唯一区别是 ``WHERE significance >= EPISODE_THRESHOLD`` 高光过滤（同
    ``episode`` 视图语义，09 Layer C）。为什么不走 ``episode`` 视图：视图
    ``ORDER BY ts`` 且无法加 ``id < ?`` cursor 条件，所以直接查 tick 表。
    cursor 用 id 理由同 ``get_ticks_page``（单调唯一，不像 ts 会撞值）。

    - ``before_id=None``：从最新一页高光开始。
    - ``before_id=k``：取 ``id < k`` 且 ``significance >= 7`` 的 N 条。

    返回按 ``id`` DESC。``limit<=0`` 返空。同样走 ``_row_to_stream_dict``（不
    碰 ``act_result`` / 8 层 state）。
    """
    if limit <= 0:
        return []
    if before_id is None:
        cur = conn.execute(
            f"SELECT {_STREAM_COLS} FROM tick WHERE significance >= :thr "
            "ORDER BY id DESC LIMIT :limit",
            {"thr": EPISODE_THRESHOLD, "limit": limit},
        )
    else:
        cur = conn.execute(
            f"SELECT {_STREAM_COLS} FROM tick WHERE significance >= :thr AND id < :before_id "
            "ORDER BY id DESC LIMIT :limit",
            {"thr": EPISODE_THRESHOLD, "before_id": before_id, "limit": limit},
        )
    return [_row_to_stream_dict(row) for row in cur.fetchall()]


def _row_to_stream_dict(row: sqlite3.Row) -> dict[str, Any]:
    """/stream 专用轻量行 → dict：仅 decode ``act_decision``（review N-3）。

    区别于 ``_row_to_summary_dict``（get_recent_ticks 用，还会解 ``act_result``）：
    本 mapper 只服务 /stream 的列子集（id/ts/note/significance/act_decision），
    不碰 ``act_result`` / 8 层 state。让 /stream 的读形状独立于 prompt 轨迹。
    nullable JSON 列（act_decision）仅在 str 时 loads。
    """
    out: dict[str, Any] = dict(row)
    if isinstance(out.get("act_decision"), str):
        out["act_decision"] = json.loads(out["act_decision"])
    return out


def get_episodes(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict[str, Any]]:
    """从 ``episode`` 视图读取最近 N 个高光闪回（09 Layer C 用）。

    **调用者契约**：“最近N个” = 按 ts DESC 排序。
    虽然 ``episode`` 视图定义里已 ORDER BY ts DESC（schema.sql），
    SQLite 不保证SELECT 从视图读时会保留视图内的 ORDER BY（optimizer
    可能会丢）。调用处显式 ORDER BY 才是可靠合约。
    """
    if limit <= 0:
        return []
    cur = conn.execute(
        "SELECT * FROM episode ORDER BY ts DESC LIMIT :limit",
        {"limit": limit},
    )
    return [_row_to_dict(row) for row in cur.fetchall()]


# 嘴侧 bundle Layer A/B 需要的列：ts + activity（名）+ interior（mood）+ note + sig。
# 不取其余 6 层 state（embodiment/bag/...），比 _row_to_dict 轻。
_BUNDLE_COLS = "id, ts, activity, interior, note, significance"


def _row_to_bundle_dict(row: sqlite3.Row) -> dict[str, Any]:
    """bundle 行 → dict：decode ``activity`` + ``interior``（两列 NOT NULL，str 时 loads）。"""
    out: dict[str, Any] = dict(row)
    for col in ("activity", "interior"):
        if isinstance(out.get(col), str):
            out[col] = json.loads(out[col])
    return out


def get_ticks_for_bundle(
    conn: sqlite3.Connection,
    *,
    limit: int,
    since_iso: str | None = None,
) -> list[dict[str, Any]]:
    """嘴侧 bundle Layer A/B 读取：最近 N 个 tick（带 activity + interior.mood）。

    Layer A 取 ``limit=BUNDLE_LAYER_A_RAW_POOL`` 喂压缩；Layer B 传 ``since_iso``=当日 0 点。
    返回按 ts DESC（最新在前）。``limit<=0`` 返空。专用轻量列 + 专用 mapper，不复用
    ``get_recent_ticks``（那个不取 interior），也不动其 sense.llm 契约。
    """
    if limit <= 0:
        return []
    if since_iso is None:
        cur = conn.execute(
            f"SELECT {_BUNDLE_COLS} FROM tick ORDER BY ts DESC LIMIT :limit",
            {"limit": limit},
        )
    else:
        cur = conn.execute(
            f"SELECT {_BUNDLE_COLS} FROM tick WHERE ts >= :since ORDER BY ts DESC LIMIT :limit",
            {"since": since_iso, "limit": limit},
        )
    return [_row_to_bundle_dict(row) for row in cur.fetchall()]


def get_highlight_episodes(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
    limit: int,
) -> list[dict[str, Any]]:
    """嘴侧 bundle Layer C：近窗口 + 未耗尽冷却的高光，按 sig DESC / ts DESC 取 top-N（09 §4.4.4）。

    单一真相源（09 §5）：走 ``episode`` 视图（已锁 sig≥7）**JOIN ``episode_recall``**，双约束
    ``cooldown > 0``（F-1 自反馈防御：闪回过的高光冷却耗尽后不再选）+ ``ts >= since_iso``（近 7 天，
    用她的世界时基准）。返回行含 ``id``（供 flush_bundle 选中后衰减 cooldown）。``limit<=0`` 返空。
    """
    if limit <= 0:
        return []
    cur = conn.execute(
        "SELECT e.id, e.ts, e.activity, e.note, e.significance "
        "FROM episode e JOIN episode_recall r ON e.id = r.tick_id "
        "WHERE r.cooldown > 0 AND e.ts >= :since "
        "ORDER BY e.significance DESC, e.ts DESC LIMIT :limit",
        {"since": since_iso, "limit": limit},
    )
    # _row_to_summary_dict 解 activity（其余 act_* 列未 SELECT → guard 跳过）。
    return [_row_to_summary_dict(row) for row in cur.fetchall()]


def decay_episode_recall(
    conn: sqlite3.Connection,
    tick_ids: list[int],
    *,
    now_iso: str,
    decay: int = EPISODE_RECALL_DECAY,
) -> None:
    """flush_bundle 选中高光后衰减其 cooldown（09 §4.4.4.2：``cooldown -= 50, last_recalled_ts``）。

    ``cooldown`` 衰减到 0 后不再被 :func:`get_highlight_episodes` 选中（"不再想起"）。``MAX(0,…)``
    兜底不让负。**调用方需在事务内**（facade ``_require_transaction``）。空 ids 跳过。
    """
    if not tick_ids:
        return
    placeholders = ",".join("?" * len(tick_ids))
    conn.execute(
        f"UPDATE episode_recall SET cooldown = MAX(0, cooldown - ?), last_recalled_ts = ? "  # noqa: S608
        f"WHERE tick_id IN ({placeholders})",
        [decay, now_iso, *tick_ids],
    )


def count_ticks(conn: sqlite3.Connection) -> int:
    """tick 总行数（运维 / 测试用）。thin wrapper 走 ``connection.count``。"""
    return count(conn, "tick")


def count_episode_recalls(conn: sqlite3.Connection) -> int:
    """episode_recall 总行数（运维 / 测试用）。thin wrapper。"""
    return count(conn, "episode_recall")
