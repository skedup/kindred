"""派生公式 helpers - interior / time 子层的纯函数派生。

参考文档:

- docs/02-state-system.md §5(tick 处理顺序)+ §6(派生公式 ratified)
- docs/02-state-system.md §6.4(arousal 独立设计)
- docs/14-heart-graph.md §3.2(T1.sense.derive)
- docs/discussions/2026-06-03-02-v03-derive-spec.md(archive ratified)

哲学:

- **纯函数**:同输入 → 同输出,无副作用,不读 db / 不调 LLM
- **常量集中**:衰减率 / 乘数 / baseline 全部在 ``_derive_constants`` 模块
- **Layer 1 永远派生**:body / mood / inner_pulse 不直接写入(§4.2 原则 1)

公开函数(按调用顺序):

- :func:`elapsed_minutes` - ISO 时间戳减法
- :func:`derive_time_layer` - 从 ``triggered_at`` 派 time 子层
- :func:`decay_interior` - needs / affect / thoughts 衰减(§5.2 / §5.3 真公式)
- :func:`apply_cascade_thresholds` - §5.4 多米诺级联表
- :func:`derive_body` - Layer 1 派生(§6.1,desc 不被生理标签抢占)
- :func:`derive_mood` - Layer 1 派生(§6.2)
- :func:`derive_inner_pulse` - Layer 1 派生(§6.3,欲求 / 行动余力 / 激活形状)

``inner_pulse`` 保持模糊：全部 Needs / Affect 稳定轴参与，但不输出需求排名或
Activity 分数。trait 接缝留给后续人格派生，不直接塞进本函数。
"""

from __future__ import annotations

import operator as op_mod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from math import isfinite
from typing import TYPE_CHECKING, Final

from kindred.state._derive_constants import (
    ACTIVITY_NEEDS_MULTIPLIERS,
    AFFECT_BASELINES,
    AFFECT_FORCE_TO_BASELINE_ACTIVITIES,
    AFFECT_TO_THOUGHT_CASCADES,
    AROUSAL_HIGH_FOCUS_PENALTY,
    AROUSAL_HIGH_THRESHOLD,
    COMFORT_COLD_C,
    COMFORT_DRIFT_RATE_PER_MIN,
    COMFORT_HOT_C,
    COMFORT_MAX_BODY_PENALTY,
    COMFORT_MAX_PRECIP_PENALTY,
    COMFORT_MAX_THERMAL_PENALTY,
    COMFORT_NEUTRAL_TARGET,
    COMFORT_OUTDOOR_LOCATION_TYPES,
    NEEDS_DECAY_RATE_PER_MIN,
    NEEDS_OR_AFFECT_TO_AFFECT_CASCADES,
    NEEDS_TO_THOUGHT_CASCADES,
    Operator,
    ThresholdToThoughtCascade,
)
from kindred.state.interior import (
    Affect,
    GaugeWithDescription,
    Interior,
    Needs,
    Thought,
)
from kindred.state.outward import Time, TimePhase, Weekday

if TYPE_CHECKING:
    from collections.abc import Sequence


# ─────────────────────────────────────────────────────────────────────
# 时间派生
# ─────────────────────────────────────────────────────────────────────


# decay 可累加的 elapsed 上限（分钟）。needs/affect 衰减 rate 按「连续清醒」
# 标定（energy 0.2/min → 8.3h 耗尽 / fatigue 0.16/min → 10.4h 满），隐含「期间
# 不睡」。但当 daemon 停机数天后冷启（cold_start 首 tick 的 elapsed = 上次
# seed/tick 到现在，可达数天乃至数周），线性外推会把 fatigue 顶到 100 /
# energy 压到 0——心一启动就「累瘫出不来」（每 tick 都说「只想瘫着」）。
#
# 现实语义：超长 gap 不是「连续清醒 N 天」而是「心不在场」——重新启动时心
# 该像「休息一段后刚醒」：轻度疲惫但能动起来，而非枯竭。
# cap = 120min(2h)：使 needs 动到「刚睡醒」的水位而非顶到极值。以 seed 健康
# 初值（energy 60 / fatigue 40 / hunger 25）为例，超长 gap 经 2h cap 后落到
# energy≈36 / fatigue≈59 / hunger≈61——「有点困、有点饿但有活力」。
# （曾用 3h→energy≈24 偏低：心醒来立刻选耗能 activity（如 explore_food walk
# 降 energy medium 10~25）会瞬间归 0；2h 起点留出活动余量。）
# 远高于正常 5min/1h tick 间隔（不影响正常节奏）。
MAX_DECAY_ELAPSED_MIN: Final[int] = 120


