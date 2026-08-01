"""``EnvironmentProvider`` —— 天气（世界 Provider 层第一块砖 + 丰富化）。

参考：``docs/02-state-system.md`` §3.4 / ``docs/10-providers.md`` /
``docs/discussions/2026-06-29-breathable-world-event-provider.md`` §7。

设计要点
========

- **产 weather/temperature + 体感字段（feels_like / humidity / wind / moon_phase /
  sunrise / sunset / uv_index / precip_mm）**，但**不产 ambience**——``ambience``（氛围）
  是 ta *自己描述*的、不走 Provider（①被给予性铁律：世界给的 vs 心的解读硬分开）。
- **pull + in-tick TTL**（§7 Q3）：Provider 不管缓存，刷新判断（地点变 或 TTL 过期）在
  ``sense_io``。
- **降级**（§3.4）：真实 Provider（wttr）失败 → ``FallbackEnvironmentProvider`` 降级回
  ``VirtualEnvironmentProvider``；sense_io 再兜一层软心。
- **精确到区**：查询位置（``world.weather_location`` 或回落 ``city``）由 ``sense_io`` 解析后
  传入 ``get_weather(location)``——Provider 不持地点、给啥查啥（如 ``"shenzhen+nanshan"``）。

两种实现
========

- ``VirtualEnvironmentProvider``：纯虚拟，由 (location, 日期, 时辰) 确定性派生（月相按真实
  天文算），无网络、可独立验。
- ``WttrEnvironmentProvider``：真实，走 wttr.in 的 j1 JSON（免 API key，字段最全）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable

_LOG = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# 契约
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WeatherReading:
    """世界给的天气事实——**不含 ambience**（那是 ta 自己描述，不走 Provider）。

    ``weather`` + ``temperature`` 是核心；其余是体感/天象增强字段（缺测留默认值）。
    """

    weather: str  # 天气描述，如 "晴" / "多云" / "小雨"
    temperature: float  # 实际气温（摄氏度）
    feels_like: float | None = None  # 体感温度（摄氏度）；缺测时由上层回退实际气温
    humidity: int = 0  # 相对湿度（%）
    wind: str = ""  # 风（向 + 速），如 "东南风 11km/h"
    moon_phase: str = ""  # 月相（emoji + 名），如 "🌕 满月"
    sunrise: str = ""  # 日出时刻 HH:MM（本地）
    sunset: str = ""  # 日落时刻 HH:MM（本地）
    uv_index: int = 0  # UV 指数（0-12）
    precip_mm: float = 0.0  # 降水量（mm）


class EnvironmentProviderError(Exception):
    """Provider 拉取天气失败（网络/解析/上游错误）。由 factory 的 fallback 兜底降级。"""


@runtime_checkable
class EnvironmentProvider(Protocol):
    """环境 Provider 契约：用 ta 的 ``location``（city 或精确到区的查询串）查天气。

    失败语义：实现拉取失败应抛 ``EnvironmentProviderError``（或子类），交给上层降级；
    不要自己静默吞成假数据（破坏①被给予性——心会被假世界骗）。
    """

    def get_weather(self, location: str) -> WeatherReading: ...


# ─────────────────────────────────────────────────────────────────────
# 共享：月相（真实天文）/ 日出日落（季节近似）/ 安全解析
# ─────────────────────────────────────────────────────────────────────

# 朔望月长度（天）+ 一个已知朔（新月）参考时刻（2000-01-06 18:14 UTC）。
_SYNODIC_MONTH = 29.530588853
_KNOWN_NEW_MOON = _dt.datetime(2000, 1, 6, 18, 14, tzinfo=_dt.timezone.utc)
_MOON_PHASES = (
    ("🌑", "新月"),
    ("🌒", "蛾眉月"),
    ("🌓", "上弦月"),
    ("🌔", "盈凸月"),
    ("🌕", "满月"),
    ("🌖", "亏凸月"),
    ("🌗", "下弦月"),
    ("🌘", "残月"),
)


def _moon_phase(now: _dt.datetime) -> str:
    """由日期算月相（真实朔望周期）——世界自带的天象循环，满足②连续。"""
    days = (now.astimezone(_dt.timezone.utc) - _KNOWN_NEW_MOON).total_seconds() / 86400.0
    fraction = (days % _SYNODIC_MONTH) / _SYNODIC_MONTH
    emoji, name = _MOON_PHASES[round(fraction * 8) % 8]
    return f"{emoji} {name}"


def _fmt_hm(total_minutes: int) -> str:
    total_minutes %= 1440
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _sun_times(now: _dt.datetime) -> tuple[str, str]:
    """季节近似的日出/日落（~22°N）：夏至最早出、最晚落；冬至反之。"""
    doy = now.timetuple().tm_yday
    seasonal = math.cos(2 * math.pi * (doy - 172) / 365.0)  # 夏至(~172)取 1
    sunrise = _fmt_hm(int(390 - 45 * seasonal))  # 06:30 ∓ 45min
    sunset = _fmt_hm(int(1110 + 60 * seasonal))  # 18:30 ± 60min
    return sunrise, sunset


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────
# VirtualEnvironmentProvider —— 纯虚拟（无网络、确定性）
# ─────────────────────────────────────────────────────────────────────

# 北半球月份均温基线（摄氏，粗略四季曲线）。索引 0..11 = 1..12 月。
_MONTH_BASE_TEMP = (4.0, 6.0, 11.0, 17.0, 22.0, 26.0, 29.0, 28.0, 24.0, 18.0, 12.0, 6.0)
# 月份 UV 峰值（晴天正午），冬低夏高。
_MONTH_UV_PEAK = (4, 5, 7, 9, 11, 12, 12, 11, 9, 7, 5, 4)
# 天气候选（确定性挑选，由当天 seed 决定）。
_CONDITIONS = ("晴", "晴间多云", "多云", "阴", "小雨", "阵雨", "雷阵雨", "雾")
_RAIN_CONDITIONS = frozenset({"小雨", "阵雨", "雷阵雨"})
_WIND_DIRS = ("北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风")


class VirtualEnvironmentProvider:
    """纯虚拟天气：由 (location, 日期, 时辰) 确定性派生，无网络、开箱即用。

    满足①被给予性（心选不了天气）+ ②连续（随日期/时辰/月相变化）。可独立验：
    同 (location, 时刻) 必得同一 ``WeatherReading``（月相由真实天文算，不靠随机）。

    ``now_fn`` 可注入（默认本地当前时刻）。``location`` 由调用方（sense_io）解析好再传入
    （精确到区的查询串 or city），provider 不再自持 override。
    """

    def __init__(self, *, now_fn: Callable[[], _dt.datetime] | None = None) -> None:
        self._now = now_fn or _local_now

    def get_weather(self, location: str) -> WeatherReading:
        now = self._now()
        seed = _day_seed(location, now.date().isoformat())
        condition = _CONDITIONS[seed % len(_CONDITIONS)]
        is_rain = condition in _RAIN_CONDITIONS

        base = _MONTH_BASE_TEMP[now.month - 1]
        diurnal = 5.0 * math.sin((now.hour - 9) / 24.0 * 2.0 * math.pi)
        daily_offset = float(seed % 7) - 3.0
        temperature = round(base + diurnal + daily_offset, 1)

        # 湿度：雨天偏高（70-95），否则 35-80。由 seed 不同位段决定（同日稳定）。
        humidity = (70 + (seed >> 8) % 26) if is_rain else (35 + (seed >> 8) % 46)
        wind_dir = _WIND_DIRS[(seed >> 16) % len(_WIND_DIRS)]
        wind_level = 1 + (seed >> 20) % 5
        wind = f"{wind_dir} {wind_level}级"
        # 体感：湿热加成 + 风寒减成（简化确定性模型）。
        feels_like = round(temperature + (humidity - 60) * 0.05 - wind_level * 0.4, 1)
        precip_mm = round(float(seed % 15) + 0.5, 1) if is_rain else 0.0

        sunrise, sunset = _sun_times(now)
        moon = _moon_phase(now)
        uv = _virtual_uv(now)

        return WeatherReading(
            weather=condition,
            temperature=temperature,
            feels_like=feels_like,
            humidity=humidity,
            wind=wind,
            moon_phase=moon,
            sunrise=sunrise,
            sunset=sunset,
            uv_index=uv,
            precip_mm=precip_mm,
        )


def _virtual_uv(now: _dt.datetime) -> int:
    """UV：夜间 0，正午峰值（按季节）。"""
    hour = now.hour + now.minute / 60.0
    if hour <= 6 or hour >= 18:
        return 0
    daylight = math.sin(math.pi * (hour - 6) / 12.0)  # 0..1，正午峰
    return max(0, min(12, round(_MONTH_UV_PEAK[now.month - 1] * daylight)))


def _local_now() -> _dt.datetime:
    """本地时区的当前时刻（带 tzinfo）。"""
    return _dt.datetime.now(_dt.timezone.utc).astimezone()


def _day_seed(location: str, date_iso: str) -> int:
    """(location, 日期) → 稳定的 32-bit 种子。同地同日恒定，跨日/跨地会变。"""
    digest = hashlib.sha256(f"{location}|{date_iso}".encode()).hexdigest()
    return int(digest[:8], 16)


# ─────────────────────────────────────────────────────────────────────
# WttrEnvironmentProvider —— 真实（wttr.in j1，免 API key）
# ─────────────────────────────────────────────────────────────────────

# wttr.in 16 方位英文 → 中文。
_WTTR_DIR_CN = {
    "N": "北", "NNE": "东北偏北", "NE": "东北", "ENE": "东北偏东",
    "E": "东", "ESE": "东南偏东", "SE": "东南", "SSE": "东南偏南",
    "S": "南", "SSW": "西南偏南", "SW": "西南", "WSW": "西南偏西",
    "W": "西", "WNW": "西北偏西", "NW": "西北", "NNW": "西北偏北",
}  # fmt: skip
# wttr.in 月相英文 → (emoji, 中文)。
_WTTR_MOON_CN = {
    "New Moon": ("🌑", "新月"),
    "Waxing Crescent": ("🌒", "蛾眉月"),
    "First Quarter": ("🌓", "上弦月"),
    "Waxing Gibbous": ("🌔", "盈凸月"),
    "Full Moon": ("🌕", "满月"),
    "Waning Gibbous": ("🌖", "亏凸月"),
    "Last Quarter": ("🌗", "下弦月"),
    "Third Quarter": ("🌗", "下弦月"),
    "Waning Crescent": ("🌘", "残月"),
}


class WttrEnvironmentProvider:
    """真实天气：wttr.in（免 API key，全球）。

    走 ``j1`` JSON 端点（字段最全、避开 ``%`` 格式串转义坑），读核心 + 体感/天象字段。
    核心字段（temp / condition）缺失或网络失败 → 抛 ``EnvironmentProviderError``，由
    ``FallbackEnvironmentProvider`` 降级回虚拟；增强字段缺测则留默认、不致命。

    ``timeout_s``：单次拉取超时（秒）。``location`` 由调用方（sense_io）解析好再传入
    （精确到区的查询串 or city）。
    """

    _BASE_URL = "https://wttr.in/{location}"

    def __init__(self, *, timeout_s: float = 5.0) -> None:
        self._timeout_s = timeout_s

    def get_weather(self, location: str) -> WeatherReading:
        import httpx

        url = self._BASE_URL.format(location=location)
        try:
            resp = httpx.get(
                url,
                params={"format": "j1", "lang": "zh-cn"},
                timeout=self._timeout_s,
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 —— 统一收口成 Provider 错误，交上层降级
            msg = f"wttr.in 拉取失败（location={location}）: {exc}"
            raise EnvironmentProviderError(msg) from exc
        return _parse_wttr_j1(data, location)


def _parse_wttr_j1(data: Any, location: str) -> WeatherReading:
    """解析 wttr.in j1 JSON → WeatherReading。核心字段坏即抛；增强字段坏留默认。"""
    try:
        current = data["current_condition"][0]
        temperature = float(current["temp_C"])
        # wttr 中文天况：必须 lang=zh-cn（lang=zh 的 lang_zh 字段值仍是英文！），key 带连字符。
        lang_zh = current.get("lang_zh-cn") or current.get("lang_xx")
        if lang_zh:
            weather = str(lang_zh[0]["value"]).strip()
        else:
            weather = str(current["weatherDesc"][0]["value"]).strip()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        msg = f"wttr.in 响应形状异常（location={location}）: {exc}"
        raise EnvironmentProviderError(msg) from exc
    if not weather:
        raise EnvironmentProviderError(f"wttr.in 返回空 condition（location={location}）")

    feels_like = _safe_float(current.get("FeelsLikeC"), temperature)
    humidity = _safe_int(current.get("humidity"), 0)
    wind = _fmt_wttr_wind(current.get("winddir16Point"), current.get("windspeedKmph"))
    uv_index = _safe_int(current.get("uvIndex"), 0)
    precip_mm = _safe_float(current.get("precipMM"), 0.0)

    moon_phase, sunrise, sunset = "", "", ""
    try:
        astro = data["weather"][0]["astronomy"][0]
        moon_phase = _fmt_wttr_moon(astro.get("moon_phase"))
        sunrise = _to_24h(str(astro.get("sunrise", "")))
        sunset = _to_24h(str(astro.get("sunset", "")))
    except (KeyError, IndexError, TypeError):
        pass  # 天象段缺失不致命

    return WeatherReading(
        weather=weather,
        temperature=temperature,
        feels_like=feels_like,
        humidity=humidity,
        wind=wind,
        moon_phase=moon_phase,
        sunrise=sunrise,
        sunset=sunset,
        uv_index=uv_index,
        precip_mm=precip_mm,
    )


def _fmt_wttr_wind(direction: Any, speed_kmph: Any) -> str:
    if direction is None and speed_kmph is None:
        return ""
    dir_cn = _WTTR_DIR_CN.get(str(direction), str(direction)) if direction else ""
    suffix = "风" if dir_cn else ""
    speed = f"{_safe_int(speed_kmph, 0)}km/h" if speed_kmph is not None else ""
    return f"{dir_cn}{suffix} {speed}".strip()


def _fmt_wttr_moon(phase_en: Any) -> str:
    if not phase_en:
        return ""
    pair = _WTTR_MOON_CN.get(str(phase_en).strip())
    if pair is None:
        return str(phase_en).strip()
    return f"{pair[0]} {pair[1]}"


def _to_24h(value: str) -> str:
    """wttr 的 "06:03 AM" / "07:12 PM" → 24h "06:03" / "19:12"；解析失败原样返回。"""
    text = value.strip()
    if not text:
        return ""
    try:
        return _dt.datetime.strptime(text, "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return text


# ─────────────────────────────────────────────────────────────────────
# FallbackEnvironmentProvider —— 真→虚降级（§3.4 降级策略）
# ─────────────────────────────────────────────────────────────────────


class FallbackEnvironmentProvider:
    """先试 ``primary``（真实），失败降级到 ``fallback``（虚拟）。

    满足「API 不可用时退回 VirtualEnvironmentProvider」（02 §3.4）。降级到虚拟仍是
    「世界给的」事实（确定性世界模型），不破坏①被给予性。
    """

    def __init__(
        self,
        *,
        primary: EnvironmentProvider,
        fallback: EnvironmentProvider,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    def get_weather(self, location: str) -> WeatherReading:
        try:
            return self._primary.get_weather(location)
        except Exception:  # noqa: BLE001 —— 任何 primary 失败都降级，世界不该因外部 I/O 停摆
            _LOG.warning(
                "environment primary provider failed, fallback to virtual (location=%s)",
                location,
                exc_info=True,
            )
            return self._fallback.get_weather(location)


__all__ = [
    "EnvironmentProvider",
    "EnvironmentProviderError",
    "FallbackEnvironmentProvider",
    "VirtualEnvironmentProvider",
    "WeatherReading",
    "WttrEnvironmentProvider",
]
