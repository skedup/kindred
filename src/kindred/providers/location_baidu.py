"""``BaiduLocationProvider`` —— 真实地图 Provider（百度地图 Place API v2，L4）。

参考：
- 圆形区域检索 https://lbsyun.baidu.com/docs/webapi?title=placev2/guide/webservice-placeapi/circle
- 地点详情检索 https://lbsyun.baidu.com/docs/webapi?title=placev2/guide/webservice-placeapi/detail
  （v1 不用：圆形检索 ``scope=2`` 已带 detail_info——评分/营业时间/评论数够渲染候选；
  详情端点留给未来按 uid 精查的场景）
- Geocoding v3（origin 地址 → 坐标）https://lbsyun.baidu.com/faq/api?title=webapi/guide/webservice-geocoding

设计要点
========

- **坐标系一律 GCJ-02**（选型拍板）：geocoding ``ret_coordtype=gcj02ll`` 出 GCJ-02
  圆心；圆形检索 ``coord_type=2`` 声明输入是 GCJ-02、``ret_coordtype=gcj02ll`` 让
  POI 坐标也返 GCJ-02。百度默认的 BD-09 不落任何一处。
- **origin 解析**：``LocationOrigin`` 带 lat/lng 直接用（约定即 GCJ-02）；否则
  geocoding v3 按 ``address``（回落 ``city+name``）解析，实例级小缓存（同一
  origin 每 tick 重查太浪费；daemon 长持 provider 实例，缓存跨 tick 生效）。
- **query 映射**：百度 query 是关键词检索（string(45)，多关键词 ``$`` 分隔），不吃
  语义长句。binding 的 ``categories`` 经 ``_CATEGORY_QUERY_CN`` 映射成中文关键词
  （food→美食）；无 categories 才退用 ``LocationQuery.query`` 原文（截 45 字符）。
  语义句子本来就渲染给心看，百度只要关键词。
- **失败不降级 virtual**（RESUME 拍板，与 weather 不同）：编造地点混进真实世界比
  没有候选更糟。任何失败（HTTP / status!=0 / 解析）抛 ``LocationProviderError``，
  由 act 层渲染「查询失败」prompt 降级（earlier review 语义）。
- **凭据走 env**（``BAIDU_MAP_AK`` + SN 校验型应用另配 ``BAIDU_MAP_SK``），
  永不进日志 / 异常文案。SN 签名算法照百度官方样例：
  ``md5(quote_plus(quote(path?query, safe=...) + sk))``，签名串与线上 URL 逐字节
  一致（自行预编码 URL，不交给 httpx 重编码）。
"""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus

from kindred.location.models import (
    LocationOrigin,
    LocationQuery,
    PlaceCandidate,
)
from kindred.providers.location import LocationProviderError

if TYPE_CHECKING:
    import httpx

_LOG = logging.getLogger(__name__)

ENV_BAIDU_MAP_AK = "BAIDU_MAP_AK"
"""百度地图开放平台 ak 的 env 变量名（factory 装配时读取）。"""

ENV_BAIDU_MAP_SK = "BAIDU_MAP_SK"
"""SN 校验型应用的 sk（签名密钥）env 变量名；IP 白名单型应用不需要。"""

_BASE_URL = "https://api.map.baidu.com"
_GEOCODING_PATH = "/geocoding/v3/"
_PLACE_SEARCH_PATH = "/place/v2/search"
_TIMEZONE_PATH = "/timezone/v1"

# 百度官方 SN 样例的 safe 字符集（这些字符不参与 %XX 编码）。
_SN_QUOTE_SAFE = "/:=&?#+!$,;'@()*[]"

# GCJ-02（国测局坐标）：百度 API 的输入坐标类型枚举值 2 / 输出坐标类型串。
_COORD_TYPE_GCJ02 = "2"
_RET_COORDTYPE_GCJ02 = "gcj02ll"

# 百度 query 上限（docs: string(45)）与多关键字分隔符。
_QUERY_MAX_CHARS = 45
_QUERY_SEPARATOR = "$"

