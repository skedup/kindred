"""派生常量表 — 02 v0.3 §5/§6 ratified 常量。

参考文档：

- docs/02-state-system.md §5.2（needs 衰减率 + activity 乘数）
- docs/02-state-system.md §5.3（affect 回归基线收敛）
- docs/02-state-system.md §5.4（多米诺级联完整表）

哲学：

- **常量集中**：所有 magic number 都收口到本模块，让微调不漫到 helper 函数里
- **YAGNI**：v0.3 最小集，未列入字段 / activity 走默认（不衰 / ×1.0）
- **trait 接口预留**：v0.4 / 03-trait-modifier 落地后由 baseline 函数读 trait

**v0.3 所有常量**单位为「**每分钟**」。线性 Needs rate 由 helper 按真实时间区间量化，
Affect 仍按 ``elapsed_min`` 回归。
"""

from __future__ import annotations

from typing import Final, Literal, NamedTuple

#: 阈值比较运算符：v0.3 仅支持 ``>=`` / ``<=``。
#: mypy 会拦住常量表里拼写错的 op（如 ``">=="`` / ``"=>"``）。
Operator = Literal[">=", "<="]


# ──────────────────────────────────────────────────────────────────
# 级联表类型（NamedTuple——让字段名可读、加字段不错位、mypy 校拼写）
# ──────────────────────────────────────────────────────────────────


class ThresholdToThoughtCascade(NamedTuple):
    """阈值命中 → 加 thought 的级联项。

    needs / affect 两表共用；check 对象由调用点传入。
    """

    field_name: str
    operator: Operator
    threshold: int
    thought_tag: str
    description: str
    mood_w: int
    expire_hours: int
    #: 生理念头反向消解阈（A）:check_field 回到健康侧（跨过此值）时，
    #: 自动移除该 thought_tag 的念头。与 ``threshold`` 拉开形成滞回（hysteresis）
    #: 避免在阈值附近拖动。None = 不自动消解（仅靠 expire_at 衰减）。
    #: 方向由 ``operator`` 推导:``>=`` 触发高位→ ``value <= clear_threshold`` 清；
    #: ``<=`` 触发低位→ ``value >= clear_threshold`` 清。
    clear_threshold: int | None = None


class NeedsOrAffectToAffectCascade(NamedTuple):
    """跨层 / 同层 affect 修改级联项。

    fatigue → stress / arousal → focus 等。
    """

    check_field: str
    check_layer: Literal["needs", "affect"]
    operator: Operator
    threshold: int
    target_field: str
    target_layer: Literal["affect"]  # v0.3 仅支持修改 affect
    delta: int


# ─────────────────────────────────────────────────────────────────────
# Layer 2 needs：单向衰减率（02 §5.2 表）
# ─────────────────────────────────────────────────────────────────────

#: 每分钟衰减/累积量（带方向：正=涨，负=降）。
#: 来自 first-party predecessor v1 经验值（v1 是 5min/tick，本表为 v1 ÷ 5）。
NEEDS_DECAY_RATE_PER_MIN: Final[dict[str, float]] = {
    "hunger": +0.3,  # 涨：越来越饿
    "energy": -0.2,  # 降：越来越没劲
    "fatigue": +0.16,  # 涨：越来越累
    "comfort": 0.0,  # 不衰：由 environment / embodiment 派生
    "social": -0.1,  # 降：社交存款消耗
    "stimulation": -0.05,  # 降：新鲜感按数小时尺度消耗
    "aesthetic": -0.06,  # 降：环境美感消磨慢
}

#: comfort 是身体/环境舒适满足；Action 增益后以固定速度回归当拍物理目标。
COMFORT_NEUTRAL_TARGET: Final[int] = 70
COMFORT_DRIFT_RATE_PER_MIN: Final[float] = 0.2
COMFORT_OUTDOOR_LOCATION_TYPES: Final[frozenset[str]] = frozenset(
    {"park", "pedestrian_area", "public_square", "scenic_spot"}
)
COMFORT_COLD_C: Final[float] = 16.0
COMFORT_HOT_C: Final[float] = 28.0
COMFORT_MAX_THERMAL_PENALTY: Final[int] = 18
COMFORT_MAX_PRECIP_PENALTY: Final[int] = 8
COMFORT_MAX_BODY_PENALTY: Final[int] = 12


