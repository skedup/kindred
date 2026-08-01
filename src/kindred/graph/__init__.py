"""LangGraph 编排层 — Kindred 的核心呼吸。

参考文档：docs/14-heart-graph.md

依赖：state/, llm/, db/（节点可读写）

模块（earlier milestone 重构后）：
- tick/             tick graph：装配（build.py）+ 7 个节点实现 + routing（14 §2/§3/§4）
- dream/            dream graph：做梦 5 步（D6 系列，随刀长出）
- _shared/          两 graph 共用内部底座（_common round-trip 桥 / _errors 契约异常）
- errors.py         public 异常契约 facade（NodeContractError，给 entry/runtime）
- bundle.py         嘴侧 bundle 渲染契约（A-1 从 state/memory.py 迁入）

Note：``routing`` 已随 earlier milestone 收进 ``graph.tick.routing``（tick-only 条件边）；
本 facade 继续 re-export ``route_after_sense_llm`` 等供公共入口兼容。
"""

from __future__ import annotations

from kindred.graph.bundle import BundleSection, NowSection
from kindred.graph.dream import build_dream_graph
from kindred.graph.errors import NodeContractError
from kindred.graph.tick import PersistDeps, make_persist_nodes
from kindred.graph.tick.build import (
    NODE_T1_SENSE_DERIVE,
    NODE_T1_SENSE_IO,
    NODE_T1_SENSE_LLM,
    NODE_T2_ACT_LLM,
    NODE_T3_PERSIST_FLUSH_BUNDLE,
    NODE_T3_PERSIST_WRITE_MEMORY,
    NODE_T3_PERSIST_WRITE_STATE,
    build_tick_graph,
)
from kindred.graph.tick.routing import (
    ROUTE_T2_ACT_LLM,
    ROUTE_T3_PERSIST_WRITE_STATE,
    route_after_sense_llm,
)

__all__ = [
    "NODE_T1_SENSE_DERIVE",
    "NODE_T1_SENSE_IO",
    "NODE_T1_SENSE_LLM",
    "NODE_T2_ACT_LLM",
    "NODE_T3_PERSIST_FLUSH_BUNDLE",
    "NODE_T3_PERSIST_WRITE_MEMORY",
    "NODE_T3_PERSIST_WRITE_STATE",
    "ROUTE_T2_ACT_LLM",
    "ROUTE_T3_PERSIST_WRITE_STATE",
    "BundleSection",
    "NodeContractError",
    "NowSection",
    "PersistDeps",
    "build_dream_graph",
    "build_tick_graph",
    "make_persist_nodes",
    "route_after_sense_llm",
]