# binding categories → 百度检索关键词（中文分类词召回最稳）。词表以百度官方
# POI 行业分类为依据：
# https://lbsyun.baidu.com/index.php?title=open%2Fpoitags
# 未收录的 category 原文透传（中文分类或普通地点关键词都可直接检索）。
_CATEGORY_QUERY_CN = {
    "food": "美食",
    "restaurant": "美食",
    "cafe": "咖啡厅",
    "bar": "酒吧",
    "bakery": "蛋糕甜品店",
    "shopping": "购物",
    "convenience_store": "便利店",
    "market": "市场",
    "life_service": "生活服务",
    "beauty": "丽人",
    "tourist_attraction": "旅游景点",
    "park": "公园",
    "museum": "博物馆",
    "scenic_spot": "风景区",
    "leisure": "休闲娱乐",
    "bookstore": "书店",
    "shopping_mall": "购物中心",
    "cinema": "电影院",
    "theater": "剧院",
    "ktv": "ktv",
    "sports": "运动健身",
    "sports_venue": "体育场馆",
    "gym": "健身中心",
    "education": "教育培训",
    "library": "图书馆",
    "culture": "文化传媒",
    "art_gallery": "美术馆",
    "exhibition_hall": "展览馆",
    "medical": "医疗",
    "hospital": "综合医院",
    "clinic": "诊所",
    "pharmacy": "药店",
    "automotive": "汽车服务",
    "transport": "交通设施",
    "airport": "飞机场",
    "train_station": "火车站",
    "subway_station": "地铁站",
    "bus_station": "公交车站",
    "parking": "停车场",
    "finance": "金融",
    "bank": "银行",
    "residential": "住宅区",
    "residential_area": "住宅区",
    "office_building": "写字楼",
    "company": "公司企业",
    "industrial_park": "园区",
    "government": "政府机构",
    "entrance": "出入口",
    "natural_feature": "自然地物",
    "mountain": "山峰",
    "water": "水系",
    "road": "道路",
    "railway": "铁路",
    "supermarket": "超市",
    "hotel": "酒店",
    "lodging": "酒店",
    "pedestrian_area": "步行街",
    "public_square": "休闲广场",
}
_KNOWN_PLACE_CATEGORIES = frozenset({"home"})

# ── Provider taxonomy → Kindred canonical 粗类型（earlier review N-2）────────────────
# PlaceCandidate.type 会被 LLM 照抄、经 arrival 事件落进 state.location.type /
# destination plan——百度的 cater/life 行业枚举不该外泄进统一状态层。canonical
# 词汇对齐 kindred.location.categories；VirtualLocationProvider 只实现其中一个
# 窄子集。百度原始标签仍完整留在 tags。
# 中文分类关键词按序匹配（细→粗：咖啡在美食前——百度把咖啡厅归在美食大类下）。
_CN_TYPE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("咖啡", "cafe"),
    ("蛋糕甜品", "bakery"),
    ("酒吧", "bar"),
    ("美食", "restaurant"),
    ("餐厅", "restaurant"),
    ("小吃", "restaurant"),
    ("便利店", "convenience_store"),
    ("超市", "supermarket"),
    ("购物中心", "shopping_mall"),
    ("百货商场", "shopping_mall"),
    ("市场", "market"),
    ("书店", "bookstore"),
    ("购物", "shopping_mall"),
    ("商场", "shopping_mall"),
    ("生活服务", "life_service"),
    ("丽人", "beauty"),
    ("美容", "beauty"),
    ("美发", "beauty"),
    ("公园", "park"),
    ("博物馆", "museum"),
    ("风景区", "scenic_spot"),
    ("旅游景点", "tourist_attraction"),
    ("景点", "tourist_attraction"),
    ("步行街", "pedestrian_area"),
    ("休闲广场", "public_square"),
    ("电影院", "cinema"),
    ("剧院", "theater"),
    ("KTV", "ktv"),
    ("ktv", "ktv"),
    ("休闲娱乐", "leisure"),
    ("体育场馆", "sports_venue"),
    ("健身中心", "gym"),
    ("运动健身", "sports"),
    ("图书馆", "library"),
    ("教育培训", "education"),
    ("美术馆", "art_gallery"),
    ("展览馆", "exhibition_hall"),
    ("文化传媒", "culture"),
    ("综合医院", "hospital"),
    ("专科医院", "hospital"),
    ("诊所", "clinic"),
    ("药店", "pharmacy"),
    ("医疗", "medical"),
    ("汽车服务", "automotive"),
    ("飞机场", "airport"),
    ("火车站", "train_station"),
    ("地铁站", "subway_station"),
    ("公交车站", "bus_station"),
    ("停车场", "parking"),
    ("交通设施", "transport"),
    ("银行", "bank"),
    ("金融", "finance"),
    ("住宅区", "residential_area"),
    ("写字楼", "office_building"),
    ("房地产", "residential"),
    ("园区", "industrial_park"),
    ("公司企业", "company"),
    ("政府机构", "government"),
    ("出入口", "entrance"),
    ("山峰", "mountain"),
    ("自然地物", "natural_feature"),
    ("水系", "water"),
    ("道路", "road"),
    ("铁路", "railway"),
    ("酒店", "hotel"),
    ("宾馆", "hotel"),
)
# 中文标签没匹配到时退行业枚举（cater/hotel 语义明确；life 太宽不映射）。
_INDUSTRY_TYPE_CANONICAL = {"cater": "restaurant", "hotel": "hotel"}

