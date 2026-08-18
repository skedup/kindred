"""MessageWatcher —— 让心「听见 partner」的**无状态唤醒器**（路 X / earlier milestone）。

daemon 职责 2（docs/13 §70/§125）：发现 partner（user）说了新话 → 以
``source="watcher"`` 的 :class:`TriggerEvent` 唤醒 tick_graph。

═══ 路 X：cursor 统一消费（2026-06-18 discussion）═══

**cursor 的唯一写者是 ``sense_io``（tick 内部读完即推 ``watcher_cursor`` 表）**，
watcher 退化为**只读表 cursor 的无状态唤醒器**——不持内存游标、不打包 messages、
不 ack、不写 cursor。这彻底消除「watcher.ack 与 sense_io 双写 cursor」的时序竞争
（原方案 Y 的坑）。

- :meth:`MessageWatcher.poll_once`：读表最新 cursor → 问 source
  ``exists_partner_after``（cursor 后是否存在任一 partner）→ 存在且 debounce 冷却
  到期则 fire。事件 ``payload`` 为空 ``{}``（graph tick 路径不消费 payload.messages，
  它自己经 sense_io 读 cursor 后消息）。
- :class:`MessageSource` 抽象「消息/游标从哪来」。生产用 :class:`DbPollMessageSource`
  （读本库 ``main_session_messages`` / ``watcher_cursor``）。测试用 fake source 注入。

═══ 唤醒判据：exists_partner_after，不翻页（earlier milestone 二轮 N-1）═══

watcher 不推 cursor，**不能只扫第一页**——若 cursor 后先有整页 my_voice/my_heart、
再有 partner，翻页 ``any()`` 会永远只读同一页非 partner，后续 partner 对 watcher
不可见。故判据下沉到 DB :meth:`MessageSource.exists_partner_after`
（``role=partner`` + row-value 严格大于游标 + ``LIMIT 1``，O(log n) 不翻页）。

═══ 稳定游标（earlier review N-2/N-4）═══

游标是 ``(ts_ms, seq, id)`` 三元组（:class:`MessageCursor`）。前两键来自消息时间 /
上游序号；第三键 ``id`` 是 DB 主键（全局单调），用于 N-4 场景——同毫秒且 ``seq``
都为 NULL 的多条消息靠 ``id`` 区分，不漏尾。``seq`` 缺失（schema 允许 NULL）按 -1
归一。``session_key`` 固定到构造函数——一个 watcher 实例只盯一个 session。

═══ prime：启动不重放历史（earlier review N-1）═══

watcher 启动时调 :meth:`prime`：**优先从库恢复**持久化的 cursor，仅首次启动
（库无游标）才把游标设到表当前尾部（``max_cursor``）并落库作 baseline——表里已有的
历史 partner 消息不被当成「新消息」唤醒。docs/13 §561「第一条消息不被冷却按住」指的
是 watcher 启动**之后**的新消息，不是持久表里的旧消息。

═══ debounce：连发合并（docs/13 §200-222，5/29 拍板）═══

partner 说完话 30min 内连发的消息在 **sense_io 读取前**仍保留于 message 表（watcher
无内存 buffer）：冷却内 watcher ``should_skip`` 不重复 fire，消息留表里等 sense_io
下个 tick（含 heartbeat 兜底）按 cursor 一并消费。``record`` 在 fire 时做。

**triggered_at 用触发时刻，不是消息时间（earlier review N-3）**：``poll_once`` 接收 wall-clock
ISO，``TriggerEvent.triggered_at`` = 该时刻。graph ``sense_derive`` 用它推进时钟 /
算 elapsed 衰减。

═══ best-effort 失败语义（earlier milestone）═══

watcher 无 pending/ack。sense_io 读取非空页后立即推进 cursor：读取或推进前失败时，heartbeat
或后续 watcher poll 仍可能再次唤醒；cursor 已推进后的下游 tick 失败不会回放消息。debounce
挡住 watcher 立即重 fire 是为了避免失败风暴，不构成可靠重试保证。

═══ 历史背景（已不在生产路径，仅供溯源）═══

旧方案 Y 曾让 watcher 持内存 buffer / pending、打包 ``payload={"messages":[...]}``、
daemon invoke 成功后 ``ack`` 推 committed 游标。路 X / earlier milestone 已全部移除，cursor 写权
收敛到 sense_io。完整演进见
``docs/archive/discussions/2026-06/2026-06-18-cursor-unified-consume.md``。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from kindred.db import (
    KindredDB,
    MainSessionMessage,
)
from kindred.runtime.scheduler import Debouncer

logger = logging.getLogger(__name__)

# seq 缺失（schema 允许 NULL）时的兜底排序键。比任何真实 seq（>=0）都小，
# 保证「无 seq 的同 ts_ms 行」排在「有 seq 行」之前，且游标比较单调。
# 与 db.messages.get_after_cursor / get_max_cursor 的 COALESCE(seq, -1) 一致。
_SEQ_MISSING = -1


# ─── 稳定游标（earlier review N-2/N-4）────────────────────────────────────


@dataclass(frozen=True, order=True)
class MessageCursor:
    """增量游标 ``(ts_ms, seq, id)``——watcher 已处理到的最新位置。

    ``order=True`` 让 dataclass 按字段顺序（ts_ms → seq → id）支持比较。``seq``
    缺失按 ``_SEQ_MISSING`` 归一；``id`` 是 DB 主键（全局单调），作为同 ts_ms+seq
    下的最终 tiebreak（earlier review N-4：同毫秒、无 seq 的多条消息靠 id 区分）。

    「是否新」判定由 source 侧 SQL 严格大于完成（:meth:`MessageSource.fetch_after`）；
    watcher 持游标仅用于传给 source 和推进。
    """

    ts_ms: int
    seq: int
    id: int

    @classmethod
    def of(cls, msg: MainSessionMessage) -> MessageCursor:
        """从消息构造游标。``seq=None`` 归一为 ``_SEQ_MISSING``；``id`` 必须已落库。"""
        if msg.id is None:
            raise ValueError("MessageCursor 需要已落库消息（msg.id 不能为 None）")
        return cls(
            ts_ms=msg.ts_ms,
            seq=msg.seq if msg.seq is not None else _SEQ_MISSING,
            id=msg.id,
        )

    @classmethod
    def from_tuple(cls, t: tuple[int, int, int] | None) -> MessageCursor | None:
        """从 db 层返回的 ``(ts_ms, seq, id)`` 元组构造（``None`` 透传）。"""
        return None if t is None else cls(ts_ms=t[0], seq=t[1], id=t[2])


# ─── TriggerEvent（daemon → tick_graph 统一事件）──────────────────


@dataclass(frozen=True)
class TriggerEvent:
    """daemon 给 tick_graph 的输入事件（docs/13 §131）。

    ``source`` 是 ``TriggerSource``（heartbeat / watcher / cold_start）；
    ``payload`` 是**通用扩展位**，按 source 不同可携带额外参数。**路 X / earlier milestone 后
    watcher 的 payload 默认为空 ``{}``**——消息不再随事件打包，graph tick 经
    ``sense_io`` 读 ``watcher_cursor`` 后消息自己填 chat_window（不读 payload.messages）。

    ``triggered_at`` 是**事件被发给 graph 的时刻**（wall-clock ISO，earlier review N-3），
    不是消息自身时间——graph ``sense_derive`` 用它推进时钟 / 算 elapsed。

    graph 怎么消化 是 docs/14 的事；本 dataclass 只定结构。
    """

    source: str
    triggered_at: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_invoke_input(self) -> dict[str, Any]:
        """转成 ``graph.invoke`` 入参（与 daemon 现有裸 dict 调用兼容）。"""
        return {
            "trigger_source": self.source,
            "triggered_at": self.triggered_at,
            **self.payload,
        }


# ─── MessageSource 接口 + DB-poll 实现 ────────────────────────────


class MessageSource(Protocol):
    """「新消息从哪来」的抽象（discussion §3.3 方案 Y）。

    earlier milestone 给 :class:`DbPollMessageSource`；ws chat.history 实现留 earlier milestone。
    测试用 fake source（返回预置列表）注入。
    """

    def fetch_after(
        self,
        *,
        session_key: str,
        cursor: MessageCursor | None,
        limit: int,
    ) -> Sequence[MainSessionMessage]:
        """事件流分页：取**严格大于** ``cursor`` 的下一页，按 ``(ts_ms, seq, id)``
        正序（老→新）。``cursor=None`` 表示「从头」（拉最早一页）。

        **辅助能力（路 X / earlier milestone）**：生产 watcher 已不用本方法唤醒（改用
        :meth:`exists_partner_after` 不翻页判存在）。保留作为 source 的分页读能力：
        sense_io 经 db facade 同款 ``get_after_cursor`` 读 chat_window；将来 step「回忆」
        可能用它拉历史页。语义：连发超过 ``limit`` 条不丢头（earlier review N-5）。
        """
        ...

    def exists_partner_after(self, *, session_key: str, cursor: MessageCursor | None) -> bool:
        """游标之后是否**存在**任一 partner 消息（watcher 唤醒判据，earlier milestone N-1）。

        watcher 不推 cursor，不能只扫一页——若 cursor 后先有整页非 partner、再有
        partner，翻页 ``any()`` 看不到后面那页。实现应一次性判存在（DB 索引 +
        ``LIMIT 1``，不翻页）。``cursor=None`` 表示「从头」。
        """
        ...

    def max_cursor(self, *, session_key: str) -> MessageCursor | None:
        """表当前尾部最大游标（watcher.prime 设基线用）；表空返 ``None``。"""
        ...

    def load_cursor(self, *, session_key: str) -> MessageCursor | None:
        """读持久化的「心已读书签」 cursor（earlier milestone 崩溃恢复）；未持久化返 ``None``。"""
        ...

    def save_cursor(self, *, session_key: str, cursor: MessageCursor) -> None:
        """持久化「心已读书签」 cursor 到 ``watcher_cursor`` 表。

        **路 X / earlier milestone**：运行期唯一写者是 ``sense_io``（读完即推）。watcher 仅
        :meth:`prime` 首启时调本方法设 baseline；运行期 watcher 不再写 cursor
        （无 ack / 无 _commit_cursor）。
        """
        ...


class DbPollMessageSource:
    """从 ``main_session_messages`` 表按游标分页增量读新行（事件流语义）。

    earlier milestone 的 DB-poll 实现：真来源（嘴写库 / ws）把消息落进表，watcher 经
    ``get_messages_after_cursor`` 按 ``(ts_ms, seq, id)`` ASC 严格大于游标拉下一页
    （N-5 不丢头），不复用 ``get_messages_since_ms`` 的「最近窗口」API。watcher
    逻辑据此独立验证，不耦合 ws（earlier milestone）。
    """

    def __init__(self, db: KindredDB) -> None:
        self._db = db

    def fetch_after(
        self,
        *,
        session_key: str,
        cursor: MessageCursor | None,
        limit: int,
    ) -> Sequence[MainSessionMessage]:
        c = cursor
        return self._db.get_messages_after_cursor(
            session_key=session_key,
            after_ts_ms=c.ts_ms if c else None,
            after_seq=c.seq if c else None,
            after_id=c.id if c else None,
            limit=limit,
        )

    def exists_partner_after(self, *, session_key: str, cursor: MessageCursor | None) -> bool:
        c = cursor
        return self._db.exists_partner_after_cursor(
            session_key=session_key,
            after_ts_ms=c.ts_ms if c else None,
            after_seq=c.seq if c else None,
            after_id=c.id if c else None,
        )

    def max_cursor(self, *, session_key: str) -> MessageCursor | None:
        return MessageCursor.from_tuple(self._db.get_max_message_cursor(session_key=session_key))

    def load_cursor(self, *, session_key: str) -> MessageCursor | None:
        return MessageCursor.from_tuple(self._db.get_watcher_cursor(session_key=session_key))

    def save_cursor(self, *, session_key: str, cursor: MessageCursor) -> None:
        with self._db.transaction():
            self._db.set_watcher_cursor(
                session_key=session_key,
                ts_ms=cursor.ts_ms,
                seq=cursor.seq,
                id=cursor.id,
            )


# ─── MessageWatcher ───────────────────────────────────────────────


class MessageWatcher:
    """轮询消息来源，发现 partner 新消息 → 产 watcher :class:`TriggerEvent`。

    一个实例只盯一个 ``session_key``（构造函数固定，earlier review N-2）——不暗示多
    session 复用。依赖注入：``source``（MessageSource）/ ``debouncer``（与 daemon
    共享，保证 watcher 冷却与 heartbeat 独立）。

    用法（接 daemon 时）::

        watcher.prime()                      # 启动：优先从库恢复，首次才设表尾 baseline
        ...
        evt = watcher.poll_once(now=monotonic(), triggered_at=now_iso())
        if evt is not None:
            graph.invoke(evt.to_invoke_input())
    """

    def __init__(
        self,
        source: MessageSource,
        debouncer: Debouncer,
        *,
        session_key: str,
    ) -> None:
        self._source = source
        self._debouncer = debouncer
        self._session_key = session_key
        # 路 X / earlier milestone：watcher 退化为**无状态唤醒器**——不持内存游标、不打包
        # messages、不 ack、不写 cursor。cursor 的唯一写者是 sense_io（读完即推，
        # 写 watcher_cursor 表）。watcher 每次 poll **只读**表 cursor 判「该不该唤醒」。
        # 这彻底消除「watcher.ack 与 sense_io 双写 cursor」的时序竞争（原路 Y 的坑）。

    # ── prime（earlier milestone：只负责首启 baseline，不持内存态）──

    def prime(self) -> None:
        """首次启动设 cursor baseline 到表，保「启动不重放历史」契约（earlier review N-1）。

        路 X / earlier milestone：watcher 不再持内存游标，cursor 唯一真相是 ``watcher_cursor`` 表。
        prime 只在**库无 cursor（首次启动）**时把表尾 ``max_cursor`` 作为 baseline
        持久化——否则 sense_io 首 tick 会 ``after_*=None`` 读全部历史（重放，破坏
        D2.5 契约）。库已有 cursor（崩溃重启 / 跑过至少一次）→ no-op，sense_io 直接
        从该位置往后读。

        baseline 是**启动期一次性初始化**（在 daemon 主循环开始前调），与 sense_io
        运行期推进无并发——不违反「sense_io 单一写者」（那针对的是运行期 ack 竞争）。

        ⚠残留极窄边界：表**完全为空**（``max_cursor`` 为 ``None``）→ 无 baseline 可写，
        sense_io 首 tick 会 ``after_*=None`` 读「从头」。实务中 daemon 启动前 earlier milestone
        baseline sync 已先填表，表几乎不会空；真空表场景下也只是首 tick 读到已有
        历史一次，sense_io 读完即推 cursor，后续不再重放。
        """
        persisted = self._source.load_cursor(session_key=self._session_key)
        if persisted is not None:
            logger.info("watcher: cursor exists (=%s), no baseline needed", persisted)
            return
        baseline = self._source.max_cursor(session_key=self._session_key)
        if baseline is None:
            logger.debug("watcher prime: empty table, no baseline to persist")
            return
        self._source.save_cursor(session_key=self._session_key, cursor=baseline)
        logger.debug("watcher prime: baseline persisted to table cursor=%s", baseline)

    # ── 单轮轮询 ──

    def poll_once(self, *, now: float, triggered_at: str) -> TriggerEvent | None:
        """读表 cursor 之后的未读消息——**含 partner 且冷却到期 → fire 唤醒事件**。

        **路 X / earlier milestone：watcher 无状态唤醒器**：cursor 唯一真相是 ``watcher_cursor`` 表
        （sense_io 读完即推）。本方法每次从表读最新 cursor、读它之后一页消息，
        **只读不写**：不推 cursor / 不 ack / 不打包 messages payload。只当唤醒信号用。

        唤醒判据（skedush 2026-06-18 拍板）：**未读消息里含任一 partner 行**就唤醒
        ——不是只看最新一条（你说完话后嘴刚好回了一句、最新变 my_voice，不该漏掉
        你前面那条 partner）。my_voice / my_heart（嘴/心自己的话）不触发唤醒。

        cursor 推进完全交给 sense_io（tick 读完即推）：唤醒后 graph.invoke 走 sense_io
        读同批消息并推进 cursor。读取/推进前失败时下轮仍可能读到同批；推进后下游失败
        不回放，符合 best-effort 感知边界。

        debounce：冷却未到期 → 不 fire（你连发多条不会 fire 多次），等冷却到期同批一起处理。
        record 在 fire 时做（原在 ack，现无 ack）。

        Parameters
        ----------
        now
            单调时钟（``time.monotonic()``），用于 Debouncer 冷却判定 + record。
        triggered_at
            wall-clock ISO——事件真正触发时刻，填进 :class:`TriggerEvent`（earlier review N-3）。

        Returns
        -------
        TriggerEvent | None
            表 cursor 之后的未读消息含 partner 且冷却到期 → watcher 事件（payload 空）；
            否则 ``None``。
        """
        if not self._has_unread_partner():
            return None
        if self._debouncer.should_skip("watcher", now):
            logger.debug("watcher debounced, accumulate (will fire when cooldown elapses)")
            return None

        self._debouncer.record("watcher", now)
        evt = TriggerEvent(source="watcher", triggered_at=triggered_at)
        logger.info("watcher fire: unread partner message(s) at %s", triggered_at)
        return evt

    def _has_unread_partner(self) -> bool:
        """表 cursor 之后的未读消息里是否含 partner 行（唤醒判据）。

        每次从表读最新 cursor（sense_io 可能刚推过）→ 问「cursor 之后是否存在
        partner」。只读不写。全非 partner（只有 my_voice / my_heart）→ 不唤醒；
        不需跳过它们（sense_io 下个 tick 读到会连同非 partner 一起推过 cursor）。

        **N-1（earlier milestone 二轮）不能只扫第一页**：watcher 不推 cursor，若 cursor 后先有
        整页 ``fetch_limit`` 条 my_voice/my_heart、再有 partner，翻页 ``any()`` 会永远
        只读同一页非 partner → 后续 partner 对 watcher 不可见（只能等 heartbeat 兼底
        消费）。下沉到 DB ``exists_partner_after``（索引 + LIMIT 1，O(log n) 不翻页）。

        表 cursor 为 None（首启未 prime baseline / 真空表）→ 从头判是否存在 partner。
        """
        cursor = self._source.load_cursor(session_key=self._session_key)
        return self._source.exists_partner_after(session_key=self._session_key, cursor=cursor)
