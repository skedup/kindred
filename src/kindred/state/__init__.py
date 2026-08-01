"""数据结构层 — 纯类型，无逻辑，无副作用。

参考文档：
- docs/02-state-system.md（8 层 state schema）
- docs/14-heart-graph.md §1（TickState）
- docs/09-memory.md §3（thought 与 02 §4.1.4 对齐）

依赖：（无外部依赖，可被任何层 import；本层不 import db / graph / llm）

模块：
- interior.py   Layer 1~4：body/mood/inner_pulse/needs/affect/thoughts
- outward.py    embodiment / bag / activity / location / time / environment / presence
- state.py      State 顶层组合（8 层 = interior + 7 outward layers）
- tick.py       TickState (TypedDict) + ChatTurn / ActDecision
- dream.py      DreamState (TypedDict) + ReflectionChange / ReflectionDiff / GateVerdict

（注：``BundleSection`` / ``NowSection`` 原位于本包 ``memory.py``，已迁至
``kindred.graph.bundle`` —bundle 是嘴侧渲染产物，不是 state 本身。）

"""

from kindred.state.dream import (
    DreamState,
    GateVerdict,
    ReflectionChange,
    ReflectionDiff,
    ReflectionOperation,
)
from kindred.state.interior import (
    Affect,
    GaugeWithDescription,
    Interior,
    Needs,
    Thought,
)
from kindred.state.outward import (
    Activity,
    ActivityContext,
    Bag,
    DestinationPlan,
    Embodiment,
    Environment,
    Location,
    Presence,
    Time,
    TimePhase,
    Weekday,
)
from kindred.state.possession import ItemSnapshot
from kindred.state.state import State
from kindred.state.tick import (
    ActDecision,
    ActDecisionKind,
    ChatTurn,
    TickState,
    TriggerSource,
)

__all__ = [
    # interior
    "Affect",
    "GaugeWithDescription",
    "Interior",
    "ItemSnapshot",
    "Needs",
    "Thought",
    # outward
    "Activity",
    "ActivityContext",
    "Bag",
    "DestinationPlan",
    "Embodiment",
    "Environment",
    "Location",
    "Presence",
    "Time",
    "TimePhase",
    "Weekday",
    # top-level
    "State",
    # tick runtime
    "ActDecision",
    "ActDecisionKind",
    "ChatTurn",
    "TickState",
    "TriggerSource",
    # dream runtime
    "DreamState",
    "GateVerdict",
    "ReflectionChange",
    "ReflectionDiff",
    "ReflectionOperation",
]
