"""Provider 中立的地点粗分类与地点类型关系。"""

# category 是检索意图提示，type 是候选被选中后进入 State 的粗类型。这里保持开放：
# 未收录的 category 默认只接受同名 type，避免把地图供应商的完整 taxonomy 固化为
# Kindred schema。
_LOCATION_TYPES_BY_CATEGORY: dict[str, frozenset[str]] = {
    "food": frozenset({"food", "restaurant", "cafe", "bar", "bakery"}),
    "dining": frozenset({"restaurant", "cafe", "bar"}),
    "cater": frozenset({"restaurant"}),
    "lodging": frozenset({"lodging", "hotel"}),
    "shopping": frozenset(
        {"shopping", "shopping_mall", "supermarket", "convenience_store", "market", "bookstore"}
    ),
    "tourist_attraction": frozenset({"tourist_attraction", "park", "museum", "scenic_spot"}),
    "leisure": frozenset(
        {"leisure", "cinema", "theater", "ktv", "public_square", "pedestrian_area"}
    ),
    "sports": frozenset({"sports", "sports_venue", "gym"}),
    "education": frozenset({"education", "library"}),
    "culture": frozenset({"culture", "art_gallery", "exhibition_hall"}),
    "medical": frozenset({"medical", "hospital", "clinic", "pharmacy"}),
    "transport": frozenset(
        {
            "transport",
            "airport",
            "train_station",
            "subway_station",
            "bus_station",
            "parking",
        }
    ),
    "finance": frozenset({"finance", "bank"}),
    "residential": frozenset({"residential", "residential_area", "office_building"}),
    "company": frozenset({"company", "industrial_park"}),
    "natural_feature": frozenset({"natural_feature", "mountain", "water"}),
    "home": frozenset({"home"}),
    "hotel": frozenset({"hotel", "lodging"}),
}


def normalized_category(category: str) -> str:
    return category.strip().lower().replace("-", "_")


def location_types_for_category(category: str) -> frozenset[str]:
    """返回粗 category 可接受的 canonical location types。"""

    normalized = normalized_category(category)
    return _LOCATION_TYPES_BY_CATEGORY.get(normalized, frozenset({normalized}))


__all__ = ["location_types_for_category", "normalized_category"]
