"""条件边谓词 —— ``route_after_sense_llm``。

参考文档：14-heart-graph.md §2.3.1（唯一条件边）+ §4

tick graph 拓扑里只有**一个分支点**：``T1.sense.llm`` 出口，看
``act_decision.act`` 决定走哪路。

| ``act_decision.act`` | 走向                   | 效果 |
|---|---|---|
| ``False``（不动手）   | 跳 T2，进 T3.write_state | 仍写一行 next_state（next == prev）|
| ``True``（动手）     | 走 T2.act.llm           | next_state.activity 等可能改 |

注意：本谓词返回的是**节点名字符串**——LangGraph ``add_conditional_edges``
约定的路由 key。节点名跟 ``build.py`` 里 ``add_node(name, fn)`` 的
``name`` 必须严格一致。
"""

from __future__ import annotations

from typing import Final, Literal

from kindred.state.tick import TickState

# 路由目标常量——与 build.py 里 add_node 名字一一对应
ROUTE_T2_ACT_LLM: Final[Literal["T2.act.llm"]] = "T2.act.llm"
ROUTE_T3_PERSIST_WRITE_STATE: Final[Literal["T3.persist.write_state"]] = "T3.persist.write_state"


def route_after_sense_llm(
    state: TickState,
) -> Literal["T2.act.llm", "T3.persist.write_state"]:
    """T1.sense.llm 出口的路由谓词。

    读 ``state.act_decision.act``：

    - ``True`` → 走 T2.act.llm（动手）
    - ``False`` 或缺席 → 跳 T2 直接进 T3.persist.write_state（不动手仍落 state）

    ``act_decision`` 字段缺席时按"不动手"处理——这是防御式默认：T1.sense.llm
    保证一定会写入 ``act_decision={"act": False, ...}``，但防御默认仍然有用，
    避免 graph 在异常状态下崩在路由层。
    """
    decision = state.get("act_decision") or {}
    if decision.get("act") is True:
        return ROUTE_T2_ACT_LLM
    return ROUTE_T3_PERSIST_WRITE_STATE
