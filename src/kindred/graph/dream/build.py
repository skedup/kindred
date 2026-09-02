"""dream graph 装配 —— 按 docs/14 §2.4 拓扑构建 LangGraph。

参考文档：14-heart-graph.md §2.4、11-dreaming.md §3。

拓扑：**线性 5 步**（D6.8b）。原本 Step 4 闸门出口是条件边（pass/warn →
land；block → END）；D6.8b 后 block 也走 land（写 index trace 供 should_dream
幂等补偿，但 land Step 0 早返不改灵魂）——条件边退化为普通边，Step 4 →
Step 5 直连。

依赖注入（factory 模式，与 tick build 对称）
============================================

``build_dream_graph`` 的每个节点参数都是 **optional**：

- 传入时用真实现 closure（``make_*_node(...)`` 的返回值，持有 LLM client /
  文件路径等运行时依赖）
- 不传时回退到顶层 **桩透传**（``dream/`` 各节点文件的同名函数）

**earlier milestone 骨架刀**：5 节点真实现 / factory 尚未长出（summarize/reflect/gate 的
LLM closure 留 D6.2/D6.4/D6.5，reset_stack/land 的文件 IO 留 D6.3/D6.6）。当前
所有节点参数不传 → 全走桩，graph 可装配可跑通线性流。参数口先开好，后续刀填。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from kindred.graph import dream as _dream_mod
from kindred.graph.dream.routing import ROUTE_STEP5_LAND
from kindred.state.dream import DreamState
from kindred.telemetry import observe_graph_node

if TYPE_CHECKING:
    from collections.abc import Callable

    from langgraph.graph.state import CompiledStateGraph

    from kindred.graph._shared._common import NodeReturn


# 节点名常量——也用于 routing 路由 key 与 mermaid label 对应（14 §2.4）
NODE_STEP1_SUMMARIZE = "Step 1.summarize"
NODE_STEP2_RESET_STACK = "Step 2.reset_stack"
NODE_STEP3_REFLECT = "Step 3.reflect"
NODE_STEP4_GATE = "Step 4.gate"
NODE_STEP5_LAND = ROUTE_STEP5_LAND  # 与 routing 共享


def _add_node(
    builder: StateGraph[DreamState, Any, Any, Any],
    name: str,
    provided: Callable[[DreamState], NodeReturn] | None,
    default: Callable[[DreamState], NodeReturn],
) -> None:
    """注册节点：``provided`` 注入时用真实现，否则回退 ``default`` 桩。

    ``default`` 走 ``_dream_mod.<attr>`` 引用——attr lookup 发生在
    ``build_dream_graph`` 运行时，因此对 ``graph.dream`` 模块的 monkeypatch 仍生效
    （与 tick build 同模式）。

    ``# type: ignore[call-overload]``：LangGraph ``add_node`` 泛型重载与本地
    ``Callable[[DreamState], dict[str, Any]]`` 推断不匹配，运行时正常（同 tick）。
    """
    node = provided if provided is not None else default
    node = observe_graph_node(name, node)
    builder.add_node(name, node)  # type: ignore[call-overload]


def build_dream_graph(
    *,
    summarize_node: Callable[[DreamState], NodeReturn] | None = None,
    reset_stack_node: Callable[[DreamState], NodeReturn] | None = None,
    reflect_node: Callable[[DreamState], NodeReturn] | None = None,
    gate_node: Callable[[DreamState], NodeReturn] | None = None,
    land_node: Callable[[DreamState], NodeReturn] | None = None,
) -> CompiledStateGraph[DreamState, Any, Any, Any]:
    """装配 dream graph 并 compile，按 14 §2.4 mermaid 拓扑。

    每个节点参数 optional：传入用真实现，不传回退桩透传（earlier milestone 全走桩）。

    拓扑（D6.8b，线性）::

        START → Step 1.summarize → Step 2.reset_stack → Step 3.reflect
              → Step 4.gate → Step 5.land → END

    闸门结论不再分叉拓扑：pass/warn → land 落盘；block → land Step 0 早返
    （不 apply / 不存 snapshot，只写 index trace）。block 不改灵魂的保证在 land 内部。
    """
    builder: StateGraph[DreamState, Any, Any, Any] = StateGraph(DreamState)

    # ─── 注册节点（按拓扑顺序）─────────────────────────────
    _add_node(builder, NODE_STEP1_SUMMARIZE, summarize_node, _dream_mod.step1_summarize)
    _add_node(builder, NODE_STEP2_RESET_STACK, reset_stack_node, _dream_mod.step2_reset_stack)
    _add_node(builder, NODE_STEP3_REFLECT, reflect_node, _dream_mod.step3_reflect)
    _add_node(builder, NODE_STEP4_GATE, gate_node, _dream_mod.step4_gate)
    _add_node(builder, NODE_STEP5_LAND, land_node, _dream_mod.step5_land)

    # ─── 边 ─────────────────────────────────────────────
    builder.add_edge(START, NODE_STEP1_SUMMARIZE)
    builder.add_edge(NODE_STEP1_SUMMARIZE, NODE_STEP2_RESET_STACK)
    builder.add_edge(NODE_STEP2_RESET_STACK, NODE_STEP3_REFLECT)
    builder.add_edge(NODE_STEP3_REFLECT, NODE_STEP4_GATE)

    # D6.8b：Step 4.gate → Step 5.land 普通边（原条件边退化：block 也走 land
    # 写 index trace，但 land Step 0 早返不改灵魂）。
    builder.add_edge(NODE_STEP4_GATE, NODE_STEP5_LAND)
    builder.add_edge(NODE_STEP5_LAND, END)

    return builder.compile()


__all__ = [
    "NODE_STEP1_SUMMARIZE",
    "NODE_STEP2_RESET_STACK",
    "NODE_STEP3_REFLECT",
    "NODE_STEP4_GATE",
    "NODE_STEP5_LAND",
    "build_dream_graph",
]
