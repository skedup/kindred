"""Interior layer — ta 的内在 4 子层。

参考文档：docs/02-state-system.md §4

4 子层：
- Layer 1（综合感受）：body / mood / inner_pulse —— 派生层，不直接写入
- Layer 2（Needs）：hunger / energy / fatigue / comfort / social / stimulation / aesthetic
- Layer 3（Affect）：stress / focus / arousal / clarity
- Layer 4（Thoughts）：事件流，影响 Layer 1 的燃料

设计原则：
- Layer 1 数值 + 描述共存（数值是依据，描述是表达）
- Layer 1 永远是派生量，从 Layer 2/3/4 算出
- thoughts 有 expire_at，过期后不再点燃 mood；Layer 4 是**有界的事件流**——
  apply_thought_diff 给所有念头兜底 TTL（无永不过期）+ 数量封顶，永久记忆归灵魂层
"""

from __future__ import annotations

from pydantic import Field

from kindred.state._base import StrictBase
from kindred.state._types import NonBlankStr


class GaugeWithDescription(StrictBase):
    """Layer 1 综合感受的统一类型：数值 + 描述共存。

    用于 body / mood / inner_pulse 三个 Layer 1 字段。
    """

    value: int = Field(ge=0, le=100, description="0-100 的数值")
    description: NonBlankStr = Field(
        description="人话描述（如：'有点累，肩膀酸'）",
    )


class Needs(StrictBase):
    """Layer 2：生理 / 心理需求，自然衰减或累积。

    全部字段 0-100，方向各异（详见 02 §3 字段语义）：
    - hunger：高=饿，需要 explore_food 类 activity
    - energy：高=精力充沛
    - fatigue：高=疲惫（独立于 energy，长期累积）
    - comfort：高=舒适（环境、衣着、位置综合）
    - social：高=社交需求得到满足
    - stimulation：高=有趣味/新鲜感
    - aesthetic：高=审美需求满足（音乐、画面、文字之美）
    """

    hunger: int = Field(ge=0, le=100)
    energy: int = Field(ge=0, le=100)
    fatigue: int = Field(ge=0, le=100)
    comfort: int = Field(ge=0, le=100)
    social: int = Field(ge=0, le=100)
    stimulation: int = Field(ge=0, le=100)
    aesthetic: int = Field(ge=0, le=100)


class Affect(StrictBase):
    """Layer 3：心理状态，按需激活。

    全部字段 0-100：
    - stress：压力（焦虑、紧张）
    - focus：注意力集中度
    - arousal：性 / 情绪唤起度
    - clarity：思维清晰度
    """

    stress: int = Field(ge=0, le=100)
    focus: int = Field(ge=0, le=100)
    arousal: int = Field(ge=0, le=100)
    clarity: int = Field(ge=0, le=100)


class Thought(StrictBase):
    """Layer 4：事件流单条 thought。

    与 09-memory.md §3.2 thought 表 schema 完全对齐（跨文档同步）。

    字段说明：
    - description：人话描述（如：'刚和闺蜜聊得久'）
    - mood_w：对 mood 的影响权重（-30 ~ +30）
    - expire_at：过期时间（ISO8601）。schema 允许 None（过滤器把 None 当不过期），但
      apply_thought_diff 在落库前给所有 None 兜底 TTL——**实际 Layer 4 无永不过期念头**。
    - tag：分类（chat / work / weather / intimacy / ...）
    """

    description: NonBlankStr
    mood_w: int = Field(ge=-30, le=30)
    expire_at: str | None = Field(
        default=None,
        description="ISO8601 timestamp; None = 不过期",
    )
    tag: NonBlankStr


class Interior(StrictBase):
    """Interior 层 = Layer 1 + Layer 2 + Layer 3 + Layer 4。

    对应 02 §3.1 的 interior 字段、§4 的完整 4 子层结构。
    """

    # Layer 1 综合感受（派生层）
    body: GaugeWithDescription
    mood: GaugeWithDescription
    inner_pulse: GaugeWithDescription

    # Layer 2 needs
    needs: Needs

    # Layer 3 affect
    affect: Affect

    # Layer 4 thoughts（事件流）
    thoughts: list[Thought] = Field(default_factory=list)
