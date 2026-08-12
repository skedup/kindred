"""TickState — graph 在节点间传递的运行时状态。

参考文档：docs/14-heart-graph.md §1

设计原则：
- TickState 是 **TypedDict**（不是 pydantic model）
  - LangGraph 推荐 TypedDict（节点函数返回 partial dict 即可 patch）
  - state 8 层的 model（kindred.state.state.State）通过 prev_state / next_state 字段以 dict 形式承载
  - 写 SQLite 前用 State.model_validate(...) 校验
- TickState 不背 errors 元数据 —— 失败由 daemon catch 异常处理
- 字段填写时机详见 14 §1.3

字段分组（与 14 §1.2 严格对齐）：
- § A. trigger 上下文：trigger_source / triggered_at
- § B. 历史/处境快照：chat_window / recent_contact
- § C. ta 的完整 state：prev_state / next_state（对齐 02 §3）
- § D. T1 sense LLM 输出：note / significance / act_decision
- § E. T2 act 输出：act_result（Optional）
- § F. T3 输出：tick_id（write_state 写完回填，write_memory / flush_bundle 读）
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from pydantic import Field, model_validator

from kindred.state._base import StrictBase
from kindred.state._types import IsoDatetime


# ─────────────────────────────────────────────────────────────
def merge_next_state(
    prev: dict[str, object] | None,
    patch: dict[str, object] | None,
) -> dict[str, object]:
    """``next_state`` 的 LangGraph reducer——**layer 级浅 merge**。

    设计（docs/discussions/2026-06-18 §6 deepcopy→patch）：取代「各节点拿同一个
    共享可变 next_state dict 原地 mutate」的旧模式。现在每个节点只 ``return
    {"next_state": {只含它改的 layer}}``，reducer 把 patch 里的 layer 浅覆盖进
    累积值。「某层被改」与否由「节点有没有在 patch 里带该 layer key」显式决定，
    不再是 deepcopy 闷头带过去——这是「决定不变」与「没决策(act:false)」可区分的根。

    浅 merge 语义：patch 里有 ``interior`` 就**整层**替换 interior——节点对自己
    改的 layer 负责（要改层内深处就读旧层、改完 return 整层，但只读自己那层，
    不 deepcopy 全树）。

    LangGraph 调用约定：首次无累积值时 ``prev=None``（或 graph 初始未提供）。
    两边都浅拷顶层，不动调用方传入的 dict。
    """
    merged: dict[str, object] = dict(prev) if prev else {}
    if patch:
        merged.update(patch)
    return merged


# ─────────────────────────────────────────────────────────────────────
# 子结构（pydantic model，便于校验；TickState 内仍以 dict 形式承载）
# ─────────────────────────────────────────────────────────────────────


class ChatTurn(StrictBase):
    """chat_window 单条对话。

    14 §1.2 § B chat_window 字段定义：
    - role: "user" | "mouth"
    - is_new: True = 本次 trigger 带来；False = 背景上下文
    """

    ts: IsoDatetime = Field(description="ISO8601 timestamp")
    role: Literal["user", "mouth"]
    content: str
    is_new: bool


class RecentContactContext(StrictBase):
    """当前 tick 可见的近期联系事实，不含消息内容或行动建议。"""

    available: bool
    recent_exchange_age_seconds: int | None = None
    recent_actor: Literal["partner", "kindred"] | None = None

    @model_validator(mode="after")
    def _check_projection(self) -> RecentContactContext:
        age = self.recent_exchange_age_seconds
        actor = self.recent_actor
        if not self.available and (age is not None or actor is not None):
            raise ValueError("unavailable recent contact cannot contain an exchange")
        if (age is None) != (actor is None):
            raise ValueError("recent contact age and actor must appear together")
        if age is not None and age < 0:
            raise ValueError("recent contact age must be non-negative")
        return self


ActDecisionKind = Literal[
    "start_activity",
    "advance_activity",
    "end_activity",
]


class ActDecision(StrictBase):
    """T1 sense LLM 输出的 act 决策。

    14 §1.2 § D act_decision 完整字段。
    注：「给 user 发消息」不是独立 kind —— 通过 chat_with_user 这类
    activity 走 start / advance 流程实现。
    """

    act: bool = Field(description="T2 是否跑（False 则跳 T2 直接 T3）")
    kind: ActDecisionKind | None = Field(
        default=None,
        description="act=False 时为 None",
    )
    target_activity: str | None = Field(
        default=None,
        description="kind ∈ {start, advance, end} 时填 activity 名",
    )
    reason: str = Field(
        description="自由文本：sense 对本决策的自我说明（也作为 T2 的目标交付意图）",
    )

    @model_validator(mode="after")
    def _check_act_consistency(self) -> ActDecision:
        """定跨字段 invariant（来源 14 §1.2 §D 行 169）。

        - act=True  ⟹ kind 不为 None 且 target_activity 不为 None
        - act=False ⟹ kind 为 None 且 target_activity 为 None

        防止 LLM 输出「{act:False, kind:'start_activity'}」这种自我矛盾状态。
        """
        if self.act:
            if self.kind is None or self.target_activity is None:
                raise ValueError("act=True requires both 'kind' and 'target_activity' to be set")
        else:
            if self.kind is not None or self.target_activity is not None:
                raise ValueError("act=False requires both 'kind' and 'target_activity' to be None")
        return self


# ─────────────────────────────────────────────────────────────────────
# TickState（TypedDict） —— graph 节点签名直接用这个
# ─────────────────────────────────────────────────────────────────────


TriggerSource = Literal["heartbeat", "watcher", "cold_start"]


class TickState(TypedDict, total=False):
    """单 tick 生命周期内 graph 节点间传递的 dict。

    14 §1.1 TickState **不是** ta 的存在快照（State 才是），
    它是这一 tick 的运行时——含 trigger 上下文、IO 节点 read 的快照、
    sense / act 的产物、graph 控制位。

    total=False：所有字段可选 —— LangGraph 节点函数返回 partial dict 即可
    patch state，不必填全。daemon invoke 时通过 initial state 提供
    trigger_source / triggered_at；其他字段在 graph 流转中由各节点填写。
    """

    # § A. trigger 上下文（daemon invoke 时注入）
    trigger_source: TriggerSource
    triggered_at: str  # ISO8601

    # § B. 历史/处境快照（IO 节点 read）
    chat_window: list[dict[str, object]]  # list[ChatTurn] 的 dict 形式（LangGraph 序列化要求）
    recent_contact: dict[str, object]  # = RecentContactContext.model_dump(mode="json")

    # § C. ta 的完整 state（02 §3 8 层结构，dict 承载，写库前用 State 校验）
    prev_state: dict[str, object]  # = State.model_dump() 的形态
    # next_state 由 reducer merge_next_state 做 layer 级浅 merge：各节点 return
    # {"next_state": {只含改的 layer}}，不再共享可变 dict 原地 mutate。
    next_state: Annotated[dict[str, object], merge_next_state]  # 同 prev_state 形态

    # § D. T1 sense LLM 输出
    note: str
    significance: int  # 1-10
    act_decision: dict[str, object]  # = ActDecision.model_dump() 的形态
    # 本拍 partner 事件实际改变的 Affect 轴；只协调 T2，不进入 canonical State/T3。
    affect_event_touched: list[str]
    # Host 已裁决的关系变化；只在本拍 graph 内透传，T3 主事务消费后即丢弃。
    relationship_change: object

    # § E. T2 act 输出——不可空，act=False 时 T2 直接不写该 key
    # 语义：key 缺席 = T2 跳过了 (act=False) 或 T2 还没跑；key 存在 = T2 产出完成。
    # 依赖 total=False 达成「缺席」，避免 Optional dict 双重语义（None vs 缺席）。
    act_result: dict[str, object]

    # § F. T3 持久层输出
    # write_state 写完 SQLite tick 表后回填（rowid），write_memory + flush_bundle
    # 读它定位本 tick 在表中的位置（thought.source_tick_id / episode_recall.tick_id）。
    # 14 §3.5 / §3.6 / §3.7 三节点合约的物理纽带；T3 之外的节点不该读写。
    tick_id: int
