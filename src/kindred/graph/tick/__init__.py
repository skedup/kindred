"""Graph 节点实现 —— 每个节点对应 14 §3 的一节。

依赖：state/, llm/, db/（节点可读写）

节点清单（14 §2.2 tick graph 7 节点）：

- ``sense_io.t1_sense_io``           T1.sense.io
- ``sense_derive.t1_sense_derive``   T1.sense.derive
- ``sense_llm.t1_sense_llm``         T1.sense.llm
- ``act_llm.t2_act_llm``             T2.act.llm（条件：act_decision.act == True）
- ``persist.t3_persist_write_state`` T3.persist.write_state
- ``persist.t3_persist_write_memory``T3.persist.write_memory
- ``persist.t3_persist_flush_bundle``T3.persist.flush_bundle

注：T2 在 graph 装配中走单节点 ``t2_act_llm``（详 14 §2.3.2 errata）。

共享：``_common.py`` 提供 round-trip 桥 + ``NodeReturn`` 类型别名：

- ``ActDecision`` round-trip
- ``State`` round-trip
- ``Thought`` round-trip（含批量便利函数）
"""

from __future__ import annotations

from kindred.graph._shared._common import (
    NodeReturn,
    act_decision_to_dict,
    state_to_dict,
    thought_to_dict,
    thoughts_to_dict_list,
    validate_act_decision,
    validate_state,
    validate_thought,
    validate_thoughts,
)
from kindred.graph.tick.act_llm import ActLlmContractError, make_act_llm_node, t2_act_llm
from kindred.graph.tick.persist import (
    PersistDeps,
    make_persist_nodes,
    t3_persist_flush_bundle,
    t3_persist_write_memory,
    t3_persist_write_state,
)
from kindred.graph.tick.sense_derive import make_sense_derive_node, t1_sense_derive
from kindred.graph.tick.sense_io import (
    ColdStartError,
    make_sense_io_node,
    t1_sense_io,
)
from kindred.graph.tick.sense_llm import make_sense_llm_node, t1_sense_llm

__all__ = [
    "ActLlmContractError",
    "ColdStartError",
    "NodeReturn",
    "PersistDeps",
    "act_decision_to_dict",
    "make_act_llm_node",
    "make_persist_nodes",
    "make_sense_io_node",
    "make_sense_derive_node",
    "make_sense_llm_node",
    "state_to_dict",
    "t1_sense_derive",
    "t1_sense_io",
    "t1_sense_llm",
    "t2_act_llm",
    "t3_persist_flush_bundle",
    "t3_persist_write_memory",
    "t3_persist_write_state",
    "thought_to_dict",
    "thoughts_to_dict_list",
    "validate_act_decision",
    "validate_state",
    "validate_thought",
    "validate_thoughts",
]
