"""dream graph —— 清晨睡眠态滑入的「做梦」状态机（D6 系列）。

参考文档：docs/11-dreaming.md（5 步 + 1 闸门）、docs/14 §2.4（dream graph 拓扑）。

与 ``tick/`` 并列：同一 daemon、共享 state 8 层子结构（``DreamState`` 与 ``TickState``
并列，earlier milestone 拍板 B），但节点独立、独立 compile（``build_dream_graph``）。触发由
daemon ``should_dream(now, index_path)`` 幂等补偿驱动（D6.8：index 最近 morning_dream
的 dream_date < 该补做的 → 滑入 dream 路径），非定时器/非窗口命中。

拓扑（D6.8b：线性 5 步，docs/14 §2.4）::

    START → Step 1.summarize → Step 2.reset_stack → Step 3.reflect
          → Step 4.gate → Step 5.land → END

    （pass/warn 落盘；block 也进 land 但 Step 0 早返不改灵魂，只写 index trace
     供 should_dream 幂等补偿覆盖所有终态）

模块：
- build.py        装配（build_dream_graph）
- routing.py      路由目标常量 ROUTE_STEP5_LAND（D6.8b：条件边退化为普通边）
- summarize.py    Step 1 总结昨日（LLM #1）
- reset_stack.py  Step 2 重置 stack（代码）
- reflect.py      Step 3 反思改灵魂（LLM #2，核心）
- gate.py         Step 4 闸门 safety check（LLM #3）
- land.py         Step 5 落地 snapshot + write（代码）

**earlier milestone 骨架刀**：5 节点均为桩（返回占位 partial dict），graph 拓扑可装配可
跑通线性流。各步 LLM / 文件 IO 真实现随后续刀（D6.2~D6.6）逐刀长出。
"""

from __future__ import annotations

from kindred.graph.dream._schedule import should_dream
from kindred.graph.dream.build import (
    NODE_STEP1_SUMMARIZE,
    NODE_STEP2_RESET_STACK,
    NODE_STEP3_REFLECT,
    NODE_STEP4_GATE,
    NODE_STEP5_LAND,
    build_dream_graph,
)
from kindred.graph.dream.gate import make_gate_node, step4_gate
from kindred.graph.dream.land import make_land_node, step5_land
from kindred.graph.dream.reflect import make_reflect_node, step3_reflect
from kindred.graph.dream.reset_stack import make_reset_stack_node, step2_reset_stack
from kindred.graph.dream.routing import ROUTE_STEP5_LAND
from kindred.graph.dream.summarize import (
    SummarizeContractError,
    make_summarize_node,
    step1_summarize,
)

__all__ = [
    "NODE_STEP1_SUMMARIZE",
    "NODE_STEP2_RESET_STACK",
    "NODE_STEP3_REFLECT",
    "NODE_STEP4_GATE",
    "NODE_STEP5_LAND",
    "ROUTE_STEP5_LAND",
    "SummarizeContractError",
    "build_dream_graph",
    "make_summarize_node",
    "make_gate_node",
    "should_dream",
    "step1_summarize",
    "make_reset_stack_node",
    "step2_reset_stack",
    "make_reflect_node",
    "step3_reflect",
    "step4_gate",
    "make_land_node",
    "step5_land",
]