# 分页（earlier review N-1）：radius_min 过滤发生在客户端，单页取 limit 条可能被全数滤掉
# ——min>0 时按服务端单页上限 over-fetch 并分页，直到拿满或确认没有更多；页数
# 有界（配额防失控：至多 _MAX_PAGES × _PAGE_SIZE_MAX = 60 条）。
_PAGE_SIZE_MAX = 20  # 百度文档：单次召回最大 20 条
_MAX_PAGES = 3

# geocode 实例缓存上限（FIFO 淘汰）；origin 通常只有一两个，32 绰绰有余。
_GEOCODE_CACHE_MAX = 32


class BaiduLocationProvider:
    """真实地点候选：百度地图圆形区域检索（``scope=2`` 带详情），坐标 GCJ-02。

    满足 ``LocationProvider`` Protocol。所有失败抛 ``LocationProviderError``
    ——**不降级 virtual**，act 层按「查询失败」提示降级。

    ``sk`` 非空时启用 SN 签名（SN 校验型应用）；IP 白名单型应用留空即可。
    """

    def __init__(self, *, ak: str, sk: str = "", timeout_s: float = 5.0) -> None:
        if not ak or not ak.strip():
            raise ValueError("BaiduLocationProvider: ak 不能为空（走 env BAIDU_MAP_AK）")
        self._ak = ak.strip()
        self._sk = sk.strip()
        self._timeout_s = timeout_s
        self._geocode_cache: dict[str, tuple[float, float]] = {}

    # ── LocationProvider Protocol ─────────────────────────────

    def find_nearby(self, query: LocationQuery) -> tuple[PlaceCandidate, ...]:
        if not query.origin.is_queryable():
            raise LocationProviderError("location origin is not queryable")
        lat, lng = self._resolve_origin(query.origin)

        # 服务端 radius_limit=true 只卡 max 半径；min 半径百度不支持，客户端过滤。
        # N-1（earlier review）：min>0 时「出远门」候选可能不在第一页——按单页上限 over-fetch
        # 并分页，直到 post-filter 拿满 limit 或确认没有更多；页数有界防配额失控。
        page_size = _PAGE_SIZE_MAX if query.radius_min_km > 0 else query.limit
        collected: dict[str, PlaceCandidate] = {}  # 按 place_key 去重（跨页稳定性防御）
        for page_num in range(_MAX_PAGES):
            results = self._circle_search(
                query, lat=lat, lng=lng, page_size=page_size, page_num=page_num
            )
            for candidate in _parse_candidates(results, origin_lat=lat, origin_lng=lng):
                if candidate.distance_km < query.radius_min_km:
                    continue
                collected.setdefault(candidate.place_key, candidate)
            if len(collected) >= query.limit or len(results) < page_size:
                break  # 拿满了，或这一页未满 = 没有下一页
        return tuple(list(collected.values())[: query.limit])

    def resolve_home(self, address: str, *, at: datetime) -> tuple[str, str, str]:
        """安装期一次性解析规范地址、城市和 IANA 时区。"""
        data = self._get_json(
            _GEOCODING_PATH,
            {
                "address": address,
                "output": "json",
                "ak": self._ak,
                "ret_coordtype": _RET_COORDTYPE_GCJ02,
                "extension_poi_infos": "true",
            },
            op="geocoding",
        )
        result = data.get("result")
        location = result.get("location") if isinstance(result, dict) else None
        poi_infos = data.get("poi_infos")
        place = poi_infos[0] if isinstance(poi_infos, list) and poi_infos else None
        if not isinstance(location, dict) or not isinstance(place, dict):
            raise LocationProviderError("geocoding response is incomplete")
        city, canonical = place.get("city"), place.get("formatted_address")
        if not isinstance(city, str) or not city.strip():
            raise LocationProviderError("geocoding city is unavailable")
        if not isinstance(canonical, str) or not canonical.strip():
            canonical = address
        timezone_data = self._get_json(
            _TIMEZONE_PATH,
            {
                "location": f"{location['lat']},{location['lng']}",
                "timestamp": str(int(at.timestamp())),
                "ak": self._ak,
            },
            op="timezone",
        )
        timezone_id = timezone_data.get("timezone_id")
        if not isinstance(timezone_id, str) or not timezone_id.strip():
            raise LocationProviderError("timezone response is incomplete")
        return canonical.strip(), city.strip(), timezone_id.strip()

    # ── origin 解析（geocoding v3 + 实例缓存）─────────────────

    def _resolve_origin(self, origin: LocationOrigin) -> tuple[float, float]:
        if origin.lat is not None and origin.lng is not None:
            return (origin.lat, origin.lng)
        address = origin.address or f"{origin.city}{origin.name}".strip()
        if not address:
            raise LocationProviderError(
                "origin 无坐标且无可 geocode 的 address/name（Baidu 圆形检索需要圆心）"
            )
        cache_key = f"{address}|{origin.city}"
        cached = self._geocode_cache.get(cache_key)
        if cached is not None:
            return cached
        latlng = self._geocode(address, city=origin.city)
        if len(self._geocode_cache) >= _GEOCODE_CACHE_MAX:
            self._geocode_cache.pop(next(iter(self._geocode_cache)))
        self._geocode_cache[cache_key] = latlng
        return latlng

    def _geocode(self, address: str, *, city: str) -> tuple[float, float]:
        params: dict[str, str] = {
            "address": address,
            "output": "json",
            "ak": self._ak,
            "ret_coordtype": _RET_COORDTYPE_GCJ02,
        }
        if city:
            params["city"] = city
        data = self._get_json(_GEOCODING_PATH, params, op="geocoding")
        result = data.get("result")
        location = result.get("location") if isinstance(result, dict) else None
        if not isinstance(location, dict):
            raise LocationProviderError(
                f"baidu geocoding 响应缺 result.location（address={address!r}）"
            )
        try:
            lat, lng = float(location["lat"]), float(location["lng"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocationProviderError(
                f"baidu geocoding 坐标不可解析（address={address!r}）: {exc}"
            ) from exc
        _LOG.debug("baidu geocoding ok address=%s -> gcj02(%.5f,%.5f)", address, lat, lng)
        return (lat, lng)

    # ── 圆形区域检索 ──────────────────────────────────────────

    def _circle_search(
        self,
        query: LocationQuery,
        *,
        lat: float,
        lng: float,
        page_size: int,
        page_num: int,
    ) -> list[Any]:
        params = {
            "query": _build_baidu_query(query),
            "location": f"{lat},{lng}",
            "radius": str(int(query.radius_max_km * 1000)),
            "radius_limit": "true",
            "scope": "2",
            "coord_type": _COORD_TYPE_GCJ02,
            "ret_coordtype": _RET_COORDTYPE_GCJ02,
            "page_size": str(page_size),
            "page_num": str(page_num),
            "output": "json",
            "ak": self._ak,
        }
        data = self._get_json(_PLACE_SEARCH_PATH, params, op="place search")
        results = data.get("results")
        if not isinstance(results, list):
            raise LocationProviderError("baidu place search 响应缺 results 列表")
        return results

    # ── HTTP + SN 签名 + 公共校验 ─────────────────────────────

    def _get_json(self, path: str, params: dict[str, str], *, op: str) -> dict[str, Any]:
        """GET + （可选）SN 签名 + JSON 解析 + 百度 status 校验。异常/日志永不带 ak/sk。

        URL 自行预编码（``_signed_query``），**不交给 httpx 重编码**——SN 是对
        编码后 query 串逐字节签名的，任何二次编码差异都会 211（APP SN 校验失败）。
        """
        query = self._signed_query(path, params)
        try:
            resp = self._http_get(f"{_BASE_URL}{path}?{query}")
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - 统一收口成 Provider 错误（不降级 virtual）
            raise LocationProviderError(f"baidu {op} 请求失败: {type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise LocationProviderError(f"baidu {op} 返回非 JSON 对象")
        status = data.get("status")
        if status != 0:
            # message 是百度侧错误说明（如配额/AK 校验失败），不含我们的 ak/sk。
            raise LocationProviderError(
                f"baidu {op} 失败: status={status} message={data.get('message')!r}"
            )
        return data

    def _http_get(self, url: str) -> httpx.Response:
        """网络出口单点（测试 monkeypatch 这里，URL 已含签名后的完整 query）。"""
        import httpx

        return httpx.get(url, timeout=self._timeout_s)

    def _signed_query(self, path: str, params: dict[str, str]) -> str:
        """按官方算法编码 query；配了 sk 则追加 ``sn``（必须是最后一个参数）。

        官方 Python 样例：对**原始** ``path?k=v&...`` 串整体
        ``quote(raw, safe="/:=&?#+!$,;'@()*[]")`` 一次（& = ? 等分隔符在 safe 集里保留，
        中文/空格转 %XX），再 ``sn = md5(quote_plus(encoded + sk))``。线上请求的
        query 必须与参与签名的编码串逐字节一致——所以这里自己编码、不让 httpx 重编码，
        也**不能**对已编码值二次 quote（% 会被转成 %25，必 211）。

        参数值不得含 ``&`` / ``=`` / ``#``（会与分隔符/fragment 歧义，且这些字符在
        官方 safe 集里不参与编码、无法靠 quote 区分）——含则 fail-fast 拒绝构造
        歧义请求（address / 关键词 / 坐标正常都满足；``$`` 是多关键词分隔符，合法）。
        """
        for key, value in params.items():
            if any(ch in value for ch in "&=#"):
                raise LocationProviderError(
                    f"baidu 参数 {key!r} 含保留分隔符（&/=/#），拒绝构造歧义请求"
                )
        raw_query = "&".join(f"{key}={value}" for key, value in params.items())
        encoded_query = quote(raw_query, safe=_SN_QUOTE_SAFE)
        if not self._sk:
            return encoded_query
        to_sign = f"{path}?{encoded_query}{self._sk}"  # path 只含 safe 字符，无需再编码
        sn = hashlib.md5(  # noqa: S324 - 百度 SN 规定 md5
            quote_plus(to_sign).encode("utf-8")
        ).hexdigest()
        return f"{encoded_query}&sn={sn}"


# ─────────────────────────────────────────────────────────────────────
# query / 响应映射（模块级纯函数，可独立测）
# ─────────────────────────────────────────────────────────────────────


def _build_baidu_query(query: LocationQuery) -> str:
    """binding categories → 百度关键词（``$`` 并集）；无 categories 退用语义原文截断。"""
    if query.categories:
        keywords = list(dict.fromkeys(k for c in query.categories if (k := _category_keyword(c))))
        if keywords:
            return _QUERY_SEPARATOR.join(keywords[:10])
    return query.query[:_QUERY_MAX_CHARS]


def _category_keyword(category: str) -> str:
    normalized = category.lower().replace("-", "_")
    if normalized in _KNOWN_PLACE_CATEGORIES:
        return ""
    return _CATEGORY_QUERY_CN.get(normalized, category)


def _parse_candidates(
    results: list[Any],
    *,
    origin_lat: float,
    origin_lng: float,
) -> list[PlaceCandidate]:
    """百度 POI 结果 → ``PlaceCandidate[]``。缺 uid/name/坐标的条目跳过（记 debug）。"""
    candidates: list[PlaceCandidate] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        uid = _text(entry.get("uid"))
        name = _text(entry.get("name"))
        location = entry.get("location")
        if not uid or not name or not isinstance(location, dict):
            _LOG.debug("baidu candidate skipped: missing required fields")
            continue
        try:
            lat, lng = float(location["lat"]), float(location["lng"])
        except (KeyError, TypeError, ValueError):
            _LOG.debug("baidu candidate skipped: invalid coordinates")
            continue

        detail = entry.get("detail_info")
        detail = detail if isinstance(detail, dict) else {}
        distance_km = _distance_km(
            entry, detail, lat=lat, lng=lng, origin_lat=origin_lat, origin_lng=origin_lng
        )
        candidates.append(
            PlaceCandidate(
                name=name,
                type=_candidate_type(entry, detail),
                address=_text(entry.get("address")),
                distance_km=distance_km,
                source="baidu",
                place_key=f"baidu:{uid}",
                provider_place_id=uid,
                lat=lat,
                lng=lng,
                city=_text(entry.get("city")),
                district=_text(entry.get("area")),
                rating=_rating(detail.get("overall_rating")),
                review_count=_int_or_none(detail.get("comment_num")),
                opening_hours_summary=_text(detail.get("shop_hours")),
                tags=_candidate_tags(entry, detail),
            )
        )
    return candidates


def _candidate_type(entry: dict[str, Any], detail: dict[str, Any]) -> str:
    """Provider taxonomy → Kindred canonical 粗类型（earlier review N-2）。

    ``PlaceCandidate.type`` 会被 LLM 照抄、随 arrival 事件落进
    ``state.location.type`` / destination plan——百度的 ``cater/life`` 行业枚举
    不外泄进统一状态层。优先中文分类标签关键词（更细、语义准），退行业枚举映射
    （cater→restaurant / hotel→hotel；life 太宽不映射），兜底 ``place``。
    原始标签完整保留在 ``tags``（快照/展示不丢信息）。
    """
    labels = ";".join(
        _text(v)
        for v in (entry.get("classified_poi_tag"), detail.get("tag"), entry.get("tag"))
        if _text(v)
    )
    for keyword, canonical in _CN_TYPE_KEYWORDS:
        if keyword in labels:
            return canonical
    for value in (detail.get("type"), entry.get("type")):
        mapped = _INDUSTRY_TYPE_CANONICAL.get(_text(value).lower())
        if mapped:
            return mapped
    return "place"


def _candidate_tags(entry: dict[str, Any], detail: dict[str, Any]) -> tuple[str, ...]:
    raw = ";".join(
        _text(v)
        for v in (entry.get("classified_poi_tag"), detail.get("tag"), detail.get("content_tag"))
        if _text(v)
    )
    parts = [p.strip() for p in raw.replace("；", ";").split(";")]
    return tuple(dict.fromkeys(p for p in parts if p))[:6]


def _distance_km(
    entry: dict[str, Any],
    detail: dict[str, Any],
    *,
    lat: float,
    lng: float,
    origin_lat: float,
    origin_lng: float,
) -> float:
    """distance 优先取百度返回（米）；缺失时按坐标 haversine 兜底。"""
    for value in (detail.get("distance"), entry.get("distance")):
        meters = _int_or_none(value)
        if meters is not None:
            return round(meters / 1000.0, 2)
    return round(_haversine_km(origin_lat, origin_lng, lat, lng), 2)


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_earth_km = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius_earth_km * math.asin(math.sqrt(a))


def _rating(value: Any) -> float | None:
    """百度 overall_rating 是字符串（"4.5"）；非法/超界返 None（不硬塞进 0-5 校验）。"""
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    return rating if 0.0 <= rating <= 5.0 else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = ["ENV_BAIDU_MAP_AK", "BaiduLocationProvider"]
