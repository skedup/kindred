"""``Step 2.reset_stack`` —— prune 昨日已总结消息（代码节点，无 LLM）。

参考文档：docs/11-dreaming.md §3（Step 2）、§6.4 / §8.1（重置是代码安全操作，
不受闸门影响）。

**B 方案（earlier milestone 拍板）下的语义**：早期文档说「重置 messages-stack.json」，但
该 json 从未落地——消息全在 ``main_session_messages`` 表（单一真相源）。「重置 stack」
真实意图是**防表无限涨撑爆 token**，所以这里改为 prune：只删**本次 Step 1 总结覆盖
的昨日窗口** ``[since_ms, until_ms)`` 内的消息。**表即 stack，没有第二份要同步。**

为什么是 bounded ``[since, until)`` 而非 ``ts_ms < until``（N-1）：Step 1 只总结
昨日窗口，若无条件删 ``until`` 之前所有消息，会连 ``since`` 之前**可能 Step 1
失败未总结**的 backlog 一起删（反例：6/12 梦 6/11 失败保留 6/11；6/13 梦 6/12
成功用 ``until=6/13`` 会连 6/11 一起删 → 6/11 从未总结就永久丢失）。更早 backlog
留给 retry / force-truncate（docs/11 §8.1）。今日凌晨「今天」消息（``ts_ms >= until``）
也自然不在区间内，保留待下次做梦。

- 输入：``state.messages_summary``（Step 1 已产出）+ ``dream_date`` + ``triggered_at``
- 输出 patch：``{"pruned_count": <删除条数>}``（观测用，可选）

**关键时序铁律**（11 §6.4 + §8.1）：

1. Step 2 在 Step 4 闸门**之前**执行——即便闸门撤销整次灵魂改写，prune 仍保留
   （昨日已被摘要，不 prune 会重复累积）。
2. **Step 1 总结失败 → 绝不 prune**：``messages_summary`` 空时跳过 prune，否则昨日
   消息「没摘要又被删」=永久丢失（§8.1 容错：stack 不重置，第二天重试）。

prune 区间 = ``day_window_ms`` 的 ``[since_ms, until_ms)``（与 summarize 读窗口**同源**，
共享 ``_window.day_window_ms``，杜绝「总结到某点、删到另一点」漂移）。

节点结构（与 summarize 对称的三层）：

- 顶层 ``_step2_reset_stack_impl(state, *, db, session_key)`` 真实现
- ``make_reset_stack_node(db, *, session_key)`` factory → closure
- ``step2_reset_stack(state)`` 顶层 mock 透传（未注 deps 时桩占位）
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kindred.config import DEFAULT_SESSION_KEY
from kindred.graph._shared._common import NodeReturn
from kindred.graph.dream._window import day_window_ms
from kindred.state.dream import DreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    from kindred.db.facade import KindredDB

_LOG = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


def make_reset_stack_node(
    db: KindredDB | None = None,
    *,
    session_key: str = DEFAULT_SESSION_KEY,
) -> Callable[[DreamState], NodeReturn]:
    """构造 dream Step 2.reset_stack closure。

    ``db`` 可选：传入时 prune 昨日已总结消息；不传（mock / 拓扑测）时软心跳过。
    """

    def reset_stack_closure(state: DreamState) -> NodeReturn:
        return _step2_reset_stack_impl(state, db=db, session_key=session_key)

    return reset_stack_closure


# ─────────────────────────────────────────────────────────────────────
# 真实现
# ─────────────────────────────────────────────────────────────────────


def _step2_reset_stack_impl(
    state: DreamState,
    *,
    db: KindredDB | None = None,
    session_key: str = DEFAULT_SESSION_KEY,
) -> NodeReturn:
    """Step 2 真实现：prune 昨日已总结消息（防表无限涨）。

    流程：

    1. 时序守卫：``messages_summary`` 空 → 跳过 prune（Step 1 失败，不能删未总结的）
    2. db 缺 → 软心跳过（mock / 拓扑测）
    3. 算昨日窗口 ``[since_ms, until_ms)``（与 summarize 同源）→ bounded prune
       （只删本次 summary 覆盖范围，不动 since 前 backlog）
    4. 返 ``{"pruned_count": n}``
    """
    summary = state.get("messages_summary")
    if not (summary and summary.strip()):
        # Step 1 总结失败 / 空白——绝不 prune，否则昨日消息没摘要又被删=永久丢失。
        _LOG.warning("dream Step 2.reset_stack: messages_summary 空 → 跳过 prune（保留昨日待重试）")
        return {"pruned_count": 0}

    if db is None:
        _LOG.debug("dream Step 2.reset_stack: db is None → 软心跳过 prune")
        return {"pruned_count": 0}

    dream_date = str(state.get("dream_date") or "")
    triggered_at = state.get("triggered_at")
    since_ms, until_ms = day_window_ms(dream_date, triggered_at)
    if since_ms is None or until_ms is None:
        _LOG.warning(
            "dream Step 2.reset_stack: 无法解析窗口 dream_date=%r → 跳过 prune",
            dream_date,
        )
        return {"pruned_count": 0}

    # 只 prune 本次 summary 覆盖的昨日窗口 [since, until)（N-1）。
    # 不能用 prune_older_than_ms(until)——那会连 ``since`` 之前可能 Step 1 失败
    # 未总结的 backlog 一起删（反例：6/12 梦 6/11 失败保留 6/11，6/13 梦 6/12
    # 成功用 until=6/13 会连 6/11 一起删→ 6/11 从未总结就永久丢失）。更早 backlog
    # 留给 retry / force-truncate（docs/11 §8.1）。
    with db.transaction():
        pruned = db.prune_messages_between_ms(
            session_key=session_key, since_ms=since_ms, until_ms=until_ms
        )
    _LOG.info(
        "dream Step 2.reset_stack: pruned %d 条 ([%d, %d), dream_date=%s)",
        pruned,
        since_ms,
        until_ms,
        dream_date,
    )
    return {"pruned_count": pruned}


# ─────────────────────────────────────────────────────────────────────
# 顶层 mock 透传（保留 D6.1 桩兼容 / 拓扑测试）
# ─────────────────────────────────────────────────────────────────────


def step2_reset_stack(state: DreamState) -> NodeReturn:
    """Step 2 顶层 mock 透传（未注 deps 时使用，桩占位）。

    不真碰 db，仅日志。真实路径：``make_reset_stack_node(db)`` 注入走
    ``_step2_reset_stack_impl``。
    """
    has_summary = bool(state.get("messages_summary"))
    _LOG.debug("dream Step 2.reset_stack (mock pass-through), summary_ready=%s", has_summary)
    return {"pruned_count": 0}