# ─────────────────────────────────────────────────────────────────────
# Activity 乘数表（02 §5.2 表）
# ─────────────────────────────────────────────────────────────────────

#: **step 名**（原子动作名）→ {needs_field: multiplier}。乘数施加在 NEEDS_DECAY_RATE_PER_MIN 上。
#: 含**反向乘数**：负值 = 反向回升（如 sleep 时 fatigue ×-2.0 = 累快速消解）。
#: 未列入字段走 ×1.0。未列入 step 走全字段 ×1.0。
#:
#: §A【下沉到 step】：原子动作重构后（docs/discussions/2026-06-18 §0.4），被动衰减乘数
#: 本质是「做某个**动作**时身体怎么变」，是 step/动作属性不是 activity 属性。
#: 所以表键从 activity 名下沉到 step 名：sense_derive 查 ``activity.step``。
#: - sleep step：睡觉动作（rest 里躺下睡）——几乎不饿 + 反向回血。
#: - eat step：吃东西动作（dine_out 里坐下吃）——吃饭过程不涨饿（合并了原
#:   explore_food/dine_out 的 activity 级 hunger×0，下沉到真正“在吃”的那一 step）。
#: （rest 是 activity 不是 step，不再入表；它的 walk/sleep 子步各自查表。）
ACTIVITY_NEEDS_MULTIPLIERS: Final[dict[str, dict[str, float]]] = {
    "sleep": {
        "hunger": 0.3,  # 睡觉时几乎不饿
        "energy": -1.0,  # 反向：睡眠回血
        "fatigue": -2.0,  # 反向：累被消解
    },
    "eat": {
        "hunger": 0.0,  # 吃东西过程不涨饿（end 时 LLM 拍 hunger 写回低值）
        # energy / fatigue 走默认 ×1.0
    },
}


# ─────────────────────────────────────────────────────────────────────
# Layer 3 affect：回归基线收敛模型（02 §5.3 表）
# ─────────────────────────────────────────────────────────────────────

#: affect 字段名 → (baseline, rate_per_min)。
#: 收敛公式：``next = prev + (baseline - prev) * rate * elapsed_min``。
#: arousal 的个体 baseline 由运行期 character-card 投影注入；这里的 20 只供
#: 未注入的纯函数/测试路径维持既有行为。
AFFECT_BASELINES: Final[dict[str, tuple[int, float]]] = {
    "stress": (20, 0.03),
    "focus": (50, 0.05),
    "arousal": (20, 0.02),
    "clarity": (60, 0.04),
}


#: 强制收敛 step：该 step 期间所有 affect 直接 → baseline，跳过收敛公式。
#: sleep 是 step 名（rest 里躺下睡）——睡着时情绪回归平静基线。
AFFECT_FORCE_TO_BASELINE_ACTIVITIES: Final[frozenset[str]] = frozenset({"sleep"})


# ─────────────────────────────────────────────────────────────────────
# 多米诺级联阈值表（02 §5.4 ratified）
# ─────────────────────────────────────────────────────────────────────
#
# v0.3 简化决策（与 02 §5.4 文档「连续 3 tick」语义对齐方式）：
# - apply_cascade_thresholds 是 stateless 纯函数，看不到 tick 历史
# - 用 thought.tag 去重 + expire_at 自然衰减替代「连续 3 tick」防重复
# - 单 tick 阈值触发，下游靠 tag 去重（同 tag 已存在则不重复加）

#: transient 生理/情绪念头的 tag 集合。
#: 这些念头描述「此刻身体/情绪状态」，本质短暂——needs/affect 恢复后就不该再留，
#: 走更短的兜底 TTL（DEFAULT_TRANSIENT_THOUGHT_TTL_HOURS）。
TRANSIENT_THOUGHT_TAGS: Final[frozenset[str]] = frozenset(
    {"body", "cascade_hunger", "cascade_energy_low", "cascade_arousal_high"}
)

