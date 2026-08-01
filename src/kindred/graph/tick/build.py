"""tick graph 装配 —— 按 14 §2.3 拓扑构建 LangGraph。

参考文档：14-heart-graph.md §2.2 / §2.3

拓扑：7 节点（T2 已按 §2.3.2 errata 合为单节点）、1 个条件分支
（``T1.sense.llm`` 出口走 ``route_after_sense_llm``）、串行 T3 三节点。

依赖注入（factory 模式）
========================

``build_tick_graph`` 的每个节点参数都是 **optional**：

- 传入时用真实现 closure（``make_*_node(...)`` 的返回值，持有 db / LLM
  client / bundle 路径等运行时依赖）
- 不传时回退到顶层 **mock 透传**（``tick/`` 各节点文件的同名函数），用于拓扑
  测试与 ``kindred tick --mock`` 最快路径

**daemon 契约**：invoke 只传 ``trigger_source / triggered_at``，``sense_io``
closure 负责 read ``state_latest`` 填 ``prev_state / next_state``；
``sense_llm`` 与 ``act_llm`` 通常共用同一个 LLM ``client`` 实例。

LangGraph 概念对照（备忘）：

- ``StateGraph(TickState)``：以 ``TickState`` TypedDict 作 schema 的 graph builder
- ``add_node(name, fn)``：注册节点；``fn`` 接受 state 返回 partial dict
- ``add_edge(src, dst)``：无条件边
- ``add_conditional_edges(src, predicate, route_map?)``：条件边；
  ``predicate`` 返回字符串作为 next node 名
- ``compile()``：装配为 ``CompiledStateGraph``，可 ``invoke(initial_state)``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from kindred.graph import tick as _nodes_mod
from kindred.graph.tick.routing import (
    ROUTE_T2_ACT_LLM,
    ROUTE_T3_PERSIST_WRITE_STATE,
    route_after_sense_llm,
)
from kindred.state.tick import TickState

if TYPE_CHECKING:
    from collections.abc import Callable

    from langgraph.graph.state import CompiledStateGraph

    from kindred.graph._shared._common import NodeReturn


# 节点名常量——也用于 routing 路由 key 与 mermaid label 对应
NODE_T1_SENSE_IO = "T1.sense.io"
NODE_T1_SENSE_DERIVE = "T1.sense.derive"
NODE_T1_SENSE_LLM = "T1.sense.llm"
NODE_T2_ACT_LLM = ROUTE_T2_ACT_LLM  # 与 routing 共享
NODE_T3_PERSIST_WRITE_STATE = ROUTE_T3_PERSIST_WRITE_STATE  # 与 routing 共享
NODE_T3_PERSIST_WRITE_MEMORY = "T3.persist.write_memory"
NODE_T3_PERSIST_FLUSH_BUNDLE = "T3.persist.flush_bundle"


def _add_node(
    builder: StateGraph[TickState, Any, Any, Any],
    name: str,
    provided: Callable[[TickState], NodeReturn] | None,
    default: Callable[[TickState], NodeReturn],
) -> None:
    """注册一个节点：``provided`` 注入时用真实现 closure，否则回退到 ``default`` mock。

    ``default`` 走调用方传入的 ``_nodes_mod.<attr>`` 引用——attr lookup 发生在
    ``build_tick_graph`` 运行时，因此对 ``graph.tick`` 模块的 monkeypatch 仍生效。

    ``# type: ignore[call-overload]``：LangGraph ``add_node`` 的形参是泛型
    ``_Node[NodeInputT]`` union，而我们的 closure 推断为
    ``Callable[[TickState], dict[str, Any]]``，mypy 严格模式无法匹配重载。
    这是 LangGraph 类型系统边界，运行时正常；单点忽略比 ``cast(Any)`` 更精准。
    """
    node = provided if provided is not None else default
    builder.add_node(name, node)  # type: ignore[call-overload]


def build_tick_graph(
    *,
    sense_io_node: Callable[[TickState], NodeReturn] | None = None,
    sense_derive_node: Callable[[TickState], NodeReturn] | None = None,
    sense_llm_node: Callable[[TickState], NodeReturn] | None = None,
    act_llm_node: Callable[[TickState], NodeReturn] | None = None,
    persist_nodes: dict[str, Callable[[TickState], NodeReturn]] | None = None,
) -> CompiledStateGraph[TickState, Any, Any, Any]:
    """装配 tick graph 并 compile，按 14 §2.3 mermaid 拓扑。

    每个节点参数为 optional：传入 ``make_*_node(...)`` 的返回值用真实现，
    不传则回退顶层 mock 透传。详见模块 docstring「依赖注入」。

    拓扑（mermaid 等价）::

        START → T1.sense.io → T1.sense.derive → T1.sense.llm
                                                    ↓ act?
                                  ┌────── False ────┴───── True ──────┐
                                  ↓                                    ↓
                         T3.persist.write_state ←──────── T2.act.llm ──┘
                                  ↓
                         T3.persist.write_memory
                                  ↓
                         T3.persist.flush_bundle
                                  ↓
                                 END
    """
    builder: StateGraph[TickState, Any, Any, Any] = StateGraph(TickState)

    # ─── 注册节点（按拓扑顺序）─────────────────────────────
    persist = persist_nodes or {}
    _add_node(builder, NODE_T1_SENSE_IO, sense_io_node, _nodes_mod.t1_sense_io)
    _add_node(
        builder,
        NODE_T1_SENSE_DERIVE,
        sense_derive_node,
        _nodes_mod.t1_sense_derive,
    )
    _add_node(builder, NODE_T1_SENSE_LLM, sense_llm_node, _nodes_mod.t1_sense_llm)
    _add_node(builder, NODE_T2_ACT_LLM, act_llm_node, _nodes_mod.t2_act_llm)
    _add_node(
        builder,
        NODE_T3_PERSIST_WRITE_STATE,
        persist.get(NODE_T3_PERSIST_WRITE_STATE),
        _nodes_mod.t3_persist_write_state,
    )
    _add_node(
        builder,
        NODE_T3_PERSIST_WRITE_MEMORY,
        persist.get(NODE_T3_PERSIST_WRITE_MEMORY),
        _nodes_mod.t3_persist_write_memory,
    )
    _add_node(
        builder,
        NODE_T3_PERSIST_FLUSH_BUNDLE,
        persist.get(NODE_T3_PERSIST_FLUSH_BUNDLE),
        _nodes_mod.t3_persist_flush_bundle,
    )

    # ─── 边 ─────────────────────────────────────────────
    builder.add_edge(START, NODE_T1_SENSE_IO)
    builder.add_edge(NODE_T1_SENSE_IO, NODE_T1_SENSE_DERIVE)
    builder.add_edge(NODE_T1_SENSE_DERIVE, NODE_T1_SENSE_LLM)

    # 唯一条件边：T1.sense.llm → T2.act.llm 或直接 → T3.persist.write_state
    builder.add_conditional_edges(
        NODE_T1_SENSE_LLM,
        route_after_sense_llm,
        {
            ROUTE_T2_ACT_LLM: NODE_T2_ACT_LLM,
            ROUTE_T3_PERSIST_WRITE_STATE: NODE_T3_PERSIST_WRITE_STATE,
        },
    )

    # T2 走完合流到 T3.write_state
    builder.add_edge(NODE_T2_ACT_LLM, NODE_T3_PERSIST_WRITE_STATE)

    # T3 串行三节点
    builder.add_edge(NODE_T3_PERSIST_WRITE_STATE, NODE_T3_PERSIST_WRITE_MEMORY)
    builder.add_edge(NODE_T3_PERSIST_WRITE_MEMORY, NODE_T3_PERSIST_FLUSH_BUNDLE)
    builder.add_edge(NODE_T3_PERSIST_FLUSH_BUNDLE, END)

    return builder.compile()


__all__ = [
    "NODE_T1_SENSE_DERIVE",
    "NODE_T1_SENSE_IO",
    "NODE_T1_SENSE_LLM",
    "NODE_T2_ACT_LLM",
    "NODE_T3_PERSIST_FLUSH_BUNDLE",
    "NODE_T3_PERSIST_WRITE_MEMORY",
    "NODE_T3_PERSIST_WRITE_STATE",
    "build_tick_graph",
]
