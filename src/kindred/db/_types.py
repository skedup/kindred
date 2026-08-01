"""Kindred db 层入参聚合体类型。

把过去 ``insert_tick(...)`` 的 8 个 kw 参数合成一个 pydantic StrictBase 实例。

为什么单独建文件而非内联到 ``ticks.py``：

- ``ticks.py`` 已经较重（实现 + 4 个 helper）；继续堆 model 会让 grep "def "
  时上下文混乱
- ``TickWriteParams`` 被 ``KindredDB`` facade 引用，独立文件减少 import 循环风险
- 与 ``state/_types.py`` 的命名风格保持一致（``_types.py`` = 单文件
  type alias / 入参类型集中地）
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, model_validator

from kindred.state._base import StrictBase
from kindred.state._types import IsoDatetime, NonBlankStr
from kindred.state.state import State
from kindred.state.tick import TriggerSource

# Trigger source 的合法取值位于 ``state.tick.TriggerSource``（14 §2.2.1 TickState 字段）。
# - "heartbeat": daemon 周期性唤醒（最常见）
# - "watcher":   user 消息 / 闹钟等外部事件唤醒
# - "cold_start": bootstrap seed tick（仅首次启动写一次）


class TickWriteParams(StrictBase):
    """``insert_tick`` 的入参聚合体。

    边界由 pydantic 校验（fail-fast in ingress），实现层只负责持久化，不再
    做"trigger_source 字符串合不合法 / triggered_at 是不是 ISO" 这类检查。

    强制约束（model_validator）：
    - ``trigger_source != "cold_start"`` 时 ``act_decision`` 必传
      （bootstrap seed 是唯一允许 None 的场景）
    - ``act_result is None`` 在 ``act_decision.act=False`` 时合法

    与 ``state.time.iso`` 的关系（参见 ``insert_tick`` docstring 已记的细节）：
    - ``triggered_at`` 是 daemon 唤醒时刻
    - ``state.time.iso`` 是 T1.sense 完成时刻
    - 两者近似相同，但保留独立字段是为诊断 daemon 调度延迟

    significance 含义：
    - None / <=6: 普通 tick，仅写主表
    - 7~10:       高重要性，同事务写入 ``episode_recall``（视图阈值）
    """

    state: State
    trigger_source: TriggerSource
    triggered_at: IsoDatetime
    note: NonBlankStr | None = None
    significance: Annotated[int, Field(ge=1, le=10)] | None = None
    act_decision: dict[str, Any] | None = None
    act_result: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_act_decision_required(self) -> TickWriteParams:
        """E-7：non-cold_start tick 必须传 act_decision，禁止静默 NULL。"""
        if self.trigger_source != "cold_start" and self.act_decision is None:
            raise ValueError(
                "act_decision is required for non-bootstrap ticks "
                f"(trigger_source={self.trigger_source!r}). "
                'Only trigger_source="cold_start" allows act_decision=None.'
            )
        return self


__all__ = ["TickWriteParams"]
