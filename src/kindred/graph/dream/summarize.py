"""``Step 1.summarize`` —— 总结昨日 messages（LLM call #1）。

参考文档：docs/11-dreaming.md §4。

职责：read **昨日窗口**的 messages（B 方案：单一真相源 ``main_session_messages``
表，非 messages-stack.json——后者是表的冗余副本，已废除），用 LLM 压成一段
~1000-3000 token 的 markdown 摘要，保留四类信息（关键事件锚 / 情绪轨迹 / ta 做了
什么 / 自反馈线索）。

- 输入：``state.dream_date``（被总结的昨日，``YYYY-MM-DD``）+ ``state.triggered_at``
  （tz-aware ISO，定窗口时区）+ db 里的消息
- 输出 patch：``{"messages_summary": "<markdown 摘要>"}``

昨日窗口
========

``[dream_date 00:00, dream_date+1 00:00)``，时区取 ``triggered_at`` 的 tzinfo
（与 life 时钟一致）。用 ``db.get_messages_between_ms(since, until, limit)`` ——该 helper
在 SQL 里同时约束 ``[since_ms, until_ms)`` 上下界，``limit`` 只在 bounded window 内
生效。**不复用 ``get_messages_since_ms``**（N-1）——那是 context-window「取最近
limit 条 + 丢头保尾」语义，若只加 since 下界再内存过滤上界，做梦当天凌晨
00:00~04:00 消息超 limit 时会把 yesterday window 挤空 / 静默丢昨天较早事件锚。

软心降级（不崩心跳，MEMORY R3）
==============================

- ``db is None``（mock / 拓扑测）→ 不调 LLM，返占位摘要
- 昨日窗口无消息 → 不调 LLM，返「昨日几乎没有可回顾的记录」（诚实摘要）
- ``triggered_at`` 缺失 / 非法 → 退化用 dream_date 当天 naive 解析（仍尽量产出窗口）

节点结构（三层，与 tick sense_llm 一致）：

- 顶层 ``_step1_summarize_impl(client, state, *, db, session_key, ...)`` 真实现
- ``make_summarize_node(client, db, *, session_key)`` factory → closure
- ``step1_summarize(state)`` 顶层 mock 透传（未注 deps 时，桩占位）
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kindred.config import DEFAULT_SESSION_KEY
from kindred.graph._shared._common import NodeReturn
from kindred.graph._shared._errors import NodeContractError
from kindred.graph.dream._window import day_window_ms
from kindred.llm.schemas import SummaryResponse
from kindred.llm.templates import render_prompt
from kindred.state.dream import DreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    from kindred.db.facade import KindredDB
    from kindred.db.messages import MainSessionMessage
    from kindred.llm.client import LlmClient

_LOG = logging.getLogger(__name__)


class SummarizeContractError(NodeContractError):
    """summarize 上游契约错 / LLM 运行时失败的语义化包装。

    与 ``sense_llm.SenseLlmContractError`` 同源哲学——节点 raise 自定义异常让
    daemon ``except NodeContractError`` 兜底，不裸 ``RuntimeError`` 靠字符串匹配。
    """


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


def make_summarize_node(
    client: LlmClient,
    db: KindredDB | None = None,
    *,
    session_key: str = DEFAULT_SESSION_KEY,
    window_limit: int = 2000,
) -> Callable[[DreamState], NodeReturn]:
    """构造 dream Step 1.summarize closure。

    用法（详 build_dream_graph / daemon）::

        client = MockLlmClient()
        summarize = make_summarize_node(client, db, session_key=key)
        graph = build_dream_graph(summarize_node=summarize)

    ``db`` 可选：传入时 read 昨日窗口消息渲染进 prompt；不传（mock / 拓扑测）时
    软心降级返占位摘要，不调 LLM。``window_limit`` 是 db 拉取上限（防超长一天撑爆
    prompt；昨日 280 tick × 3 轮规模，2000 条够覆盖且远低于 token 极限）。
    """

    def summarize_closure(state: DreamState) -> NodeReturn:
        return _step1_summarize_impl(
            client, state, db=db, session_key=session_key, window_limit=window_limit
        )

    return summarize_closure


# ─────────────────────────────────────────────────────────────────────
# 真实现（顶层 _impl）
# ─────────────────────────────────────────────────────────────────────


def _step1_summarize_impl(
    client: LlmClient,
    state: DreamState,
    *,
    db: KindredDB | None = None,
    session_key: str = DEFAULT_SESSION_KEY,
    window_limit: int = 2000,
) -> NodeReturn:
    """Step 1 真实现：read 昨日窗口 + LLM 总结 → markdown 摘要。

    流程：

    1. 算昨日窗口 ``[dream_date 00:00, +1d)``（时区取 triggered_at）
    2. db ``get_messages_between_ms(since, until, limit)`` 拉 bounded 昨日窗口
       （SQL 同卡上下界，limit 只在窗口内）
    3. 软心降级：db 缺 / 窗口空 → 占位摘要，不调 LLM
    4. 渲染 messages → prompt → ``client.complete(role="dream.summarize")``
    5. validate ``SummaryResponse`` → 返 ``{"messages_summary": summary}``
    """
    dream_date = str(state.get("dream_date") or "")
    triggered_at = state.get("triggered_at")

    if db is None:
        _LOG.debug("dream Step 1.summarize: db is None → 软心占位摘要")
        return {"messages_summary": _empty_summary(dream_date)}

    since_ms, until_ms = day_window_ms(dream_date, triggered_at)
    if since_ms is None or until_ms is None:
        _LOG.warning(
            "dream Step 1.summarize: 无法解析窗口 dream_date=%r triggered_at=%r → 软心占位",
            dream_date,
            triggered_at,
        )
        return {"messages_summary": _empty_summary(dream_date)}

    # bounded day-window：SQL 同时卡上下界，limit 只在窗口内生效（N-1 修复）。
    # 不能复用 get_messages_since_ms——那是 context-window「丢头保尾」语义，做梦
    # 当天凌晨消息超 limit 时会把昨天挤空 / 静默丢昨天较早的事件锚。
    window = db.get_messages_between_ms(
        session_key=session_key,
        since_ms=since_ms,
        until_ms=until_ms,
        limit=window_limit,
    )
    _LOG.debug(
        "dream Step 1.summarize: dream_date=%s window=[%d,%d) in_window=%d",
        dream_date,
        since_ms,
        until_ms,
        len(window),
    )

    if not window:
        return {"messages_summary": _empty_summary(dream_date)}

    prompt = _render_summarize_prompt(dream_date, window)
    try:
        out = client.complete(prompt, role="dream.summarize")
    except NodeContractError:
        raise
    except Exception as exc:  # noqa: BLE001 - 边界包装 client 任意运行时异常
        raise SummarizeContractError(
            f"dream Step 1.summarize: LLM client.complete 失败（{type(exc).__name__}）",
        ) from exc

    summary = _validate_summary_response(out)
    return {"messages_summary": summary}


# ─────────────────────────────────────────────────────────────────────
# Helpers（私有）
# ─────────────────────────────────────────────────────────────────────


def _empty_summary(dream_date: str) -> str:
    """昨日无消息 / db 缺时的诚实占位摘要（不调 LLM）。"""
    label = dream_date or "（未知日期）"
    return f"# 昨日摘要 · {label}\n\n昨日几乎没有可回顾的记录。"


def _render_summarize_prompt(
    dream_date: str,
    window: list[MainSessionMessage],
) -> str:
    """把昨日窗口消息渲染成 summarize 的 user prompt。

    每条取 ``role`` + ``text_summary``（已是压缩纯文本，content 不存 JSON 原文）。
    无 text_summary 的条跳过（如纯 toolResult 已在入表时被滤）。
    """
    lines: list[str] = []
    for m in window:
        text = (m.text_summary or "").strip()
        if not text:
            continue
        lines.append(f"[{m.role}] {text}")
    body = "\n".join(lines) or "（昨日窗口内无可读消息）"
    return render_prompt("dream_summarize.md.j2", dream_date=dream_date, messages_block=body)


def _validate_summary_response(out: object) -> str:
    """校 LLM 返回 dict 的 ``SummaryResponse`` schema，返回 summary 文本。

    非 dict / 缺 summary / 空白 summary 一律包成 ``SummarizeContractError``，
    让 daemon ``except NodeContractError`` 兜底（不裸下标逃出谱系）。
    """
    from pydantic import ValidationError

    if not isinstance(out, dict):
        raise SummarizeContractError(
            f"dream Step 1.summarize: LLM 返回必须是 dict，got {type(out).__name__}",
        )
    try:
        parsed = SummaryResponse.model_validate(out)
    except ValidationError as exc:
        raise SummarizeContractError(
            f"dream Step 1.summarize: LLM 返回不合 SummaryResponse 契约：{exc}",
        ) from exc
    return parsed.summary


# ─────────────────────────────────────────────────────────────────────
# 顶层 mock 透传（保留 D6.1 桩兼容 / 拓扑测试）
# ─────────────────────────────────────────────────────────────────────


def step1_summarize(state: DreamState) -> NodeReturn:
    """Step 1 顶层 mock 透传（未注 deps 时使用，桩占位）。

    返回标注的占位摘要，让 graph 拓扑能跑通线性流（``build_dream_graph()`` 不传
    ``summarize_node`` 时走这里）。真实路径：``make_summarize_node(client, db)``
    注入走 ``_step1_summarize_impl``。
    """
    dream_date = state.get("dream_date", "<unknown>")
    _LOG.debug("dream Step 1.summarize (mock pass-through) for date=%s", dream_date)
    return {"messages_summary": _empty_summary(str(dream_date))}
