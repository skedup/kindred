"""把 canonical 穿着与随身物品投影成 tick graph 共用的叙事事实。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

_OUTFIT_SLOTS = (
    ("top", "上衣"),
    ("bottom", "下装"),
    ("outer", "外套"),
    ("bra", "文胸"),
    ("panties", "内裤"),
    ("socks", "袜子"),
    ("shoes", "鞋"),
)
_MAX_BAG_ITEMS = 6
_MAX_ITEM_NAME_CHARS = 80
_MAX_MAKEUP_CHARS = 120


def _display_text(value: object, *, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > max_chars:
        return f"{text[: max_chars - 1]}…"
    return text


def _item_name(value: object, *, barefoot_is_none: bool = False) -> str | None:
    if isinstance(value, str):
        if barefoot_is_none and value.strip() == "裸足":
            return None
        return _display_text(value, max_chars=_MAX_ITEM_NAME_CHARS)
    if not isinstance(value, Mapping):
        return None
    return _display_text(value.get("name"), max_chars=_MAX_ITEM_NAME_CHARS)


def _item_names(value: object, *, limit: int | None = None) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names = tuple(name for item in value if (name := _item_name(item)) is not None)
    if limit is None or len(names) <= limit:
        return names
    return (*names[:limit], f"另有{len(names) - limit}件")


def current_possession_fact_lines(state: Mapping[str, Any]) -> tuple[str, ...]:
    """展示全部当前穿搭、妆容与随身事实，不披露 key 或整份 Catalog。"""

    embodiment_raw = state.get("embodiment")
    if isinstance(embodiment_raw, Mapping):
        has_embodiment = True
        embodiment = cast("Mapping[str, Any]", embodiment_raw)
    else:
        has_embodiment = False
        embodiment = {}
    outfit_parts = []
    if has_embodiment:
        for slot, label in _OUTFIT_SLOTS:
            name = _item_name(
                embodiment.get(slot),
                barefoot_is_none=slot == "shoes",
            )
            outfit_parts.append(f"{label}={name or '未穿'}")
    accessories = _item_names(embodiment.get("accessory"))
    if accessories:
        outfit_parts.append(f"配饰={'、'.join(accessories)}")

    makeup = _display_text(embodiment.get("makeup"), max_chars=_MAX_MAKEUP_CHARS)

    bag_raw = state.get("bag")
    if isinstance(bag_raw, Mapping):
        has_bag = True
        bag = cast("Mapping[str, Any]", bag_raw)
    else:
        has_bag = False
        bag = {}
    bag_name = _item_name(bag.get("item"))
    if bag_name is None:
        bag_name = _item_name(bag.get("type"))
    bag_items = _item_names(bag.get("items"), limit=_MAX_BAG_ITEMS)

    lines = [
        f"- 穿着：{'；'.join(outfit_parts) if outfit_parts else '暂无记录'}",
        f"- 妆容：{makeup or ('无' if has_embodiment else '暂无记录')}",
    ]
    if not has_bag:
        lines.append("- 随身：暂无记录")
    elif bag_name is None:
        lines.append("- 随身：未带包")
    else:
        suffix = f"；包内={'、'.join(bag_items)}" if bag_items else "；包内=空"
        lines.append(f"- 随身：包={bag_name}{suffix}")
    return tuple(lines)


def render_current_possession_facts(state: Mapping[str, Any]) -> str:
    """为 LLM prompt 渲染带标题的当前 possession 事实块。"""

    return "\n".join(
        (
            "## 当前穿着与随身事实（以此刻状态为准；叙事不得与其冲突）",
            *current_possession_fact_lines(state),
        )
    )


__all__ = ["current_possession_fact_lines", "render_current_possession_facts"]
