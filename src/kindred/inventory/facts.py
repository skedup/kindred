"""Host-owned Inventory facts exposed to the Portable read package."""

from __future__ import annotations

from typing import Any

from kindred.activity.tool_binding import action_names_declaring_capability
from kindred.capability_host.internal import HostTickContext
from kindred.inventory.catalog import InventoryItem
from kindred.state.possession import Bag, Embodiment
from kindred_capability_sdk import FactView

INVENTORY_CHOICE_CONTEXT_FACT = "inventory.choice_context.v1"
_CACHE_KEY = "inventory_choice_context"
_ACTION_SCOPES = (("change_outfit", "outfit"), ("pack_bag", "bag"))


def build_inventory_choice_context(context: HostTickContext) -> FactView:
    cached = context.caches.get(_CACHE_KEY)
    if isinstance(cached, FactView) and cached.name == INVENTORY_CHOICE_CONTEXT_FACT:
        return cached

    def finish(
        status: str,
        *,
        items: tuple[dict[str, Any], ...] = (),
        embodiment: dict[str, Any] | None = None,
        bag: dict[str, Any] | None = None,
    ) -> FactView:
        fact = FactView(
            INVENTORY_CHOICE_CONTEXT_FACT,
            {
                "status": status,
                "items": items,
                "embodiment": {} if embodiment is None else embodiment,
                "bag": {} if bag is None else bag,
                "allowed_scopes": allowed_scopes,
            },
        )
        context.caches[_CACHE_KEY] = fact
        return fact

    actions = action_names_declaring_capability(
        context.anchor_activity,
        "inventory",
        activities_dir=context.activities_dir,
        actions_dir=context.actions_dir,
    )
    allowed_scopes = tuple(scope for action, scope in _ACTION_SCOPES if action in actions)
    try:
        embodiment = Embodiment.model_validate(context.next_state.get("embodiment")).model_dump()
        bag = Bag.model_validate(context.next_state.get("bag")).model_dump()
    except Exception:
        return finish("store_error")
    if context.db is None or not hasattr(context.db, "list_inventory_items"):
        return finish("unavailable", embodiment=embodiment, bag=bag)
    try:
        raw_items = context.db.list_inventory_items()
        if not isinstance(raw_items, list):
            raise TypeError("inventory reader returned a non-list")
        items = tuple(
            InventoryItem.model_validate(
                item.model_dump() if isinstance(item, InventoryItem) else item
            ).model_dump()
            for item in raw_items
        )
    except Exception:
        return finish("store_error", embodiment=embodiment, bag=bag)
    return finish("ok", items=items, embodiment=embodiment, bag=bag)


__all__ = ["INVENTORY_CHOICE_CONTEXT_FACT", "build_inventory_choice_context"]
