"""``main_session_messages`` 表读写。

watcher 进程把 main session 的消息历史定时缓存到本地 SQLite，心（子 agent）
启动时通过本模块读取最近 N 条消息，从而感知嘴（main agent）正在和 partner
聊什么（spawn 模式下子 session 默认看不到主 session 的对话）。

沿用 first-party predecessor ``messages.py`` v9 的成熟经验，并完成去专名化。

``role`` 业务层语义（earlier milestone 拍板 Q1，覆盖 docs/13 §9 的 kindred_voice/heart）：

- ``partner``  : 对面的人（user）说的话（watcher 从 protocol 层 user 映射）
- ``my_voice`` : 嘴（main agent）说的话（watcher 从 protocol 层 assistant 映射）
- ``my_heart`` : **[legacy]** 旧协议下心（子 agent）自产写表的行
  （旧 ``record_outbound`` 路径）。
  当前生产路径不再产生 my_heart 行。本常量 + 读侧 role 映射只用于读取既有数据。

LLM 协议层仍是 user / assistant；仅在喂模型前由调用方映射回去。

═══ 风格 ═══

与 ``thoughts.py`` / ``ticks.py`` 一致——**函数式 + conn 显式传入**：

- 读函数直接 ``conn.execute`` 返回结果（只读 SELECT 不需事务）
- 写函数分 ``_xxx_impl``（无事务，调用方提供边界）+ thin wrapper（``with conn:``）
- 不引入仓储类（Repository）——first-party predecessor 的 ``self._conn()`` 自持连接风格不沿用

本刀（earlier milestone §3.1）做：model + role 映射 + extract_text_summary +
upsert / upsert_many / get_recent / get_since_ms / get_latest /
get_max_ts_ms / get_max_seq / count / prune。

宿主 transcript 的原始协议解析位于对应 Mouth Host wrapper；本模块只接收
规范化后的 ``MainSessionMessage``。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import Any, Final

from kindred.db.connection import count as _count

# ─── role 取值与协议层映射 ────────────────────────────────────────

ROLE_PARTNER: Final[str] = "partner"
ROLE_MY_VOICE: Final[str] = "my_voice"
ROLE_MY_HEART: Final[str] = "my_heart"

# protocol 层（LLM 看到的）→ business 层（表里存的）映射
PROTOCOL_TO_BUSINESS: Final[dict[str, str]] = {
    "user": ROLE_PARTNER,
    "assistant": ROLE_MY_VOICE,
}

# business 层 → protocol 层。喂模型时反向映射。
# my_heart 也是某次「ta 说的话」，映射为 assistant。
BUSINESS_TO_PROTOCOL: Final[dict[str, str]] = {
    ROLE_PARTNER: "user",
    ROLE_MY_VOICE: "assistant",
    ROLE_MY_HEART: "assistant",
}


def protocol_role_to_business(protocol_role: str) -> str:
    """把协议层的 user/assistant 映射为业务层。未知值原样返回。

    例：``protocol_role_to_business("user")`` → ``"partner"``
    例：``protocol_role_to_business("assistant")`` → ``"my_voice"``

    本函数不产生 ``my_heart``——它应由心（子 agent）主动写入（earlier milestone）。
    """
    return PROTOCOL_TO_BUSINESS.get(protocol_role, protocol_role)


# ─── model ────────────────────────────────────────────────────────

# 列顺序契约：与 schema.sql main_session_messages 列定义（id 之后）一致。
_MSG_COLUMNS: Final[tuple[str, ...]] = (
    "session_key",
    "msg_id",
    "seq",
    "role",
    "content",
    "text_summary",
    "ts_ms",
    "cached_at",
)


@dataclass
class MainSessionMessage:
    """对应 ``main_session_messages`` 表一条记录。

    Attributes
    ----------
    id
        自增主键，插入前为 ``None``。
    session_key
        来源 session（如 ``agent:main:wecom:...``）。
    msg_id
        来源系统的稳定 id（``__openclaw.id``），``(session_key, msg_id)`` 唯一。
    seq
        来源系统的序号（``__openclaw.seq``），单调递增。
    role
        业务层语义 — ``partner`` / ``my_voice`` / ``my_heart``。
    content
        默认 ``None``，不存 JSON 原文；只依赖 ``text_summary``。
    text_summary
        抽取出的纯文本摘要（子 agent 直接读）。
    ts_ms
        来源系统 timestamp（毫秒 epoch）。
    cached_at
        watcher 写入本地的 ISO 时间。
    """

    id: int | None
    session_key: str
    msg_id: str
    seq: int | None
    role: str
    content: str | None
    text_summary: str | None
    ts_ms: int
    cached_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> MainSessionMessage:
        data = {f.name: row[f.name] for f in fields(cls) if f.name in row.keys()}
        return cls(**data)

    def to_db_tuple(self) -> tuple[Any, ...]:
        return tuple(getattr(self, c) for c in _MSG_COLUMNS)


def extract_text_summary(content: Any) -> str | None:
    """从 content 数组里抽出可读纯文本，便于子 agent 快速浏览。

    规则：
    - 字符串 → 直接返回（去空白后为空则 ``None``）
    - 列表 → 拼接所有 ``type=text`` 的 ``.text`` 字段
    - 列表里 ``type=toolCall`` → 输出 ``[tool: name]``（精简，不含参数）
    - 列表里 ``type=image`` → 输出 ``[image]``
    - 其他 → ``None``
    """
    if content is None:
        return None
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "text":
                txt = item.get("text") or ""
                if txt:
                    parts.append(txt)
            elif kind == "toolCall":
                name = item.get("name") or "?"
                parts.append(f"[tool: {name}]")
            elif kind == "image":
                parts.append("[image]")
        joined = "\n".join(p.strip() for p in parts if p and p.strip())
        return joined or None
    return None


# ─── 写入（_impl 无事务 + thin wrapper 含事务）────────────────────


def _upsert_impl(
    conn: sqlite3.Connection,
    message: MainSessionMessage,
) -> bool:
    """插入一条消息；如 ``(session_key, msg_id)`` 已存在则跳过。

    **事务不在本函数**——与 ``thoughts._insert_thoughts_impl`` 同处理：
    调用方（thin wrapper / facade）提供事务边界。

    Returns
    -------
    bool
        ``True`` 表示真正插入了新行，``False`` 表示已有同 id 被跳过。
    """
    cols = ", ".join(_MSG_COLUMNS)
    placeholders = ", ".join("?" * len(_MSG_COLUMNS))
    cur = conn.execute(
        f"INSERT OR IGNORE INTO main_session_messages ({cols}) "  # noqa: S608
        f"VALUES ({placeholders})",
        message.to_db_tuple(),
    )
    return cur.rowcount > 0


def upsert(conn: sqlite3.Connection, message: MainSessionMessage) -> bool:
    """插入一条消息（幂等）；返回是否真正写入新行。thin wrapper（含事务）。"""
    with conn:
        return _upsert_impl(conn, message)


def _upsert_many_impl(
    conn: sqlite3.Connection,
    messages: Iterable[MainSessionMessage],
) -> int:
    """批量插入，返回真正写入的新行数。**事务不在本函数。**"""
    rows = [m.to_db_tuple() for m in messages]
    if not rows:
        return 0
    cols = ", ".join(_MSG_COLUMNS)
    placeholders = ", ".join("?" * len(_MSG_COLUMNS))
    before = conn.execute("SELECT COUNT(*) FROM main_session_messages").fetchone()[0]
    conn.executemany(
        f"INSERT OR IGNORE INTO main_session_messages ({cols}) "  # noqa: S608
        f"VALUES ({placeholders})",
        rows,
    )
    after = conn.execute("SELECT COUNT(*) FROM main_session_messages").fetchone()[0]
    return int(after) - int(before)


def upsert_many(conn: sqlite3.Connection, messages: Iterable[MainSessionMessage]) -> int:
    """批量插入（幂等）；返回真正写入的新行数。thin wrapper（含事务）。"""
    with conn:
        return _upsert_many_impl(conn, messages)


# ─── 读取（只读 SELECT，不需事务）─────────────────────────────────

# 统一排序：ts_ms（消息发生时间）为主键，ts_ms 相同看归一后的 seq
# （NULL 与缺失值 -1 等价），seq 仍相同才看 id（本地入表顺序）。避免用会受
# INSERT OR IGNORE 抽号影响的 id 作为主排序。
_ORDER_DESC: Final[str] = "ORDER BY ts_ms DESC, COALESCE(seq, -1) DESC, id DESC"
# watcher 事件流用正序（老→新），配合 cursor “严格大于” 分页拉取。
# seq NULL 用 COALESCE(-1) 归一，与 watcher MessageCursor.of 一致。
_ORDER_ASC: Final[str] = "ORDER BY ts_ms ASC, COALESCE(seq, -1) ASC, id ASC"

# SQLite 的单参数 TRIM 只移除普通空格；历史行或 facade 直写可能仍含制表符/
# 换行。资格判断与 tool 占位过滤必须共用同一份 ASCII 空白归一表达式。
_STRIPPED_TEXT_SUMMARY_SQL: Final[str] = (
    "TRIM(COALESCE(text_summary, ''), "
    "' ' || CHAR(9) || CHAR(10) || CHAR(11) || CHAR(12) || CHAR(13))"
)


def get_recent(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    limit: int = 30,
) -> list[MainSessionMessage]:
    """取某 session 最近 N 条消息，返回时按时间正序（便于阅读）。"""
    if limit <= 0:
        return []
    rows = conn.execute(
        f"SELECT * FROM main_session_messages WHERE session_key = ? {_ORDER_DESC} LIMIT ?",  # noqa: S608
        (session_key, limit),
    ).fetchall()
    return list(reversed([MainSessionMessage.from_row(r) for r in rows]))


def get_since_ms(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    since_ms: int,
    limit: int = 200,
) -> list[MainSessionMessage]:
    """取某 session 自 ``since_ms`` 以来的消息（时间正序）。

    窗口内超过 ``limit`` 时**保留最近的 limit 条**（丢头保尾）——本函数服务
    bundle / heart context 记忆场景，丢掉最近会导致「看不到刚才的对话」；
    丢掉老的可靠 episodes / Layer C 高光兑付中期记忆。
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        f"SELECT * FROM main_session_messages "  # noqa: S608
        f"WHERE session_key = ? AND ts_ms >= ? {_ORDER_DESC} LIMIT ?",
        (session_key, since_ms, limit),
    ).fetchall()
    msgs = [MainSessionMessage.from_row(r) for r in rows]
    msgs.reverse()
    return msgs


