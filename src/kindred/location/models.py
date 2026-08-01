"""地点领域的 provider 中立模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MAX_LOCATION_RADIUS_KM = 50.0
MAX_LOCATION_CANDIDATES = 10
MAX_REVIEW_SUMMARY_CHARS = 240


@dataclass(frozen=True)
class LocationOrigin:
    """一次地点搜索的圆心 / 出发点。"""

    name: str = ""
    address: str = ""
    city: str = ""
    timezone: str = ""
    lat: float | None = None
    lng: float | None = None
    provider_place_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "address", self.address.strip())
        object.__setattr__(self, "city", self.city.strip())
        object.__setattr__(self, "timezone", self.timezone.strip())
        object.__setattr__(self, "provider_place_id", self.provider_place_id.strip())
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be provided together")
        if self.lat is not None and not -90.0 <= self.lat <= 90.0:
            raise ValueError("lat must be between -90 and 90")
        if self.lng is not None and not -180.0 <= self.lng <= 180.0:
            raise ValueError("lng must be between -180 and 180")

    def is_queryable(self) -> bool:
        """是否含有足够信息供地图或虚拟 Provider 查询。"""

        return bool(
            self.provider_place_id
            or (self.lat is not None and self.lng is not None)
            or self.address
            or self.city
            or self.name
        )


@dataclass(frozen=True)
class LocationQuery:
    """``find_nearby`` 查询意图与地理约束。"""

    origin: LocationOrigin
    query: str
    categories: tuple[str, ...] = ()
    radius_min_km: float = 0.0
    radius_max_km: float = 3.0
    limit: int = 5
    locale: str = "zh-CN"
    now: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", self.query.strip())
        object.__setattr__(self, "categories", _clean_tuple(self.categories))
        object.__setattr__(self, "locale", self.locale.strip() or "zh-CN")
        object.__setattr__(self, "now", self.now.strip())
        if not self.query:
            raise ValueError("query must not be blank")
        if self.radius_min_km < 0:
            raise ValueError("radius_min_km must be non-negative")
        if self.radius_max_km < self.radius_min_km:
            raise ValueError("radius_max_km must be >= radius_min_km")
        if self.radius_max_km > MAX_LOCATION_RADIUS_KM:
            raise ValueError(f"radius_max_km must be <= {MAX_LOCATION_RADIUS_KM:g}")
        if not 1 <= self.limit <= MAX_LOCATION_CANDIDATES:
            raise ValueError(f"limit must be between 1 and {MAX_LOCATION_CANDIDATES}")
        if self.now:
            _validate_iso_datetime(self.now)


@dataclass(frozen=True)
class PlaceCandidate:
    """Provider 返回的世界事实候选，不夹带 Kindred 本地记忆。"""

    name: str
    type: str
    address: str
    distance_km: float
    source: str
    place_key: str
    provider_place_id: str = ""
    lat: float | None = None
    lng: float | None = None
    city: str = ""
    district: str = ""
    timezone: str = ""
    rating: float | None = None
    review_count: int | None = None
    review_summary: str = ""
    price_level: int | None = None
    open_now: bool | None = None
    opening_hours_summary: str = ""
    tags: tuple[str, ...] = ()
    url: str = ""
    confidence: float = 1.0

    def __post_init__(self) -> None:
        for field_name in (
            "name",
            "type",
            "address",
            "source",
            "place_key",
            "provider_place_id",
            "city",
            "district",
            "timezone",
            "review_summary",
            "opening_hours_summary",
            "url",
        ):
            object.__setattr__(self, field_name, getattr(self, field_name).strip())
        object.__setattr__(self, "tags", _clean_tuple(self.tags))
        if not self.name:
            raise ValueError("name must not be blank")
        if not self.type:
            raise ValueError("type must not be blank")
        if not self.source:
            raise ValueError("source must not be blank")
        if not self.place_key:
            raise ValueError("place_key must not be blank")
        if self.distance_km < 0:
            raise ValueError("distance_km must be non-negative")
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be provided together")
        if self.lat is not None and not -90.0 <= self.lat <= 90.0:
            raise ValueError("lat must be between -90 and 90")
        if self.lng is not None and not -180.0 <= self.lng <= 180.0:
            raise ValueError("lng must be between -180 and 180")
        if self.rating is not None and not 0.0 <= self.rating <= 5.0:
            raise ValueError("rating must be between 0 and 5")
        if self.review_count is not None and self.review_count < 0:
            raise ValueError("review_count must be non-negative")
        if len(self.review_summary) > MAX_REVIEW_SUMMARY_CHARS:
            raise ValueError(f"review_summary must be <= {MAX_REVIEW_SUMMARY_CHARS} chars")
        if self.price_level is not None and not 0 <= self.price_level <= 5:
            raise ValueError("price_level must be between 0 and 5")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


def _clean_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value for value in (item.strip() for item in values) if value)


def _validate_iso_datetime(value: str) -> None:
    if "T" not in value and " " not in value:
        raise ValueError("now must include a time component")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"now must be a valid ISO8601 datetime: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


__all__ = [
    "MAX_LOCATION_CANDIDATES",
    "MAX_LOCATION_RADIUS_KM",
    "MAX_REVIEW_SUMMARY_CHARS",
    "LocationOrigin",
    "LocationQuery",
    "PlaceCandidate",
]
