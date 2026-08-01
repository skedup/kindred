"""心跳节奏控制 + 按 source 去抖（earlier milestone）。

本模块是 daemon 主循环的纯逻辑底座，**零外部依赖**（不碰 db / ws / LLM），
便于单测覆盖。daemon.py 负责把这些纯逻辑串进真实循环。

两个组件：

- ``HeartbeatScheduler``：根据当前 state 决定下一次心跳间隔
  （醒着 5min / 睡着 1h）。对应 docs/13 §3.1 「heartbeat 节奏由 state 决定」。
- ``Debouncer``：按 trigger source 各自独立冷却，互不干扰
  （docs/13 §3.1）。冷启动 / 长闲置时 ``last_at=0``，第一次任何 trigger
  立刻放行（「手机响了第一时间看一下是谁」）。

earlier milestone 边界：只实现 heartbeat + cold_start 两个 source 的纯节奏 / 去抖。
watcher 的 30min 冷却由 earlier milestone 接入；路 X 后消息留在表里，由 sense_io 按 cursor 消费。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 心跳间隔（秒）。docs/13 §3.1：醒着 5min / 睡着 1h。
AWAKE_INTERVAL_SECONDS = 300
SLEEP_INTERVAL_SECONDS = 3600

# watcher（chat tick）冷却（秒）。docs/13 §3.1/§162 硬定 30min：partner 刚说完话
# 30min 内不再触发新 watcher tick；消息留在 main_session_messages，等 sense_io 消费。
WATCHER_COOLDOWN_SECONDS = 1800

# 睡眠态的 activity.step 标记。原子动作重构后（docs/discussions/2026-06-18 §0.4），
# sleep 不再是 activity.name，而是 rest activity 里的一个 step（原子动作）。
SLEEP_STEP = "sleep"


def is_sleeping(state: Mapping[str, Any] | None) -> bool:
    """判断当前 state 是否处于睡眠态。

    state 为 ``get_state_latest()`` 返回的 dict（``activity`` 已反序列化为
    JSON dict，形如 ``{"name": "rest", "step": "sleep", ...}``）。冷启动
    （state=None）或 activity 缺失时按「醒着」处理——首 tick 应以正常节奏对齐。

    原子动作重构（§0.4）：sleep 现为 rest activity 的 step（躺下恢复），不再
    是 activity.name。判断下沉到 step：``activity.step == 'sleep'`` 才是真睡
    （settle.step=settle≠sleep → 醒着 5min；rest 走到 step=sleep → 睡着 1h）。
    """
    if not state:
        return False
    activity = state.get("activity")
    if not isinstance(activity, Mapping):
        return False
    return activity.get("step") == SLEEP_STEP


class HeartbeatScheduler:
    """心跳间隔由当前 state 决定（docs/13 §3.1）。

    - 醒着（activity.step != 'sleep'）→ 5min 一次
    - 睡着（activity.step == 'sleep'）→ 1h 一次

    心在 tick 末尾自决是否切到 sleep（D-009 心有自决权）。daemon 在下一次
    schedule 时 read 当前 state 决定下一次心跳什么时候——本类只做这个换算，
    不持有状态、不读 db（db 读取由 daemon 主循环负责注入 state）。
    """

    def __init__(
        self,
        *,
        awake_interval: int = AWAKE_INTERVAL_SECONDS,
        sleep_interval: int = SLEEP_INTERVAL_SECONDS,
    ) -> None:
        if awake_interval <= 0 or sleep_interval <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._awake_interval = awake_interval
        self._sleep_interval = sleep_interval

    def next_interval(self, state: Mapping[str, Any] | None) -> int:
        """返回下一次心跳间隔（秒）。"""
        return self._sleep_interval if is_sleeping(state) else self._awake_interval


class Debouncer:
    """按 trigger source 分别去抖，互不干扰（docs/13 §3.1）。

    - heartbeat: 5min 兜底（实际间隔由 HeartbeatScheduler 控制，此处防重复触发）
    - cold_start: 一次性，不参与冷却（COOLDOWNS 无此 key → should_skip 返 False）

    冷启动 / 长闲置：``last_at`` 初始缺省 = 0.0，第一次任何 trigger
    都立刻通过（now - 0 ≫ cooldown）。

    earlier milestone 起接入 watcher(1800s=30min)——MessageWatcher 用此 source 冷却，与
    heartbeat 独立（docs/13 §3.1 按 source 各自冷却，心跳节奏不被对话打断）。
    """

    COOLDOWNS: dict[str, int] = {
        "heartbeat": AWAKE_INTERVAL_SECONDS,  # 5min 兜底
        "watcher": WATCHER_COOLDOWN_SECONDS,  # 30min chat tick 冷却（earlier milestone）
        # "cold_start": 一次性，不配 → 不去抖
    }

    def __init__(self) -> None:
        # source → 上次触发时刻；**缺键 = 从未触发**（冷启动 / 长闲置）。
        # 用 “键不存在” 而非哨兵 0.0 区分“从未”——避免 monotonic now（可能
        # 小于 cooldown，如 1800s watcher）时 ``now - 0 < cooldown`` 误判为“冷却内”。
        self._last_at_by_source: dict[str, float] = {}

    def should_skip(self, source: str, now: float) -> bool:
        """now 距上次同 source 触发不足冷却 → True（跳过本次）。

        - 未配置冷却的 source（如 cold_start）永远不跳过。
        - **从未触发过**（source 不在 ``_last_at_by_source``）→ 立即放行（docs/13
          §3.1 “手机响了第一时间看一下是谁”）。不依赖 ``now`` 是否 ≫ cooldown——
          monotonic 时钟下 ``now`` 可能小于 cooldown，靠“键不存在”才能正确放行。
        """
        cooldown = self.COOLDOWNS.get(source)
        if cooldown is None:
            return False
        last = self._last_at_by_source.get(source)
        if last is None:
            return False  # 从未触发 → 首次立即放行
        return (now - last) < cooldown

    def record(self, source: str, now: float) -> None:
        """记录某 source 本次触发时刻，用于下次去抖判断。"""
        self._last_at_by_source[source] = now
