"""世界 Provider 工厂 —— 按 config 选实现（与 ``llm/factory.py`` 同范式）。

``config.world.location_provider``：

- ``none``（默认）→ 不注入 LocationProvider
- ``virtual`` → ``VirtualLocationProvider``（纯虚拟、无网络）
- ``baidu`` → ``BaiduLocationProvider``（百度地图 Place API，GCJ-02；凭据走 env
  ``BAIDU_MAP_AK`` [+ SN 校验型应用的 ``BAIDU_MAP_SK``]，缺 ak fail-fast）。
  **失败不降级 virtual**——编造地点混进真实世界比没有候选更糟（与 weather 的
  Fallback 策略不同），查询失败由 act 层按「查询失败」prompt 降级。

``config.world.weather_provider``：

- ``virtual``（默认）→ ``VirtualEnvironmentProvider``（纯虚拟、无网络）
- ``wttr`` → ``WttrEnvironmentProvider`` 包一层 ``FallbackEnvironmentProvider``
  （真实拉取失败自动降级回虚拟，§3.4）

合法取值由 config loader 已校验（``_VALID_WEATHER_PROVIDERS``），故此处只分支不再校验。
节点代码（sense_io）只认 ``EnvironmentProvider`` Protocol，换实现零改动。

"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from kindred.providers.environment import (
    FallbackEnvironmentProvider,
    VirtualEnvironmentProvider,
    WttrEnvironmentProvider,
)
from kindred.providers.location import VirtualLocationProvider
from kindred.providers.location_baidu import (
    ENV_BAIDU_MAP_AK,
    ENV_BAIDU_MAP_SK,
    BaiduLocationProvider,
)
from kindred.runtime.clock import life_now

if TYPE_CHECKING:
    from kindred.config import KindredConfig
    from kindred.providers.environment import EnvironmentProvider
    from kindred.providers.location import LocationProvider


def build_location_provider(config: KindredConfig) -> LocationProvider | None:
    """据 ``config.world.location_provider`` 装配 LocationProvider。

    ``none`` 表示地点候选能力关闭；``virtual`` / ``baidu`` 都是显式 opt-in。
    Provider 查询 origin / query 由 act 节点按当前 ``state.location`` 组装；
    本工厂不持地点，也不读 state。

    ``baidu`` 凭据从 env 读（ak 必需，缺了在装配期 fail-fast——而不是每 tick
    运行时报错；sk 仅 SN 校验型应用需要）。
    """
    if config.world.location_provider == "none":
        return None
    if config.world.location_provider == "virtual":
        return VirtualLocationProvider()
    if config.world.location_provider == "baidu":
        ak = os.environ.get(ENV_BAIDU_MAP_AK, "")
        if not ak.strip():
            raise ValueError(
                f"location_provider=baidu 需要 env {ENV_BAIDU_MAP_AK}（百度地图开放平台 ak）"
            )
        return BaiduLocationProvider(ak=ak, sk=os.environ.get(ENV_BAIDU_MAP_SK, ""))
    # loader 已限定合法值；这里只保类型完整性，避免未来新增枚举时静默走错实现。
    raise ValueError(f"unsupported location_provider: {config.world.location_provider!r}")


def build_environment_provider(config: KindredConfig) -> EnvironmentProvider:
    """据 ``config.world.weather_provider`` 装配 EnvironmentProvider。

    查询位置（``world.weather_location`` 或回落 ``city``）由 sense_io 解析后传入
    ``get_weather(location)``——本工厂不持地点，provider 给啥查啥。
    """
    world = config.world
    if world.weather_provider == "wttr":
        return FallbackEnvironmentProvider(
            primary=WttrEnvironmentProvider(timeout_s=float(world.weather_timeout_s)),
            fallback=_build_virtual_provider(world.timezone),
        )
    # weather_provider == "virtual"（loader 已限定二选一）
    return _build_virtual_provider(world.timezone)


def _build_virtual_provider(timezone_name: str) -> VirtualEnvironmentProvider:
    """Virtual world facts must follow ta's life clock, not the host process TZ."""
    return VirtualEnvironmentProvider(now_fn=lambda: life_now(timezone_name))


__all__ = ["build_environment_provider", "build_location_provider"]
