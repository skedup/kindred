"""web 层对外 JSON 契约（前端消费的形状）。

═══ 为什么独立定义，不直接吐 tick dict ═══

db 的 ``get_state_latest`` 返回的是**存储形状**（8 层 JSON 列原样 loads）。
直接把它丢给前端有三个问题：

1. **耦合存储**：前端会依赖 schema.sql 的列名 / 嵌套结构，schema 一动前端就碎。
2. **混淆两类 user**：叙事和指标揉在一坨，前端得自己挑拣。
3. **泄露内部**：triggered_at / act_decision 这种内部字段不该进 companion 视图。

所以 web 层定一套**稳定的对外契约**，service 层负责 存储形状 → 契约 的整形。
schema.sql 可以重构，只要 service 层适配，前端契约不变。

═══ 契约分层（对应两类 user）═══

- ``NarrativeView``：companion user。ta 此刻在做什么 + 心情 + 在哪。
- ``MetricsView``：engineer user。7 needs + 4 affect + 3 gauge + significance。
- ``NowResponse``：``GET /now`` 的完整响应，叙事为主、指标可选附带。

字段都用 Optional + 合理默认，**容忍空库 / 字段缺失**——可视化是观察窗，
读到半截数据也要优雅降级，不能因为某个 nullable 字段缺失就 500。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Gauge(BaseModel):
    """带描述的 0-100 表盘（body / mood / inner_pulse）。"""

    value: int = Field(ge=0, le=100)
    description: str = ""


class Outfit(BaseModel):
    """此刻穿戴 —— 来自 embodiment 层。

    只出**外观层**（上下衣/外套/鞋/袜/配饰/妆容）。私密层（bra/panties）
    不进可视化契约——那是 ta 的体面，不是观実窗该看的。
    """

    top: str = Field(default="", description="上衣")
    bottom: str = Field(default="", description="下衣")
    outer: str | None = Field(default=None, description="外套")
    shoes: str = Field(default="", description="鞋（或裸足）")
    accessory: list[str] = Field(default_factory=list, description="配饰")
    makeup: str | None = Field(default=None, description="妆容")


class Intimate(BaseModel):
    """私密层——内衣 + 袜，来自 embodiment。

    **三态 OR（N-6 后用户拍板）**：明文 bra/panties/socks 仅在 ``revealed=True`` 时返回。
    ``revealed = 揭示开关开（?reveal=1） OR 亲密度够（web.reveal_intimate）``；
    两者都不满足时 ``revealed=False``，三个明文字段均为 None，只给 ``tease`` 俏皮
    拒绝语——隐私边界有 ta 的人格，不是冷冰冰的 [已隐藏]。
    注：?reveal=1 对任意调用方无条件生效，陌生人绕过由网络层兜底（out-of-scope），
    本 contract 不声称是「数据隐私边界」。
    """

    revealed: bool = Field(
        default=False,
        description="是否已揭示明文（?reveal=1 OR 亲密度够）",
    )
    tease: str = Field(default="", description="未揭示时的俏皮拒绝语")
    bra: str | None = Field(default=None, description="文胸（仅 revealed 时）")
    panties: str | None = Field(default=None, description="内裤（仅 revealed 时）")
    socks: str | None = Field(default=None, description="袜子（仅 revealed 时）")


class ThoughtView(BaseModel):
    """心里的一个念头 —— 来自 interior.thoughts。"""

    description: str = Field(default="", description="念头内容")
    tag: str = Field(default="", description="分类标签")
    mood_w: int = Field(default=0, description="对 mood 的权重（-30~30，负=压心事）")


class NarrativeView(BaseModel):
    """叙事视图 —— 面向 companion user。

    主角是自然语言：ta 现在在做什么、为什么、在哪、什么心情、穿什么、
    身边有谁、心里转着什么念头。数值只留 mood 短描述做底色，不铺进度条。
    """

    activity_name: str = Field(default="", description="活动模板类型，如 explore_food")
    activity_desc: str = Field(default="", description="这次活动的具体描述")
    activity_for_what: str = Field(default="", description="动机：为什么开始")
    activity_step: str | None = Field(default=None, description="当前进行到哪一步")
    engagement: float = Field(default=0.0, ge=0.0, le=1.0, description="投入度/心流")
    with_whom: list[str] = Field(default_factory=list, description="这件事和谁一起做")

    location_name: str = Field(default="", description="此刻在哪")
    location_type: str | None = Field(default=None, description="地点类型（home/restaurant/...）")
    mood_note: str = Field(default="", description="心情底色一句话（mood.description）")
    note: str | None = Field(default=None, description="这一 tick 的心声（tick.note）")
    thoughts: list[ThoughtView] = Field(default_factory=list, description="心里正转着的念头")

    outfit: Outfit = Field(default_factory=Outfit, description="此刻穿戴（外观层）")
    intimate: Intimate = Field(
        default_factory=Intimate, description="私密层（内衣+袜，前端 hover）"
    )
    bag_items: list[str] = Field(default_factory=list, description="随身包里的东西")

    time_phase: str = Field(default="", description="时间相位（morning/noon/.../late_night）")
    weekday: str = Field(default="", description="星期几")

    user_present: bool = Field(default=False, description="user 此刻是否在场")
    others_present: list[str] = Field(default_factory=list, description="其他在场的人")
    city: str = Field(default="", description="城市")
    weather: str = Field(default="", description="天气一瞥")
    temperature: float | None = Field(default=None)
    ambience: str = Field(default="", description="ta 自己描述的氛围")


class MetricsView(BaseModel):
    """指标视图 —— 面向 engineer user。

    7 维 needs + 4 维 affect + 3 个 gauge + significance。
    用来理解 ta 怎么运转、调参、看趋势。默认在前端收起。
    """

    # interior.body / mood / inner_pulse（带描述）
    body: Gauge = Field(default_factory=lambda: Gauge(value=0))
    mood: Gauge = Field(default_factory=lambda: Gauge(value=0))
    inner_pulse: Gauge = Field(default_factory=lambda: Gauge(value=0))

    # interior.needs（7 维）
    needs: dict[str, int] = Field(default_factory=dict)
    # interior.affect（4 维）
    affect: dict[str, int] = Field(default_factory=dict)

    significance: int | None = Field(default=None, description="这一 tick 的高光度 1-10")


class NowResponse(BaseModel):
    """``GET /now`` 响应 —— 此刻切片。

    叙事为主视图，指标为可展开副视图。两类 user 一个端点全拿，
    前端按 user 类型决定展开哪块。
    """

    ts: str | None = Field(default=None, description="这一 tick 的 ISO8601 时间")
    trigger_source: str | None = Field(default=None, description="heartbeat / watcher / cold_start")
    narrative: NarrativeView
    metrics: MetricsView
    empty: bool = Field(default=False, description="True = 空库（心还没跑过任何 tick）")


class RelationshipView(BaseModel):
    """可信 loopback 观察面的当前 Relationship 原始四轴。"""

    declared_role: Literal["unlabeled", "friend", "lover", "hostile"]
    trust: int = Field(ge=0, le=100)
    attachment: int = Field(ge=0, le=100)
    attraction: int = Field(ge=0, le=100)
    friction: int = Field(ge=0, le=100)


class StreamItem(BaseModel):
    """生命流的一条——一个 tick 的轻量摘要（不含 8 层 state）。

    `/stream` 是「回放 ta 走过的路」，不是「此刻的全量切片」，所以只带
    每条的心声 + 高光度 + 行为结果，足够渲染一条时间线。点进某条
    再拉 ``/now`` 类的详情（后续 PR）。
    """

    id: int = Field(description="tick 主键，也是翻页 cursor")
    ts: str | None = Field(default=None, description="ISO8601 时间")
    note: str | None = Field(default=None, description="这一 tick 的心声")
    significance: int | None = Field(default=None, description="高光度 1-10")
    # 语义字段名（review N-2）：返回的是活动名字符串（act_decision.target_activity），
    # 不能叫 ``act_decision``——那是内部完整决策对象（act/kind/target_activity/reason）的
    # 列名，前端看到同名会预期拿到对象。web contract 职责是隐藏存储形状。
    target_activity: str | None = Field(
        default=None, description="这一 tick 决定做的活动名（无决定/act=False 为 None）"
    )


class StreamResponse(BaseModel):
    """``GET /stream`` | ``/episodes`` 响应 —— 一页轻量 tick（按 id DESC，最新在前）。

    两个端点共用同一形状，仅集合范围不同：/stream = 全量 tick；/episodes =
    significance>=7 高光。

    cursor 分页：拿到 ``next_cursor`` 后，下一页请求 ``?before=<next_cursor>``。
    ``next_cursor=None`` = 已到底（没更早的了）。
    """

    items: list[StreamItem] = Field(default_factory=list)
    next_cursor: int | None = Field(default=None, description="下一页的 before 值；None=到底")
    # review N-1：collection 语义，不是「物理空库」。/stream 首屏空=无 tick；
    # /episodes 首屏空=无高光（库里可能有 tick 但都不达阈值）。
    empty: bool = Field(
        default=False, description="True = 本端点首屏集合为空（/stream 无 tick；/episodes 无高光）"
    )


class InteriorHistoryValues(BaseModel):
    """单个 tick 的 interior 数值快照；缺失值保持 None，让折线自然断开。"""

    body: int | None = None
    mood: int | None = None
    inner_pulse: int | None = None
    needs: dict[str, int | None] = Field(default_factory=dict)
    affect: dict[str, int | None] = Field(default_factory=dict)


class InteriorHistoryPoint(BaseModel):
    """趋势图的一点：窄指标快照 + hover 所需的少量 tick 叙事。"""

    id: int
    ts: str | None = None
    trigger_source: str | None = None
    activity_name: str = ""
    activity_step: str | None = None
    note: str | None = None
    significance: int | None = None
    values: InteriorHistoryValues = Field(default_factory=InteriorHistoryValues)


class InteriorHistoryResponse(BaseModel):
    """``GET /interior/history`` 响应；points 按 tick id 正序。"""

    points: list[InteriorHistoryPoint] = Field(default_factory=list)
    empty: bool = False


ArtifactKind = Literal["text", "image", "mixed", "files"]
ArtifactAvailability = Literal["available", "partial", "unavailable"]
ArtifactMemberRole = Literal["title", "content", "image", "file"]
ArtifactMemberAvailability = Literal["available", "unavailable", "unsupported"]


class ArtifactListItem(BaseModel):
    tick_id: int
    artifact_ordinal: int
    ts: str
    activity_name: str
    kind: ArtifactKind
    label: str
    member_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    availability: ArtifactAvailability


class ArtifactMemberView(BaseModel):
    member_ordinal: int = Field(ge=0)
    role: ArtifactMemberRole
    media_type: str
    bytes: int = Field(ge=0)
    availability: ArtifactMemberAvailability


class ArtifactDetailResponse(ArtifactListItem):
    members: list[ArtifactMemberView] = Field(default_factory=list)


class ArtifactListResponse(BaseModel):
    items: list[ArtifactListItem] = Field(default_factory=list)
    next_cursor: str | None = None
