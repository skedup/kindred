"""Portable inventory candidate discovery."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypeGuard, cast

from kindred_capability_sdk import (
    CapabilityContribution,
    CapabilityResult,
    InvocationContext,
    SecretResolver,
    ToolBinding,
    ToolCall,
    ToolDef,
    ToolResult,
)

INVENTORY_FACT = "inventory.choice_context.v1"
InventoryScope = Literal["outfit", "bag"]

LIST_INVENTORY = ToolDef(
    name="list_inventory",
    description=(
        "List a small, deterministic sample of the resident's owned outfit or bag items. "
        "Use only when the current action needs a new inventory choice. For outfit choices, "
        "an omitted slot stays unchanged, while an explicit null means choosing not to wear "
        "that slot. Replacing a multi-slot item must update every affected slot in the same "
        "state diff."
    ),
    effect="read_only",
    allow_before_action_lock=True,
    parameters={
        "type": "OBJECT",
        "properties": {"scope": {"type": "STRING", "enum": ["outfit", "bag"]}},
        "required": ["scope"],
    },
)

_SCOPE_ACTION = {"outfit": "change_outfit", "bag": "pack_bag"}
_OUTFIT_BUCKETS = (
    "top",
    "bottom",
    "top_bottom",
    "outer",
    "bra",
    "panties",
    "socks",
    "shoes",
    "accessory",
)
_BAG_BUCKETS = ("bag", "bedside", "storage", "wardrobe")
_MAX_CANDIDATES = 6


@dataclass(frozen=True)
class _Item:
    item_key: str
    name: str
    home_pool: str
    kind: str
    equip_to: tuple[str, ...]
    description: str | None


def create_capability(
    *,
    settings: Mapping[str, Any],
    secrets: SecretResolver,
) -> CapabilityContribution:
    del secrets
    if settings:
        raise ValueError("inventory settings must be empty")

    def handle(call: ToolCall, context: InvocationContext) -> CapabilityResult:
        scope = call.args.get("scope")
        if set(call.args) != {"scope"} or not isinstance(scope, str) or scope not in _SCOPE_ACTION:
            return _error(call, "InvalidArgs", "list_inventory requires scope=outfit or bag")
        scope = cast("InventoryScope", scope)
        fact = context.fact(INVENTORY_FACT)
        if fact is None:
            return _error(call, "Unavailable", "inventory catalog is unavailable")
        allowed = fact.value.get("allowed_scopes")
        if not _string_sequence(allowed) or scope not in allowed:
            return _error(call, "UnauthorizedScope", "inventory scope is not authorized")
        if not _valid_triggered_at(context.triggered_at):
            return _error(call, "MissingContext", "list_inventory requires triggered_at")
        status = fact.value.get("status")
        if status == "unavailable":
            return _error(call, "Unavailable", "inventory catalog is unavailable")
        if status != "ok":
            return _error(call, "StoreError", "inventory catalog could not be read")

        cache_key = f"list_{scope}"
        cached = context.transient.get(cache_key)
        if isinstance(cached, Mapping):
            return CapabilityResult(ToolResult.ok(call, cached))
        try:
            items = _parse_items(fact.value.get("items"))
            current = _current_payload(scope, fact.value)
            candidates, total = _sample_items(
                items,
                scope=scope,
                seed=cast("str", context.triggered_at),
                excluded_keys=_occupied_item_keys(fact.value),
            )
        except (KeyError, TypeError, ValueError):
            return _error(call, "StoreError", "inventory catalog could not be read")
        response = {
            "status": "ok" if total else "empty",
            "current": current,
            "candidates": [_candidate_payload(item) for item in candidates],
            "total": total,
            "sampled": len(candidates) < total,
        }
        context.transient.put(cache_key, response)
        return CapabilityResult(ToolResult.ok(call, response))

    return CapabilityContribution(
        (ToolBinding(LIST_INVENTORY, handle),),
        required_fact_views=frozenset({INVENTORY_FACT}),
    )


def _parse_items(value: Any) -> list[_Item]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("items must be a sequence")
    if any(not isinstance(item, Mapping) for item in value):
        raise TypeError("item must be a mapping")
    return [
        _Item(
            raw["item_key"],
            raw["name"],
            raw["home_pool"],
            raw["kind"],
            tuple(raw["equip_to"]),
            raw.get("description"),
        )
        for raw in value
    ]


def _current_payload(scope: InventoryScope, fact: Mapping[str, Any]) -> dict[str, Any]:
    key = "bag" if scope == "bag" else "embodiment"
    value = fact.get(key)
    if not isinstance(value, Mapping):
        raise TypeError("current inventory snapshot is unavailable")
    result = _copy_json(value)
    if not isinstance(result, dict):
        raise TypeError("current inventory snapshot is invalid")
    if scope == "outfit":
        result.pop("makeup", None)
    return result


def _occupied_item_keys(fact: Mapping[str, Any]) -> frozenset[str]:
    embodiment = fact.get("embodiment")
    bag = fact.get("bag")
    values: list[Any] = []
    if isinstance(embodiment, Mapping):
        values.extend(
            embodiment.get(slot)
            for slot in ("top", "bottom", "outer", "bra", "panties", "socks", "shoes")
        )
        accessory = embodiment.get("accessory")
        if isinstance(accessory, (list, tuple)):
            values.extend(accessory)
    if isinstance(bag, Mapping):
        values.append(bag.get("item"))
        items = bag.get("items")
        if isinstance(items, (list, tuple)):
            values.extend(items)
    return frozenset(
        key
        for value in values
        if isinstance(value, Mapping) and isinstance((key := value.get("item_key")), str) and key
    )


def _sample_items(
    items: list[_Item],
    *,
    scope: InventoryScope,
    seed: str,
    excluded_keys: frozenset[str],
) -> tuple[list[_Item], int]:
    bucket_names = _OUTFIT_BUCKETS if scope == "outfit" else _BAG_BUCKETS
    buckets: dict[str, list[_Item]] = {name: [] for name in bucket_names}
    seen: set[str] = set()
    for item in items:
        if item.item_key in seen or item.item_key in excluded_keys:
            continue
        bucket = _bucket_for(item, scope)
        if bucket is not None:
            seen.add(item.item_key)
            buckets[bucket].append(item)
    per_bucket = 2 if scope == "outfit" else 4
    selected_by_bucket = {
        bucket: sorted(
            buckets[bucket],
            key=lambda item: _rank(seed, scope, item.item_key),
        )[:per_bucket]
        for bucket in bucket_names
    }
    total = sum(len(bucket) for bucket in buckets.values())
    selected = [item for bucket in bucket_names for item in selected_by_bucket[bucket]]
    selected.sort(key=lambda item: _rank(seed, scope, item.item_key, global_rank=True))
    if scope == "outfit":
        anchors = [
            selected_by_bucket[bucket][0]
            for bucket in ("top_bottom", "top", "bottom")
            if selected_by_bucket[bucket]
        ]
        anchor_keys = {item.item_key for item in anchors}
        selected = anchors + [item for item in selected if item.item_key not in anchor_keys]
    return selected[:_MAX_CANDIDATES], total


def _rank(seed: str, scope: InventoryScope, item_key: str, *, global_rank: bool = False) -> bytes:
    marker = "\0global" if global_rank else ""
    return hashlib.sha256(f"{seed}\0{scope}{marker}\0{item_key}".encode()).digest()


def _bucket_for(item: _Item, scope: InventoryScope) -> str | None:
    if scope == "outfit":
        if not item.equip_to or item.equip_to == ("bag",):
            return None
        return "top_bottom" if item.equip_to == ("top", "bottom") else item.equip_to[0]
    return "bag" if item.equip_to == ("bag",) else item.home_pool


def _candidate_payload(item: _Item) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "item_key": item.item_key,
        "name": item.name,
        "kind": item.kind,
        "equip_to": list(item.equip_to),
    }
    if item.description:
        payload["description"] = item.description[:120]
    return payload


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json(item) for item in value]
    return value


def _string_sequence(value: Any) -> TypeGuard[Sequence[str]]:
    return isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value)


def _valid_triggered_at(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def _error(call: ToolCall, error_type: str, message: str) -> CapabilityResult:
    return CapabilityResult(ToolResult.error(call, error_type=error_type, message=message))


__all__ = ["INVENTORY_FACT", "LIST_INVENTORY", "create_capability"]
