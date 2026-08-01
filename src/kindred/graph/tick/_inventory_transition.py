"""Action-scoped Inventory selector 到 canonical snapshot 的提交接缝。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn

from kindred.activity import ActivitySkillError, load_activity_skill
from kindred.activity.action import load_atomic_action
from kindred.graph.tick._act_contract import ActLlmContractError
from kindred.inventory.catalog import InventoryItem, InventoryReader
from kindred.state.possession import Bag, Embodiment

_OUTFIT_ACTION = "change_outfit"
_BAG_ACTION = "pack_bag"
_MAKEUP_ACTION = "makeup"
_REMOVE_MAKEUP_ACTION = "remove_makeup"
_ITEM_SLOTS = ("top", "bottom", "outer", "bra", "panties", "socks", "shoes")


def resolve_inventory_diff(
    next_state: dict[str, Any],
    final_state_diff: dict[str, Any],
    *,
    effective_action: str | None,
    target: Any,
    activities_dir: Path,
    actions_dir: Path,
    reader: InventoryReader | None,
) -> dict[str, Any]:
    """在私有副本解析 Inventory diff；无实际变化时移除对应字段。"""
    resolved = deepcopy(final_state_diff)
    embodiment_raw = resolved.get("embodiment")
    bag_raw = resolved.get("bag")
    if embodiment_raw is None and bag_raw is None:
        return resolved
    try:
        before_embodiment = Embodiment.model_validate(next_state.get("embodiment")).model_dump()
        before_bag = Bag.model_validate(next_state.get("bag")).model_dump()
    except Exception:
        _reject("inventory", "invalid_current_state")

    item_changes = makeup_change = bag_change = False
    if embodiment_raw is not None:
        if not isinstance(embodiment_raw, dict):
            _reject("embodiment", "invalid_type")
        embodiment_patch = _resolve_embodiment_patch(
            embodiment_raw,
            before_embodiment,
            reader,
        )
        makeup_change = "makeup" in embodiment_patch
        item_changes = bool(set(embodiment_patch) - {"makeup"})
        if embodiment_patch:
            resolved["embodiment"] = embodiment_patch
        else:
            resolved.pop("embodiment", None)
    if bag_raw is not None:
        if not isinstance(bag_raw, dict):
            _reject("bag", "invalid_type")
        bag_patch = _resolve_bag_patch(bag_raw, before_bag, reader)
        bag_change = bool(bag_patch)
        if bag_patch:
            resolved["bag"] = bag_patch
        else:
            resolved.pop("bag", None)

    required_actions: set[str] = set()
    if item_changes:
        required_actions.add(_OUTFIT_ACTION)
    if bag_change:
        required_actions.add(_BAG_ACTION)
    if makeup_change:
        value = resolved["embodiment"]["makeup"]
        makeup_action = _MAKEUP_ACTION if isinstance(value, str) and value.strip() else None
        if value is None:
            makeup_action = _REMOVE_MAKEUP_ACTION
        if makeup_action is None:
            _reject("embodiment.makeup", "invalid_makeup_value")
        required_actions.add(makeup_action)
    if len(required_actions) > 1:
        _reject("inventory", "multiple_action_domains")
    if required_actions:
        _validate_action_authority(
            required_actions.pop(),
            effective_action=effective_action,
            target=target,
            activities_dir=activities_dir,
            actions_dir=actions_dir,
        )
    return resolved


def _resolve_embodiment_patch(
    raw: dict[str, Any],
    before: dict[str, Any],
    reader: InventoryReader | None,
) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    unknown = set(raw) - set(Embodiment.model_fields)
    if unknown:
        _reject("embodiment.unknown_field", "unknown_field")
    for slot, value in raw.items():
        if slot == "makeup":
            canonical = value
        elif slot == "accessory":
            canonical = _resolve_list(value, before[slot], reader, path="embodiment.accessory")
        else:
            canonical = _resolve_selector(value, reader, path=f"embodiment.{slot}")
        if canonical != before.get(slot):
            patch[slot] = canonical
    if set(patch) - {"makeup"}:
        try:
            merged = Embodiment.model_validate({**before, **patch}).model_dump()
        except Exception:
            _reject("embodiment", "schema_invalid")
        _validate_outfit_footprints(merged, reader)
    return patch


def _resolve_bag_patch(
    raw: dict[str, Any], before: dict[str, Any], reader: InventoryReader | None
) -> dict[str, Any]:
    unknown = set(raw) - {"item", "items"}
    if unknown:
        _reject("bag.unknown_field", "unknown_field")
    patch: dict[str, Any] = {}
    if "item" in raw:
        item = _resolve_selector(raw["item"], reader, path="bag.item")
        if item != before["item"]:
            patch["item"] = item
    if "items" in raw:
        items = _resolve_list(raw["items"], before["items"], reader, path="bag.items")
        if items != before["items"]:
            patch["items"] = items
    if patch:
        try:
            merged = Bag.model_validate({**before, **patch}).model_dump()
        except Exception:
            _reject("bag", "schema_invalid")
        _validate_bag_footprints(merged, reader)
    return patch


def _resolve_selector(
    value: Any, reader: InventoryReader | None, *, path: str
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"item_key"}:
        _reject(path, "invalid_selector")
    item_key = value.get("item_key")
    if not isinstance(item_key, str) or not item_key:
        _reject(path, "invalid_item_key")
    item = _lookup(reader, item_key, path=path)
    return {"item_key": item.item_key, "name": item.name}


def _resolve_list(
    value: Any,
    before: list[dict[str, Any]],
    reader: InventoryReader | None,
    *,
    path: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _reject(path, "invalid_type")
    legacy = Counter(item.get("name") for item in before if item.get("item_key") is None)
    result: list[dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, Mapping) and set(entry) == {"item_key", "name"}:
            name = entry.get("name")
            if entry.get("item_key") is not None or not isinstance(name, str) or legacy[name] <= 0:
                _reject(path, "invalid_legacy_snapshot")
            legacy[name] -= 1
            result.append({"item_key": None, "name": name})
        else:
            selected = _resolve_selector(entry, reader, path=path)
            if selected is None:
                _reject(path, "null_item_not_allowed")
            result.append(selected)
    return result


def _validate_outfit_footprints(embodiment: dict[str, Any], reader: InventoryReader | None) -> None:
    occupied: dict[str, list[str]] = {}
    for slot in _ITEM_SLOTS:
        item = embodiment.get(slot)
        if isinstance(item, Mapping) and isinstance(item.get("item_key"), str):
            occupied.setdefault(item["item_key"], []).append(slot)
    for item in embodiment.get("accessory", []):
        if isinstance(item, Mapping) and isinstance(item.get("item_key"), str):
            occupied.setdefault(item["item_key"], []).append("accessory")
    for item_key, slots in occupied.items():
        item = _lookup(reader, item_key, path="embodiment")
        if tuple(slots) != tuple(item.equip_to):
            _reject("embodiment", "footprint_mismatch")


def _validate_bag_footprints(bag: dict[str, Any], reader: InventoryReader | None) -> None:
    bag_item = bag.get("item")
    if isinstance(bag_item, Mapping) and isinstance(bag_item.get("item_key"), str):
        if tuple(_lookup(reader, bag_item["item_key"], path="bag.item").equip_to) != ("bag",):
            _reject("bag.item", "footprint_mismatch")
    for entry in bag.get("items", []):
        if isinstance(entry, Mapping) and isinstance(entry.get("item_key"), str):
            if tuple(_lookup(reader, entry["item_key"], path="bag.items").equip_to) == ("bag",):
                _reject("bag.items", "bag_inside_bag")


def _lookup(reader: InventoryReader | None, item_key: str, *, path: str) -> InventoryItem:
    if reader is None:
        _reject(path, "catalog_unavailable")
    try:
        item = reader.get_inventory_item(item_key)
    except Exception:
        _reject(path, "catalog_lookup_failed")
    if item is None:
        _reject(path, "unknown_item_key")
    return item


def _validate_action_authority(
    required_action: str,
    *,
    effective_action: str | None,
    target: Any,
    activities_dir: Path,
    actions_dir: Path,
) -> None:
    if effective_action != required_action or not isinstance(target, str):
        _reject("inventory", "action_not_authorized")
    try:
        skill = load_activity_skill(
            target,
            activities_dir=activities_dir,
            actions_dir=actions_dir,
        )
        if required_action not in {use.action for use in skill.uses}:
            _reject("inventory", "action_not_in_activity")
        action = load_atomic_action(required_action, actions_dir=actions_dir)
    except ActivitySkillError:
        _reject("inventory", "package_unavailable")
    if required_action in {_OUTFIT_ACTION, _BAG_ACTION} and "inventory" not in action.capabilities:
        _reject("inventory", "capability_not_declared")


def _reject(path: str, reason: str) -> NoReturn:
    raise ActLlmContractError(f"T2.act.llm: inventory rejected path={path} reason={reason}")


__all__ = ["resolve_inventory_diff"]
