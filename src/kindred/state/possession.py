"""穿戴与随身物品的 canonical snapshot，以及旧 State 读取兼容。"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, StrictStr, StringConstraints, field_validator, model_validator

from kindred.state._base import StrictBase
from kindred.state._types import is_safe_name

StrictItemName = Annotated[StrictStr, StringConstraints(strip_whitespace=True, min_length=1)]


class ItemSnapshot(StrictBase):
    """某一拍里物品的稳定身份与当时展示名称。"""

    item_key: StrictStr | None = None
    name: StrictItemName

    @field_validator("item_key")
    @classmethod
    def _validate_item_key(cls, value: str | None) -> str | None:
        if value is not None and (len(value) > 64 or not is_safe_name(value)):
            raise ValueError("item_key must be a safe name with at most 64 characters")
        return value


def _legacy_snapshot(value: Any, *, barefoot_is_none: bool = False) -> Any:
    if isinstance(value, str):
        if barefoot_is_none and value == "裸足":
            return None
        return {"item_key": None, "name": value}
    return value


class Embodiment(StrictBase):
    """穿在身上的物品 snapshot 与妆容。"""

    top: ItemSnapshot | None
    bottom: ItemSnapshot | None
    outer: ItemSnapshot | None = None
    bra: ItemSnapshot | None = None
    panties: ItemSnapshot | None = None
    socks: ItemSnapshot | None = None
    shoes: ItemSnapshot | None = None
    accessory: list[ItemSnapshot] = Field(default_factory=list, strict=True)
    makeup: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_strings(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for slot in ("top", "bottom", "outer", "bra", "panties", "socks"):
            if slot in normalized:
                normalized[slot] = _legacy_snapshot(normalized[slot])
        if "shoes" in normalized:
            normalized["shoes"] = _legacy_snapshot(normalized["shoes"], barefoot_is_none=True)
        if isinstance(normalized.get("accessory"), list):
            normalized["accessory"] = [_legacy_snapshot(item) for item in normalized["accessory"]]
        return normalized


class Bag(StrictBase):
    """当前携带的包与其中物品；无包时两字段均为空。"""

    item: ItemSnapshot | None = None
    items: list[ItemSnapshot] = Field(default_factory=list, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_shape(cls, value: Any) -> Any:
        if value is None:
            return {"item": None, "items": []}
        if not isinstance(value, dict):
            return value
        if "type" in value and "item" in value:
            raise ValueError("bag.type and bag.item cannot coexist")
        normalized = dict(value)
        if "type" in normalized:
            normalized["item"] = normalized.pop("type")
        if "item" in normalized:
            normalized["item"] = _legacy_snapshot(normalized["item"])
        if isinstance(normalized.get("items"), list):
            normalized["items"] = [_legacy_snapshot(item) for item in normalized["items"]]
        return normalized

    @model_validator(mode="after")
    def _check_bag_consistency(self) -> Bag:
        if self.item is None and self.items:
            raise ValueError("Bag.item=None implies Bag.items=[]")
        keys = [item.item_key for item in self.items if item.item_key is not None]
        if len(keys) != len(set(keys)):
            raise ValueError("Bag.items contains duplicate item_key")
        if self.item is not None and self.item.item_key in set(keys):
            raise ValueError("Bag.item cannot also appear in Bag.items")
        return self


__all__ = ["Bag", "Embodiment", "ItemSnapshot"]
