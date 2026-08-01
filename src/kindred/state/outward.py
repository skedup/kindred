"""Outward layers — ta 的 7 层外在状态（与 interior 对偶）。

参考文档：docs/02-state-system.md §3 / 附录 A

7 层（02 §3.2 删除 scene 包装层后保留的全部外在层）：
- embodiment：穿在身上 + 妆容 + 配饰
- bag：带在身边的东西
- activity：当下在做什么
- location：在哪里
- time：什么时辰
- environment：天气 / 城市 / 氛围
- presence：一个人 / 跟谁

设计原则：
- 这些层的 model 都允许 extra="forbid"（schema 严格）
- ISO8601 时间戳全部用 str（pydantic 校验通过 Annotated 后续可加）
- 部分字段 Optional（如 bra/panties 私密层；bag.item 没带包；address 虚拟模式）

未来拆分指引：
- 当前 7 层共置一个文件（175 行）。若任一层 class 演化超过 30 行，或演化节奏
  与其他层显著不同（如 Embodiment / Activity 加字段 vs Time / Bag 几乎不变），
  则按 02 §3 子节切到 ``state/outward/`` 子包，每层独立文件 + ``__init__.py``
  re-export 保证向后兼容。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from kindred.state._base import StrictBase
from kindred.state._types import IsoDatetime, NonBlankStr, is_safe_name
from kindred.state.possession import Bag as Bag
from kindred.state.possession import Embodiment as Embodiment

# ─────────────────────────────────────────────────────────────────────
# activity：当下在做什么
# ─────────────────────────────────────────────────────────────────────


class DestinationPlan(StrictBase):
    """跨 tick 持续存在的「正在去哪里」计划（destination lifecycle 第五趴 §4.2/§5）。

    由 act 节点从 committed ``act_result.destination_choice`` **派生写入**——LLM 不得
    经 ``final_state_diff.activity.context`` 直写（防同一目的地计划出现两份可漂移的
    事实源）。``location_arrival`` / ``destination_abandoned`` 时由节点关闭（移除条目）。

    这不是 ``state.location``：选了要去哪里 ≠ 已经在那里。到达才改 location。
    """

    status: Literal["planned"] = Field(
        default="planned",
        description="计划状态。v1 只有 planned（en route 语义由 activity.step 表达）",
    )
    place_key: NonBlankStr = Field(
        description="稳定地点身份（如 virtual:xxx / baidu:uid / character_card:home:<hash>）"
    )
    name: NonBlankStr = Field(description="地点名（选中候选的快照）")
    address: str | None = Field(default=None, description="地址（虚拟模式可空）")
    city: str | None = Field(default=None, description="所在城市（选中候选的快照，可空）")
    type: str | None = Field(default=None, description="地点类型（restaurant / park / ...）")
    chosen_at: IsoDatetime = Field(description="选中时刻（tick 的 time.iso）")
    source: NonBlankStr = Field(description="候选来源（Provider 或 character-card 已知地点）")
    candidate_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        description="选中时的候选世界事实快照（distance_km / open_now / ...），只留档不再解释",
    )


class ActivityContext(StrictBase):
    """activity 的结构化执行上下文（第五趴 §5：跨 tick 执行计划的显式载体）。

    不塞 ``for_what`` / ``desc`` / thought——那些是叙事，机器不可读、跨 tick 易漂。
    v1 只有 destinations；未来其他跨 tick 执行状态（如携带清单）再扩字段。
    """

    destinations: dict[str, DestinationPlan] = Field(
        default_factory=dict,
        description="按 activity location binding id 键控的目的地计划（如 meal_place）",
    )

    @model_validator(mode="after")
    def _check_destination_keys(self) -> ActivityContext:
        """key 必须是合法 binding id（与 ``ActivitySkill.location_bindings`` 同一安全规则）。

        state schema 是持久化边界——不校验的话，节点 bug / 迁移数据里的空 key、
        ``../meal``、``meal/place`` 会被静默落库（earlier review N-1）。
        """
        for binding_id in self.destinations:
            if not is_safe_name(binding_id):
                raise ValueError(
                    f"destinations key={binding_id!r} 非法 binding id"
                    "（须匹配 ^[a-z][a-z0-9_]*$，拒空白、/ 与 ..）"
                )
        return self


class Activity(StrictBase):
    """02 §3.8 activity 字段语义。

    name 是 character card 里定义的模板类型；desc 是这次的具体描述。
    """

    name: NonBlankStr = Field(description="模板类型（character card 定义）")
    desc: NonBlankStr = Field(description="这次的具体描述")
    started_at: IsoDatetime = Field(description="ISO8601 开始时间")
    engagement: float = Field(ge=0.0, le=1.0, description="投入度（心流维度）")
    with_whom: list[str] = Field(default_factory=list, description="关系档案 key 列表")
    for_what: NonBlankStr = Field(description="动机（为什么开始这件事）")
    step: NonBlankStr | None = Field(
        default=None,
        description="高级意图内此刻所在原子状态（activity SKILL states 之一，"
        "如 explore_food 的 walk/eat）；与 act.llm 的 current_state 合一（同一"
        "个状态既驱动 state_effects 档位 clamp、又是 step）；None=未进入状态机",
    )
    context: ActivityContext | None = Field(
        default=None,
        description="结构化执行上下文（destination plan 等跨 tick 状态）；节点派生写入，"
        "LLM 不得直写；None=无执行上下文（含 context 概念引入前的历史行）",
    )


# ─────────────────────────────────────────────────────────────────────
# location：在哪里
# ─────────────────────────────────────────────────────────────────────


class Location(StrictBase):
    """02 §3.4 LocationProvider / 02 附录 A location 字段。"""

    name: NonBlankStr = Field(description="地点名")
    address: str | None = Field(default=None, description="真实地址（虚拟模式可空）")
    city: str | None = Field(
        default=None,
        description="所在城市（ta 此刻在哪个城市；旅行/异地时随 location 变）。"
        "在家时 = home.city；environment.city 取此值。虚拟模式可空。",
    )
    type: str | None = Field(
        default=None,
        description="地点类型（home / restaurant / shopping_mall / ...）",
    )
    arrived_at: IsoDatetime = Field(description="ISO8601 到达时间")


# ─────────────────────────────────────────────────────────────────────
# time：什么时辰
# ─────────────────────────────────────────────────────────────────────


# weekday 用 Literal 而非枚举：保持 schema 简洁、可序列化
TimePhase = Literal["morning", "noon", "afternoon", "evening", "night", "late_night"]
Weekday = Literal[
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


class Time(StrictBase):
    """02 附录 A time 字段。

    iso 是 tick 触发时刻；phase 由 iso 派生（详见 02 §6）。
    """

    iso: IsoDatetime = Field(description="ISO8601 时间戳")
    phase: TimePhase
    weekday: Weekday


# ─────────────────────────────────────────────────────────────────────
# environment：天气 / 城市 / 氛围
# ─────────────────────────────────────────────────────────────────────


class Environment(StrictBase):
    """02 §3.4 EnvironmentProvider / 02 附录 A environment 字段。

    weather / temperature + 体感/天象增强字段走 Provider TTL 缓存；ambience 由 ta 自己
    描述（不走 Provider，守①被给予性铁律）。

    增强字段（feels_like 起）均带默认值——既往 state_latest（无这些字段）仍能加载，
    下个 TTL 刷新即由 Provider 填真值（向后兼容，2026-06-30 天气丰富化）。
    """

    city: NonBlankStr = Field(
        description="城市（取自 location.city —— ta 当前所在城市；在家时即 home.city，"
        "旅行/异地时随 location 变）。天气 Provider 用此值查当前城市天气。"
    )
    weather: NonBlankStr = Field(description="天气描述（如 '晴'）")
    temperature: float = Field(description="摄氏度（实际气温）")
    ambience: NonBlankStr = Field(description="ta 自己描述的氛围")
    weather_cached_at: IsoDatetime = Field(description="ISO8601 缓存时间戳（TTL 判断）")
    weather_cached_for: str = Field(
        default="",
        description="缓存天气对应的查询位置（weather_location 或 city）；与当前位置不一致即失效",
    )
    feels_like: float = Field(default=0.0, description="体感温度（摄氏度）")
    humidity: int = Field(default=0, description="相对湿度（%）")
    wind: str = Field(default="", description="风（向 + 速），如 '东南风 11km/h'")
    moon_phase: str = Field(default="", description="月相（emoji + 名），如 '🌕 满月'")
    sunrise: str = Field(default="", description="日出时刻 HH:MM（本地）")
    sunset: str = Field(default="", description="日落时刻 HH:MM（本地）")
    uv_index: int = Field(default=0, description="UV 指数（0-12）")
    precip_mm: float = Field(default=0.0, description="降水量（mm）")


# ─────────────────────────────────────────────────────────────────────
# presence：一个人 / 跟谁
# ─────────────────────────────────────────────────────────────────────


class Presence(StrictBase):
    """02 §3.9 presence 字段语义。

    others：关系档案 key 列表（不存关系数据本身，只存引用）
    """

    user_present: bool = Field(description="user 此刻是否在场")
    others: list[str] = Field(default_factory=list, description="其他在场的人（关系档案 key 列表）")
