"""``KindredDB`` facade — 全部 db 写操作的入口。

═══ 为什么 ═══

历史上有过一个结构性风险：写函数忘了用 ``with conn:`` 包事务。每加一个写
函数都可能再犯。一旦 ``T3.persist.write_state`` 跨 tick + thought +
episode_recall 三表写时有任一步漏 ``with conn:``，就会导致：

- 异常时只回滚一部分
- 留下半状态：``tick`` 写了但 ``thought`` 没写
- 下一轮 ``T1.sense.io`` 读 ``state_latest`` 视图时数据不自洽

facade 的核心职责是把"事务边界"从"调用者每次记得 ``with conn:``"
变成"必须在 ``db.transaction()`` 里调写方法，否则报错"。

═══ 设计 ═══

- 一个进程一个 ``KindredDB`` 实例（长持 ``sqlite3.Connection``）
- ``KindredDB.open(path)`` ContextManager 自动 ``connect`` + ``migrate`` + ``close``
- ``__init__`` 直接 raise——必须走 ``open()``，避免 schema 未建的"能调但会坏"入口
- ``db.transaction()`` 上下文管理跨多写同事务（``with conn:`` 等价语义，
  错误回滚）
- 写方法（``insert_tick`` / ``insert_thoughts``）**必须**在 transaction
  里调用，否则 raise——这是 facade 的根本价值
- 读方法（``get_state_latest`` / ``count`` / ``query_active_thoughts``）
  不强制事务（只读 SELECT，不需要写锁）

═══ 使用 ═══

::

    # 跨表原子写（T3.persist 典型用法）
    with KindredDB.open(db_path) as db:
        with db.transaction():
            tick_id = db.insert_tick(params)
            db.insert_thoughts(thoughts, ts=..., source_tick_id=tick_id)
            # 任一步异常 → 自动 ROLLBACK，两表都不写

    # 仅读
    with KindredDB.open(db_path) as db:
        latest = db.get_state_latest()
        episodes = db.get_episodes(limit=10)

═══ Thread-safety ═══

**KindredDB 实例是 single-thread only。**

- ``sqlite3.Connection`` 默认 ``check_same_thread=True``，跨线程访问
  会 raise ``sqlite3.ProgrammingError``——这是 stdlib 的硬约束
- ``_in_transaction`` 是普通实例 ``bool``，不是 ``threading.local()``——
  多线程交错调用 ``transaction()`` 会 race
- 即便把 flag 改成 thread-local 也无意义，conn 本身仍不能跨线程共享

所以使用约定（daemon 设计要谨记）：

- 单进程心跳 / cli / 测试：一个 ``KindredDB`` 实例
- 多线程场景：每线程一个独立实例（各自 ``open()``）
- asyncio：用单线程 event loop，事务边界严格在同一协程内闭合

═══ 与过程式 API 的关系 ═══

过程式 ``insert_tick(conn, *, state=..., ...)`` / ``insert_thoughts(conn, ...)``
保留为 backward-compat thin wrappers。facade 是新代码（graph 节点 / cli /
cron）的推荐入口；老测试 / 文档示例继续走过程式 API。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from kindred.db import artifacts as _artifacts_mod
from kindred.db import connection as _conn_mod
from kindred.db import inventory as _inventory_mod
from kindred.db import messages as _messages_mod
from kindred.db import places as _places_mod
from kindred.db import thoughts as _thoughts_mod
from kindred.db import ticks as _ticks_mod
from kindred.db import watcher_cursor as _watcher_cursor_mod
from kindred.db._types import TickWriteParams
from kindred.db.messages import MainSessionMessage
from kindred.inventory.catalog import InventoryItem
from kindred.state._types import IsoDatetime
from kindred.state.interior import Thought

_LOG = logging.getLogger(__name__)

# 首次 migrate 临界区保护（codex earlier review 二轮 N-1）：``open()`` 对 fresh DB 自动跑
# ``migrate()``（``executescript`` 全量 schema DDL）。心 daemon 与聊天 API 并发首次 open
# 同一库时，两线程同时跑 DDL → ``database is locked``（``busy_timeout`` 不挡 executescript
# 的 DDL 临界区）。按 db_path 进程内串行化 migrate：第一个建表、其余等它建完再各自跑
# 幂等 no-op DDL，不再并发撞锁。**进程内锁不跨进程**——多进程部署仍应启动前 ``kindred db
# migrate`` 预迁移（runbook 已如此）。
_migrate_locks: dict[str, threading.Lock] = {}
_migrate_registry_guard = threading.Lock()


def _migrate_lock_for(db_path: Path) -> threading.Lock:
    key = str(db_path)
    with _migrate_registry_guard:
        lock = _migrate_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _migrate_locks[key] = lock
        return lock


# ─── Exceptions ───────────────────────────────────────────────────


class TransactionStateError(RuntimeError):
    """facade 的事务边界状态错误（基类）。

    具体错误见两个子类。捕获基类即可处理两种情况；想区分时按子类 catch。
    """


class TransactionRequiredError(TransactionStateError):
    """写方法在 ``db.transaction()`` 之外被调用——应进 transaction。

    facade 的核心约束：一切写入必须显式声明事务边界，不允许"裸调
    insert_tick 然后被静默 autocommit"——那正是漏 ``with conn:`` 的根本症状。
    """


class NestedTransactionError(TransactionStateError):
    """嵌套调用 ``db.transaction()``——SQLite 原生不支持嵌套事务。

    需要 savepoint 才能模拟，当前阶段不实现。graph 节点是平铺的 T1/T2/T3，
    不会嵌套。如果触发：检查调用栈是否能合并到同一外层事务。
    """


# ─── 入参校验工具 ────────────────────────────────────────────────

# facade 入参 IsoDatetime 运行时校验。
# IsoDatetime 是 Annotated[str, AfterValidator]——直接用于参数声明无法触发
# 校验，需走 TypeAdapter.validate_python。LLM 输出 ``now="2026-06-03 evening"``
# 之类的非法值会立即 raise，而非走到 SQL 层 lexicographic 比较失败。
_iso_adapter: TypeAdapter[str] = TypeAdapter(IsoDatetime)


# ─── KindredDB ───────────────────────────────────────────────────


class KindredDB:
    """SQLite 持久化 facade。

    生命周期由 :meth:`open` ContextManager 管理；外部不应直接 ``__init__``——
    本类的 ``__init__`` 直接 ``raise`` 来强制走 ``open()``，避免 schema 未建
    的"能调但会坏"入口。

    实例属性：

    - ``_conn``：底层 ``sqlite3.Connection``。**实现细节**——外部代码不要
      直接 ``db._conn.execute(...)`` 绕过 facade 事务保护。仅 backward-compat
      测试 / 过程式 API thin wrapper 路径用。

    - ``_in_transaction``：当前是否在 ``transaction()`` 上下文内。写方法
      据此决定是否 raise。**single-thread**——见模块 docstring "Thread-safety"。
    """

    # 类层面 type annotation——__init__ raise，_create 走 object.__new__，
    # mypy 看不到 self.x = ... 产生的属性。
    _conn: sqlite3.Connection
    _in_transaction: bool

    def __init__(self) -> None:
        """禁止直接实例化。请使用 :meth:`open`。

        Python 没有真私有构造；这里靠 raise 把"误用即报错"的语义在第一时间
        给到 IDE / type checker / 新人。内部走 :meth:`_create` 工厂。
        """
        raise RuntimeError(
            "KindredDB cannot be instantiated directly; "
            "use `with KindredDB.open(db_path) as db:` instead "
            "(open() handles connect + migrate + close)."
        )

    @classmethod
    def _create(cls, conn: sqlite3.Connection) -> KindredDB:
        """内部工厂——绕过 ``__init__`` raise。仅 :meth:`open` 调用。"""
        instance = object.__new__(cls)
        instance._conn = conn
        instance._in_transaction = False
        return instance

    # ─── 生命周期 ─────────────────────────────────────────────

    @classmethod
    @contextmanager
    def open(cls, db_path: Path) -> Iterator[KindredDB]:
        """打开 + 自动 migrate + 退出时 close。

        ``connect`` 已设置 ``isolation_level="DEFERRED"``——保证 ``with conn:``
        块结束时真正 COMMIT、异常时真正 ROLLBACK。
        """
        _LOG.debug("db.open path=%s", db_path)
        conn = _conn_mod.connect(db_path)
        try:
            # 按 db_path 串行 migrate（防并发首次迁移撞 database is locked，二轮 N-1）。
            with _migrate_lock_for(db_path):
                _conn_mod.migrate(conn)
            yield cls._create(conn)
        finally:
            _LOG.debug("db.close path=%s", db_path)
            conn.close()

    @classmethod
    @contextmanager
    def open_readonly(cls, db_path: Path) -> Iterator[KindredDB]:
        """以**真只读**方式打开（``mode=ro`` URI）+ 退出时 close。

        与 :meth:`open` 的本质区别（earlier milestone codex review N-2）：

        - **不 migrate / 不建表 / 不建文件**：``open()`` 会 ``connect + migrate``
          触发 DDL 写库；本方法只 ``connect_readonly``，对被观察库零副作用。
        - **引擎级只读**：拿到的连接任何写（DDL/DML）都会被 SQLite 拒绝，
          ``transaction()`` / 写方法即便误调也写不进去。

        用途：web 可视化等"观察窗"——观察旧库 / 备份库 / 只读挂载库时，
        HTTP GET 绝不能改写被观察对象。

        前置条件：**库文件必须已存在且已 migrate**。文件不存在 → SQLite
        raise ``OperationalError``；库存在但未建表 → 读视图时 raise。
        调用方应先用 ``db_path.exists()`` + 读方法 try 兜底（web 层即如此）。
        """
        _LOG.debug("db.open_readonly path=%s", db_path)
        conn = _conn_mod.connect_readonly(db_path)
        try:
            yield cls._create(conn)
        finally:
            _LOG.debug("db.close (ro) path=%s", db_path)
            conn.close()

    # ─── 事务 ─────────────────────────────────────────────────

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """声明跨多写同事务边界。

        块内可调多个写方法；任一异常 → 整体 ROLLBACK。

        嵌套限制：本 facade 不支持嵌套 transaction（SQLite 原生不支持，需
        savepoint 才能模拟）。当前阶段嵌套调用直接 raise
        :class:`NestedTransactionError`——graph 节点是平铺的 T1/T2/T3，不会嵌套。
        """
        if self._in_transaction:
            raise NestedTransactionError(
                "nested transaction not supported; "
                "current implementation expects flat T1/T2/T3 graph nodes"
            )
        self._in_transaction = True
        try:
            _LOG.debug("db.transaction begin")
            with self._conn:
                yield
            _LOG.debug("db.transaction commit")
        except Exception:
            _LOG.debug("db.transaction rollback", exc_info=True)
            raise
        finally:
            self._in_transaction = False

    def _require_transaction(self, op: str) -> None:
        """所有写方法第一行调用：不在 transaction 内则报错。"""
        if not self._in_transaction:
            raise TransactionRequiredError(
                f"{op}() must be called inside `with db.transaction():` block; "
                "writes without explicit transaction boundary are forbidden by "
                "facade design."
            )

    # ─── 写方法（必须在 transaction 内）──────────────────────

    def insert_tick(self, params: TickWriteParams) -> int:
        """写入一行 tick；返回 rowid。

        当 ``params.significance >= 7`` 时同事务写入 episode_recall。
        事务由 ``transaction()`` 上下文统一提供，本处仅调用不含事务的
        ``_insert_tick_impl``。
        """
        self._require_transaction("insert_tick")
        return _ticks_mod._insert_tick_impl(self._conn, params)

    def insert_thoughts(
        self,
        thoughts: list[Thought],
        *,
        ts: str,
        source_tick_id: int | None = None,
    ) -> list[int]:
        """批量写入 thoughts；返回 rowid 列表。事务由 ``transaction()`` 提供。"""
        self._require_transaction("insert_thoughts")
        return _thoughts_mod._insert_thoughts_impl(
            self._conn,
            thoughts=thoughts,
            ts=ts,
            source_tick_id=source_tick_id,
        )

    def import_inventory_items(self, items: list[InventoryItem]) -> bool:
        """向空 Catalog 原子导入；完全一致时返回 ``False``。"""
        self._require_transaction("import_inventory_items")
        return _inventory_mod._import_inventory_items_impl(self._conn, items)

    def add_inventory_items(self, items: list[InventoryItem]) -> tuple[int, int]:
        """向 Catalog 原子追加；返回 ``(added, unchanged)``。"""
        self._require_transaction("add_inventory_items")
        return _inventory_mod._add_inventory_items_impl(self._conn, items)

    def upsert_message(self, message: MainSessionMessage) -> bool:
        """写入一条 main session 消息（幂等）；返回是否真正写入新行。

        事务由 ``transaction()`` 提供，本处仅调不含事务的 ``_upsert_impl``。
        """
        self._require_transaction("upsert_message")
        return _messages_mod._upsert_impl(self._conn, message)

    def upsert_messages(self, messages: list[MainSessionMessage]) -> int:
        """批量写入 main session 消息（幂等）；返回真正写入的新行数。"""
        self._require_transaction("upsert_messages")
        return _messages_mod._upsert_many_impl(self._conn, messages)

    def prune_messages_older_than_ms(
        self,
        *,
        session_key: str,
        before_ms: int,
    ) -> int:
        """删除某 session 中早于 ``before_ms`` 的旧消息；返回删除条数。"""
        self._require_transaction("prune_messages_older_than_ms")
        return _messages_mod._prune_older_than_ms_impl(
            self._conn, session_key=session_key, before_ms=before_ms
        )

    def prune_messages_between_ms(
        self,
        *,
        session_key: str,
        since_ms: int,
        until_ms: int,
    ) -> int:
        """删除某 session ``[since_ms, until_ms)`` 区间内消息；返回删除条数。

        dream Step 2.reset_stack 专用：只 prune 本次 summary 覆盖的昨日窗口，不动
        ``since`` 之前可能未总结的 backlog。
        """
        self._require_transaction("prune_messages_between_ms")
        return _messages_mod._prune_between_ms_impl(
            self._conn,
            session_key=session_key,
            since_ms=since_ms,
            until_ms=until_ms,
        )

    def prune_messages_keep_recent(self, *, session_key: str, keep: int) -> int:
        """只保留某 session 最近 ``keep`` 条消息；返回删除条数。"""
        self._require_transaction("prune_messages_keep_recent")
        return _messages_mod._prune_keep_recent_impl(self._conn, session_key=session_key, keep=keep)

    # ─── 读方法（不强制 transaction）─────────────────────────

    def get_state_latest(self) -> dict[str, Any] | None:
        """读最新 tick（state_latest 视图）。"""
        return _ticks_mod.get_state_latest(self._conn)

    def get_inventory_item(self, item_key: str) -> InventoryItem | None:
        """按稳定 key exact lookup；不存在返回 ``None``。"""
        return _inventory_mod.get_inventory_item(self._conn, item_key)

    def list_inventory_items(self) -> list[InventoryItem]:
        """按 key 稳定排序读取完整 Catalog。"""
        return _inventory_mod.list_inventory_items(self._conn)

    def count_inventory_items(self) -> int:
        """Catalog 当前物品数。"""
        return _inventory_mod.count_inventory_items(self._conn)

    def get_episodes(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """读最近的 episode 列表（significance >= 7 的 tick）。"""
        return _ticks_mod.get_episodes(self._conn, limit=limit)

    def get_recent_ticks(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """读最近 N 个连续 tick 轨迹（不过滤 significance，Layer A）。"""
        return _ticks_mod.get_recent_ticks(self._conn, limit=limit)

    def get_activity_artifacts(
        self, *, activity_name: str, started_at: str
    ) -> list[dict[str, str]]:
        """读取精确 Activity run 已持久化的 Host artifact facts。"""
        return _artifacts_mod.get_activity_artifacts(
            self._conn,
            activity_name=activity_name,
            started_at=started_at,
        )

    def list_committed_artifacts(
        self,
        *,
        limit: int,
        cursor: tuple[int, int] | None = None,
    ) -> list[_artifacts_mod.ArtifactCommitRow]:
        return _artifacts_mod.list_committed_artifacts(self._conn, limit=limit, cursor=cursor)

    def get_committed_artifact(
        self,
        *,
        tick_id: int,
        artifact_ordinal: int,
    ) -> _artifacts_mod.ArtifactCommitRow | None:
        return _artifacts_mod.get_committed_artifact(
            self._conn, tick_id=tick_id, artifact_ordinal=artifact_ordinal
        )

    def get_activity_send_statuses(self, *, activity_name: str, started_at: str) -> dict[str, str]:
        """读取精确 Activity run 的 artifact 发送状态。"""
        return _ticks_mod.get_activity_send_statuses(
            self._conn,
            activity_name=activity_name,
            started_at=started_at,
        )

    def get_ticks_page(self, *, limit: int, before_id: int | None = None) -> list[dict[str, Any]]:
        """生命流分页：按 id DESC 取一页 tick 摘要（web /stream 用）。"""
        return _ticks_mod.get_ticks_page(self._conn, limit=limit, before_id=before_id)

    def get_interior_history(self, *, limit: int) -> list[dict[str, Any]]:
        """趋势图：读取最近 N 个 tick 的 interior 与 hover 摘要。"""
        return _ticks_mod.get_interior_history(self._conn, limit=limit)

    def get_ticks_for_bundle(
        self, *, limit: int, since_iso: str | None = None
    ) -> list[dict[str, Any]]:
        """嘴侧 bundle Layer A/B：最近 N tick（带 activity + interior.mood，09 §4.4.2/3）。"""
        return _ticks_mod.get_ticks_for_bundle(self._conn, limit=limit, since_iso=since_iso)

    def get_highlight_episodes(self, *, since_iso: str, limit: int) -> list[dict[str, Any]]:
        """嘴侧 bundle Layer C：近窗口 + cooldown>0 高光，按 sig/ts top-N（09 §4.4.4）。"""
        return _ticks_mod.get_highlight_episodes(self._conn, since_iso=since_iso, limit=limit)

    def decay_episode_recall(self, *, tick_ids: list[int], now_iso: str) -> None:
        """flush_bundle 选中高光后衰减其 cooldown（09 §4.4.4.2）。需在事务内。"""
        self._require_transaction("decay_episode_recall")
        _ticks_mod.decay_episode_recall(self._conn, tick_ids, now_iso=now_iso)

    def get_episodes_page(
        self, *, limit: int, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        """高光闪回分页：按 id DESC 取一页 significance>=7 tick（web /episodes 用）。"""
        return _ticks_mod.get_episodes_page(self._conn, limit=limit, before_id=before_id)

    def query_active_thoughts(
        self,
        *,
        now: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """读未过期 thought（``status='active'`` 且 ``expire_at > now``）。

        ``now`` 必须是合法 ISO8601；非法值在入口立即 raise——避免
        SQL ``WHERE expire_at > :now`` 拿到 ``"banana"`` 后 lexicographic
        比较永远 false 的静默 bug。
        """
        validated_now = _iso_adapter.validate_python(now)
        return _thoughts_mod.query_active_thoughts(
            self._conn,
            now=validated_now,
            limit=limit,
        )

    # ─── main session messages 读方法（仅读，不需事务）──────

    def get_recent_messages(
        self,
        *,
        session_key: str,
        limit: int = 30,
    ) -> list[MainSessionMessage]:
        """取某 session 最近 N 条消息（时间正序）。"""
        return _messages_mod.get_recent(self._conn, session_key=session_key, limit=limit)

    def get_messages_since_ms(
        self,
        *,
        session_key: str,
        since_ms: int,
        limit: int = 200,
    ) -> list[MainSessionMessage]:
        """取某 session 自 ``since_ms`` 以来的消息（时间正序，丢头保尾）。"""
        return _messages_mod.get_since_ms(
            self._conn, session_key=session_key, since_ms=since_ms, limit=limit
        )

    def get_messages_between_ms(
        self,
        *,
        session_key: str,
        since_ms: int,
        until_ms: int,
        limit: int = 2000,
    ) -> list[MainSessionMessage]:
        """取某 session ``[since_ms, until_ms)`` 区间内的消息（bounded day-window）。

        与 ``get_messages_since_ms``（仅 since 下界）不同，SQL 同时加上下界，
        ``limit`` 只在窗口内生效。dream Step 1.summarize 总结昨日整天窗用。
        """
        return _messages_mod.get_between_ms(
            self._conn,
            session_key=session_key,
            since_ms=since_ms,
            until_ms=until_ms,
            limit=limit,
        )

    def get_messages_after_cursor(
        self,
        *,
        session_key: str,
        after_ts_ms: int | None,
        after_seq: int | None,
        after_id: int | None,
        limit: int = 200,
    ) -> list[MainSessionMessage]:
        """事件流：取严格大于游标 ``(ts_ms, seq, id)`` 的下一页（正序）。

        watcher 专用——与 get_messages_since_ms（丢头保尾）不同，这里不丢头，
        连发多条分页拉取不丢失（earlier review N-5）。
        """
        return _messages_mod.get_after_cursor(
            self._conn,
            session_key=session_key,
            after_ts_ms=after_ts_ms,
            after_seq=after_seq,
            after_id=after_id,
            limit=limit,
        )

    def exists_partner_after_cursor(
        self,
        *,
        session_key: str,
        after_ts_ms: int | None,
        after_seq: int | None,
        after_id: int | None,
    ) -> bool:
        """游标之后是否存在任一 ``role=partner`` 消息（watcher 唤醒判据，earlier milestone N-1）。

        watcher 不推 cursor，不能只扫第一页；DB 索引 + ``LIMIT 1`` 一次性判存在。
        """
        return _messages_mod.exists_partner_after_cursor(
            self._conn,
            session_key=session_key,
            after_ts_ms=after_ts_ms,
            after_seq=after_seq,
            after_id=after_id,
        )

    def get_max_message_cursor(self, *, session_key: str) -> tuple[int, int, int] | None:
        """表尾最大游标 ``(ts_ms, seq, id)``（watcher.prime 设基线）。"""
        return _messages_mod.get_max_cursor(self._conn, session_key=session_key)

    def get_latest_message(
        self,
        *,
        session_key: str,
    ) -> MainSessionMessage | None:
        """某 session 最后一条消息（chat_in_progress 检测用），不存在返回 None。"""
        return _messages_mod.get_latest(self._conn, session_key=session_key)

    def get_latest_visible_contact_expression(
        self,
        *,
        session_key: str,
        cursor: tuple[int, int, int],
        now_ms: int,
    ) -> tuple[int, str] | None:
        """取当前已读范围内最近的双方可见表达，不返回正文。"""
        return _messages_mod.get_latest_visible_contact_expression(
            self._conn,
            session_key=session_key,
            cursor=cursor,
            now_ms=now_ms,
        )

    # ── watcher_cursor（「心已读书签」持久化；运行期写权收敛到 sense_io）──

    def get_watcher_cursor(self, *, session_key: str) -> tuple[int, int, int] | None:
        """读持久化的「心已读书签」 ``(ts_ms, seq, id)``；未持久化返 ``None``。"""
        return _watcher_cursor_mod.get(self._conn, session_key=session_key)

    def set_watcher_cursor(self, *, session_key: str, ts_ms: int, seq: int, id: int) -> None:
        """持久化「心已读书签」。路 X / earlier milestone：运行期写者是 ``sense_io``（读完即推），
        watcher 仅 prime 首启设 baseline。需在 ``transaction()`` 里调。"""
        self._require_transaction("set_watcher_cursor")
        _watcher_cursor_mod._set_impl(
            self._conn, session_key=session_key, ts_ms=ts_ms, seq=seq, id=id
        )

    def get_message_by_msg_id(self, *, session_key: str, msg_id: str) -> MainSessionMessage | None:
        """按 ``(session_key, msg_id)`` 唯一键取一条（幂等回查用）。"""
        return _messages_mod.get_by_msg_id(self._conn, session_key=session_key, msg_id=msg_id)

    def get_max_message_ts_ms(self, *, session_key: str) -> int | None:
        """某 session 已缓存的最新 ts_ms（watcher 增量拉取用）。"""
        return _messages_mod.get_max_ts_ms(self._conn, session_key=session_key)

    def get_max_message_seq(self, *, session_key: str) -> int | None:
        """某 session 已缓存的最大 seq（watcher 判断是否有新消息）。"""
        return _messages_mod.get_max_seq(self._conn, session_key=session_key)

    def count_messages(self, *, session_key: str | None = None) -> int:
        """统计消息行数；不传 ``session_key`` 时统计全表。"""
        return _messages_mod.count_messages(self._conn, session_key=session_key)

    # ─── place_visits（PlaceStore，L3）───────────────────────

    def get_place_visit_stats(
        self, place_keys: list[str]
    ) -> dict[str, _places_mod.PlaceVisitStats]:
        """按候选 ``place_key`` 批量查到访聚合（pre-act exact lookup，第四趴 §6）。

        仅读不需事务；没来过的 key 不在返回 dict 里。写入无独立方法——
        ``place_visits`` 由 ``insert_tick`` 同事务派生（第四趴 §3），不提供绕过
        派生逻辑的直写入口。
        """
        return _places_mod.get_visit_stats(self._conn, place_keys)

    def rebuild_place_visits(self) -> int:
        """清空并从 tick 历史重建 ``place_visits``（第四趴 §8）；返回重建行数。需在事务内。"""
        self._require_transaction("rebuild_place_visits")
        return _places_mod.rebuild_place_visits(self._conn)

    # ─── 计数 / 元信息（仅读，不需事务）──────────────────────

    def count_place_visits(self) -> int:
        """place_visits 表行数。"""
        return _conn_mod.count(self._conn, "place_visits")

    def count_ticks(self) -> int:
        """tick 表行数。"""
        return _conn_mod.count(self._conn, "tick")

    def count_thoughts(self) -> int:
        """thought 表行数。"""
        return _conn_mod.count(self._conn, "thought")

    def count_episode_recalls(self) -> int:
        """episode_recall 表行数。"""
        return _conn_mod.count(self._conn, "episode_recall")

    def get_schema_version(self) -> int | None:
        """schema 版本（``schema_version`` 表 max(version)）。"""
        return _conn_mod.get_schema_version(self._conn)


__all__ = [
    "KindredDB",
    "NestedTransactionError",
    "TransactionRequiredError",
    "TransactionStateError",
]
