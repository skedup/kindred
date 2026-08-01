"""Typed runtime configuration objects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kindred.openclaw import OpenClawWire


@dataclass(frozen=True)
class KindredPaths:
    """Resolved runtime path bundle."""

    life_root: Path
    run_dir: Path
    db: Path
    context_bundle: Path
    soul_excerpt: Path
    soul_full: Path
    identity: Path
    user: Path
    #: 运行期 character-card；home 等实例级设置从这里读，不进入 8 层 state。
    character_card: Path
    bundle_highlights: Path
    debug_dump_dir: Path
    soul_history_dir: Path
    #: 醒着的、她自发的轻量留档根目录（compose 动作产出落这里，按 kind 分子目录；
    #: 与 dream 的 gated 灵魂写入完全分开）。见 artifacts.compose OUTPUT_TARGETS。
    doc_dir: Path


@dataclass(frozen=True)
class KindredLlmConfig:
    """LLM runtime settings.

    ``provider`` 选择 ``claude_code``、``anthropic``、``google``、``deepseek`` 或
    ``openai``。HTTP Provider 的 credential 只从 Kindred secrets/env 注入；
    ``claude_code`` 使用本机 Claude CLI 登录态。

    ``claude_code_timeout_s``：``provider=claude_code`` 时单次 ``claude -p`` 子进程超时（秒）。
    心每个 tick 起一个 claude 子进程（冷启 + 模型延迟本就慢），整机试运行实测：心嘴并发各起
    一个 claude 子进程（共用同一订阅）时单次心调用可超 120s。故**可配 + 默认放宽到 300s**
    （仅 claude_code 使用）。
    """

    model: str
    provider: str
    claude_code_timeout_s: int


@dataclass(frozen=True)
class KindredLoggingConfig:
    """Logging runtime settings."""

    level: str
    file_max_bytes: int
    file_backup_count: int


@dataclass(frozen=True)
class KindredDebugConfig:
    """Debug artifact controls."""

    dump_enabled: bool
    dump_dir: Path
    dump_max_files: int


@dataclass(frozen=True)
class KindredDaemonConfig:
    """Daemon runtime settings.

    ``session_key`` identifies the canonical transcript partition used by the
    graph. When OpenClaw wire is configured, the loader requires it to equal
    ``openclaw.transcript_session``.
    """

    session_key: str
    pid_file: Path
    log_file: Path


@dataclass(frozen=True)
class KindredGatewayConfig:
    """OpenClaw Gateway WebSocket connection settings.

    The daemon pulls main-session ``chat.history`` over the Gateway JSON-RPC
    WebSocket (earlier milestone) to mirror real conversation (partner + my_voice) into the
    ``main_session_messages`` table. ``token`` is a secret and must come from
    env / explicit override, never the packaged YAML default (which is empty).
    """

    host: str
    port: int
    token: str


@dataclass(frozen=True)
class KindredWebConfig:
    """只读可视化 web 设置。

    ``reveal_intimate`` 语义 = 本实例「亲密度够」标志（docs/02 §401 / DECISIONS
    §219 的「关系深度控制是否渲染」）。默认 ``False``：陌生人实例。
    亲密关系的实例显式设 ``True``。

    私密层明文三态（用户拍板）：**显明文 = ?reveal=1 OR reveal_intimate**。
    揭示开关（?reveal=1）无条件生效；开关关时靠这个亲密度标志决定
    显明文还是俏皮拒绝语。亲密度数值源（relationship.passion）未实装，
    先用 config 当代理。CSS hover 不是隐私边界，这个才是。
    """

    reveal_intimate: bool


@dataclass(frozen=True)
class KindredWorldConfig:
    """世界系统（Provider 层）runtime 配置。

    「能呼吸的世界」已落两块入口：EnvironmentProvider（天气）与 LocationProvider
    （地点 affordance）。详见
    ``docs/discussions/2026-06-29-breathable-world-event-provider.md`` §7 +
    ``src/kindred/providers``。

    - ``location_provider``：``none``（默认关闭）| ``virtual``（纯虚拟、无网络、显式
      opt-in）| ``baidu``（百度地图 Place API，GCJ-02；凭据走 env ``BAIDU_MAP_AK``
      [+ ``BAIDU_MAP_SK``]；失败不降级 virtual——避免把虚拟地点混入真实世界）。
    - ``weather_provider``：``virtual``（纯虚拟、无网络、默认）| ``wttr``（wttr.in
      真实天气，免 API key；拉取失败自动降级回 virtual）。
    - ``weather_ttl_minutes``：天气 TTL（分钟）。tick 的 sense_io 按
      ``environment.weather_cached_at`` 比对触发时刻判过期，过期才调 Provider
      （pull + in-tick TTL，§7 Q3）。
    - ``weather_timeout_s``：真实 Provider 单次拉取超时（秒）——慢 I/O 不拖住 tick。
    - ``weather_location``：精确查询位置（如 ``"shenzhen+nanshan"`` 到区）；留空则回落
      到 ta 的 ``city``。
    - ``timezone``：ta 所在世界的时区（IANA 名，如 ``"Asia/Shanghai"``）。CLI /
      daemon 入口据此显式生成 life clock——她的时间=她世界的时间，**不靠宿主 TZ**
      （治「运行环境是 UTC → triggered_at/phase 差 8 小时 → 黄昏被算成 morning」）。
    """

    location_provider: str
    weather_provider: str
    weather_ttl_minutes: int
    weather_timeout_s: int
    weather_location: str
    timezone: str


@dataclass(frozen=True)
class KindredCapabilityConfig:
    """Runtime policy for one capability.

    ``enabled`` and ``settings`` belong to Portable entry-point discovery.
    Internal capabilities share this policy namespace without treating
    ``enabled`` as an owner selector.
    """

    enabled: bool
    side_effect_activities: tuple[str, ...]
    settings: Mapping[str, Any]


@dataclass(frozen=True)
class KindredResidentConfig:
    """OPEN2 resident 安装身份；空值表示尚未走公开冷启动链。"""

    install_id: str
    resident_id: str
    agent_id: str
    workspace: Path | None
    marker_path: Path | None
    secrets_file: Path | None


@dataclass(frozen=True)
class KindredConfig:
    """Fully merged runtime configuration."""

    paths: KindredPaths
    llm: KindredLlmConfig
    logging: KindredLoggingConfig
    debug: KindredDebugConfig
    daemon: KindredDaemonConfig
    gateway: KindredGatewayConfig
    openclaw: OpenClawWire | None
    web: KindredWebConfig
    world: KindredWorldConfig
    resident: KindredResidentConfig
    capabilities: Mapping[str, KindredCapabilityConfig]
