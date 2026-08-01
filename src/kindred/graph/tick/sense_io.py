"""``T1.sense.io`` —— tick 入口的 IO 读节点。

参考文档：14-heart-graph.md §3.1

职责：

- read ``state_latest`` view → ``state["prev_state"]``
- ``dict(prev_state)``（浅拷起底） → ``state["next_state"]``
- read ``watcher_cursor`` 后的新消息 → ``state["chat_window"]``（路 X 统一消费）
- 消费后 pull 近期联系事实 → ``state["recent_contact"]``（只供 sense.llm）

路 X：cursor 统一消费（2026-06-18 discussion）
=============================================

``chat_window`` 不再 stub ``[]``。**无论 watcher 还是 heartbeat 触发**，本节点
都从 ``watcher_cursor`` 读心「已读书签」之后的新消息（``get_messages_after_cursor``），
转成 chat_window 喂进 sense prompt——让心每次睁眼都感知「自上次以来你说了什么」。

cursor 由本节点在读到非空页后**立即推进**到页尾，不穿过 graph 等待 persist。若读取或推进
本身失败，后续 tick 仍可能重读；cursor 已推进后的 sense/act/persist 失败不会回放本批消息。
这是有意接受的 best-effort 感知语义，不提供 ack/replay 保证。

**不带 is_new / 不给背景**（Q1 拍板）：chat_window 只承载 cursor 后的新消息，要么
全新要么全空。将来 step「回忆」往 chat_window 注历史背景时再引入 is_new 区分。
role 映射：``partner→user``，``my_voice``/``my_heart→mouth``（ChatTurn role 二值）。
session_key 缺省（CLI 单跑/未配）→ chat_window 空，向后兼容。

为什么 daemon 只传触发元数据
============================

设计上 ``prev_state`` 应该是 db 真相源——daemon 不该自己读 state 然后塞
进 invoke。invoke 入口只承诺"我什么时候被触发的、谁触发的"，sense_io
自己 read 真相源 state_latest。这与 14 §3.1 设计一致，也避免 daemon /
node / db 三方对 state 的读视图不一致。

cold_start：库为空时 sense_io raise ``ColdStartError``——daemon 应在 invoke
前检测 cold_start 并走 bootstrap 路径，**不是** 由 sense_io 自己造空 state。

节点结构（三层，与其它节点一致）：

- 顶层 ``_t1_sense_io_impl(db, state)`` 是真实现，可独立测
- ``_make_t1_sense_io(db)`` 是 thin wrapper closure 只绑 db
- ``t1_sense_io(state)`` 是顶层 mock 透传，未注 deps 时使用

Provider 依赖以 keyword 显式注入；当它们出现共享生命周期或成组不变式时，再考虑
``SenseIoDeps``，不为参数数量提前造通用容器。
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import TYPE_CHECKING, Any, Literal

from kindred.db.messages import ROLE_PARTNER
from kindred.db.ticks import STATE_LAYERS
from kindred.graph._shared._common import NodeReturn
from kindred.graph._shared._errors import NodeContractError
from kindred.state.tick import ChatTurn, RecentContactContext, TickState

if TYPE_CHECKING:
    from collections.abc import Callable

    from kindred.db.facade import KindredDB
    from kindred.providers.environment import EnvironmentProvider
    from kindred.providers.recent_contact import RecentContactProvider


# 天气 TTL 默认值（分钟）——make_sense_io_node 未显式传时用此；生产路径由
# build_client_tick_graph 注 config.world.weather_ttl_minutes 覆盖。
_DEFAULT_WEATHER_TTL_MINUTES = 60


_LOG = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────


class ColdStartError(NodeContractError):
    """库中无 state_latest 行——cold_start 路径 daemon 应负责处理。

    sense_io 不自己造空 state；遇到空库直接 raise，由 daemon 在 invoke 前检测
    并走 bootstrap。
    """


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


def make_sense_io_node(
    db: KindredDB,
    *,
    session_key: str | None = None,
    environment_provider: EnvironmentProvider | None = None,
    recent_contact_provider: RecentContactProvider | None = None,
    weather_ttl_minutes: int = _DEFAULT_WEATHER_TTL_MINUTES,
    weather_location: str = "",
) -> Callable[[TickState], NodeReturn]:
    """构造 T1.sense.io closure。

    ``session_key``（路 X）：传入时 sense_io 读 ``watcher_cursor`` 后的新消息
    填 chat_window；``None``（CLI 单跑 / 未配）→ chat_window 恒空，向后兼容。

    ``environment_provider``（世界 Provider 层第一块砖）：传入时 sense_io 按「地点变 或
    TTL 过期」刷 ``next_state.environment`` 的天气（pull + in-tick TTL，§7 Q3）；``None``
    （CLI 单跑 / 未配）→ environment 原样透传，向后兼容。``weather_location``（config
    ``world.weather_location``）：精确查询位置，留空回落 ta 的 ``city``——sense_io 是其
    唯一解析处。**守铁律**：Provider 只产「世界给的」天气，写只落在 in-flight 的
    ``next_state``，真正落库仍是 T3.persist 唯一负责。

    ``recent_contact_provider`` 每拍在 chat consume 之后读当前持久 cursor 以内的
    最近双方表达；它不推 cursor，不写八层 state。未注入时返回 unavailable。

    用法（详 build.py / cli.py）::

        with KindredDB.open(db_path) as db:
            sense_io = make_sense_io_node(db, session_key=cfg.daemon.session_key)
            persist_nodes = make_persist_nodes(db, bundle_path)
            graph = build_tick_graph(
                sense_io_node=sense_io,
                persist_nodes=persist_nodes,
            )
            graph.invoke({"trigger_source": "heartbeat", "triggered_at": "..."})
    """
    return _make_t1_sense_io(
        db,
        session_key=session_key,
        environment_provider=environment_provider,
        recent_contact_provider=recent_contact_provider,
        weather_ttl_minutes=weather_ttl_minutes,
        weather_location=weather_location,
    )


# ─────────────────────────────────────────────────────────────────────
# 真实现（顶层 _impl）
# ─────────────────────────────────────────────────────────────────────


def _t1_sense_io_impl(
    db: KindredDB,
    state: TickState,
    *,
    session_key: str | None = None,
    environment_provider: EnvironmentProvider | None = None,
    recent_contact_provider: RecentContactProvider | None = None,
    weather_ttl_minutes: int = _DEFAULT_WEATHER_TTL_MINUTES,
    weather_location: str = "",
) -> NodeReturn:
    """T1.sense.io 真实现。

    14 §3.1：

    1. read ``state_latest`` view → prev_state（库空 → ``ColdStartError``）
    2. ``dict(prev_state)``（浅拷） → next_state（事件级字段 T2 / 衰减 T1.derive
       会改 next，prev 永远是真相源不可变）
    3. ``chat_window`` ← ``watcher_cursor`` 后的新消息，**读完即推 cursor**
       （路 X；``_read_chat_window`` → ``_advance_cursor``）
    4. ``recent_contact`` ← 当前已读表达的临时 pull projection

    入参契约：``state`` 应已含 ``trigger_source / triggered_at``（daemon
    invoke 注入），本节点不校（_common.validate_* 已穷举），上游传啥就是啥。

    返 patch（不带其它 key 干扰下游 derive / llm）::

        {
            "prev_state": dict,        # State.model_dump() 的 dict
            "next_state": dict,        # = dict(prev_state)（浅拷起底）
            "chat_window": [ChatTurn], # cursor 后新消息（读完即推 cursor，路 X）
            "recent_contact": dict,    # 只供 sense.llm，不进 canonical persistence
        }
    """
    # triggered_at（daemon 注入）用于天气 TTL 判断；trigger_source 仍由上游透传到 T3。
    triggered_at = state.get("triggered_at")

    prev_state_full = db.get_state_latest()
    if prev_state_full is None:
        _LOG.debug("T1.sense.io state_latest_found=false")
        raise ColdStartError(
            "T1.sense.io: state_latest is empty (cold_start). "
            "daemon should handle bootstrap before invoke.",
        )

    # state_latest 视图是 tick 整行（含元数据列），只取 8 层 state——
    # 否则 T3.validate_state 会拒额外字段（State extra=forbid）。
    prev_state: dict[str, Any] = {layer: prev_state_full[layer] for layer in STATE_LAYERS}
    prev_time = prev_state.get("time")
    prev_time_iso = prev_time.get("iso") if isinstance(prev_time, dict) else None
    _LOG.debug(
        "T1.sense.io state_latest_found=true prev_state_time=%s",
        prev_time_iso,
    )

    # next_state base = prev_state 的**浅拷**（不再 deepcopy 全树）。layer 对象此刻
    # 与 prev_state 共享，但下游节点（derive/sense_llm/act）都改成「读旧层 →
    # 构造全新层 → return 该层 patch」，不再原地 mutate 层内部，故 prev_state 不被
    # 污染（deepcopy→patch 改造，docs/discussions/2026-06-18 §6）。
    next_state = dict(prev_state)
    # 世界 Provider（第一块砖）：按「地点变 或 TTL 过期」刷天气。守铁律——构造**新**
    # environment dict 赋回，不原地 mutate 与 prev_state 共享的层。
    next_state["environment"] = _refresh_environment(
        next_state.get("environment"),
        triggered_at,
        environment_provider,
        weather_location,
        weather_ttl_minutes,
    )
    chat_window = _read_chat_window(db, session_key)
    recent_contact = _observe_recent_contact(recent_contact_provider, triggered_at)

    return {
        "prev_state": prev_state,
        "next_state": next_state,
        "chat_window": chat_window,
        "recent_contact": recent_contact.model_dump(mode="json"),
    }


def _observe_recent_contact(
    provider: RecentContactProvider | None,
    triggered_at: object,
) -> RecentContactContext:
    """严格解析本拍时间并 pull 近期联系投影；失败只影响该临时事实。"""
    unavailable = RecentContactContext(available=False)
    if provider is None or not isinstance(triggered_at, str):
        return unavailable
    try:
        now = _dt.datetime.fromisoformat(triggered_at.replace("Z", "+00:00"))
    except ValueError:
        return unavailable
    if now.tzinfo is None or now.utcoffset() is None:
        return unavailable
    context = provider.observe(now=now)
    _LOG.debug(
        "T1.sense.io recent_contact_available=%s visible=%s",
        context.available,
        context.recent_actor is not None,
    )
    return context


# ─────────────────────────────────────────────────────
# environment：天气 TTL 刷新（世界 Provider 第一块砖）
# ─────────────────────────────────────────────────────


def _refresh_environment(
    env: object,
    triggered_at: object,
    provider: EnvironmentProvider | None,
    weather_location: str,
    ttl_minutes: int,
) -> object:
    """**地点变 或 TTL 过期** 则调 Provider 刷天气；否则留旧值。

    查询位置 ``effective_loc = weather_location（配置） or env.city``——sense_io 是其
    唯一解析处（provider 给啥查啥）。缓存键 = ``weather_cached_for``：当它与当前
    ``effective_loc`` 不一致（搬家 / 改 weather_location / 旧 state 无此字段），即便没到
    TTL 也立即刷——否则会端着旧地区的天气。

    守铁律：Provider 只产「世界给的」天气，``ambience``（ta 自己描述）原样保留。返回
    **新** environment dict（不原地 mutate 共享层）；未刷新时原样返回（含
    ``provider is None`` 的 CLI 单跑路径，向后兼容）。

    软心：Provider 失败（连降级虚拟都崩）→ 记 warning + 留旧值，**不崩 tick**。
    """
    if provider is None or not isinstance(env, dict) or not isinstance(triggered_at, str):
        return env
    city = env.get("city")
    if not isinstance(city, str) or not city:
        return env
    effective_loc = weather_location or city
    if weather_cache_is_current(
        env,
        triggered_at,
        weather_location=weather_location,
        ttl_minutes=ttl_minutes,
    ):
        return env  # 同地点 且 未超时 → 不刷
    try:
        reading = provider.get_weather(effective_loc)
    except Exception:  # noqa: BLE001 —— 感知软心：拉天气失败不该崩 tick
        _LOG.warning(
            "T1.sense.io weather refresh failed, keep stale (location=%s)",
            effective_loc,
            exc_info=True,
        )
        return env
    _LOG.debug(
        "T1.sense.io weather refreshed location=%s weather=%s temp=%.1f",
        effective_loc,
        reading.weather,
        reading.temperature,
    )
    return {
        **env,
        "weather": reading.weather,
        "temperature": reading.temperature,
        "feels_like": (reading.temperature if reading.feels_like is None else reading.feels_like),
        "humidity": reading.humidity,
        "wind": reading.wind,
        "moon_phase": reading.moon_phase,
        "sunrise": reading.sunrise,
        "sunset": reading.sunset,
        "uv_index": reading.uv_index,
        "precip_mm": reading.precip_mm,
        "weather_cached_at": triggered_at,
        "weather_cached_for": effective_loc,
    }


def weather_cache_is_current(
    environment: object,
    triggered_at: str,
    *,
    weather_location: str,
    ttl_minutes: int,
) -> bool:
    if not isinstance(environment, dict):
        return False
    city = environment.get("city")
    cached_at = environment.get("weather_cached_at")
    if not isinstance(city, str) or not city or not isinstance(cached_at, str):
        return False
    try:
        cached = _dt.datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
        now = _dt.datetime.fromisoformat(triggered_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if cached.tzinfo is None:
        cached = cached.replace(tzinfo=_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    effective_location = weather_location or city
    age = now - cached
    return environment.get("weather_cached_for") == effective_location and _dt.timedelta(
        0
    ) <= age < _dt.timedelta(minutes=ttl_minutes)


# ─────────────────────────────────────────────────────
# chat_window（路 X：cursor 后新消息）
# ─────────────────────────────────────────────────────


def _read_chat_window(db: KindredDB, session_key: str | None) -> list[dict[str, object]]:
    """read ``watcher_cursor`` 后的新消息 → chat_window，**读完即推 cursor**（路 X）。

    路 X（skedush 2026-06-18 拍板「sense_io 读完直接处理」）：读消息 + 标记已读
    在**同一节点、同一 db handle** 顺序完成——不再把 cursor 书签穿过半个 graph
    交给 persist。cursor = 「心已读到哪条」的单一书签。

    **事务边界（N-2，codex 二轮）**：read（``get_watcher_cursor`` /
    ``get_messages_after_cursor``）在事务外，``_advance_cursor`` 另开事务写——
    **非事务原子，依赖单线程 daemon 顺序语义**。唤醒/心跳 tick 串行跑在 daemon
    主循环，唯一写表者（HistorySync.sync_once）也在同一
    主循环串行——sense_io 读完到 ``_advance_cursor`` 写之间不会有并发写者插行。
    所以不存在「新行排序落在本批末行之前、cursor 越过它」的窗口。若将来引入
    并发写者（多线程 / 多进程），需把 read + convert + set_watcher_cursor 收进同一
    ``db.transaction()`` 快照。

    推进语义：读到非空消息 → 立即把 cursor 推到**本页末行**（含非 partner 行，与
    watcher 原 「末行游标」语义一致，MessageCursor.of 统一 COALESCE(seq,-1)）。
    已读过的消息不再重读——这就是「感知非应答」：心听见了就算处理过，
    后续 sense_llm / act / persist 失败也不回放（路 X 主动放松“消息=专属 tick”，
      漏听一批感知不致命，下次你再说话照样被看见）。

    - ``session_key=None``（CLI 单跑 / 未配 daemon）→ 空 chat_window，不推。
    - cursor 为 None（首次启动未 prime / 表空）→ ``get_messages_after_cursor``
      的 after_* 均 None 语义为「从头」。但 daemon prime 已把 cursor 设到表尾，
      所以正常运行不会重放全部历史。
    - 软心：读 / 推 cursor 出错不崩 tick，降级空窗（并不推进）。
    """
    if session_key is None:
        return []
    try:
        cursor = db.get_watcher_cursor(session_key=session_key)
        after_ts_ms, after_seq, after_id = cursor if cursor is not None else (None, None, None)
        msgs = db.get_messages_after_cursor(
            session_key=session_key,
            after_ts_ms=after_ts_ms,
            after_seq=after_seq,
            after_id=after_id,
        )
    except Exception:  # noqa: BLE001 —— 感知软心：读消息失败不该崩 tick
        _LOG.warning("T1.sense.io read chat_window failed, degrade to empty", exc_info=True)
        return []

    if not msgs:
        return []

    _advance_cursor(db, session_key, msgs[-1])
    return [_msg_to_chat_turn(m) for m in msgs]


# seq NULL 归一为 -1——与 db.messages 的 COALESCE(seq, -1) / watcher MessageCursor.of 一致。
_SEQ_MISSING = -1


def _advance_cursor(db: KindredDB, session_key: str, last_msg: Any) -> None:
    """把 watcher_cursor 推到 ``last_msg`` 位置（读完即标记已读，路 X）。

    末行游标归一：``seq=None`` → ``_SEQ_MISSING``（-1），``id`` 必已落库（None 跳过
    推进）——与 db.messages COALESCE(seq,-1) / watcher ``MessageCursor.of`` 同语义，但
    不依赖 runtime 层（graph 只依赖 db facade，不反向依赖 runtime.watcher）。
    ``set_watcher_cursor`` 需事务，包在 ``db.transaction()`` 里。

    软心：推 cursor 失败不崩 tick（记 warning，下个 tick 会重读同批——与「读成功但
    推失败」同语义，至多重复感知一次，不丢）。
    """
    if last_msg.id is None:
        # id 是 DB 主键（全局单调），未落库无法作游标 tiebreak。正常不会发生
        # （get_messages_after_cursor 返的都是已落库行）；防御性跳过推进。
        _LOG.warning("T1.sense.io advance cursor skipped: last_msg.id is None")
        return
    try:
        with db.transaction():
            db.set_watcher_cursor(
                session_key=session_key,
                ts_ms=last_msg.ts_ms,
                seq=last_msg.seq if last_msg.seq is not None else _SEQ_MISSING,
                id=last_msg.id,
            )
    except Exception:  # noqa: BLE001 —— 感知软心：推 cursor 失败不该崩 tick
        _LOG.warning("T1.sense.io advance cursor failed, will re-read next tick", exc_info=True)


def _ms_to_iso(ts_ms: int) -> str:
    """毫秒 epoch → timezone-aware ISO8601 字符串（``ChatTurn.ts`` 要求）。

    ``ChatTurn.ts`` 是 ``IsoDatetime``（docs/14 §1.2 ``ts: str``），必须 timezone-aware
    且含时间分量。与 io_bridge ``_iso_now_ms`` 同范式：UTC + ``Z`` 后缀。
    """
    return (
        _dt.datetime.fromtimestamp(ts_ms / 1000, tz=_dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _msg_to_chat_turn(m: Any) -> dict[str, object]:
    """MainSessionMessage → ChatTurn dict（过 ``ChatTurn`` schema round-trip）。

    role 映射：partner（你）→ user；my_voice/my_heart（心嘴自己）→ mouth。
    ts：ms epoch → ISO8601 字符串（N-1：``ChatTurn.ts`` 是 ``IsoDatetime``，不是 int）。
    is_new 恒 True（Q1：chat_window 只装 cursor 后新消息）。content 用 text_summary。

    **经 ``ChatTurn.model_validate`` 构造再 dump**：坏形状（如 ts 不合法）当场报错，
    不会静默流进 TickState 把下游 persist/act 读 chat_window 的契约埋歪（N-1）。
    """
    role: Literal["user", "mouth"] = "user" if m.role == ROLE_PARTNER else "mouth"
    turn = ChatTurn(
        ts=_ms_to_iso(m.ts_ms),
        role=role,
        content=m.text_summary or "",
        is_new=True,
    )
    return turn.model_dump()


# ─────────────────────────────────────────────────────────────────────
# Factory thin wrapper
# ─────────────────────────────────────────────────────────────────────


def _make_t1_sense_io(
    db: KindredDB,
    *,
    session_key: str | None = None,
    environment_provider: EnvironmentProvider | None = None,
    recent_contact_provider: RecentContactProvider | None = None,
    weather_ttl_minutes: int = _DEFAULT_WEATHER_TTL_MINUTES,
    weather_location: str = "",
) -> Callable[[TickState], NodeReturn]:
    """绑 db、session_key 与 T1 Provider，返回 LangGraph 节点 closure。"""

    def t1_sense_io_closure(state: TickState) -> NodeReturn:
        return _t1_sense_io_impl(
            db,
            state,
            session_key=session_key,
            environment_provider=environment_provider,
            recent_contact_provider=recent_contact_provider,
            weather_ttl_minutes=weather_ttl_minutes,
            weather_location=weather_location,
        )

    return t1_sense_io_closure


# ─────────────────────────────────────────────────────────────────────
# 顶层 mock（保留兼容 build.py 默认装配）
# ─────────────────────────────────────────────────────────────────────
#
# build.py 默认走顶层 mock（拓扑测试不需要真 db）；真实接 db 时 build.py 接
# sense_io_node 参数走 closure。详 build.py docstring。


def t1_sense_io(state: TickState) -> NodeReturn:
    """T1 第 1 节点：mock 透传（无 deps 注入时使用）。

    返回空 patch，LangGraph 仍按拓扑 advance 到下一节点。真实路径：
    ``make_sense_io_node(db)`` 注入 db 走真实现。
    """
    del state
    return {}


__all__ = [
    "ColdStartError",
    "make_sense_io_node",
    "t1_sense_io",
]
