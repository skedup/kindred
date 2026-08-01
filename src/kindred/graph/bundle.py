"""嘴侧 bundle 渲染契约 (09-memory §4)。

09 §4.1 嘴侧 ``life/state/context-bundle.md`` 是状态快照（“现在在过什么”），
分为 4 段：

1. **NOW** —— 当下切片（state.time / activity / location / presence / 三 gauge）
2. **Layer A** —— Episodes 压缩（最近 5 个 episode，read-time Python 函数）
3. **Layer B** —— 最近轨迹（significance 中等，未触 Layer C 阈值的 tick）
4. **Layer C** —— 高光闪回（``episode`` 视图：significance >= 7）

定位（为何在 graph/ 而非 state/）：
----------------------------------
嘴侧 bundle 渲染产物（``BundleSection.rendered`` 已是 Markdown 字符串）不是
心侧 state 的一部分。

01 §3.2 6 层依赖纪律：
- ``state/`` = ta 此刻的存在切片（纯类型、不依赖任何其它层）
- ``graph/`` = LangGraph 节点代码及其输入 / 输出型别

bundle 数据结构属于后者：``T3.persist.flush_bundle`` 节点会从这里 import。
state 包仍然仅纯“ta 的存在”。

本模块只定义 BundleSection / NowSection 数据结构（pydantic model）；渲染规则
（拼成 Markdown）+ 写入逻辑由 graph 节点 ``T3.persist.flush_bundle`` 实装。

设计决定：
- bundle 是**状态快照**，不是事件流——每 tick 整文重写（atomic tmpfile + rename）
- Layer A/B/C 的具体筛选 / 压缩规则在渲染层落地，本节只暴露**数据形状**

参考：09-memory.md §4 嘴侧 bundle 渲染规则
"""

from __future__ import annotations

from kindred.state._base import StrictBase
from kindred.state._types import IsoDatetime, NonBlankStr


class NowSection(StrictBase):
    """bundle "NOW" 段 —— 当下切片摘要。

    09 §4.4 NOW 段描述 ta 此刻在过什么，是 bundle 中信息密度最高的一段。
    具体字段筛选 / 渲染由 ``T3.persist.flush_bundle`` 决定，本结构只是渲染
    输入的强类型契约（避免裸 dict）。
    """

    ts: IsoDatetime
    """快照时间戳（=tick.ts，与 state.time.iso 一致）。"""

    activity_name: NonBlankStr
    """state.activity.name（"和 user 聊天" / "看书" / ...）。"""

    location_name: NonBlankStr
    """state.location.name（"家·客厅" / ...）。"""

    body_value: int
    """state.interior.body.value（0~100）。"""

    mood_value: int
    """state.interior.mood.value（0~100）。"""

    inner_pulse_value: int
    """state.interior.inner_pulse.value（0~100）。"""


class BundleSection(StrictBase):
    """bundle 的一段（NOW / A / B / C 共用）。

    只声明渲染产物形态。

    - ``label``：段名（"NOW" / "Layer A: Episodes" / ...）
    - ``rendered``：已渲染好的 Markdown 文本
    - ``source_tick_ids``：参与渲染的 tick.id 列表（用于审计 / 回查；NOW 段为单元素）

    具体渲染逻辑由 graph 节点 ``flush_bundle`` 实装，本节只是契约。
    """

    label: NonBlankStr
    """段标题（在 bundle Markdown 里作为二级标题）。"""

    rendered: str
    """渲染好的 Markdown 文本（允许空字符串：例如 Layer C 暂无高光时）。"""

    source_tick_ids: list[int]
    """参与渲染的 tick.id（从 SQLite 读出来的）。空列表 = 这一段没有数据。"""
