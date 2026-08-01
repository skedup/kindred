"""共享 helper —— 节点间复用的类型别名与 round-trip 桥。

参考文档：14-heart-graph.md §3

为什么需要 round-trip 桥
========================

LangGraph TypedDict + JSON 序列化需要 dict-form，而 ``state/`` 下的
pydantic model（``ActDecision`` / ``State`` / ``Thought`` / ...）才是
真正的 schema。节点边界做 dict ↔ pydantic round-trip：

- 节点入口：dict → pydantic（``model_validate``，校验 raise）
- 节点出口：pydantic → dict（``model_dump(mode='json')``，LangGraph 序列化）
- 节点内部：纯 pydantic instance，享受类型安全

sense_llm 拿到 LLM 输出 dict 必须先过 ``validate_*``——不能跳过 schema 让坏
数据漂到下一节点。

当前已桥接的边界类型
--------------------

仅扩到 SQLite IO 真实需要的边界类型：

- ``State`` —— T3.write_state ↔ T1.sense_io 的 next_state / prev_state
- ``Thought`` —— T3.write_state→thoughts 的 list[Thought]

暂未桥接（按「写了≠做了」不预设没看到的 schema）：

- ``ChatTurn`` —— messages-stack 文件结构未定，真接 sense_io 时再加
- ``Memory`` —— ``KindredDB.get_episodes()`` 已返 dict，节点直接用
- ``ActResult`` —— 14 §1.2 故意 free-form dict，schema 留 Phase β 再细化

不可绕过原则（02 §1 / extra=forbid 哲学的延伸）
-----------------------------------------------------

每个 ingress（dict → pydantic）必须 raise，不允许 ``try/except`` 默默
吞掉 ``ValidationError``——否则坏 state 会漂过节点边界，直到 SQLite 写
入或更下游的下个节点才报错，调试地狱。

正确用法::

    # ingress
    state = validate_state(state_dict)  # ValidationError raise

    # internal logic on pydantic
    state.interior.mood.value -= 5

    # egress
    return {"next_state": state_to_dict(state)}

错误用法（永远禁止）::

    try:
        state = validate_state(state_dict)
    except ValidationError:
        state = State()  # 静默 fallback——禁止
"""

from __future__ import annotations

from typing import Any

from kindred.state.interior import Thought
from kindred.state.state import State
from kindred.state.tick import ActDecision

# Node return type —— LangGraph 接受 partial dict 做 state patch
NodeReturn = dict[str, Any]


# ─── ActDecision round-trip ───────────────────────


def validate_act_decision(d: dict[str, Any]) -> ActDecision:
    """边界 in：dict → pydantic ActDecision，失败立即 raise。

    用法（真 LLM 节点示例）::

        llm_dict = await llm.complete(prompt)  # 取信不足的 dict
        decision = validate_act_decision(llm_dict)  # raise 在第一时间
        # 节点内部用 decision: ActDecision 享受类型安全
    """
    return ActDecision.model_validate(d)


def act_decision_to_dict(ad: ActDecision) -> dict[str, Any]:
    """边界 out：pydantic ActDecision → dict（LangGraph 可序列化）。

    用 ``mode='json'`` 保证嵌套类型（如 enum / datetime）转为基本类型，
    LangGraph TypedDict 才能直接放进去。
    """
    return ad.model_dump(mode="json")


# ─── State round-trip ──────────────────────────────


def validate_state(d: dict[str, Any]) -> State:
    """边界 in：dict → pydantic State，失败立即 raise。

    用于 T3.persist.write_state 的入口（``state["next_state"]: dict``）和
    T1.sense_io 从 SQLite ``state_latest`` 视图 read 回来的 dict round-trip。

    State 本身有跨 model invariant（如 ``with_whom`` 必须是 ``presence.others ``
    ∩ 原始身份 `` ∪ {"user"}`` 的子集）—— ``model_validate`` 会全部跑一
    遍。这是为什么节点出口必须用 ``state_to_dict`` 而不是手写
    ``dict(state)``——后者会 bypass invariant 校验。
    """
    return State.model_validate(d)


def state_to_dict(state: State) -> dict[str, Any]:
    """边界 out：pydantic State → dict（LangGraph + JSON 可序列化）。

    用 ``mode='json'`` 保证 datetime / enum / set 等 Python-native 类型转为
    JSON 基本类型——LangGraph TypedDict 才能直接放进去，下游 ``insert_tick``
    走 ``json.dumps`` 序列化也不会报错。
    """
    return state.model_dump(mode="json")


# ─── Thought round-trip ────────────────────────────


def validate_thought(d: dict[str, Any]) -> Thought:
    """边界 in：单个 dict → pydantic Thought，失败立即 raise。

    Thought schema (interior.py §Layer 4)::

        description: NonBlankStr
        mood_w:      int (-30 ~ +30)
        expire_at:   ISO8601 | None
        tag:         NonBlankStr

    用于 T1.sense_io 从 SQLite ``thought`` 表读出 dict 后 validate，
    或 T1.sense_llm 输出 thoughts dict list 后 validate。
    """
    return Thought.model_validate(d)


def validate_thoughts(items: list[dict[str, Any]]) -> list[Thought]:
    """批量入口便利函数：list[dict] → list[Thought]，逐个 validate。

    任何一个 raise 都会立即冒泡——批量场景禁止「先收集失败再 raise」，
    那样会让单条坏数据污染整批操作的语义。
    """
    return [validate_thought(d) for d in items]


def thought_to_dict(t: Thought) -> dict[str, Any]:
    """边界 out：单个 pydantic Thought → dict。"""
    return t.model_dump(mode="json")


def thoughts_to_dict_list(items: list[Thought]) -> list[dict[str, Any]]:
    """批量出口便利函数：list[Thought] → list[dict]。"""
    return [thought_to_dict(t) for t in items]
