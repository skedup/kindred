"""开发期 seed state helper（供 cli + 测试共享）。

用途
====

- ``cli`` ``kindred db bootstrap`` 子命令：往空库种 1 行 cold_start tick
- 单元测试 / 集成测试：``DOC_EXAMPLE_DICT`` / ``make_doc_example_state``
  共享同一份 02 §3.1 字面量

为什么前缀 ``_`` + ``_seed`` 而非 ``fixtures``
=============================================

- ``_`` 前缀强调"内部 helper"——产品发布后真 daemon 不应依赖此模块
  （character card 加载 + LLM 生 first state 才是真 bootstrap）
- ``_seed`` 比 ``fixtures`` 语义更准——前者是"开发期种子数据"，后者
  容易误读为"测试 fixture"（pytest 概念）

未来
====

接通真 daemon 后：

- ``cli db bootstrap`` 应改为读 ``life/data/character/<name>/SKILL.md`` +
  调真 LLM 生 state → ``insert_tick``
- 本模块可以保留作为开发期 helper，但 daemon 路径不再 import 它
"""

from __future__ import annotations

import copy
from typing import Any

from kindred.state.state import State

DOC_EXAMPLE_DICT: dict[str, Any] = {
    "interior": {
        "body": {"value": 65, "description": "有点累，肩膀酸"},
        "mood": {"value": 72, "description": "心情不错"},
        "inner_pulse": {
            "value": 55,
            "description": "想做点事，但没那么强烈",
        },
        "needs": {
            "energy": 60,
            "fatigue": 40,
            "hunger": 25,
            "comfort": 70,
            "social": 45,
            "stimulation": 50,
            "aesthetic": 60,
        },
        "affect": {
            "stress": 20,
            "focus": 65,
            "arousal": 30,
            "clarity": 70,
        },
        "thoughts": [
            {
                "description": "刚和闺蜜聊得久",
                "mood_w": 12,
                "expire_at": "2026-05-27T22:00",
                "tag": "chat",
            },
            {
                "description": "下午有点闷",
                "mood_w": -5,
                "expire_at": "2026-05-27T20:00",
                "tag": "weather",
            },
        ],
    },
    "embodiment": {
        "top": "深酒红色吊带",
        "bottom": "黑色短裙",
        "outer": "米白针织开衫",
        "bra": "深酒红蕾丝文胸",
        "panties": "深酒红蕾丝内裤",
        "socks": "透明丝袜",
        "shoes": "高跟鞋",
        "accessory": ["珍珠手链", "酒杯吊坠银链", "水滴银耳钉"],
        "makeup": "淡妆",
    },
    "bag": {
        "type": "棕色小皮包",
        "items": ["钱包", "手机", "钥匙", "口红", "纸巾"],
    },
    "activity": {
        "name": "explore_food",
        "desc": "去海岸城逛街吃饭",
        "started_at": "2026-05-27T19:30:00+08:00",
        "engagement": 0.85,
        "with_whom": [],
        "for_what": "饿了想吃饭",
    },
    "location": {
        "name": "海岸城",
        "address": "深圳市南山区海岸城",
        "city": "深圳",
        "type": "shopping_mall",
        "arrived_at": "2026-05-27T19:30:00+08:00",
    },
    "time": {
        "iso": "2026-05-27T19:35+08:00",
        "phase": "evening",
        "weekday": "Wednesday",
    },
    "environment": {
        "city": "深圳",
        "weather": "晴",
        "temperature": 22,
        "ambience": "傍晚、人渐多",
        "weather_cached_at": "2026-05-27T18:00:00+08:00",
        "weather_cached_for": "深圳",
    },
    "presence": {
        "user_present": False,
        "others": [],
    },
}
"""02-state-system.md §3.1 完整 8 层 State 实例的字典形态。

修改本常量 = 修改 02 §3.1（反之亦然）。两边不同步，
``test_state.py::test_doc_example_round_trip`` 会爆。
"""


def make_doc_example_state(
    *,
    mood_value: int = 60,
    ts: str = "2026-06-02T19:30:00+08:00",
) -> State:
    """返回以 02 §3.1 例为底、可调整 mood 与时间戳的 ``State``。

    工厂仅改 mood / time / activity.started_at / location.arrived_at（都与
    ts 联动）——保证不同场景常需变动的"表面字段"可被参数化，其余字段
    （服装 / Bag / Environment...）跟 02 §3.1 合一。
    """
    d = copy.deepcopy(DOC_EXAMPLE_DICT)
    d["interior"]["mood"]["value"] = mood_value
    d["time"]["iso"] = ts
    d["activity"]["started_at"] = ts
    d["location"]["arrived_at"] = ts
    return State.model_validate(d)


__all__ = ["DOC_EXAMPLE_DICT", "make_doc_example_state"]
