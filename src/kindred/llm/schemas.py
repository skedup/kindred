"""LLM 输出顶层 schema 的 pydantic 单一真相源。

T1.sense.llm / T2.act.llm 的 LLM 返回 dict 顶层契约（14 §3.3 / §3.4）集中在此，
取代两个节点里手写的 isinstance 表驱动校验：字段 / 类型 / 范围规则只在这里定义
一次。节点侧 ``_validate_*`` 仍是薄封装——捕 pydantic ``ValidationError`` 包成各自
的 ``NodeContractError``（保 daemon ``except NodeContractError`` 兜底语义），但不再
重写校验逻辑。

单一真相源的两个外延（防 prompt / mock 与校验漂移，见 ``render_contract``）：

- ``real_client`` 的 system prompt 把 ``render_contract(model)`` 生成的硬约束字段
  清单**直接拼进**真 LLM prompt——字段名 / 必选性 / 范围与校验逻辑同源生成，改
  model 即改 prompt，不会两头各写一份漂移。
- ``mock`` 的 deterministic fixture 由 ``tests/unit/llm/test_schema_sync`` 反向钉死
  ——所有合法场景输出必须能过 ``model_validate``。

为什么用 ``extra="ignore"`` 而非 state 层的 ``extra="forbid"``：
LLM 输出是外部不可信源，真实模型可能夹带额外解释字段。只校我们消费的字段、忽略
多余键，比 fail-fast 拒整包更稳；下游仍按 key 取用，多余键不落 state。

为什么不把 ``act_decision`` / ``thought_diff`` 建成嵌套 model：
它们的深层结构另有专责校验器（``validate_act_decision`` → ``ActDecision``、
``apply_thought_diff``），且节点下游按 dict 取用。这里只把顶层「存在 + 是 dict」门
收住，避免裸下标在真 LLM 缺字段时裸 ``KeyError`` 逃出 ``NodeContractError`` 谱系。

注：``significance`` / ``mood_subjective`` / ``committed`` 用 ``strict=True`` ——
bool 是 int 子类，lax 模式会把 ``True`` 当 1、``3.0`` 截成 3 放行，strict 才能把
这些「类型对但语义错」的输入拒在门外（与原手写 ``isinstance(x, bool)`` 排除等价）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kindred.state._types import NonBlankStr

# 数值范围单一真相源——model 的 Field 约束、生成的 prompt 字段清单、测试都引用这里，
# 改一处即同步三处（避免 prompt 里硬写「1~10」与校验 ge/le 各自漂移）。
SIGNIFICANCE_MIN = 1
SIGNIFICANCE_MAX = 10
MOOD_MIN = 0
MOOD_MAX = 100


class _LlmResponseBase(BaseModel):
    """LLM 输出 model 共同基类：忽略多余字段（区别于 state 层的 forbid）。"""

    model_config = ConfigDict(extra="ignore")


class SenseResponse(_LlmResponseBase):
    """T1.sense.llm 顶层返回契约（14 §3.3）。

    深层结构（``act_decision`` / ``thought_diff`` 内部）由
    ``validate_act_decision`` / ``apply_thought_diff`` 细校，这里只收顶层门。
    """

    observed_user_present: bool | None = Field(
        description=(
            "仅根据本拍 partner 肯定式当前表达观察到的物理在场事实；"
            "计划、否定、回忆、转述、假设、含糊或无法确认时为 null"
        ),
    )
    affect_event_response: dict[str, Literal["up", "down"]] = Field(
        description=(
            "仅评价本拍 partner 新表达引起的主观 Affect 方向；key 只允许 "
            "stress/focus/arousal/clarity，最多两项，无响应时为空对象"
        ),
    )
    note: NonBlankStr = Field(
        description="此刻的内心独白；去空白后非空字符串",
    )
    significance: Annotated[
        int,
        Field(
            strict=True,
            ge=SIGNIFICANCE_MIN,
            le=SIGNIFICANCE_MAX,
            description=f"这一刻对你的重要度；整数 {SIGNIFICANCE_MIN}~{SIGNIFICANCE_MAX}",
        ),
    ]
    act_decision: dict[str, Any] = Field(
        description="行动意图对象（act/kind/target_activity/reason；内部结构另行细校）",
    )
    mood_subjective: Annotated[
        int,
        Field(
            strict=True,
            ge=MOOD_MIN,
            le=MOOD_MAX,
            description=f"此刻主观心情值；整数 {MOOD_MIN}~{MOOD_MAX}（覆写客观 derive 值）",
        ),
    ]
    ambience: NonBlankStr = Field(
        description=(
            "此刻主观氛围描述；由你根据地点/天气/时间/内在状态感受生成，"
            "写入 state.environment.ambience"
        ),
    )
    thought_diff: dict[str, Any] | None = Field(
        default=None,
        description="念头增删对象（add/remove；可省，内部结构另行细校）",
    )

    @field_validator("observed_user_present", mode="before")
    @classmethod
    def _normalize_observed_user_present(cls, value: object) -> bool | None:
        if type(value) is bool:
            return value
        return None

    @field_validator("affect_event_response", mode="before")
    @classmethod
    def _normalize_affect_event_response(cls, value: object) -> dict[str, str]:
        allowed_keys = {"stress", "focus", "arousal", "clarity"}
        if not isinstance(value, dict) or len(value) > 2:
            return {}
        if any(
            type(key) is not str
            or key not in allowed_keys
            or type(direction) is not str
            or direction not in {"up", "down"}
            for key, direction in value.items()
        ):
            return {}
        return dict(value)


class _PlaceEvent(_LlmResponseBase):
    """``destination_choice`` / ``location_arrival`` 共用的地点事件字段（第五趴 §6）。

    节点从 tick-local candidate resolver 或已提交计划恢复这些 canonical 世界事实；
    ``candidate_snapshot`` 作为留档快照写入 ``DestinationPlan``，不再交给模型复制。
    """

    binding_id: NonBlankStr = Field(description="activity location binding id（如 meal_place）")
    place_key: NonBlankStr = Field(
        description="代码恢复的稳定地点身份（如 virtual:xxx / baidu:uid）"
    )
    name: NonBlankStr = Field(description="代码从候选或计划恢复的地点名")
    address: str | None = Field(default=None, description="地址（候选没给就 null）")
    city: str | None = Field(default=None, description="所在城市（候选/计划没给就 null）")
    type: str | None = Field(
        default=None, description="地点类型（restaurant / park / ...，可 null）"
    )
    source: NonBlankStr = Field(description="候选来源（Provider 或 character-card 已知地点）")
    candidate_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        description="代码恢复的其余世界事实快照（distance_km / open_now / ...）",
    )


class DestinationChoiceEvent(_PlaceEvent):
    """本 tick 选中目的地（≠ 到达）——节点据此派生 activity.context 的跨 tick 计划。"""


class LocationArrivalEvent(_PlaceEvent):
    """本 tick 真实到达目的地——唯一允许更新 ``state.location`` 的事件。"""


class DestinationAbandonedEvent(_LlmResponseBase):
    """本 tick 放弃已有目的地计划——节点据此移除对应计划条目。"""

    binding_id: NonBlankStr = Field(description="要放弃的计划对应的 binding id")
    reason: str | None = Field(default=None, description="为什么放弃（一句话，可省）")


class ActResponse(_LlmResponseBase):
    """T2.act.llm 最终 JSON 契约（14 §3.4 + tool-loop M4.5）。

    ``failure_reason`` 不与 committed 强制互斥（failure 场景 committed=False +
    failure_reason；其他场景 committed=True + failure_reason=None）。只查类型 +
    committed↔final_state_diff 的交叉约束。

    M4.5 后地点事件、compose 与 send 都必须经工具调用表达，最终 JSON 只承载
    ``final_state_diff`` / ``committed`` / ``failure_reason``。节点会把真实工具事件
    归一化进 ``act_result``，不会消费最终 JSON 里的声明式 tool 字段。
    """

    committed: Annotated[
        bool,
        Field(strict=True, description="行动是否原子地走完"),
    ]
    final_state_diff: dict[str, Any] | None = Field(
        default=None,
        description="state 变更包；committed=true 时必填且为对象，committed=false 时可省",
    )
    failure_reason: str | None = Field(
        default=None,
        description="committed=false 时的失败原因；否则给 null（可省）",
    )

    @model_validator(mode="after")
    def _final_state_diff_required_when_committed(self) -> ActResponse:
        if self.committed and self.final_state_diff is None:
            raise ValueError(
                "committed=True 但缺 final_state_diff（committed=True 时必含且为 dict）",
            )
        return self


class SummaryResponse(_LlmResponseBase):
    """dream Step 1.summarize 顶层返回契约（docs/11 §4）。

    总结昨日 messages 为一段 markdown 摘要（保留人称视角、四类信息：
    关键事件 / 情绪轨迹 / 做了什么 / 自反馈线索）。输出只一个字段，
    不走复杂结构——摘要本身是有机文本，后续 Step 3 反思节点读它。
    """

    summary: NonBlankStr = Field(
        description="昨日 messages 的 markdown 摘要；去空白后非空字符串",
    )


def render_contract(model: type[BaseModel]) -> str:
    """据 model 字段生成「硬约束字段清单」文本——prompt 与校验同源的桥。

    每行 ``- <字段>（必填/可选）：<description>``。``real_client`` 把它直接拼进真 LLM
    的 system prompt，使「程序会校验哪些字段 / 范围」与校验逻辑由同一处 model 派生，
    改 model 即改 prompt，杜绝两头各写一份漂移。
    """
    lines = []
    for name, field in model.model_fields.items():
        req = "必填" if field.is_required() else "可选"
        desc = field.description or ""
        lines.append(f"- {name}（{req}）：{desc}")
    return "\n".join(lines)


__all__ = [
    "SummaryResponse",
    "MOOD_MAX",
    "MOOD_MIN",
    "SIGNIFICANCE_MAX",
    "SIGNIFICANCE_MIN",
    "ActResponse",
    "DestinationAbandonedEvent",
    "DestinationChoiceEvent",
    "LocationArrivalEvent",
    "SenseResponse",
    "render_contract",
]
