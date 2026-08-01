"""State 顶层组合 —— 02 §3.1 完整 8 层 = interior + 7 outward layers。

参考文档：docs/02-state-system.md §3 / 附录 A

State 是 ta 此刻的"完整存在切片"。
组合 interior（4 子层）+ 7 个 outward 层，**没有 scene 包装层**（02 v0.2 已删）。

用途：
- 14 §1.2 TickState.prev_state / next_state 字段类型（虽 TickState 用 dict，但 State 提供校验）
- T3.persist.write_state 写 SQLite tick.state JSON 列前先用 State 校验
- bundle 渲染前用 State 解析 SQLite 读出的 dict
"""

from __future__ import annotations

from pydantic import model_validator

from kindred.state._base import StrictBase
from kindred.state.interior import Interior
from kindred.state.outward import (
    Activity,
    Bag,
    Embodiment,
    Environment,
    Location,
    Presence,
    Time,
)


class State(StrictBase):
    """02 §3.1 完整 8 层 state。

    层顺序对齐 02 §3.1 实例（interior 在前，外在 7 层依 §3.2 表格次序）。
    """

    interior: Interior
    embodiment: Embodiment
    bag: Bag
    activity: Activity
    location: Location
    time: Time
    environment: Environment
    presence: Presence

    @model_validator(mode="after")
    def _check_with_whom_consistency(self) -> State:
        """跨 model invariant（来源 02 §3.8 行 465）。

        activity.with_whom 子集于 presence.others ∩ 原始身份 ∪ {"user"}。
        防止「独自一人但 with_whom=['friend']」这种意图与现实冲突状态。

        可接受集：
        - "user"（与 user 在一起）
        - presence.others 里出现过的名字
        """
        allowed = set(self.presence.others) | {"user"}
        unknown = [w for w in self.activity.with_whom if w not in allowed]
        if unknown:
            raise ValueError(
                f"activity.with_whom contains names not in presence.others ∪ {{user}}: {unknown}"
            )
        return self

    @model_validator(mode="after")
    def _check_possession_identity(self) -> State:
        """同一稳定物品不能同时占据互斥身体槽或包内位置。"""
        body: list[tuple[str, str]] = []
        for slot in ("top", "bottom", "outer", "bra", "panties", "socks", "shoes"):
            item = getattr(self.embodiment, slot)
            if item is not None and item.item_key is not None:
                body.append((slot, item.item_key))
        body.extend(
            (f"accessory.{index}", item.item_key)
            for index, item in enumerate(self.embodiment.accessory)
            if item.item_key is not None
        )
        by_key: dict[str, list[str]] = {}
        for slot, item_key in body:
            by_key.setdefault(item_key, []).append(slot)
        for slots in by_key.values():
            if set(slots) != {"top", "bottom"} and len(slots) > 1:
                raise ValueError("body item_key reused across incompatible slots")
        bag_keys = {item.item_key for item in self.bag.items if item.item_key is not None}
        if bag_keys.intersection(by_key):
            raise ValueError("body item_key cannot also appear in bag.items")
        return self
