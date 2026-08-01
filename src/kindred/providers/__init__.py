"""Provider 集成接口层。

参考文档：``docs/10-providers.md`` / ``docs/02-state-system.md`` §3.4 /
``docs/DECISIONS.md`` D-020（世界事实默认由 tick 主动 pull）。

``Provider`` 是仓库内对外部/可替换实现边界的命名惯例，名称本身不决定数据方向：
Observation Provider 与 OutcomeProvider 产出入向世界事实；Location 等
adapter-style Provider 也可作为 Capability 的依赖，提供查询。共同边界是 Provider
不直接写八层 State，也不越过 graph/capability 的提交与授权规则；状态落库仍由 tick 的
T3.persist 统一负责。不为方向差异另建 Resolver/Adapter 框架。本层与 ``kindred.llm`` 的
*LLM* provider 完全是两回事——后者决定「心用哪个大模型思考」。

当前成员：

- ``EnvironmentProvider``（天气）—— pull + in-tick TTL（§7 Q3）。第一块砖。
- ``RecentContactProvider``（近期联系）—— 每拍只读当前已读消息的临时投影。
- ``LocationProvider``（地点 affordance）—— 按 origin + query 提供可去地点候选。

后继 Observation Provider 按真实来源分别设计；OutcomeProvider 在行动边界裁决。
不预设通用 EventProvider / durable stream。
"""

from __future__ import annotations

from kindred.providers.environment import (
    EnvironmentProvider,
    EnvironmentProviderError,
    FallbackEnvironmentProvider,
    VirtualEnvironmentProvider,
    WeatherReading,
    WttrEnvironmentProvider,
)
from kindred.providers.factory import (
    build_environment_provider,
    build_location_provider,
)
from kindred.providers.location import (
    LocationProvider,
    LocationProviderError,
    VirtualLocationProvider,
)
from kindred.providers.location_baidu import (
    ENV_BAIDU_MAP_AK,
    ENV_BAIDU_MAP_SK,
    BaiduLocationProvider,
)
from kindred.providers.recent_contact import (
    RECENT_CONTACT_VISIBILITY_SECONDS,
    RecentContactProvider,
)

__all__ = [
    "ENV_BAIDU_MAP_AK",
    "ENV_BAIDU_MAP_SK",
    "BaiduLocationProvider",
    "EnvironmentProvider",
    "EnvironmentProviderError",
    "FallbackEnvironmentProvider",
    "LocationProvider",
    "LocationProviderError",
    "RECENT_CONTACT_VISIBILITY_SECONDS",
    "RecentContactProvider",
    "VirtualEnvironmentProvider",
    "VirtualLocationProvider",
    "WeatherReading",
    "WttrEnvironmentProvider",
    "build_environment_provider",
    "build_location_provider",
]
