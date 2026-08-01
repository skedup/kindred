"""``T1.sense.derive`` —— 纯函数派生节点。

参考文档：14-heart-graph.md §3.2

职责：

- 调 `state/_derive.py` 真公式——衰减率 / activity 乘数 / affect 收敛 /
  多米诺级联全实现
- 本节点读取 ``next_state.activity.step`` 汇入 ``decay_interior``（§A 下沉到 step），
  传入 ``triggered_at`` 作 ``curr_iso`` 让衰减 / 级联能过滤过期 thought + 加新 thought
- 输入：``state.prev_state.interior`` + ``state.prev_state.time.iso`` +
  ``state.triggered_at`` + ``state.next_state``（T1.sense.io 已填）
- 输出 patch：``{"next_state": next_state}``——写 ``next_state.time`` +
  ``next_state.interior``

**纯函数 + 小型 baseline 依赖**：本节点不调 LLM、不读 db、不写文件。生产装配
通过 factory 注入 character-card 派生的个体 arousal baseline；顶层函数保留给
mock/拓扑测试，并使用既有默认 baseline。

哲学（与 14 §3.2 一致）：

- 衰减 / 级联 / Layer 1 重派 = 必然发生的被动变化
- next_state 其它子层（activity / embodiment / bag / location）属 T2.act 责任
- environment 已由 T1.sense.io 刷完，本节点不动；presence 原样沿用 working state，
  后续由 T1.sense.llm 的外部观察 projector 按实际变化更新

详 ``state/_derive.py`` docstring 与
``docs/discussions/2026-06-03-02-v03-derive-spec.md``。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from math import isfinite
from typing import Any, cast

from kindred.graph._shared._common import NodeReturn
from kindred.graph.tick.sense_io import weather_cache_is_current
from kindred.state._derive import (
    ComfortInputs,
    apply_cascade_thresholds,
    decay_interior,
    derive_time_layer,
    elapsed_minutes,
    rederive_layer1,
)
from kindred.state._derive_constants import (
    AFFECT_BASELINES,
    AFFECT_FORCE_TO_BASELINE_ACTIVITIES,
)
from kindred.state.interior import Interior
from kindred.state.tick import TickState

_LOG = logging.getLogger(__name__)


def make_sense_derive_node(
    *,
    arousal_baseline: int,
    focus_baseline: int = AFFECT_BASELINES["focus"][0],
    weather_ttl_minutes: int = 60,
    weather_location: str = "",
) -> Callable[[TickState], NodeReturn]:
    """注入运行期 affect baseline，返回纯函数 derive 节点。"""

    def _node(state: TickState) -> NodeReturn:
        return _t1_sense_derive(
            state,
            arousal_baseline=arousal_baseline,
            focus_baseline=focus_baseline,
            weather_ttl_minutes=weather_ttl_minutes,
            weather_location=weather_location,
        )

    return _node


def t1_sense_derive(state: TickState) -> NodeReturn:
    """使用既有默认 baseline 的 mock/拓扑测试入口。"""
    return _t1_sense_derive(
        state,
        arousal_baseline=AFFECT_BASELINES["arousal"][0],
        focus_baseline=AFFECT_BASELINES["focus"][0],
        weather_ttl_minutes=60,
        weather_location="",
    )


def _t1_sense_derive(
    state: TickState,
    *,
    arousal_baseline: int,
    focus_baseline: int,
    weather_ttl_minutes: int,
    weather_location: str,
) -> NodeReturn:
    """T1 第 2 节点：纯函数推导 next_state 的被动变化。

    抽 02 §5 的步 1 + 4 + 5，不动 §5 的步 2 / 3 / 6。

    步骤：

    1. ``next_state.time = derive_time_layer(triggered_at)`` — 推进时钟
    2. ``elapsed_min = elapsed_minutes(prev_time.iso, triggered_at)``
    3. 查 ``next_state.activity.step`` 作为 step_name（未起 activity / 未落 step 为 None）
    4. ``next_interior = decay_interior(prev_interior, prev_iso, curr_iso, step_name, ...)``
       — §5 步 1
    5. ``next_interior = apply_cascade_thresholds(next_interior, curr_iso)`` — §5 步 4；
       普通 step 的 arousal 高位判定读取 T1 入口事实，sleep 使用回归后的 baseline
    6. ``next_interior = rederive_layer1(next_interior)`` — §5 步 5
    7. ``next_state.interior = next_interior``

    入参契约（上游已落定）：

    - ``state["triggered_at"]``：daemon invoke 注入的 ISO 时间戳
    - ``state["prev_state"]``：T1.sense.io read state_latest 派生
    - ``state["next_state"]``：T1.sense.io ``dict(prev_state)``（浅拷）后的 base

    **mock 路径兼容**（拓扑测试 + cli --mock）：如果上游 sense_io 是 mock
    （未填 prev_state / next_state 或未填 triggered_at），本节点 no-op 返空 patch。
    cli 接真 db 后这条路径会被 cli 侧 cold-start 处理接管。

    返 patch：

    .. code-block:: python

        {"next_state": next_state}
    """
    triggered_at = state.get("triggered_at")
    prev_state_raw = state.get("prev_state")
    next_state_raw = state.get("next_state")
    if triggered_at is None or prev_state_raw is None or next_state_raw is None:
        # mock 上游（sense_io 未注 deps）——no-op 保拓扑测试运行
        return {}
    # narrow None 后 mypy 已认 triggered_at 为 str；prev/next_state 是 object需 cast。
    prev_state = cast("dict[str, Any]", prev_state_raw)
    next_state = cast("dict[str, Any]", next_state_raw)

    # deepcopy→patch：不再 deepcopy 整个 next_state 原地 mutate。只读需要的层，
    # 构造本节点改的 time / interior 两层，末尾作 patch return（reducer 浅 merge）。

    # ——— 推进 time ———
    time_layer = derive_time_layer(triggered_at).model_dump()

    # ——— elapsed_min（用 prev.time.iso 作上 tick 时刻）———
    prev_time_iso = prev_state["time"]["iso"]
    elapsed_min = elapsed_minutes(prev_time_iso, triggered_at)

    # ——— 查当前原子动作 step（乘数表查表 key，§A 下沉到 step；读 next_state 未被本节点改的层）———
    # 乘数是「做某动作时身体怎么变」，属 step/动作不属 activity，所以查 activity.step。
    activity_obj = next_state.get("activity")
    step_name: str | None = activity_obj.get("step") if isinstance(activity_obj, dict) else None

    # ——— Interior：衰减 + 级联 + Layer 1 重派 ———
    prev_interior = Interior.model_validate(prev_state["interior"])
    environment = next_state.get("environment")
    weather_fresh = weather_cache_is_current(
        environment,
        triggered_at,
        weather_location=weather_location,
        ttl_minutes=weather_ttl_minutes,
    )
    next_interior = decay_interior(
        prev_interior,
        prev_iso=prev_time_iso,
        curr_iso=triggered_at,
        step_name=step_name,
        comfort_inputs=_comfort_inputs(next_state, weather_fresh=weather_fresh),
        arousal_baseline=arousal_baseline,
    )
    arousal_cascade_value = (
        next_interior.affect.arousal
        if step_name in AFFECT_FORCE_TO_BASELINE_ACTIVITIES
        else prev_interior.affect.arousal
    )
    next_interior = apply_cascade_thresholds(
        next_interior,
        curr_iso=triggered_at,
        focus_baseline=focus_baseline,
        arousal_cascade_value=arousal_cascade_value,
    )
    next_interior = rederive_layer1(next_interior)
    interior_layer = next_interior.model_dump()
    _LOG.debug(
        "T1.sense.derive elapsed_min=%.2f step_name=%s body=%d mood=%d inner_pulse=%d",
        elapsed_min,
        step_name,
        next_interior.body.value,
        next_interior.mood.value,
        next_interior.inner_pulse.value,
    )

    # patch：只交出本节点改的 time + interior 两层（reducer merge_next_state 浅覆盖）。
    return {"next_state": {"time": time_layer, "interior": interior_layer}}


def _comfort_inputs(next_state: dict[str, Any], *, weather_fresh: bool) -> ComfortInputs:
    environment = next_state.get("environment")
    location = next_state.get("location")
    embodiment = next_state.get("embodiment")

    apparent_temperature: float | None = None
    precip_mm: float | None = None
    if weather_fresh and isinstance(environment, dict):
        apparent_temperature = _finite_number(environment.get("feels_like"))
        if apparent_temperature is None:
            apparent_temperature = _finite_number(environment.get("temperature"))
        precip_mm = _finite_number(environment.get("precip_mm"))

    location_type = location.get("type") if isinstance(location, dict) else None
    if not isinstance(location_type, str):
        location_type = None
    coverage = (
        sum(embodiment.get(slot) is not None for slot in ("top", "bottom", "outer", "shoes"))
        if isinstance(embodiment, dict)
        else 0
    )
    return ComfortInputs(
        apparent_temperature=apparent_temperature,
        precip_mm=precip_mm,
        location_type=location_type,
        clothing_coverage=coverage,
    )


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if isfinite(number) else None
