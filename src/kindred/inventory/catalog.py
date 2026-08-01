from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from kindred.state._base import StrictBase
from kindred.state._types import SAFE_NAME_PATTERN

HomePool = Literal["wardrobe", "bedside", "storage"]
EquipSlot = Literal[
    "top", "bottom", "outer", "bra", "panties", "socks", "shoes", "accessory", "bag"
]
_BODY_SLOTS = ("top", "bottom", "outer", "bra", "panties", "socks", "shoes")


class InventoryCatalogError(RuntimeError):
    """私有导入文件违反 Catalog 契约。"""


class InventoryPreflightError(RuntimeError):
    """当前 State 引用了 Catalog 无法解释的身份。"""


class InventoryItem(StrictBase):
    """一件长期拥有物品的稳定身份与归位信息。"""

    item_key: str = Field(
        strict=True, min_length=1, max_length=64, pattern=SAFE_NAME_PATTERN.pattern
    )
    name: str = Field(strict=True, min_length=1, max_length=80)
    home_pool: HomePool
    kind: str = Field(strict=True, min_length=1, max_length=64, pattern=SAFE_NAME_PATTERN.pattern)
    equip_to: list[EquipSlot] = Field(default_factory=list, strict=True)
    description: str | None = Field(default=None, strict=True, max_length=240)

    @field_validator("name", "description", mode="before")
    @classmethod
    def _strip_display_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("equip_to")
    @classmethod
    def _normalize_footprint(cls, value: list[EquipSlot]) -> list[EquipSlot]:
        if len(value) <= 1 and len(value) == len(set(value)):
            return value
        if len(value) == 2 and set(value) == {"top", "bottom"}:
            return ["top", "bottom"]
        raise ValueError("invalid equip_to footprint")


class InventoryCatalogImport(StrictBase):
    schema_version: Literal["kindred.inventory.v1"] = Field(alias="schema")
    items: list[InventoryItem]

    @model_validator(mode="after")
    def _unique_keys(self) -> InventoryCatalogImport:
        if len({item.item_key for item in self.items}) != len(self.items):
            raise ValueError("duplicate item_key")
        return self


class _UniqueKeySafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        pairs = self.construct_pairs(node, deep=deep)
        try:
            if len({key for key, _ in pairs}) != len(pairs):
                raise yaml.YAMLError("duplicate mapping key")
            return dict(pairs)
        except TypeError:
            raise yaml.YAMLError("invalid mapping key") from None


def load_inventory_import(path: Path) -> InventoryCatalogImport:
    try:
        loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise InventoryCatalogError("inventory import parse_failed") from None
    try:
        return InventoryCatalogImport.model_validate(loaded)
    except ValidationError as exc:
        first = exc.errors(include_input=False)[0]
        error_path = first["loc"]
        if first["type"] == "extra_forbidden" and error_path:
            error_path = (*error_path[:-1], "unknown_field")
        path_text = ".".join(str(part) for part in error_path) or "root"
        raise InventoryCatalogError(
            f"inventory import validation_failed path={path_text} type={first['type']}"
        ) from None


class InventoryReader(Protocol):
    def get_inventory_item(self, item_key: str) -> InventoryItem | None: ...


def _inventory_values(state: Mapping[str, Any]) -> Iterator[tuple[str, object]]:
    embodiment = state.get("embodiment")
    if isinstance(embodiment, Mapping):
        for slot in _BODY_SLOTS:
            yield f"embodiment.{slot}", embodiment.get(slot)
        accessories = embodiment.get("accessory")
        if isinstance(accessories, list):
            for index, item in enumerate(accessories):
                yield f"embodiment.accessory.{index}", item
    bag = state.get("bag")
    if isinstance(bag, Mapping):
        if "item" in bag:
            yield "bag.item", bag["item"]
        if "type" in bag:
            yield "bag.type", bag["type"]
        items = bag.get("items")
        if isinstance(items, list):
            for index, item in enumerate(items):
                yield f"bag.items.{index}", item


def validate_inventory_state_keys(state: Mapping[str, Any], reader: InventoryReader) -> None:
    for path, value in _inventory_values(state):
        if not isinstance(value, Mapping) or "item_key" not in value:
            continue
        item_key = value["item_key"]
        if item_key is None:
            continue
        if not isinstance(item_key, str) or not item_key.strip():
            shape = "non_string" if not isinstance(item_key, str) else "blank_string"
            raise InventoryPreflightError(
                f"inventory preflight path={path} reason=invalid_item_key key_shape={shape}"
            )
        try:
            known = reader.get_inventory_item(item_key)
        except Exception:
            raise InventoryPreflightError(
                f"inventory preflight path={path} reason=catalog_lookup_failed "
                "key_shape=nonempty_string"
            ) from None
        if known is None:
            raise InventoryPreflightError(
                f"inventory preflight path={path} reason=unknown_item_key key_shape=nonempty_string"
            )