def get_between_ms(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    since_ms: int,
    until_ms: int,
    limit: int = 2000,
) -> list[MainSessionMessage]:
    """取某 session 在 ``[since_ms, until_ms)`` 半开区间内的消息（时间正序）。

    与 :func:`get_since_ms`（context-window 语义，只有 since 下界 + 丢头保尾）
    不同——本函数是 **bounded day-window** 语义：``ts_ms`` 同时受上下界
    约束，``limit`` 只在窗口内生效。

    为什么需要它（earlier milestone N-1）：dream Step 1.summarize 要总结
    ``[dream_date 00:00, dream_date+1 00:00)`` 的整天窗口。若复用 ``get_since_ms``
    只加 since 下界，``limit`` 会在 DB 层先把「昨天 + 今天凌晨」一起取进来
    再在内存过滤，导致：做梦当天 00:00~04:00 消息超 ``limit`` 时 ``rows`` 全是
    「今天」过滤后空窗口；yesterday + today 总量超 ``limit`` 时静默丢掉昨天
    较早的事件锡。SQL 同时加上下界后 ``limit`` 只作用于窗口内、今天消息
    根本不进来。

    窗口内超 ``limit`` 时与 ``get_since_ms`` 一致采「丢头保尾」（DESC + LIMIT
    + reverse，保留较新的 limit 条）。
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        f"SELECT * FROM main_session_messages "  # noqa: S608
        f"WHERE session_key = ? AND ts_ms >= ? AND ts_ms < ? {_ORDER_DESC} LIMIT ?",
        (session_key, since_ms, until_ms, limit),
    ).fetchall()
    msgs = [MainSessionMessage.from_row(r) for r in rows]
    msgs.reverse()
    return msgs


def get_after_cursor(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    after_ts_ms: int | None,
    after_seq: int | None,
    after_id: int | None,
    limit: int = 200,
) -> list[MainSessionMessage]:
    """事件流语义：取严格大于游标 ``(ts_ms, seq, id)`` 的**下一页**（正序）。

    与 :func:`get_since_ms`（context-window 语义，丢头保尾）不同——本函数服务
    watcher trigger，要求 docs/13 §201-203 / discussion §74-78 的“连发不丢失”：
    按 ``(ts_ms, seq, id)`` ASC 取 cursor 后的下一页，只推本页末尾。还有更多就
    下一轮继续拉，不用“最近窗口” API。

    ``after_*`` 均为 ``None`` 表示“从头”（无游标，拉最早一页）。``seq`` 用
    ``COALESCE(seq, -1)`` 与游标的 ``after_seq``（调用方已归一）比较。严格大于用
    row-value 比较：``(ts, seq', id) > (after_ts, after_seq, after_id)``。
    """
    if limit <= 0:
        return []
    if after_ts_ms is None:
        rows = conn.execute(
            f"SELECT * FROM main_session_messages "  # noqa: S608
            f"WHERE session_key = ? {_ORDER_ASC} LIMIT ?",
            (session_key, limit),
        ).fetchall()
    else:
        # row-value 严格大于：SQLite 支持 (a,b,c) > (?,?,?) 元组比较。
        rows = conn.execute(
            f"SELECT * FROM main_session_messages "  # noqa: S608
            f"WHERE session_key = ? "
            f"  AND (ts_ms, COALESCE(seq, -1), id) > (?, ?, ?) "
            f"{_ORDER_ASC} LIMIT ?",
            (session_key, after_ts_ms, after_seq, after_id, limit),
        ).fetchall()
    return [MainSessionMessage.from_row(r) for r in rows]


def get_latest_visible_contact_expression(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    cursor: tuple[int, int, int],
    now_ms: int,
) -> tuple[int, str] | None:
    """取当前已读 cursor 以内、不晚于 ``now_ms`` 的最近可见表达。

    摘要只在 SQL 内判断资格；函数边界只返回 ``(ts_ms, role)``，不带出正文。
    """
    row = conn.execute(
        "SELECT ts_ms, role FROM main_session_messages "
        "WHERE session_key = ? "
        "  AND (ts_ms, COALESCE(seq, -1), id) <= (?, ?, ?) "
        "  AND ts_ms <= ? "
        "  AND role IN (?, ?, ?) "
        f"  AND {_STRIPPED_TEXT_SUMMARY_SQL} <> '' "
        f"  AND NOT (role IN (?, ?) "
        f"    AND SUBSTR({_STRIPPED_TEXT_SUMMARY_SQL}, 1, 6) = '[tool:') "
        f"{_ORDER_DESC} LIMIT 1",  # noqa: S608
        (
            session_key,
            *cursor,
            now_ms,
            ROLE_PARTNER,
            ROLE_MY_VOICE,
            ROLE_MY_HEART,
            ROLE_MY_VOICE,
            ROLE_MY_HEART,
        ),
    ).fetchone()
    return (int(row[0]), str(row[1])) if row else None


def get_latest_visible_partner_expression(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    cursor: tuple[int, int, int],
    now_ms: int,
) -> int | None:
    """取当前已读范围内最近一条非空 partner 表达的时间，不返回正文。"""
    row = conn.execute(
        "SELECT ts_ms FROM main_session_messages "
        "WHERE session_key = ? "
        "  AND (ts_ms, COALESCE(seq, -1), id) <= (?, ?, ?) "
        "  AND ts_ms <= ? "
        "  AND role = ? "
        f"  AND {_STRIPPED_TEXT_SUMMARY_SQL} <> '' "
        f"{_ORDER_DESC} LIMIT 1",  # noqa: S608
        (session_key, *cursor, now_ms, ROLE_PARTNER),
    ).fetchone()
    return int(row[0]) if row else None


def exists_partner_after_cursor(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    after_ts_ms: int | None,
    after_seq: int | None,
    after_id: int | None,
) -> bool:
    """游标 ``(ts_ms, seq, id)`` 之后是否**存在**任一 ``role=partner`` 消息。

    watcher 唤醒判据（earlier milestone N-1）：watcher 不推 cursor，不能只扫第一页——若
    cursor 后先有整页 my_voice/my_heart、再有 partner，翻页 ``any()`` 会永远
    看不到第二页的 partner。下沉到 DB 用索引 + ``LIMIT 1`` 一次性判存在，
    O(log n) 不翻页。

    与 :func:`get_after_cursor` 同语义：row-value 严格大于 + ``COALESCE(seq,-1)``
    归一。``after_*`` 均 ``None`` 表示“从头”（无游标）。
    """
    if after_ts_ms is None:
        row = conn.execute(
            "SELECT 1 FROM main_session_messages "  # noqa: S608
            "WHERE session_key = ? AND role = ? LIMIT 1",
            (session_key, ROLE_PARTNER),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM main_session_messages "  # noqa: S608
            "WHERE session_key = ? AND role = ? "
            "  AND (ts_ms, COALESCE(seq, -1), id) > (?, ?, ?) LIMIT 1",
            (session_key, ROLE_PARTNER, after_ts_ms, after_seq, after_id),
        ).fetchone()
    return row is not None


def get_latest(
    conn: sqlite3.Connection,
    *,
    session_key: str,
) -> MainSessionMessage | None:
    """某 session 最后一条消息，不存在返回 ``None``。

    主要给 chat_in_progress 检测使用（earlier milestone §3.1.4 抢答判定）。
    """
    row = conn.execute(
        f"SELECT * FROM main_session_messages WHERE session_key = ? {_ORDER_DESC} LIMIT 1",  # noqa: S608
        (session_key,),
    ).fetchone()
    return MainSessionMessage.from_row(row) if row else None


def get_by_msg_id(
    conn: sqlite3.Connection, *, session_key: str, msg_id: str
) -> MainSessionMessage | None:
    """按 ``(session_key, msg_id)`` 唯一键取一条，不存在返回 ``None``。

    幂等回查：upsert（INSERT OR IGNORE）后回查**实际持久化行**，使「同 msg_id 重写」返回库里
    真实 seq（而非本次分配但未落库的 seq）；并发下也返回最终落库行。
    """
    row = conn.execute(
        "SELECT * FROM main_session_messages WHERE session_key = ? AND msg_id = ? LIMIT 1",
        (session_key, msg_id),
    ).fetchone()
    return MainSessionMessage.from_row(row) if row else None


def get_max_cursor(conn: sqlite3.Connection, *, session_key: str) -> tuple[int, int, int] | None:
    """表尾最大游标 ``(ts_ms, seq', id)``，watcher.prime 用于设基线。

    按 ``_ORDER_DESC`` 取第一条即最大。``seq`` 用 ``COALESCE(-1)`` 归一。
    表为空返 ``None``。
    """
    row = conn.execute(
        f"SELECT ts_ms, COALESCE(seq, -1), id FROM main_session_messages "  # noqa: S608
        f"WHERE session_key = ? {_ORDER_DESC} LIMIT 1",
        (session_key,),
    ).fetchone()
    if not row:
        return None
    return (int(row[0]), int(row[1]), int(row[2]))


def get_max_ts_ms(conn: sqlite3.Connection, *, session_key: str) -> int | None:
    """某 session 已缓存的最新 ts_ms，watcher 用于增量拉取。"""
    row = conn.execute(
        "SELECT MAX(ts_ms) FROM main_session_messages WHERE session_key = ?",
        (session_key,),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def get_max_seq(conn: sqlite3.Connection, *, session_key: str) -> int | None:
    """某 session 已缓存的最大 seq；watcher 可据此判断是否有新消息。"""
    row = conn.execute(
        "SELECT MAX(seq) FROM main_session_messages WHERE session_key = ?",
        (session_key,),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def count_messages(conn: sqlite3.Connection, *, session_key: str | None = None) -> int:
    """统计行数；不传 ``session_key`` 时统计全表。

    全表计数走 ``connection.count``（表名 Literal 钉死）；按 session 计数另走
    参数化 SQL。
    """
    if session_key is None:
        return _count(conn, "main_session_messages")
    row = conn.execute(
        "SELECT COUNT(*) FROM main_session_messages WHERE session_key = ?",
        (session_key,),
    ).fetchone()
    return int(row[0]) if row else 0


# ─── 维护（_impl 无事务 + thin wrapper 含事务）────────────────────


def _prune_older_than_ms_impl(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    before_ms: int,
) -> int:
    """删除某 session 中早于 ``before_ms`` 的旧消息，返回删除条数。**无事务。**"""
    cur = conn.execute(
        "DELETE FROM main_session_messages WHERE session_key = ? AND ts_ms < ?",
        (session_key, before_ms),
    )
    return cur.rowcount


def prune_older_than_ms(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    before_ms: int,
) -> int:
    """删除早于 ``before_ms`` 的旧消息；返回删除条数。thin wrapper（含事务）。"""
    with conn:
        return _prune_older_than_ms_impl(conn, session_key=session_key, before_ms=before_ms)


def _prune_between_ms_impl(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    since_ms: int,
    until_ms: int,
) -> int:
    """删除某 session ``[since_ms, until_ms)`` 区间内的消息，返回删除条数。**无事务。**

    与 :func:`_prune_older_than_ms_impl`（只有上界）不同，本函数同时卡下界——
    dream Step 2.reset_stack 只 prune「本次 summary 覆盖的昨日窗口」``[since, until)``，
    **不动 ``since`` 之前的 backlog**（可能是 Step 1 失败未总结的历史消息，留给
    重试 / force-truncate，见 docs/11 §8.1）。
    """
    cur = conn.execute(
        "DELETE FROM main_session_messages WHERE session_key = ? AND ts_ms >= ? AND ts_ms < ?",
        (session_key, since_ms, until_ms),
    )
    return cur.rowcount


def prune_between_ms(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    since_ms: int,
    until_ms: int,
) -> int:
    """删除 ``[since_ms, until_ms)`` 区间内消息；返回删除条数。thin wrapper（含事务）。"""
    with conn:
        return _prune_between_ms_impl(
            conn, session_key=session_key, since_ms=since_ms, until_ms=until_ms
        )


def _prune_keep_recent_impl(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    keep: int,
) -> int:
    """只保留某 session 最近 ``keep`` 条消息，返回删除条数。**无事务。**

    排序规则与 ``get_recent`` 一致：ts_ms 优先，同时间看 seq，同 seq 看 id。
    """
    cur = conn.execute(
        "DELETE FROM main_session_messages "
        "WHERE session_key = ? AND id NOT IN ("
        "  SELECT id FROM main_session_messages "
        f"  WHERE session_key = ? {_ORDER_DESC} LIMIT ?"  # noqa: S608
        ")",
        (session_key, session_key, keep),
    )
    return cur.rowcount


def prune_keep_recent(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    keep: int,
) -> int:
    """只保留最近 ``keep`` 条；返回删除条数。thin wrapper（含事务）。"""
    with conn:
        return _prune_keep_recent_impl(conn, session_key=session_key, keep=keep)
