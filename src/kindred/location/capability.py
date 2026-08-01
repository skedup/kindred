"""地点 capability 工具。

C1 只把 ``find_places`` 迁入 capability plane。地点状态事件
（choose/arrive/abandon）跨 F1 state commit 边界，仍留在 act kernel。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from kindred.capability_host.internal import HostTickContext, HostToolResult
from kindred.location.candidates import (
    LOCATION_CANDIDATE_RESOLVER_KEY,
    LocationCandidateResolver,
)
from kindred.location.models import MAX_LOCATION_CANDIDATES
from kindred.location.query import LocationOptionsCache, find_places_for_binding
from kindred.state._types import is_safe_name
from kindred_capability_sdk import ToolCall, ToolDef, ToolResult

if TYPE_CHECKING:
    from kindred.providers.location import LocationProvider

TOOL_FIND_PLACES = "find_places"
LOCATION_PROVIDER_DEPENDENCY = "location_provider"
LOCATION_CACHE_KEY = "location.find_places"

FIND_PLACES_TOOL = ToolDef(
    name=TOOL_FIND_PLACES,
    description=(
        "Find place candidates for one declared activity location binding. "
        "The query, categories, radius, and limit come from the activity skill; "
        "provide only binding_id."
    ),
    effect="read_only",
    parameters={
        "type": "OBJECT",
        "properties": {
            "binding_id": {"type": "STRING"},
        },
        "required": ["binding_id"],
    },
)


@dataclass(frozen=True)
class LocationCapability:
    """只读地点工具。

    ``find_places`` 在 provider 缺失时仍保持可见。provider 缺失是通过 tool
    result/tool_trace 呈现给心的世界事实，不是 sense 隐藏 activity 或 act 隐藏工具的理由。
    """

    name: str = "location"

    def tool_defs(self) -> tuple[ToolDef, ...]:
        return (FIND_PLACES_TOOL,)

    def handle(self, call: ToolCall, ctx: HostTickContext) -> HostToolResult:
        if set(call.args) != {"binding_id"}:
            return _find_error(call, "InvalidArgs", "find_places accepts only binding_id")
        binding_id = call.args.get("binding_id")
        if not isinstance(binding_id, str) or not binding_id.strip():
            return _find_error(call, "InvalidArgs", "find_places requires non-empty binding_id")
        binding_id = binding_id.strip()
        if not is_safe_name(binding_id):
            return _find_error(call, "InvalidBinding", "binding_id must match ^[a-z][a-z0-9_]*$")
        if ctx.activities_dir is None or ctx.actions_dir is None:
            return _find_error(
                call,
                "MissingContext",
                "find_places requires activities_dir and actions_dir",
            )
        resolver = ctx.caches.get(LOCATION_CANDIDATE_RESOLVER_KEY)
        if not isinstance(resolver, LocationCandidateResolver):
            return _find_error(call, "MissingContext", "find_places requires a candidate resolver")

        response = find_places_for_binding(
            binding_id,
            target=ctx.target if isinstance(ctx.target, str) else None,
            next_state=dict(ctx.next_state),
            kind=ctx.act_kind,
            triggered_at=ctx.triggered_at,
            location_provider=cast(
                "LocationProvider | None",
                ctx.provider_handles.get(LOCATION_PROVIDER_DEPENDENCY),
            ),
            activities_dir=ctx.activities_dir,
            actions_dir=ctx.actions_dir,
            db=ctx.db,
            home=ctx.home,
            cache=self._cache(ctx),
        )
        if response.get("ok") is not True:
            error_type = response.get("error_type")
            message = response.get("message")
            return _find_error(
                call,
                error_type if isinstance(error_type, str) else "FindPlacesError",
                message if isinstance(message, str) else "find_places failed",
            )
        try:
            projected = _project_find_response(response, resolver)
        except ValueError:
            error_type = "CandidateCollision"
        except TypeError:
            error_type = "InvalidCandidate"
        except LookupError:
            error_type = "UnknownStatus"
        else:
            return HostToolResult(
                ToolResult.ok(call, projected).with_trace(
                    args={"binding_id": binding_id},
                    response=_find_trace_summary(response),
                )
            )
        return _find_error(call, error_type, "location candidates could not be registered")

    @staticmethod
    def _cache(ctx: HostTickContext) -> LocationOptionsCache:
        cache = ctx.caches.get(LOCATION_CACHE_KEY)
        if isinstance(cache, dict):
            return cast("LocationOptionsCache", cache)
        new_cache: LocationOptionsCache = {}
        ctx.caches[LOCATION_CACHE_KEY] = new_cache
        return new_cache


def _project_find_response(
    response: Mapping[str, Any], resolver: LocationCandidateResolver
) -> dict[str, Any]:
    result: dict[str, Any] = {
        key: response[key] for key in ("binding_id", "status", "notices") if key in response
    }
    status = response.get("status")
    if status in {"ok", "no_places"}:
        result.update(ok=True, cached=response.get("cached"), places=[])
        places = response.get("places", [])
        if not isinstance(places, list):
            raise TypeError("places must be a list")
        for place in places[:MAX_LOCATION_CANDIDATES]:
            if not isinstance(place, Mapping):
                raise TypeError("candidate must be an object")
            canonical = place.get("canonical_candidate")
            if not isinstance(canonical, Mapping):
                raise TypeError("candidate missing canonical payload")
            snapshot = canonical.get("candidate_snapshot")
            snapshot = snapshot if isinstance(snapshot, Mapping) else {}
            item: dict[str, Any] = {"candidate_ref": resolver.register(canonical)}
            for key, limit in _CANDIDATE_TEXT_LIMITS.items():
                value = _clip(canonical.get(key), limit)
                if value:
                    item[key] = value
            for key in ("distance_km", "rating", "review_count", "price_level", "open_now"):
                if snapshot.get(key) is not None:
                    item[key] = snapshot[key]
            for key in ("review_summary", "opening_hours_summary"):
                value = _clip(snapshot.get(key), 240)
                if value and (key != "opening_hours_summary" or snapshot.get("open_now") is None):
                    item[key] = value
            tags = snapshot.get("tags")
            if isinstance(tags, list):
                item["tags"] = [value for tag in tags[:8] if (value := _clip(tag, 32))]
            confidence = snapshot.get("confidence")
            if isinstance(confidence, (int, float)) and confidence < 1:
                item["confidence"] = confidence
            if isinstance(place.get("local_experience"), Mapping):
                item["local_experience"] = dict(place["local_experience"])
            result["places"].append(item)
    elif status == "already_planned" and isinstance(response.get("plan"), Mapping):
        result["plan"] = {"name": _clip(response["plan"].get("name"), 80)}
    elif status == "already_at_current_location" and isinstance(
        response.get("current_location"), Mapping
    ):
        location = response["current_location"]
        result["current_location"] = {
            "name": _clip(location.get("name"), 80),
            "type": _clip(location.get("type"), 64),
        }
    elif status not in {"ok", "no_places"}:
        raise LookupError("unknown successful find_places status")
    return result


def _find_trace_summary(response: Mapping[str, Any]) -> dict[str, Any]:
    summary = {
        key: response[key]
        for key in ("ok", "binding_id", "status", "cached", "notices")
        if key in response
    }
    places = response.get("places")
    summary["candidate_count"] = (
        min(len(places), MAX_LOCATION_CANDIDATES) if isinstance(places, list) else 0
    )
    query = response.get("query")
    if isinstance(query, Mapping):
        shape = {
            key: query[key] for key in ("radius_min_km", "radius_max_km", "limit") if key in query
        }
        if "provider_categories" in query:
            shape["categories"] = query["provider_categories"]
        summary["query_shape"] = shape
    origin = response.get("origin")
    if isinstance(origin, Mapping):
        if origin.get("provider_place_id"):
            summary["origin_kind"] = "provider_place_id"
        elif origin.get("lat") is not None and origin.get("lng") is not None:
            summary["origin_kind"] = "coordinates"
        else:
            summary["origin_kind"] = next(
                (key for key in ("address", "city", "name") if origin.get(key)), "unavailable"
            )
    return summary


def _clip(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _find_error(call: ToolCall, error_type: str, message: str) -> HostToolResult:
    return HostToolResult(
        ToolResult.error(call, error_type=error_type, message=message).with_trace(
            args={"arg_keys": sorted(call.args)},
            response={"ok": False, "error_type": error_type},
        )
    )


_CANDIDATE_TEXT_LIMITS = {"name": 80, "address": 160, "city": 64, "type": 64, "source": 64}