def elapsed_minutes(prev_iso: str, curr_iso: str) -> int:
    """两个 ISO 时间戳之间的分钟数(向下取整)。

    入参须是 timezone-aware ISO8601(与 IsoDatetime 类型契约一致)。
    """
    prev_dt = datetime.fromisoformat(prev_iso)
    curr_dt = datetime.fromisoformat(curr_iso)
    delta = curr_dt - prev_dt
    return int(delta.total_seconds() // 60)


# 02 §3 time.phase 6 桶定义(discussions/2026-06-03-02-v03-derive-spec.md §3)
_PHASE_BOUNDARIES: tuple[tuple[int, int, TimePhase], ...] = (
    (5, 11, "morning"),
    (11, 13, "noon"),
    (13, 17, "afternoon"),
    (17, 22, "evening"),
    (22, 26, "night"),  # 22:00 ~ 02:00 跨午夜,用 26 表 2:00 next day
    (2, 5, "late_night"),
)

# Python datetime.weekday() 0=Monday → state Weekday Literal
_WEEKDAY_NAMES: tuple[Weekday, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _phase_from_hour(hour: int) -> TimePhase:
    """从小时(0-23)查 phase。

    跨午夜 night 桶(22-26)用扩展表,再 mod 24 比较。
    """
    for start, end, phase in _PHASE_BOUNDARIES:
        # 22-26 桶:22-23 命中或 0-1 命中
        if end > 24:
            if hour >= start or hour < (end - 24):
                return phase
        elif start <= hour < end:
            return phase
    # late_night 兜底(02-05):上面已穷举但保留 raise 防 schema 漂移
    raise AssertionError(f"_phase_from_hour: hour={hour} 没匹配--_PHASE_BOUNDARIES 漂移?")


def derive_time_layer(triggered_at: str) -> Time:
    """从 ``triggered_at`` ISO 时间戳派 Time 子层(iso / phase / weekday)。

    - iso = triggered_at(透传)
    - phase = 按 hour 查 _PHASE_BOUNDARIES
    - weekday = datetime.weekday() → Literal name

    入参契约:triggered_at 是 timezone-aware ISO8601 字符串。
    """
    dt = datetime.fromisoformat(triggered_at)
    return Time(
        iso=triggered_at,
        phase=_phase_from_hour(dt.hour),
        weekday=_WEEKDAY_NAMES[dt.weekday()],
    )


# ─────────────────────────────────────────────────────────────────────
# Interior 时间演化 + 级联
# ─────────────────────────────────────────────────────────────────────


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECONDS_PER_MINUTE = 60_000_000


@dataclass(frozen=True)
class ComfortInputs:
    apparent_temperature: float | None = None
    precip_mm: float | None = None
    location_type: str | None = None
    clothing_coverage: int = 0


def _epoch_microseconds(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"非合法 ISO8601：{value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"ISO8601 缺少时区：{value!r}")
    delta = parsed.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _quantized_interval_delta(
    rate_per_minute: float | Fraction,
    prev_iso: str,
    curr_iso: str,
) -> int:
    start = _epoch_microseconds(prev_iso)
    end = _epoch_microseconds(curr_iso)
    if end <= start:
        return 0
    start = max(start, end - MAX_DECAY_ELAPSED_MIN * _MICROSECONDS_PER_MINUTE)
    rate = Fraction(str(rate_per_minute))
    if rate == 0:
        return 0

    def _total(at: int) -> int:
        scaled = abs(rate) * Fraction(at, _MICROSECONDS_PER_MINUTE)
        units = scaled.numerator // scaled.denominator
        return units if rate > 0 else -units

    return _total(end) - _total(start)


def derive_comfort_target(needs: Needs, inputs: ComfortInputs) -> int:
    """从直接身体压力与新鲜户外数值事实派生 comfort 目标。"""
    hunger_penalty = max(0.0, needs.hunger - 65) * 0.15
    fatigue_penalty = max(0.0, needs.fatigue - 65) * 0.15
    body_penalty = min(COMFORT_MAX_BODY_PENALTY, round(hunger_penalty + fatigue_penalty))
    target = COMFORT_NEUTRAL_TARGET - body_penalty

    location_type = (inputs.location_type or "").strip().lower()
    outdoors = location_type in COMFORT_OUTDOOR_LOCATION_TYPES
    apparent = inputs.apparent_temperature
    if outdoors:
        if apparent is not None and isfinite(apparent):
            coverage = max(0, min(4, inputs.clothing_coverage))
            if apparent < COMFORT_COLD_C:
                mismatch = (COMFORT_COLD_C - apparent) * (4 - coverage) / 4
            elif apparent > COMFORT_HOT_C:
                mismatch = (apparent - COMFORT_HOT_C) * coverage / 4
            else:
                mismatch = 0.0
            target -= min(COMFORT_MAX_THERMAL_PENALTY, round(mismatch * 1.5))
        precip = inputs.precip_mm
        if precip is not None and isfinite(precip):
            target -= min(COMFORT_MAX_PRECIP_PENALTY, round(max(0.0, precip) * 2))

    return max(0, min(100, target))


def _decay_needs(
    prev: Needs,
    prev_iso: str,
    curr_iso: str,
    step_name: str | None,
    comfort_inputs: ComfortInputs,
) -> Needs:
    """Layer 2 needs 按真实时间积分，并让 comfort 向物理目标回归。"""
    multipliers = ACTIVITY_NEEDS_MULTIPLIERS.get(step_name, {}) if step_name else {}

    updates: dict[str, int] = {}
    for field, base_rate in NEEDS_DECAY_RATE_PER_MIN.items():
        if field == "comfort":
            continue
        mult = multipliers.get(field, 1.0)
        rate = Fraction(str(base_rate)) * Fraction(str(mult))
        delta = _quantized_interval_delta(rate, prev_iso, curr_iso)
        prev_value = getattr(prev, field)
        new_value = max(0, min(100, prev_value + delta))
        updates[field] = new_value

    passive = prev.model_copy(update=updates)
    comfort_target = derive_comfort_target(passive, comfort_inputs)
    comfort_step = abs(
        _quantized_interval_delta(
            COMFORT_DRIFT_RATE_PER_MIN,
            prev_iso,
            curr_iso,
        )
    )
    target_offset = prev.comfort - derive_comfort_target(prev, comfort_inputs)
    target_offset = max(-comfort_target, min(100 - comfort_target, target_offset))
    if target_offset < 0:
        target_offset = min(0, target_offset + comfort_step)
    else:
        target_offset = max(0, target_offset - comfort_step)
    comfort = comfort_target + target_offset
    return passive.model_copy(update={"comfort": comfort})


def _decay_affect(
    prev: Affect,
    elapsed_min: int,
    step_name: str | None,
    arousal_baseline: int,
) -> Affect:
    """Layer 3 affect 回归个体基线（02 §5.3 / §6.4）。

    默认公式:``next = prev + (baseline - prev) * rate * elapsed_min``。

    step 特例（按 activity.step 查）:

    - sleep 等 ``AFFECT_FORCE_TO_BASELINE_ACTIVITIES`` → 所有 affect 直接 = baseline
    """
    force_baseline = step_name in AFFECT_FORCE_TO_BASELINE_ACTIVITIES

    updates: dict[str, int] = {}
    for field, (default_baseline, rate) in AFFECT_BASELINES.items():
        baseline = arousal_baseline if field == "arousal" else default_baseline
        prev_value = getattr(prev, field)
        if force_baseline:
            new_value = baseline
        else:
            delta = (baseline - prev_value) * rate * elapsed_min
            candidate = round(prev_value + delta)
            new_value = max(min(prev_value, baseline), min(max(prev_value, baseline), candidate))
        updates[field] = new_value

    return prev.model_copy(update=updates)


def _validate_iso_or_raise(curr_iso: str | None) -> None:
    """校 ``curr_iso`` 是合法 ISO8601；None 跳过。

    防御：字符串直接 ``>`` 比 ``expire_at`` 是字符级 lexicographic。
    “banana” 开头 ``b``(98) 比 “2026…” 开头 ``2``(50) 大，所有有 ``expire_at`` 的
    thought 会被 lexicographic 静默判为过期 → 隐式清空队列。

    本函数是节点级红线：``decay_interior`` / ``apply_cascade_thresholds`` 入口调，
    不合法 ISO 立即 ``ValueError``，不静默匆过（与 ``query_active_thoughts`` 同款）。
    """
    if curr_iso is None:
        return
    try:
        datetime.fromisoformat(curr_iso)
    except (ValueError, TypeError) as exc:
        msg = f"非合法 ISO8601：{curr_iso!r}"
        raise ValueError(msg) from exc


def _has_thought_with_tag(thoughts: Sequence[Thought], tag: str) -> bool:
    """检查 thoughts 列表里是否已有指定 tag 的 thought。

    用于级联触发去重——同 tag 不重复加，让现有 thought 自然按 expire_at 衰减。
    仅供 ``apply_cascade_thresholds`` 内部使用，P-3 从 ``_derive_constants`` 移出
    避 layering 症状（常量模块不应装 domain helper）。
    """
    return any(t.tag == tag for t in thoughts)


def _filter_expired_thoughts(
    thoughts: Sequence[Thought],
    curr_iso: str,
) -> list[Thought]:
    """从 thoughts 队列过滤过期项（02 §4.2 原则 4）。

    运行时不读过期项，但物理保留（09 §3.2 记忆层起收）。
    本函数只是“不传出去”——不从 DB 删。

    expire_at 为 None 的 thought 本函数当**不过期**始终保留——但这只是过滤器的防御兜底：
    apply_thought_diff 已在落库前给所有 None 念头补 TTL，**Layer 4 实际不存永不过期念头**
    （永久记忆归灵魂层）。故正常运行不会有 None 漂到这里。
    """
    return [t for t in thoughts if t.expire_at is None or t.expire_at > curr_iso]


def decay_interior(
    prev: Interior,
    *,
    prev_iso: str,
    curr_iso: str,
    step_name: str | None = None,
    comfort_inputs: ComfortInputs,
    arousal_baseline: int = AFFECT_BASELINES["arousal"][0],
) -> Interior:
    """needs / affect / thoughts 自然衰减(02 §5 步 1 / §A 下沉到 step)。

    实现 §5.2 §5.3 真公式:

    - needs:单向衰减 + step 乘数
    - affect:回归基线收敛 + step tick delta
    - thoughts:过滤 expire_at 已过运行期项(需 ``curr_iso``)

    入参:

    - ``prev_iso / curr_iso``:相邻 tick 的 timezone-aware ISO 时间戳
    - ``step_name``:当前原子动作名=``activity.step``(用于查乘数表，§A 下沉到 step)。
      为 None 走默认 ×1.0
    - ``comfort_inputs``:T1 从 State 构造的瞬时数值事实

    返回:新 Interior 实例,不 mutate 入参。

    Raises:
        ValueError: 时间戳不是合法 timezone-aware ISO8601。
    """
    start_us = _epoch_microseconds(prev_iso)
    end_us = _epoch_microseconds(curr_iso)
    if end_us <= start_us:
        return prev
    elapsed_min = elapsed_minutes(prev_iso, curr_iso)

    # 超长 gap（如 daemon 停机数天后 cold_start 首 tick）不按连续清醒线性外推，
    # 否则 fatigue 顶 100 / energy 压 0，心一启动就累瘫出不来。封顶到一个自然
    # 疲惫周期（见 MAX_DECAY_ELAPSED_MIN 语义注）。
    elapsed_min = min(elapsed_min, MAX_DECAY_ELAPSED_MIN)

    new_needs = _decay_needs(
        prev.needs,
        prev_iso,
        curr_iso,
        step_name,
        comfort_inputs,
    )
    new_affect = _decay_affect(
        prev.affect,
        elapsed_min,
        step_name,
        arousal_baseline,
    )
    new_thoughts = _filter_expired_thoughts(prev.thoughts, curr_iso)

    return prev.model_copy(
        update={
            "needs": new_needs,
            "affect": new_affect,
            "thoughts": new_thoughts,
        },
    )


_OPS: Final[dict[Operator, Callable[[int, int], bool]]] = {
    ">=": op_mod.ge,
    "<=": op_mod.le,
}


def _eval_threshold(value: int, op: Operator, threshold: int) -> bool:
    """阈值比较（P-6 从 if/elif 升级）。

    ``op`` 类型是 ``Literal[">=", "<="]``——mypy 在调用点拾住拼写错，
    运行期查不到 ``op`` 是常量表中未定义的字面量。
    """
    return _OPS[op](value, threshold)


def apply_cascade_thresholds(
    interior: Interior,
    *,
    curr_iso: str | None = None,
    focus_baseline: int = AFFECT_BASELINES["focus"][0],
    arousal_cascade_value: int | None = None,
) -> Interior:
    """02 §5.4 多米诺级联阈值检查。

    三类级联:

    - ``NEEDS_TO_THOUGHT_CASCADES``:needs 阈值 → 加 thought(hunger / energy)
    - ``NEEDS_OR_AFFECT_TO_AFFECT_CASCADES``:跨层 / 同层 affect 修改
      (fatigue → stress)
    - ``AFFECT_TO_THOUGHT_CASCADES``:affect 阈值 → 加 thought(arousal 高)
    - arousal 高位时，将 focus 限制在个体 focus baseline 下方的幂等上限

    **去重**:同 tag thought 已存在则不重复加。
    **expire_at**:需 ``curr_iso`` 计算;None 时跳过加 thought 的级联，
    但字面合法性依然需检查（节点级 IsoDatetime 红线）。
    ``arousal_cascade_value`` 只覆盖高位 arousal 的触发采样值，不会覆盖
    ``interior.affect.arousal``；T1 用它消费自然回归前已提交的行动结果。

    Raises:
        ValueError: ``curr_iso`` 不为 None 且不是合法 ISO8601。
    """
    _validate_iso_or_raise(curr_iso)
    new_needs = interior.needs
    new_affect = interior.affect
    new_thoughts = list(interior.thoughts)

    # 1) needs/affect 阈值 → affect 修改
    for c in NEEDS_OR_AFFECT_TO_AFFECT_CASCADES:
        check_obj = new_needs if c.check_layer == "needs" else new_affect
        if not _eval_threshold(getattr(check_obj, c.check_field), c.operator, c.threshold):
            continue
        # NamedTuple 上 c.target_layer: Literal["affect"] 已费权锁死下游层，
        # 运行时不需 raise——mypy 在常量定义点拦住跨层拼写错。
        prev_v = getattr(new_affect, c.target_field)
        new_v = max(0, min(100, prev_v + c.delta))
        new_affect = new_affect.model_copy(update={c.target_field: new_v})

    effective_arousal = (
        new_affect.arousal if arousal_cascade_value is None else arousal_cascade_value
    )
    if effective_arousal >= AROUSAL_HIGH_THRESHOLD:
        focus_cap = max(0, focus_baseline - AROUSAL_HIGH_FOCUS_PENALTY)
        if new_affect.focus > focus_cap:
            new_affect = new_affect.model_copy(update={"focus": focus_cap})

    # 2) needs / 3) affect 阈值 → 加 thought(curr_iso 必备)
    if curr_iso is not None:
        curr_dt = datetime.fromisoformat(curr_iso)
        new_thoughts = _apply_threshold_to_thought_cascades(
            NEEDS_TO_THOUGHT_CASCADES,
            new_needs,
            new_thoughts,
            curr_dt,
        )
        affect_for_cascade = new_affect.model_copy(
            update={"arousal": effective_arousal},
        )
        new_thoughts = _apply_threshold_to_thought_cascades(
            AFFECT_TO_THOUGHT_CASCADES,
            affect_for_cascade,
            new_thoughts,
            curr_dt,
        )
        # 4) 反向消解（A）:needs/affect 回到健康侧→自动清掉生理念头。
        # 不依赖 LLM 主动 remove，也不只等 expire_at 衰减——饱了/睡醒就该立刻不再
        # “身体在抗议”。滞回（clear_threshold 与 threshold 拉开）避免阈值附近拖动。
        new_thoughts = _clear_recovered_thought_cascades(
            NEEDS_TO_THOUGHT_CASCADES, new_needs, new_thoughts
        )
        new_thoughts = _clear_recovered_thought_cascades(
            AFFECT_TO_THOUGHT_CASCADES, new_affect, new_thoughts
        )

    return interior.model_copy(
        update={
            "needs": new_needs,
            "affect": new_affect,
            "thoughts": new_thoughts,
        },
    )


def _apply_threshold_to_thought_cascades(
    cascades: list[ThresholdToThoughtCascade],
    check_obj: Needs | Affect,
    thoughts: list[Thought],
    curr_dt: datetime,
) -> list[Thought]:
    """「阈值命中 + tag 去重 + append Thought」通用级联 helper（P-5）。

    不 mutate 入参 thoughts——返回新 list（如果有加项）或同一引用（没加项）。
    """
    result = thoughts
    for c in cascades:
        if not _eval_threshold(getattr(check_obj, c.field_name), c.operator, c.threshold):
            continue
        if _has_thought_with_tag(result, c.thought_tag):
            continue
        # 首次需加项时才 copy list（微优化，多次 copy 也无害）
        if result is thoughts:
            result = list(thoughts)
        result.append(
            Thought(
                description=c.description,
                mood_w=c.mood_w,
                expire_at=(curr_dt + timedelta(hours=c.expire_hours)).isoformat(),
                tag=c.thought_tag,
            ),
        )
    return result


def _opposite_satisfied(operator: str, value: int, clear_threshold: int) -> bool:
    """反向消解判定：value 是否已回到健康侧（跨过 clear_threshold）。

    方向由触发 ``operator`` 镜像推导：
    - ``>=`` 触发高位（如 hunger>=90）→ ``value <= clear_threshold`` 算恢复。
    - ``<=`` 触发低位（如 energy<=10）→ ``value >= clear_threshold`` 算恢复。
    """
    if operator == ">=":
        return value <= clear_threshold
    return value >= clear_threshold


def _clear_recovered_thought_cascades(
    cascades: list[ThresholdToThoughtCascade],
    check_obj: Needs | Affect,
    thoughts: list[Thought],
) -> list[Thought]:
    """生理念头反向消解（A）：needs/affect 回健康侧 → 移除对应 tag 的念头。

    仅处理配了 ``clear_threshold`` 的 cascade（None 跳过，仅靠 expire_at 衰减）。
    不 mutate 入参——有清项返新 list，无清项返同一引用。与
    ``_apply_threshold_to_thought_cascades`` 镜像：一个负责加、一个负责清。
    """
    result = thoughts
    for c in cascades:
        if c.clear_threshold is None:
            continue
        value = getattr(check_obj, c.field_name)
        if not _opposite_satisfied(c.operator, value, c.clear_threshold):
            continue
        if not _has_thought_with_tag(result, c.thought_tag):
            continue
        if result is thoughts:
            result = list(thoughts)
        result = [t for t in result if t.tag != c.thought_tag]
    return result


# ─────────────────────────────────────────────────────────────────────
# Layer 1 派生(02 §6 草案,可用)
# ─────────────────────────────────────────────────────────────────────


def _clamp(low: int, high: int, value: float) -> int:
    """夹紧到 [low, high],向下取整。"""
    return int(max(low, min(high, value)))


def _pick_first_match(rules: Sequence[tuple[bool, str]]) -> str:
    """找第一个 True 的描述;rules 末尾应有 (True, fallback) 兜底。"""
    for cond, desc in rules:
        if cond:
            return desc
    raise AssertionError("_pick_first_match: rules 末尾缺 (True, fallback) 兜底")


def derive_body(needs: Needs, affect: Affect) -> GaugeWithDescription:
    """02 §6.1 body 派生公式。

    value = energy*0.30 + (100-fatigue)*0.25 + (100-hunger)*0.20
            + comfort*0.15 + clarity*0.10

    description 不让 ``hunger>80`` / ``fatigue>80`` **无条件抢占**——否则
    hunger>80 会盖过 energy 高的事实（“有力气但身体说饿了”）。
    现让 value 高（>80 状态很好 / >60 还行）优先于生理标签，仅在 value 不高
    时才用 “饿了 / 心智疲倦” 描述。
    """
    value = _clamp(
        0,
        100,
        needs.energy * 0.30
        + (100 - needs.fatigue) * 0.25
        + (100 - needs.hunger) * 0.20
        + needs.comfort * 0.15
        + affect.clarity * 0.10,
    )
    description = _pick_first_match(
        [
            # value 高时优先抢胜（B2）——有力气就该显“状态很好/还行”，
            # 不被“饿了”盖住（饿由 hunger thought / inner_pulse 驱动去表达）。
            (value > 80, "状态很好"),  # noqa: PLR2004
            (value > 60, "还行"),  # noqa: PLR2004
            # value 不高时，生理标签才上位解释“为何不好”。
            (needs.hunger > 80, "饿了"),  # noqa: PLR2004
            (needs.fatigue > 80, "心智疲倦"),  # noqa: PLR2004
            (needs.energy < 30, "累"),  # noqa: PLR2004
            (needs.comfort < 30, "不舒服"),  # noqa: PLR2004
            (True, "一般"),
        ],
    )
    return GaugeWithDescription(value=value, description=description)


def _describe_mood_level(value: int) -> str:
    """mood 数值 → 程度词(02 §6.2 占位)。"""
    if value >= 80:
        return "心情很好"
    if value >= 60:
        return "心情不错"
    if value >= 40:
        return "一般"
    if value >= 20:
        return "心情有点低"
    return "心情很差"


def derive_mood(
    needs: Needs,
    affect: Affect,
    thoughts: Sequence[Thought],
) -> GaugeWithDescription:
    """02 §6.2 mood 派生公式。

    baseline 50 + sum(thought.mood_w) + needs_bonus - stress_penalty。

    **expire 过滤**：thoughts 由上游 ``decay_interior`` 在 §5 步 1 过滤完毕；
    本函数信任入参不再过滤（纯函数职责边界）。
    """
    baseline = 50
    thought_sum = sum(t.mood_w for t in thoughts)

    needs_bonus = 0
    if (
        100 - needs.hunger > 60  # noqa: PLR2004
        and needs.energy > 60  # noqa: PLR2004
        and needs.comfort > 60  # noqa: PLR2004
    ):
        needs_bonus = 5

    stress_penalty = max(0, affect.stress - 50) * 0.3

    value = _clamp(0, 100, baseline + thought_sum + needs_bonus - stress_penalty)

    # description:从最显著 thought 派生(abs(mood_w) > 8)
    salient = max(thoughts, key=lambda t: abs(t.mood_w), default=None)
    salient_threshold = 8
    if salient is not None and abs(salient.mood_w) > salient_threshold:
        description = f"{_describe_mood_level(value)},因为{salient.description}"
    else:
        description = _describe_mood_level(value)

    return GaugeWithDescription(value=value, description=description)


def derive_inner_pulse(needs: Needs, affect: Affect) -> GaugeWithDescription:
    """从欠缺、行动余力和激活形状派生模糊的内在涌动。

    Needs 的方向并不统一：hunger / fatigue 高表示压力，其余满足度低表示
    欠缺。先把它们投影为 ``need_pressure``，再由 ``capacity`` 门控；
    ``activation`` 只做小幅修饰，不会把 arousal 直接翻译成亲密意图。
    """
    recovery_pressure = max(100 - needs.energy, needs.fatigue)
    pressures = (
        needs.hunger,
        recovery_pressure,
        100 - needs.comfort,
        100 - needs.social,
        100 - needs.stimulation,
        100 - needs.aesthetic,
    )
    peak_pressure = max(pressures)
    need_pressure = peak_pressure * 0.55 + (sum(pressures) / len(pressures)) * 0.45
    capacity = needs.energy * 0.50 + (100 - needs.fatigue) * 0.30 + affect.clarity * 0.20
    activation = affect.arousal * 0.55 + affect.stress * 0.45

    capacity_gate = 0.45 + capacity / 100 * 0.55
    open_capacity = max(0.0, capacity - need_pressure) * 0.15
    activation_shift = (activation - 50) * 0.12
    value = _clamp(
        0,
        100,
        need_pressure * capacity_gate + open_capacity + activation_shift,
    )

    description = _describe_need_capacity(max(need_pressure, peak_pressure), capacity, value)
    activation_description = _describe_activation(affect)
    if activation_description is not None:
        description = f"{description}；{activation_description}"
    return GaugeWithDescription(value=value, description=description)


def _describe_need_capacity(need_pressure: float, capacity: float, value: int) -> str:
    if need_pressure >= 70:  # noqa: PLR2004
        if capacity < 30:  # noqa: PLR2004
            return "欠缺很明显，身体却想慢下来"
        if capacity >= 55:  # noqa: PLR2004
            return "欠缺很明显，也有余力去回应"
        return "欠缺很明显，余力还不算充足"
    if need_pressure >= 55:  # noqa: PLR2004
        if capacity < 30:  # noqa: PLR2004
            return "欠缺正在涌起，身体却想慢下来"
        if capacity >= 55:  # noqa: PLR2004
            return "心里有些欠缺，也有余力去回应"
        return "心里有些欠缺在涌动"
    if need_pressure < 35 and capacity >= 60:  # noqa: PLR2004
        return "松弛着，也对接下来保持开放"
    if value >= 50:  # noqa: PLR2004
        return "有些东西在心里涌动"
    return "没什么非做不可的"


def _describe_activation(affect: Affect) -> str | None:
    focus_shape = (
        "，心思也有点散"
        if affect.focus < 35  # noqa: PLR2004
        else "，注意收得很紧"
        if affect.focus > 70  # noqa: PLR2004
        else ""
    )
    if affect.stress >= 70:  # noqa: PLR2004
        return f"有些绷着{focus_shape or '，心里不太安静'}"
    if affect.arousal >= 70:  # noqa: PLR2004
        return f"身心有些被唤起{focus_shape}"
    if affect.focus < 30:  # noqa: PLR2004
        return "心思有些散"
    if affect.focus > 75:  # noqa: PLR2004
        return "注意收得很紧"
    return None


# ─────────────────────────────────────────────────────────────────────
# 组合:Layer 1 整体重派
# ─────────────────────────────────────────────────────────────────────


def rederive_layer1(interior: Interior) -> Interior:
    """从 Layer 2/3/4 重派 Layer 1(body / mood / inner_pulse)。

    02 §5 步 5。返回新 Interior 实例(不 mutate 入参)。
    """
    return interior.model_copy(
        update={
            "body": derive_body(interior.needs, interior.affect),
            "mood": derive_mood(interior.needs, interior.affect, interior.thoughts),
            "inner_pulse": derive_inner_pulse(interior.needs, interior.affect),
        },
    )


__all__ = [
    "ComfortInputs",
    "apply_cascade_thresholds",
    "decay_interior",
    "derive_body",
    "derive_comfort_target",
    "derive_inner_pulse",
    "derive_mood",
    "derive_time_layer",
    "elapsed_minutes",
    "rederive_layer1",
]
