"""Configuration loading, merging, and validation."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, fields
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import TypeAdapter, ValidationError

from kindred.config.schema import (
    KindredCapabilityConfig,
    KindredConfig,
    KindredDaemonConfig,
    KindredDebugConfig,
    KindredGatewayConfig,
    KindredLlmConfig,
    KindredLoggingConfig,
    KindredPaths,
    KindredResidentConfig,
    KindredWebConfig,
    KindredWorldConfig,
)
from kindred.config.secrets import KindredSecretError, merge_runtime_secrets
from kindred.hermes.wire import HermesWire
from kindred.mouth_host.model import (
    HostRuntimeModel,
    OpenClawRuntimeModel,
)
from kindred.openclaw import OpenClawWire


class KindredConfigError(RuntimeError):
    """Configuration file, env, or explicit override is malformed."""


ConfigOverrides: TypeAlias = Mapping[str, Any]

DEFAULT_CONFIG_PATH = Path("conf/kindred.yaml")

ENV_CONFIG = "KINDRED_CONFIG"
ENV_XDG_CONFIG_HOME = "XDG_CONFIG_HOME"
ENV_HOME = "HOME"
ENV_LIFE_ROOT = "KINDRED_LIFE_ROOT"
ENV_RUN_DIR = "KINDRED_RUN_DIR"
ENV_PID_FILE = "KINDRED_PID_FILE"
ENV_LOG_FILE = "KINDRED_LOG_FILE"
ENV_LLM_MODEL = "KINDRED_LLM_MODEL"
ENV_LLM_PROVIDER = "KINDRED_LLM_PROVIDER"
ENV_LLM_CLAUDE_CODE_TIMEOUT_S = "KINDRED_LLM_CLAUDE_CODE_TIMEOUT_S"
ENV_LOG_LEVEL = "KINDRED_LOG_LEVEL"
ENV_DEBUG_DUMP = "KINDRED_DEBUG_DUMP"
ENV_DEBUG_DUMP_DIR = "KINDRED_DEBUG_DUMP_DIR"
ENV_SESSION_KEY = "KINDRED_SESSION_KEY"
ENV_GATEWAY_HOST = "KINDRED_GATEWAY_HOST"
ENV_GATEWAY_PORT = "KINDRED_GATEWAY_PORT"
ENV_GATEWAY_TOKEN = "KINDRED_GATEWAY_TOKEN"
ENV_WEB_HOST = "KINDRED_WEB_HOST"
ENV_WEB_PORT = "KINDRED_WEB_PORT"
ENV_REVEAL_INTIMATE = "KINDRED_WEB_REVEAL_INTIMATE"
ENV_LOCATION_PROVIDER = "KINDRED_WORLD_LOCATION_PROVIDER"
ENV_WEATHER_PROVIDER = "KINDRED_WORLD_WEATHER_PROVIDER"
ENV_WEATHER_TTL_MINUTES = "KINDRED_WORLD_WEATHER_TTL_MINUTES"
ENV_WEATHER_TIMEOUT_S = "KINDRED_WORLD_WEATHER_TIMEOUT_S"
ENV_WEATHER_LOCATION = "KINDRED_WORLD_WEATHER_LOCATION"
ENV_WORLD_TIMEZONE = "KINDRED_WORLD_TIMEZONE"

_DEFAULT_CONFIG_RESOURCE = "defaults/kindred.yaml"
_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_VALID_LLM_PROVIDERS = frozenset(
    {"anthropic", "claude_code", "google", "deepseek", "openai", "xai"}
)
# 世界 Provider 层（能呼吸的世界）：地点能力 / 天气 Provider 合法取值。
_VALID_LOCATION_PROVIDERS = frozenset({"none", "virtual", "baidu"})
_VALID_WEATHER_PROVIDERS = frozenset({"virtual", "wttr"})
_CAPABILITIES_SECTION = "capabilities"
_MOUTH_HOST_SECTION = "mouth_host"
_CAPABILITY_KEYS = frozenset({"enabled", "settings", "side_effect_activities"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n", ""})
# section 名 → 对应 schema dataclass。这是配置形状的**唯一手护处**：
# 加一个 section 在这里登记一行，其 key 白名单从 dataclass 字段自动派生。
_SECTION_SCHEMAS: dict[str, type] = {
    "paths": KindredPaths,
    "llm": KindredLlmConfig,
    "logging": KindredLoggingConfig,
    "debug": KindredDebugConfig,
    "daemon": KindredDaemonConfig,
    "gateway": KindredGatewayConfig,
    "web": KindredWebConfig,
    "world": KindredWorldConfig,
    "resident": KindredResidentConfig,
}

# 派生字段：是 schema dataclass 字段，但**不是**该 section 的 YAML 输入键
# （由别处计算填充）。如 ``debug.dump_dir`` 实来自 ``paths.debug_dump_dir``，
# 写进 ``debug:`` section 是无效死配置——须从白名单排除，否则静默吞。
# （加新派生字段时才需动这里；普通输入字段零维护。）
_DERIVED_FIELDS: frozenset[str] = frozenset({"debug.dump_dir"})


def _known_keys() -> dict[str, frozenset[str]]:
    """从 schema dataclass 自动派生「已知配置 key」白名单。

    避免手护第三份字段名（schema dataclass + ``_build_*`` 已是两份）。
    唯一例外是 ``_DERIVED_FIELDS``（计算字段，非该 section YAML 输入）。
    """
    result: dict[str, frozenset[str]] = {}
    for section_name, schema_cls in _SECTION_SCHEMAS.items():
        names = {
            f.name for f in fields(schema_cls) if f"{section_name}.{f.name}" not in _DERIVED_FIELDS
        }
        result[section_name] = frozenset(names)
    return result


_KNOWN_KEYS: dict[str, frozenset[str]] = _known_keys()


def _validate_known_keys(raw: Mapping[str, Any]) -> None:
    """校验合并后的 raw config，未知 section / key 直接报错。

    配置扁平化（2026-06-17）：``build_overrides`` 成为唯一程序化入口后，
    拼错 key（如 ``paths.context_bundel``）不能再静默吞——会被 ``_build_*``
    忽略、最终读默认值，调用方零报错。本函数在 merge 后、``_build_config``
    前全量校验，连用户 YAML 拼错 key 也一并拓。
    """
    for section_name, section in raw.items():
        if section_name == _CAPABILITIES_SECTION:
            _validate_capabilities_keys(section)
            continue
        if section_name == _MOUTH_HOST_SECTION:
            _validate_mouth_host_keys(section)
            continue
        known = _KNOWN_KEYS.get(section_name)
        if known is None:
            if section_name == "openclaw":
                raise KindredConfigError(
                    "legacy OpenClaw config is unsupported; remove top-level openclaw and "
                    "resident.agent_id/resident.workspace, then rerun kindred install"
                )
            known_sections = sorted((*_KNOWN_KEYS, _MOUTH_HOST_SECTION))
            raise KindredConfigError(
                f"unknown config section: {section_name!r} (known: {', '.join(known_sections)})"
            )
        if not isinstance(section, Mapping):
            continue
        for key in section:
            if key not in known:
                if section_name == "resident" and key in {"agent_id", "workspace"}:
                    raise KindredConfigError(
                        "legacy OpenClaw config is unsupported; remove top-level openclaw and "
                        "resident.agent_id/resident.workspace, then rerun kindred install"
                    )
                raise KindredConfigError(
                    f"unknown config key: {section_name}.{key} "
                    f"(known in {section_name}: {', '.join(sorted(known))})"
                )


def _validate_mouth_host_keys(section: Any) -> None:
    if not isinstance(section, Mapping):
        raise KindredConfigError("mouth_host must be a mapping")


def _validate_capabilities_keys(section: Any) -> None:
    if not isinstance(section, Mapping):
        raise KindredConfigError("capabilities must be a mapping")
    for capability_name, capability_raw in section.items():
        if not isinstance(capability_name, str) or not capability_name.strip():
            raise KindredConfigError("capability name must be a non-empty string")
        if capability_name != capability_name.strip():
            raise KindredConfigError("capability name must not contain leading/trailing whitespace")
        if not isinstance(capability_raw, Mapping):
            raise KindredConfigError(f"capabilities.{capability_name} must be a mapping")
        for key in capability_raw:
            if key not in _CAPABILITY_KEYS:
                raise KindredConfigError(
                    f"unknown config key: capabilities.{capability_name}.{key} "
                    f"(known in capability policy: {', '.join(sorted(_CAPABILITY_KEYS))})"
                )


def load_kindred_config(
    config_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    overrides: ConfigOverrides | None = None,
    load_secrets: bool = True,
) -> KindredConfig:
    """Load config with precedence: packaged YAML < YAML < env < overrides.

    四层覆盖链，每层职责单一、不重叠：

    1. packaged default YAML —— wheel 自带基线，开箱即跑
    2. user YAML —— 用户「可感知 + 可手改」层（``conf/kindred.yaml``）
    3. env —— 部署 / 容器 ambient 注入（``KINDRED_*``）
    4. overrides —— **唯一程序化覆盖入口**（nested dotted-section dict）

    CLI 把 ``--life-root`` / ``--db`` 等组装成 ``overrides`` dict 传入，不再走
    专属 ``cli_*`` shim（2026-06-17 配置扁平化：原第 4 层 cli_* shim 与第 5 层
    overrides 重复，已 collapse 成单一 overrides 入口）。

    ``load_secrets=False`` 只供 stop、DB 与 soul rollback 等恢复命令读取路径；
    正常运行组合根必须保留默认值，继续校验并加载 Resident secrets。
    """

    env_map = os.environ if env is None else env
    resolved_config_path, explicit_config = _resolve_config_path(config_path, env_map)
    raw = deepcopy(_PACKAGED_DEFAULTS)
    _deep_merge(raw, _load_yaml_config(resolved_config_path, explicit=explicit_config))
    effective_env = dict(env_map)
    secrets_path = _resident_secrets_path(raw) if load_secrets else None
    if secrets_path is not None:
        try:
            effective_env.update(merge_runtime_secrets(secrets_path, env=env_map))
        except KindredSecretError as exc:
            raise KindredConfigError(str(exc)) from exc
    _deep_merge(raw, _env_overrides(effective_env))
    if overrides:
        _deep_merge(raw, overrides)
    _validate_known_keys(raw)
    return _build_config(raw)


def _load_packaged_defaults() -> dict[str, Any]:
    resource = files("kindred.config").joinpath(_DEFAULT_CONFIG_RESOURCE)
    try:
        loaded = yaml.safe_load(resource.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:  # pragma: no cover - packaging invariant
        raise KindredConfigError(f"packaged default config missing: {resource}") from exc
    except yaml.YAMLError as exc:  # pragma: no cover - packaging invariant
        raise KindredConfigError(f"packaged default config is invalid YAML: {exc}") from exc
    if not isinstance(loaded, dict):  # pragma: no cover - packaging invariant
        raise KindredConfigError("packaged default config root must be a mapping")
    return cast("dict[str, Any]", loaded)


_PACKAGED_DEFAULTS = _load_packaged_defaults()


def _resolve_config_path(
    config_path: str | Path | None,
    env: Mapping[str, str],
) -> tuple[Path, bool]:
    if config_path is not None:
        return Path(config_path), True
    env_config = env.get(ENV_CONFIG)
    if env_config:
        return Path(env_config), True
    xdg_home = env.get(ENV_XDG_CONFIG_HOME)
    home = env.get(ENV_HOME)
    if xdg_home:
        config_home = Path(xdg_home)
    elif home:
        config_home = Path(home) / ".config"
    else:
        config_home = None
    if config_home is not None:
        xdg_config = config_home / "kindred" / "config.yaml"
        if xdg_config.exists():
            return xdg_config, True
    return DEFAULT_CONFIG_PATH, False


def _resident_secrets_path(raw: Mapping[str, Any]) -> Path | None:
    resident = raw.get("resident")
    if not isinstance(resident, Mapping):
        return None
    value = resident.get("secrets_file")
    if value in (None, ""):
        return None
    if not isinstance(value, str | Path):
        raise KindredConfigError("resident.secrets_file must be a path or empty")
    return Path(value)


def _load_yaml_config(path: Path, *, explicit: bool) -> dict[str, Any]:
    if not path.exists():
        if explicit:
            raise KindredConfigError(f"config file does not exist: {path}")
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise KindredConfigError(f"invalid YAML config {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise KindredConfigError(f"config root must be a mapping: {path}")
    return cast("dict[str, Any]", loaded)


def _deep_merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _deep_merge(existing, value)
        else:
            target[key] = value


def _set_dotted(raw: dict[str, Any], dotted_path: str, value: object) -> None:
    if value is None:
        return
    section_name, key = dotted_path.split(".", 1)
    section = raw.setdefault(section_name, {})
    if not isinstance(section, dict):
        raise KindredConfigError(f"{section_name} must be a mapping")
    section[key] = value


def build_overrides(items: Mapping[str, object]) -> dict[str, Any]:
    """把 dotted-section dict（如 ``{"paths.db": p, "llm.model": m}``）转成
    ``load_kindred_config(overrides=...)`` 接受的 nested dict。

    ``None`` 值自动跳过（套同 CLI：未传的选项 = None = 不覆盖）。
    这是 CLI / 程序化调用方组装 overrides 的官方入口（2026-06-17
    配置扁平化：cli_* shim collapse 后，dotted 写法由本 helper 统一转换）。
    """
    raw: dict[str, Any] = {}
    for dotted_path, value in items.items():
        _set_dotted(raw, dotted_path, value)
    return raw


@dataclass(frozen=True)
class _EnvOverride:
    path: str
    parser: Callable[[str, str], object] = lambda value, _field: value


_ENV_OVERRIDES = {
    ENV_LIFE_ROOT: _EnvOverride("paths.life_root"),
    ENV_RUN_DIR: _EnvOverride("paths.run_dir"),
    ENV_PID_FILE: _EnvOverride("daemon.pid_file"),
    ENV_LOG_FILE: _EnvOverride("daemon.log_file"),
    ENV_LLM_MODEL: _EnvOverride("llm.model"),
    ENV_LLM_PROVIDER: _EnvOverride("llm.provider"),
    ENV_LLM_CLAUDE_CODE_TIMEOUT_S: _EnvOverride(
        "llm.claude_code_timeout_s",
        lambda value, field: _parse_int(value, field),
    ),
    ENV_LOG_LEVEL: _EnvOverride("logging.level"),
    ENV_DEBUG_DUMP: _EnvOverride(
        "debug.dump_enabled",
        lambda value, field: _parse_bool(value, field),
    ),
    ENV_DEBUG_DUMP_DIR: _EnvOverride("paths.debug_dump_dir"),
    ENV_SESSION_KEY: _EnvOverride("daemon.session_key"),
    ENV_GATEWAY_HOST: _EnvOverride("gateway.host"),
    ENV_GATEWAY_PORT: _EnvOverride(
        "gateway.port",
        lambda value, field: _parse_int(value, field),
    ),
    ENV_GATEWAY_TOKEN: _EnvOverride("gateway.token"),
    ENV_WEB_HOST: _EnvOverride("web.host"),
    ENV_WEB_PORT: _EnvOverride(
        "web.port",
        lambda value, field: _parse_int(value, field),
    ),
    ENV_REVEAL_INTIMATE: _EnvOverride(
        "web.reveal_intimate",
        lambda value, field: _parse_bool(value, field),
    ),
    ENV_LOCATION_PROVIDER: _EnvOverride("world.location_provider"),
    ENV_WEATHER_PROVIDER: _EnvOverride("world.weather_provider"),
    ENV_WEATHER_TTL_MINUTES: _EnvOverride(
        "world.weather_ttl_minutes",
        lambda value, field: _parse_int(value, field),
    ),
    ENV_WEATHER_TIMEOUT_S: _EnvOverride(
        "world.weather_timeout_s",
        lambda value, field: _parse_int(value, field),
    ),
    ENV_WEATHER_LOCATION: _EnvOverride("world.weather_location"),
    ENV_WORLD_TIMEZONE: _EnvOverride("world.timezone"),
}


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    for env_name, override in _ENV_OVERRIDES.items():
        if env_name in env:
            _set_dotted(raw, override.path, override.parser(env[env_name], env_name))
    if ENV_DEBUG_DUMP_DIR in env and ENV_DEBUG_DUMP not in env:
        _set_dotted(raw, "debug.dump_enabled", True)
    return raw


def _build_config(raw: Mapping[str, Any]) -> KindredConfig:
    paths = _build_paths(_section(raw, "paths"))
    llm_raw = _section(raw, "llm")
    logging_raw = _section(raw, "logging")
    debug_raw = _section(raw, "debug")

    daemon = _build_daemon(_section(raw, "daemon"), paths)
    mouth_host = _build_mouth_host(_section(raw, _MOUTH_HOST_SECTION))
    if mouth_host is not None and _canonical_transcript(mouth_host) != daemon.session_key:
        raise KindredConfigError("mouth_host canonical transcript must match daemon.session_key")
    return KindredConfig(
        paths=paths,
        llm=KindredLlmConfig(
            model=_str_value(llm_raw, "model"),
            provider=_choice_value(llm_raw, "provider", _VALID_LLM_PROVIDERS, "llm.provider"),
            # claude_code 单次 claude -p 超时（秒）；整机试运行实测 120s 在心嘴并发下不够。
            # 必正数（codex N-3）：配 0/负数会把每次 headless 调用变必超时。
            claude_code_timeout_s=_positive_int_value(llm_raw, "claude_code_timeout_s"),
        ),
        logging=KindredLoggingConfig(
            level=_normalize_log_level(_str_value(logging_raw, "level")),
            file_max_bytes=_positive_int_value(logging_raw, "file_max_bytes"),
            file_backup_count=_positive_int_value(logging_raw, "file_backup_count"),
        ),
        debug=KindredDebugConfig(
            dump_enabled=_bool_value(debug_raw, "dump_enabled"),
            dump_dir=paths.debug_dump_dir,
            dump_max_files=_positive_int_value(debug_raw, "dump_max_files"),
        ),
        daemon=daemon,
        gateway=_build_gateway(_section(raw, "gateway")),
        mouth_host=mouth_host,
        web=_build_web(_section(raw, "web")),
        world=_build_world(_section(raw, "world")),
        resident=_build_resident(_section(raw, "resident")),
        capabilities=_build_capabilities(_section(raw, _CAPABILITIES_SECTION)),
    )


def _build_daemon(daemon_raw: Mapping[str, Any], paths: KindredPaths) -> KindredDaemonConfig:
    # pid_file/log_file 可引用 {run_dir}/{life_root}（派生自 paths，已解析）。
    placeholders = {"life_root": str(paths.life_root), "run_dir": str(paths.run_dir)}
    return KindredDaemonConfig(
        session_key=_str_value(daemon_raw, "session_key"),
        pid_file=_path_value(daemon_raw, "pid_file", placeholders=placeholders),
        log_file=_path_value(daemon_raw, "log_file", placeholders=placeholders),
    )


def _build_gateway(gateway_raw: Mapping[str, Any]) -> KindredGatewayConfig:
    return KindredGatewayConfig(
        host=_str_value(gateway_raw, "host"),
        port=_int_value(gateway_raw, "port"),
        token=_str_value(gateway_raw, "token", allow_empty=True),
    )


def _build_mouth_host(raw: Mapping[str, Any]) -> HostRuntimeModel | None:
    if not raw:
        return None
    try:
        kind = raw.get("kind")
        wire_raw = raw.get("wire")
        if not isinstance(wire_raw, Mapping):
            raise ValueError
        values = dict(raw)
        if kind == "openclaw":
            values["wire"] = OpenClawWire.model_validate(wire_raw)
            values["workspace"] = Path(_strict_text(raw.get("workspace")))
        elif kind == "hermes":
            identity = raw.get("identity")
            if not isinstance(identity, list) or len(identity) != 2:
                raise ValueError
            wire_values = dict(wire_raw)
            wire_values["host_executable"] = Path(_strict_text(wire_values.get("host_executable")))
            wire_values["host_home"] = Path(_strict_text(wire_values.get("host_home")))
            values["wire"] = HermesWire(**wire_values)
            values["identity"] = tuple(_strict_text(value) for value in identity)
        else:
            raise ValueError
        return TypeAdapter(HostRuntimeModel).validate_python(values)
    except (TypeError, ValueError, ValidationError) as exc:
        paths = (
            sorted(
                ".".join(str(part) for part in error["loc"]) or "<root>" for error in exc.errors()
            )
            if isinstance(exc, ValidationError)
            else ["<root>"]
        )
        raise KindredConfigError("invalid mouth_host at: " + ", ".join(paths)) from None


def _strict_text(value: Any) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ValueError
    return value


def _canonical_transcript(model: HostRuntimeModel) -> str:
    if isinstance(model, OpenClawRuntimeModel):
        return model.wire.transcript_session
    return model.wire.canonical_session_id


def _build_web(web_raw: Mapping[str, Any]) -> KindredWebConfig:
    return KindredWebConfig(
        host=_str_value(web_raw, "host"),
        port=_tcp_port_value(web_raw, "port"),
        reveal_intimate=_bool_value(web_raw, "reveal_intimate"),
    )


def _build_world(world_raw: Mapping[str, Any]) -> KindredWorldConfig:
    # 天气 TTL / timeout 必正数：TTL=0 会每 tick 都拉（拖慢 + 打爆 wttr），timeout<=0 必超时。
    return KindredWorldConfig(
        location_provider=_choice_value(
            world_raw,
            "location_provider",
            _VALID_LOCATION_PROVIDERS,
            "world.location_provider",
        ),
        weather_provider=_choice_value(
            world_raw, "weather_provider", _VALID_WEATHER_PROVIDERS, "world.weather_provider"
        ),
        weather_ttl_minutes=_positive_int_value(world_raw, "weather_ttl_minutes"),
        weather_timeout_s=_positive_int_value(world_raw, "weather_timeout_s"),
        # 留空 = 回落到 ta 的 city；非空可精确到区（如 "shenzhen+nanshan"）。
        weather_location=_str_value(world_raw, "weather_location", allow_empty=True),
        # ta 世界的时区（IANA），运行入口据此生成 life clock——不靠宿主时区。
        timezone=_timezone_value(world_raw, "timezone"),
    )


def _build_resident(resident_raw: Mapping[str, Any]) -> KindredResidentConfig:
    return KindredResidentConfig(
        install_id=_str_value(resident_raw, "install_id", allow_empty=True),
        resident_id=_str_value(resident_raw, "resident_id", allow_empty=True),
        marker_path=_optional_path_value(resident_raw, "marker_path"),
        secrets_file=_optional_path_value(resident_raw, "secrets_file"),
    )


def _build_capabilities(
    capabilities_raw: Mapping[str, Any],
) -> dict[str, KindredCapabilityConfig]:
    result: dict[str, KindredCapabilityConfig] = {}
    for capability_name, raw_policy in capabilities_raw.items():
        if not isinstance(capability_name, str) or not capability_name.strip():
            raise KindredConfigError("capability name must be a non-empty string")
        if capability_name != capability_name.strip():
            raise KindredConfigError("capability name must not contain leading/trailing whitespace")
        if not isinstance(raw_policy, Mapping):
            raise KindredConfigError(f"capabilities.{capability_name} must be a mapping")
        enabled = raw_policy.get("enabled", False)
        if type(enabled) is not bool:
            raise KindredConfigError(f"capabilities.{capability_name}.enabled must be a boolean")
        settings = raw_policy.get("settings", {})
        if not isinstance(settings, Mapping):
            raise KindredConfigError(f"capabilities.{capability_name}.settings must be a mapping")
        result[capability_name] = KindredCapabilityConfig(
            enabled=enabled,
            side_effect_activities=_str_tuple_value(
                raw_policy,
                "side_effect_activities",
                default_empty=True,
            ),
            settings=MappingProxyType(deepcopy(dict(settings))),
        )
    return result


def _build_paths(paths_raw: Mapping[str, Any]) -> KindredPaths:
    life_root = _path_value(paths_raw, "life_root")
    placeholders = {"life_root": str(life_root)}
    # run_dir 可引用 {life_root}；先解析它，再入 placeholder 供 daemon pid/log 用 {run_dir}。
    run_dir = _path_value(paths_raw, "run_dir", placeholders=placeholders)
    placeholders["run_dir"] = str(run_dir)
    return KindredPaths(
        life_root=life_root,
        run_dir=run_dir,
        db=_path_value(paths_raw, "db", placeholders=placeholders),
        context_bundle=_path_value(paths_raw, "context_bundle", placeholders=placeholders),
        soul_excerpt=_path_value(paths_raw, "soul_excerpt", placeholders=placeholders),
        soul_full=_path_value(paths_raw, "soul_full", placeholders=placeholders),
        identity=_path_value(paths_raw, "identity", placeholders=placeholders),
        user=_path_value(paths_raw, "user", placeholders=placeholders),
        character_card=_path_value(paths_raw, "character_card", placeholders=placeholders),
        bundle_highlights=_path_value(paths_raw, "bundle_highlights", placeholders=placeholders),
        debug_dump_dir=_path_value(paths_raw, "debug_dump_dir", placeholders=placeholders),
        soul_history_dir=_path_value(paths_raw, "soul_history_dir", placeholders=placeholders),
        doc_dir=_path_value(paths_raw, "doc_dir", placeholders=placeholders),
    )


def _section(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise KindredConfigError(f"{name} must be a mapping")
    return cast("dict[str, Any]", section)


def _str_value(raw: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise KindredConfigError(f"{key} must be a string")
    stripped = value.strip()
    if not stripped and not allow_empty:
        raise KindredConfigError(f"{key} must be a non-empty string")
    return stripped


def _str_tuple_value(
    raw: Mapping[str, Any],
    key: str,
    *,
    default_empty: bool = False,
) -> tuple[str, ...]:
    if key not in raw:
        if default_empty:
            return ()
        raise KindredConfigError(f"{key} must be a list of strings")
    value = raw.get(key)
    if isinstance(value, str):
        return tuple(_parse_str_list(value, key))
    if not isinstance(value, list):
        raise KindredConfigError(f"{key} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise KindredConfigError(f"{key} must be a list of strings")
        stripped = item.strip()
        if stripped:
            result.append(stripped)
    return tuple(result)


def _timezone_value(raw: Mapping[str, Any], key: str) -> str:
    """IANA timezone config value, validated at config-load boundary."""
    value = _str_value(raw, key)
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise KindredConfigError(f"{key} must be a valid IANA timezone, got {value!r}") from exc
    return value


def _int_value(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise KindredConfigError(f"{key} must be an integer")
    return value


def _positive_int_value(raw: Mapping[str, Any], key: str) -> int:
    """正整数 config 值（>0）。0/负数 fail-fast——如超时配 0 会把每次调用变必超时（codex N-3）。"""
    value = _int_value(raw, key)
    if value <= 0:
        raise KindredConfigError(f"{key} must be a positive integer, got {value}")
    return value


def _tcp_port_value(raw: Mapping[str, Any], key: str) -> int:
    value = _int_value(raw, key)
    if not 1 <= value <= 65535:
        raise KindredConfigError(f"{key} must be between 1 and 65535, got {value}")
    return value


def _parse_int(value: str, field: str) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError) as exc:
        raise KindredConfigError(f"{field} must be an integer") from exc


def _parse_str_list(value: str, field: str) -> list[str]:
    del field
    return [part.strip() for part in value.split(",") if part.strip()]


def _path_value(
    raw: Mapping[str, Any],
    key: str,
    *,
    placeholders: Mapping[str, str] | None = None,
) -> Path:
    value = raw.get(key)
    if not isinstance(value, str | Path) or not str(value):
        raise KindredConfigError(f"{key} must be a non-empty path")
    text = str(value)
    if placeholders:
        try:
            text = text.format(**placeholders)
        except KeyError as exc:
            missing = cast("str", exc.args[0])
            raise KindredConfigError(f"{key} references unknown placeholder {{{missing}}}") from exc
        except ValueError as exc:
            raise KindredConfigError(f"{key} has invalid path template: {value!r}") from exc
    return Path(text)


def _optional_path_value(raw: Mapping[str, Any], key: str) -> Path | None:
    value = raw.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str | Path):
        raise KindredConfigError(f"{key} must be a path or empty")
    return Path(value)


def _bool_value(raw: Mapping[str, Any], key: str) -> bool:
    return _parse_bool(raw.get(key), key)


def _parse_bool(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    raise KindredConfigError(f"{field} must be a boolean")


def _normalize_log_level(level: str) -> str:
    normalized = level.strip().upper()
    if normalized not in _VALID_LOG_LEVELS:
        raise KindredConfigError(
            f"logging.level must be one of {sorted(_VALID_LOG_LEVELS)}, got {level!r}"
        )
    return normalized


def _choice_value(raw: Mapping[str, Any], key: str, valid: frozenset[str], field: str) -> str:
    """取一个枚举型字符串 config 值，限定在 ``valid`` 集合内（否则 fail-fast）。"""
    value = _str_value(raw, key)
    if value not in valid:
        raise KindredConfigError(f"{field} must be one of {sorted(valid)}, got {value!r}")
    return value


DEFAULT_CONFIG = _build_config(_PACKAGED_DEFAULTS)
DEFAULT_PATHS = DEFAULT_CONFIG.paths
DEFAULT_LIFE_ROOT = DEFAULT_PATHS.life_root
DEFAULT_DB_PATH = DEFAULT_PATHS.db
DEFAULT_CONTEXT_BUNDLE_PATH = DEFAULT_PATHS.context_bundle
DEFAULT_SOUL_EXCERPT_PATH = DEFAULT_PATHS.soul_excerpt
DEFAULT_SOUL_FULL_PATH = DEFAULT_PATHS.soul_full
DEFAULT_IDENTITY_PATH = DEFAULT_PATHS.identity
DEFAULT_USER_PATH = DEFAULT_PATHS.user
DEFAULT_CHARACTER_CARD_PATH = DEFAULT_PATHS.character_card
DEFAULT_BUNDLE_HIGHLIGHTS_PATH = DEFAULT_PATHS.bundle_highlights
DEFAULT_DEBUG_DUMP_DIR = DEFAULT_PATHS.debug_dump_dir
DEFAULT_SOUL_HISTORY_DIR = DEFAULT_PATHS.soul_history_dir
DEFAULT_LLM_MODEL = DEFAULT_CONFIG.llm.model
DEFAULT_LLM_PROVIDER = DEFAULT_CONFIG.llm.provider
DEFAULT_LLM_CLAUDE_CODE_TIMEOUT_S = DEFAULT_CONFIG.llm.claude_code_timeout_s
DEFAULT_LOG_LEVEL = DEFAULT_CONFIG.logging.level
DEFAULT_SESSION_KEY = DEFAULT_CONFIG.daemon.session_key
DEFAULT_RUN_DIR = DEFAULT_PATHS.run_dir
DEFAULT_PID_FILE = DEFAULT_CONFIG.daemon.pid_file
DEFAULT_LOG_FILE = DEFAULT_CONFIG.daemon.log_file
DEFAULT_GATEWAY_HOST = DEFAULT_CONFIG.gateway.host
DEFAULT_GATEWAY_PORT = DEFAULT_CONFIG.gateway.port
DEFAULT_WEB_HOST = DEFAULT_CONFIG.web.host
DEFAULT_WEB_PORT = DEFAULT_CONFIG.web.port
DEFAULT_LOCATION_PROVIDER = DEFAULT_CONFIG.world.location_provider
DEFAULT_WEATHER_PROVIDER = DEFAULT_CONFIG.world.weather_provider
DEFAULT_WEATHER_TTL_MINUTES = DEFAULT_CONFIG.world.weather_ttl_minutes
DEFAULT_WEATHER_TIMEOUT_S = DEFAULT_CONFIG.world.weather_timeout_s
DEFAULT_WEATHER_LOCATION = DEFAULT_CONFIG.world.weather_location
DEFAULT_WORLD_TIMEZONE = DEFAULT_CONFIG.world.timezone
