"""``LocationProvider`` —— 地点 affordance 的世界事实源。

参考：``docs/archive/discussions/2026-07/2026-07-01-location-provider-contract.md`` /
``docs/archive/discussions/2026-07/2026-07-01-location-query-candidate-contract.md``。

设计要点
========

- Provider 只回答「以这个 origin 和 query 看，世界里有哪些可去的地点？」。
- Provider 不读取 SOUL / memory / character preference，不判断 ta 要不要去。
- 候选列表只是 act prompt 的局部上下文；未选即丢弃，选中后仍由 T3.persist 写 state。
- ``PlaceCandidate`` 等中立模型位于 ``kindred.location.models``；本文件只持有
  Provider 协议、错误与虚拟实现。
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol, runtime_checkable

from kindred.location.models import LocationQuery, PlaceCandidate

# ─────────────────────────────────────────────────────────────────────
# 契约
# ─────────────────────────────────────────────────────────────────────


class LocationProviderError(Exception):
    """Provider 拉取地点候选失败（上游错误、origin 不可查询或结果不可解析）。"""


@runtime_checkable
class LocationProvider(Protocol):
    """地点 affordance Provider 契约：按 origin + query 拉取世界事实候选。"""

    def find_nearby(self, query: LocationQuery) -> tuple[PlaceCandidate, ...]: ...


# ─────────────────────────────────────────────────────────────────────
# VirtualLocationProvider —— 纯虚拟（无网络、确定性）
# ─────────────────────────────────────────────────────────────────────


_TYPE_KEYWORDS = {
    "restaurant": ("restaurant", "dining", "dine", "food", "meal", "吃", "饭", "餐", "馆"),
    "cafe": ("cafe", "coffee", "咖啡", "写东西", "坐下来"),
    "park": ("park", "walk", "公园", "散步", "绿地", "湖"),
    "bookstore": ("book", "bookstore", "书", "书店", "阅读"),
    "shopping_mall": ("mall", "shopping", "商场", "购物"),
    "lodging": (
        "lodging",
        "hotel",
        "rest",
        "sleep",
        "bed",
        "休息",
        "补觉",
        "睡",
        "躺",
        "酒店",
        "旅馆",
    ),
}

_VIRTUAL_TEMPLATES: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "restaurant": (
        ("一碗清汤", "安静的小馆，适合吃点清淡的东西", ("清淡", "安静")),
        ("转角小食堂", "家常口味，离主路不远", ("家常", "方便")),
        ("晚风面屋", "热汤面和简单小菜", ("热汤", "夜间")),
        ("榕树下餐室", "座位不多，节奏偏慢", ("慢节奏", "小店")),
    ),
    "cafe": (
        ("窗边咖啡", "靠窗座位较多，适合坐一会儿", ("靠窗", "可停留")),
        ("白噪音咖啡", "背景声轻，适合写东西", ("安静", "写作")),
        ("街角烘焙咖啡", "有简单面包和热饮", ("烘焙", "热饮")),
        ("二楼小咖啡", "入口不显眼，人流少", ("隐蔽", "少人")),
    ),
    "park": (
        ("海风步道", "开阔，傍晚风比较明显", ("开阔", "风")),
        ("口袋公园", "面积不大，适合短暂停留", ("短途", "绿地")),
        ("湖边慢行道", "绕水面一圈，视野柔和", ("湖边", "散步")),
        ("树影广场", "树荫多，白天更舒服", ("树荫", "白天")),
    ),
    "bookstore": (
        ("灯下书店", "书架密，座位少", ("阅读", "安静")),
        ("慢翻书局", "新书和杂志多一些", ("杂志", "可逛")),
        ("旧页书房", "二手书为主，节奏很慢", ("二手书", "慢节奏")),
        ("沿街书店", "交通方便，停留时间灵活", ("方便", "短停留")),
    ),
    "shopping_mall": (
        ("潮汐广场", "餐饮和商店集中，人流偏多", ("商场", "便利")),
        ("中央生活城", "室内动线清楚，适合雨天", ("室内", "雨天")),
        ("南侧商业街", "店铺分散，需要多走几步", ("街区", "可逛")),
        ("天台市集", "有露台，天气好时更舒服", ("露台", "市集")),
    ),
    "lodging": (
        ("安静客房", "能躺下好好睡一觉，环境偏安静", ("可躺下", "安静")),
        ("夜间小旅宿", "临时休息方便，适合补觉", ("旅宿", "补觉")),
        ("小憩房间", "适合短暂停靠，把身体缓回来", ("短休", "恢复")),
        ("柔光休息室", "灯光柔和，人流少", ("柔和", "少人")),
    ),
    "place": (
        ("半日停靠点", "适合临时改变节奏", ("临时", "可停留")),
        ("安静角落", "不太醒目，但能短暂缓冲", ("安静", "缓冲")),
        ("附近去处", "距离适中，没有强烈功能属性", ("附近", "普通")),
        ("小小出口", "适合从当前环境里挪开一点", ("离开", "轻量")),
    ),
}


class VirtualLocationProvider:
    """纯虚拟地点候选：由 ``LocationQuery`` 确定性派生，无网络、可独立验。

    它用于本地开发和缺省世界，不伪装成真实地图结果：所有候选 ``source`` 都是
    ``"virtual"``，``place_key`` 也使用 ``virtual:`` 前缀。
    """

    def find_nearby(self, query: LocationQuery) -> tuple[PlaceCandidate, ...]:
        if not query.origin.is_queryable():
            raise LocationProviderError("location origin is not queryable")

        place_type = _infer_place_type(query)
        templates = _VIRTUAL_TEMPLATES.get(place_type, _VIRTUAL_TEMPLATES["place"])
        seed = _stable_int(
            "|".join(
                (
                    query.origin.name,
                    query.origin.address,
                    query.origin.city,
                    query.query,
                    ",".join(query.categories),
                    query.locale,
                )
            )
        )
        offset = seed % len(templates)
        count = min(query.limit, len(templates))
        city = query.origin.city or _city_from_address(query.origin.address) or "附近"

        candidates: list[PlaceCandidate] = []
        for idx in range(count):
            name, summary, template_tags = templates[(offset + idx) % len(templates)]
            address = _virtual_address(city, name)
            distance = _distance_for_index(query, idx, count, seed)
            place_key = _virtual_place_key(name=name, city=city, place_type=place_type)
            tags = tuple(dict.fromkeys((*query.categories, *template_tags)))
            candidates.append(
                PlaceCandidate(
                    name=name,
                    type=place_type,
                    address=address,
                    distance_km=distance,
                    source="virtual",
                    place_key=place_key,
                    provider_place_id=place_key.removeprefix("virtual:"),
                    city=city,
                    timezone=query.origin.timezone,
                    rating=round(3.8 + ((_stable_int(place_key) % 12) / 10), 1),
                    review_count=20 + (_stable_int(f"{place_key}:reviews") % 180),
                    review_summary=summary,
                    price_level=1 + (_stable_int(f"{place_key}:price") % 4),
                    open_now=True,
                    opening_hours_summary="虚拟世界中当前可去",
                    tags=tags,
                    confidence=1.0,
                )
            )
        return tuple(candidates)


def _infer_place_type(query: LocationQuery) -> str:
    for category in query.categories:
        normalized = category.lower().replace("-", "_")
        if normalized in _VIRTUAL_TEMPLATES:
            return normalized

    text = f"{query.query} {' '.join(query.categories)}".lower()
    for place_type, keywords in _TYPE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return place_type
    return "place"


def _distance_for_index(query: LocationQuery, index: int, count: int, seed: int) -> float:
    span = query.radius_max_km - query.radius_min_km
    if span == 0:
        return round(query.radius_min_km, 2)
    base = query.radius_min_km + span * ((index + 1) / (count + 1))
    jitter = (((seed >> (index % 16)) & 0xF) / 15.0 - 0.5) * min(span * 0.12, 0.4)
    distance = min(query.radius_max_km, max(query.radius_min_km, base + jitter))
    return round(distance, 2)


def _virtual_address(city: str, place_name: str) -> str:
    return f"{city} · 虚拟街区 · {place_name}"


def _city_from_address(address: str) -> str:
    for marker in ("市", "City"):
        if marker in address:
            return address.split(marker, 1)[0] + marker
    return ""


def _virtual_place_key(*, name: str, city: str, place_type: str) -> str:
    slug = _slugify(name) or place_type
    digest = hashlib.sha256(f"{city}|{place_type}|{name}".encode()).hexdigest()[:8]
    return f"virtual:{slug}-{digest}"


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:40]


def _stable_int(value: str) -> int:
    digest = hashlib.sha256(value.encode()).hexdigest()
    return int(digest[:8], 16)


__all__ = [
    "LocationProvider",
    "LocationProviderError",
    "VirtualLocationProvider",
]
