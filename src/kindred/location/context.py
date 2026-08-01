"""Location capability 与 act kernel 共用的纯状态投影。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, Any

from kindred.activity.skill import ActivitySkill, LocationBinding
from kindred.location.categories import location_types_for_category, normalized_category
from kindred.location.models import LocationOrigin

if TYPE_CHECKING:
    from kindred.character_card import HomeProfile


def destination_plans_from_state(
    next_state: dict[str, Any],
    skill: ActivitySkill,
) -> dict[str, dict[str, Any]]:
    """返回当前 activity package 声明过的有效目的地计划。"""

    activity = next_state.get("activity")
    if not isinstance(activity, dict):
        return {}
    context = activity.get("context")
    if not isinstance(context, dict):
        return {}
    destinations = context.get("destinations")
    if not isinstance(destinations, dict):
        return {}
    return {
        binding_id: deepcopy(plan)
        for binding_id, plan in destinations.items()
        if binding_id in skill.location_bindings and isinstance(plan, dict)
    }


def current_location_satisfied_bindings(
    next_state: dict[str, Any],
    skill: ActivitySkill,
) -> dict[str, dict[str, Any]]:
    current = _current_location_payload(next_state)
    if current is None:
        return {}
    return {
        binding_id: current
        for binding_id, binding in skill.location_bindings.items()
        if _binding_accepts_location_type(binding, current["type"])
    }


def current_home_satisfied_bindings(
    next_state: dict[str, Any],
    skill: ActivitySkill,
    *,
    home: HomeProfile | None,
) -> dict[str, dict[str, Any]]:
    """start 首拍已位于配置 home 时，允许固定 home binding 直接满足。"""

    if home is None or not home.is_usable():
        return {}
    current = _current_location_payload_without_activity(next_state)
    if current is None or not _location_matches_home(current, home):
        return {}
    return {
        binding_id: current
        for binding_id, binding in skill.location_bindings.items()
        if binding_accepts_home(binding)
    }


def location_origin_from_state(
    next_state: dict[str, Any],
    *,
    home: HomeProfile | None = None,
) -> LocationOrigin:
    location = next_state.get("location")
    environment = next_state.get("environment")
    loc = location if isinstance(location, dict) else {}
    env = environment if isinstance(environment, dict) else {}
    current_name = as_text(loc.get("name"))
    current_address = as_text(loc.get("address"))
    current_city = as_text(loc.get("city"))
    env_city = as_text(env.get("city"))
    if current_name or current_address:
        return LocationOrigin(
            name=current_name,
            address=current_address,
            city=current_city or env_city,
            timezone="",
        )
    if home is not None:
        return LocationOrigin(
            name=home.name,
            address=home.address,
            city=home.city or env_city,
            timezone=home.timezone,
        )
    return LocationOrigin(city=env_city)


def binding_accepts_home(binding: LocationBinding) -> bool:
    """判断 binding 是否显式接受 character-card home。"""

    return any(normalized_category(category) == "home" for category in binding.categories)


def as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _current_location_payload(next_state: dict[str, Any]) -> dict[str, Any] | None:
    location = next_state.get("location")
    activity = next_state.get("activity")
    loc = location if isinstance(location, dict) else {}
    act = activity if isinstance(activity, dict) else {}
    name = as_text(loc.get("name"))
    address = as_text(loc.get("address"))
    loc_type = as_text(loc.get("type"))
    arrived_at = as_text(loc.get("arrived_at"))
    started_at = as_text(act.get("started_at"))
    if not (name or address) or not loc_type or not arrived_at or not started_at:
        return None
    if not _iso_at_or_after(arrived_at, started_at):
        return None
    payload = {
        "name": name,
        "address": address,
        "city": as_text(loc.get("city")),
        "type": loc_type,
        "arrived_at": arrived_at,
    }
    return {key: value for key, value in payload.items() if value}


def _current_location_payload_without_activity(
    next_state: dict[str, Any],
) -> dict[str, Any] | None:
    location = next_state.get("location")
    loc = location if isinstance(location, dict) else {}
    name = as_text(loc.get("name"))
    address = as_text(loc.get("address"))
    loc_type = as_text(loc.get("type"))
    arrived_at = as_text(loc.get("arrived_at"))
    if not (name or address) or not loc_type or not arrived_at:
        return None
    payload = {
        "name": name,
        "address": address,
        "city": as_text(loc.get("city")),
        "type": loc_type,
        "arrived_at": arrived_at,
    }
    return {key: value for key, value in payload.items() if value}


def _location_matches_home(location: dict[str, Any], home: HomeProfile) -> bool:
    if normalized_category(as_text(location.get("type"))) != "home":
        return False
    return bool(home.address) and as_text(location.get("address")) == home.address


def _binding_accepts_location_type(binding: LocationBinding, location_type: str) -> bool:
    normalized_type = normalized_category(location_type)
    if not binding.categories:
        return bool(normalized_type)
    acceptable_types: set[str] = set()
    for category in binding.categories:
        acceptable_types.update(location_types_for_category(category))
    return normalized_type in acceptable_types


def _iso_at_or_after(value: str, lower_bound: str) -> bool:
    try:
        return datetime.fromisoformat(value) >= datetime.fromisoformat(lower_bound)
    except ValueError:
        return value >= lower_bound


__all__ = [
    "as_text",
    "binding_accepts_home",
    "current_home_satisfied_bindings",
    "current_location_satisfied_bindings",
    "destination_plans_from_state",
    "location_origin_from_state",
    "normalized_category",
]