#: transient 念头兜底 TTL（小时）。取 4h 与 cascade hunger expire 同量级——
#: 一顿饭/一觉的尺度，足够当下被感知又不至于隔夜还在。
DEFAULT_TRANSIENT_THOUGHT_TTL_HOURS: Final[int] = 4

#: 非 transient 念头（情感/关系/记忆…）的兜底 TTL（小时）。**Layer 4 不再有永不过期**：
#: 任何 expire_at=None 的念头都套兜底 TTL（治「线上一堆 null 念头永久点燃 mood + 滚雪球」，
#: 也治 F-3 自我催眠）。真正要永久留存的记忆归 soul/episode 层，不是 Layer 4 事件流。
#: 取 12h——Thought 是当前仍在影响生活的活跃燃料，不应仅因情感/关系 tag 连续占据数天注意力。
#: 明确仍会跨日影响生活的命题可由模型显式给更长 expire_at。
DEFAULT_THOUGHT_TTL_HOURS: Final[int] = 12

#: interior.thoughts 数量硬上限。Layer 4 是「有界的事件流」，超出按列表序淘汰最旧
#: （新 add 在尾、旧在头），防止在 state_latest 里无界滚雪球。
MAX_INTERIOR_THOUGHTS: Final[int] = 6

#: 「needs 阈值 → 加 thought」级联触发表。
# clear_threshold 与 threshold 拉开滞回：hunger ↑90 触发、↓70 消解（吃过东西）；
# energy ↓10 触发、≥30 消解（睡醒/恢复）。避免在阈值附近反复增删拖动。
NEEDS_TO_THOUGHT_CASCADES: Final[list[ThresholdToThoughtCascade]] = [
    ThresholdToThoughtCascade(
        "hunger", ">=", 90, "cascade_hunger", "饿了", -5, 4, clear_threshold=70
    ),
    ThresholdToThoughtCascade(
        "energy", "<=", 10, "cascade_energy_low", "累得不行", -8, 2, clear_threshold=30
    ),
]


#: 「needs 阈值 → affect 修改」级联触发表（同层 / 跨层）。
NEEDS_OR_AFFECT_TO_AFFECT_CASCADES: Final[list[NeedsOrAffectToAffectCascade]] = [
    NeedsOrAffectToAffectCascade("fatigue", "needs", ">=", 90, "stress", "affect", 5),
]

AROUSAL_HIGH_THRESHOLD: Final[int] = 80
AROUSAL_HIGH_FOCUS_PENALTY: Final[int] = 15


#: 「affect 阈值 → 加 thought」级联触发表。
AFFECT_TO_THOUGHT_CASCADES: Final[list[ThresholdToThoughtCascade]] = [
    ThresholdToThoughtCascade(
        "arousal",
        ">=",
        AROUSAL_HIGH_THRESHOLD,
        "cascade_arousal_high",
        "身体起了反应",
        3,
        2,
    ),
]


__all__ = [
    "ACTIVITY_NEEDS_MULTIPLIERS",
    "AROUSAL_HIGH_FOCUS_PENALTY",
    "AROUSAL_HIGH_THRESHOLD",
    "AFFECT_BASELINES",
    "AFFECT_FORCE_TO_BASELINE_ACTIVITIES",
    "AFFECT_TO_THOUGHT_CASCADES",
    "DEFAULT_THOUGHT_TTL_HOURS",
    "DEFAULT_TRANSIENT_THOUGHT_TTL_HOURS",
    "MAX_INTERIOR_THOUGHTS",
    "NEEDS_DECAY_RATE_PER_MIN",
    "NEEDS_OR_AFFECT_TO_AFFECT_CASCADES",
    "NEEDS_TO_THOUGHT_CASCADES",
    "NeedsOrAffectToAffectCascade",
    "Operator",
    "TRANSIENT_THOUGHT_TAGS",
    "ThresholdToThoughtCascade",
]
