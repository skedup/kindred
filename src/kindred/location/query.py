"""Location capability 与 act prompt 渲染共用的地点查询执行逻辑。

本模块只承接 read-only ``find_places`` 的查询、缓存、Provider/PlaceStore 访问与
候选 payload；与 graph 共用的纯状态投影位于 ``kindred.location.context``。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kindred.activity import ActivitySkillError, load_activity_skill
from kindred.activity.skill import LocationBinding
from kindred.character_card import CHARACTER_CARD_PLACE_SOURCE
from kindred.location.context import (
    as_text,
    binding_accepts_home,
    current_home_satisfied_bindings,
    current_location_satisfied_bindings,
    destination_plans_from_state,
    location_origin_from_state,
    normalized_category,
)
from kindred.location.models import (
    MAX_LOCATION_RADIUS_KM,
    LocationOrigin,
    LocationQuery,
    PlaceCandidate,
)
from kindred.providers.location import LocationProvider

if TYPE_CHECKING:
    from kindred.character_card import HomeProfile
    from kindred.db.facade import KindredDB
    from kindred.db.places import PlaceVisitStats

logger = logging.getLogger(__name__)

_HOME_SOURCE = CHARACTER_CARD_PLACE_SOURCE
_KNOWN_PLACE_CATEGORIES = frozenset({"home"})
LocationOptionsCache = dict[str, dict[str, Any]]


def find_places_for_binding(
    binding_id: str,
    *,
    target: str | None,
    next_state: dict[str, Any],
    kind: str | None,
    triggered_at: str | None,
    location_provider: LocationProvider | None,
    activities_dir: Path,
    actions_dir: Path,
    db: KindredDB | None = None,
    home: HomeProfile | None = None,
    cache: LocationOptionsCache | None = None,
) -> dict[str, Any]:
    """按单个 activity location binding 返回结构化地点候选。

    模型只提供 ``binding_id``；query 文本、半径、类别和 limit 都从 activity package
    解析。``cache`` 是单 tick 缓存，同一个 binding 重复调用不会二次打 provider。
    """

    if cache is not None and binding_id in cache:
        response = deepcopy(cache[binding_id])
        response["cached"] = True
        return response

    response = _find_places_for_binding_uncached(
        binding_id,
        target=target,
        next_state=next_state,
        kind=kind,
        triggered_at=triggered_at,
        location_provider=location_provider,
        activities_dir=activities_dir,
        actions_dir=actions_dir,
        db=db,
        home=home,
    )
    if cache is not None and response.get("ok") is True:
        cache[binding_id] = deepcopy(response)
    return response


def _find_places_for_binding_uncached(
    binding_id: str,
    *,
    target: str | None,
    next_state: dict[str, Any],
    kind: str | None,
    triggered_at: str | None,
    location_provider: LocationProvider | None,
    activities_dir: Path,
    actions_dir: Path,
    db: KindredDB | None,
    home: HomeProfile | None,
) -> dict[str, Any]:
    if not target:
        return _find_places_error(
            binding_id,
            "MissingActivity",
            "current activity is missing; cannot resolve location binding",
        )
    try:
        skill = load_activity_skill(target, activities_dir=activities_dir, actions_dir=actions_dir)
    except ActivitySkillError:
        return _find_places_error(
            binding_id,
            "ActivitySkillError",
            "current activity skill cannot be loaded",
        )
    binding = skill.location_bindings.get(binding_id)
    if binding is None:
        return _find_places_error(
            binding_id,
            "InvalidBinding",
            "binding_id is not declared for the current activity",
        )

    plans = {} if kind == "start_activity" else destination_plans_from_state(next_state, skill)
    if binding_id in plans:
        return {
            "ok": True,
            "cached": False,
            "binding_id": binding_id,
            "status": "already_planned",
            "plan": deepcopy(plans[binding_id]),
            "places": [],
            "notices": ["已有目的地计划，本 tick 不重新查询候选。"],
        }

    if kind == "start_activity":
        satisfied = current_home_satisfied_bindings(next_state, skill, home=home)
    else:
        satisfied = current_location_satisfied_bindings(next_state, skill)
    if binding_id in satisfied:
        return {
            "ok": True,
            "cached": False,
            "binding_id": binding_id,
            "status": "already_at_current_location",
            "current_location": deepcopy(satisfied[binding_id]),
            "places": [],
            "notices": ["当前 state.location 已满足这个 binding；不要重新查询或重新选择目的地。"],
        }

    origin = location_origin_from_state(next_state, home=home)
    now = location_query_now(triggered_at, next_state)
    places: list[dict[str, Any]] = []
    notices: list[str] = []

    if home is not None and binding_accepts_home(binding):
        home_stats = _lookup_visit_stats_by_keys(db, [home.place_key])
        places.append(
            _home_candidate_payload(
                binding_id,
                home,
                origin=origin,
                stats=home_stats.get(home.place_key) if home_stats is not None else None,
                memory_enabled=home_stats is not None,
            )
        )

    provider_categories = provider_categories_for_binding(binding)
    if binding.categories and not provider_categories:
        notices.append("本 binding 只声明 character-card 已知地点 category，未查询地图 Provider。")
    elif location_provider is None:
        notices.append("LocationProvider 未启用；只返回已知固定地点。")
    elif not origin.is_queryable():
        notices.append("当前 location / environment / home 不足以构造可查询 origin。")
    else:
        try:
            query = build_location_query(origin, binding, now=now, categories=provider_categories)
            candidates = location_provider.find_nearby(query)
        except Exception as exc:  # noqa: BLE001 - Provider 失败不应打穿 tick
            logger.warning(
                "LocationCapability: LocationProvider.find_nearby failed for binding=%s (%s)",
                binding_id,
                type(exc).__name__,
            )
            notices.append(f"LocationProvider 查询失败：{type(exc).__name__}。")
        else:
            if not candidates:
                notices.append("LocationProvider 未返回候选。")
            visit_stats = _lookup_visit_stats(db, candidates)
            places.extend(
                _place_candidate_payload(
                    binding_id,
                    candidate,
                    stats=visit_stats.get(candidate.place_key) if visit_stats is not None else None,
                    memory_enabled=visit_stats is not None,
                )
                for candidate in candidates
            )

    if not places:
        notices.append(
            "places 为空：本 tick 没有可选择目的地；不要调用 choose_destination/arrive，"
            "也不要把到店、坐下、吃上、住下等未发生的地点结果写成事实。"
        )

    return {
        "ok": True,
        "cached": False,
        "binding_id": binding_id,
        "status": "ok" if places else "no_places",
        "query": _binding_query_payload(binding, provider_categories=provider_categories),
        "origin": _origin_payload(origin),
        "places": places,
        "notices": notices,
    }


def _find_places_error(binding_id: str, error_type: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "binding_id": binding_id,
        "error_type": error_type,
        "message": message,
        "places": [],
    }


def _lookup_visit_stats(
    db: KindredDB | None,
    candidates: tuple[PlaceCandidate, ...],
) -> dict[str, PlaceVisitStats] | None:
    return _lookup_visit_stats_by_keys(db, [c.place_key for c in candidates])


def _lookup_visit_stats_by_keys(
    db: KindredDB | None,
    place_keys: list[str],
) -> dict[str, PlaceVisitStats] | None:
    """批量读取本地到访统计；``None`` 表示本地记忆查询不可用。"""

    if db is None:
        return None
    try:
        return db.get_place_visit_stats(place_keys)
    except Exception as exc:  # noqa: BLE001 - 本地记忆读取失败按无记忆降级
        logger.warning("LocationCapability: place_visits lookup failed, degrading: %s", exc)
        return None


def _binding_query_payload(
    binding: LocationBinding,
    *,
    provider_categories: tuple[str, ...],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": binding.query,
        "categories": list(binding.categories),
        "provider_categories": list(provider_categories),
    }
    if binding.radius_min_km is not None:
        payload["radius_min_km"] = binding.radius_min_km
    if binding.radius_max_km is not None:
        payload["radius_max_km"] = binding.radius_max_km
    if binding.limit is not None:
        payload["limit"] = binding.limit
    return payload


def _origin_payload(origin: LocationOrigin) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": origin.name,
        "address": origin.address,
        "city": origin.city,
        "timezone": origin.timezone,
    }
    if origin.lat is not None and origin.lng is not None:
        payload["lat"] = origin.lat
        payload["lng"] = origin.lng
    if origin.provider_place_id:
        payload["provider_place_id"] = origin.provider_place_id
    return payload


def _place_candidate_payload(
    binding_id: str,
    candidate: PlaceCandidate,
    *,
    stats: PlaceVisitStats | None = None,
    memory_enabled: bool = False,
) -> dict[str, Any]:
    snapshot = _candidate_snapshot(candidate)
    canonical = {
        "binding_id": binding_id,
        "place_key": candidate.place_key,
        "name": candidate.name,
        "address": candidate.address or None,
        "city": candidate.city or None,
        "type": candidate.type,
        "source": candidate.source,
        "candidate_snapshot": snapshot,
    }
    return {
        "local_experience": _local_experience_payload(stats, memory_enabled=memory_enabled),
        "canonical_candidate": canonical,
    }


def _candidate_snapshot(candidate: PlaceCandidate) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "distance_km": candidate.distance_km,
    }
    for key, value in (
        ("provider_place_id", candidate.provider_place_id),
        ("district", candidate.district),
        ("lat", candidate.lat),
        ("lng", candidate.lng),
        ("rating", candidate.rating),
        ("review_count", candidate.review_count),
        ("review_summary", candidate.review_summary),
        ("price_level", candidate.price_level),
        ("open_now", candidate.open_now),
        ("opening_hours_summary", candidate.opening_hours_summary),
        ("tags", list(candidate.tags) if candidate.tags else None),
        ("url", candidate.url),
        ("confidence", candidate.confidence),
    ):
        if value not in (None, "", []):
            snapshot[key] = value
    return snapshot


def _home_candidate_payload(
    binding_id: str,
    home: HomeProfile,
    *,
    origin: LocationOrigin,
    stats: PlaceVisitStats | None = None,
    memory_enabled: bool = False,
) -> dict[str, Any]:
    distance_km = 0.0 if _origin_matches_home(origin, home) else None
    snapshot: dict[str, Any] = {"known": "fixed_home"}
    if distance_km is not None:
        snapshot["distance_km"] = distance_km
    canonical = {
        "binding_id": binding_id,
        "place_key": home.place_key,
        "name": home.name,
        "address": home.address or None,
        "city": home.city or None,
        "type": "home",
        "source": _HOME_SOURCE,
        "candidate_snapshot": snapshot,
    }
    return {
        "local_experience": _local_experience_payload(stats, memory_enabled=memory_enabled),
        "canonical_candidate": canonical,
    }


def _local_experience_payload(
    stats: PlaceVisitStats | None,
    *,
    memory_enabled: bool,
) -> dict[str, Any]:
    if not memory_enabled:
        return {"memory_enabled": False}
    if stats is None:
        return {"memory_enabled": True, "visited": False, "visit_count": 0}
    payload: dict[str, Any] = {
        "memory_enabled": True,
        "visited": True,
        "visit_count": stats.visit_count,
        "last_visited_at": stats.last_visited_at,
    }
    if stats.last_activity_name:
        payload["last_activity_name"] = stats.last_activity_name
    return payload


def location_query_now(triggered_at: str | None, next_state: dict[str, Any]) -> str:
    if triggered_at and triggered_at.strip():
        return triggered_at.strip()
    time_layer = next_state.get("time")
    if isinstance(time_layer, dict):
        return as_text(time_layer.get("iso"))
    return ""


def build_location_query(
    origin: LocationOrigin,
    binding: LocationBinding,
    *,
    now: str,
    categories: tuple[str, ...] | None = None,
) -> LocationQuery:
    kwargs: dict[str, Any] = {
        "origin": origin,
        "query": binding.query,
        "categories": tuple(binding.categories) if categories is None else categories,
        "now": now,
    }
    if binding.radius_min_km is not None:
        kwargs["radius_min_km"] = binding.radius_min_km
    if binding.radius_max_km is not None:
        kwargs["radius_max_km"] = binding.radius_max_km
    elif binding.radius_min_km is not None:
        kwargs["radius_max_km"] = MAX_LOCATION_RADIUS_KM
    if binding.limit is not None:
        kwargs["limit"] = binding.limit
    return LocationQuery(**kwargs)


def provider_categories_for_binding(binding: LocationBinding) -> tuple[str, ...]:
    """查询地图 provider 前，剔除 character-card 已知地点 category。"""

    return tuple(
        category
        for category in binding.categories
        if normalized_category(category) not in _KNOWN_PLACE_CATEGORIES
    )


def _origin_matches_home(origin: LocationOrigin, home: HomeProfile) -> bool:
    """只有当前 origin 强匹配 home 时才输出 0km。"""

    if home.address and origin.address:
        return _normalize_place_text(origin.address) == _normalize_place_text(home.address)
    return False


def _normalize_place_text(value: str) -> str:
    return "".join(value.split()).lower()


__all__ = [
    "LocationOptionsCache",
    "as_text",
    "binding_accepts_home",
    "build_location_query",
    "current_location_satisfied_bindings",
    "destination_plans_from_state",
    "find_places_for_binding",
    "location_origin_from_state",
    "location_query_now",
    "normalized_category",
    "provider_categories_for_binding",
]
