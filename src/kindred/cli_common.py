"""Shared implementation helpers for the Kindred CLI."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import click

from kindred.config import (
    KindredConfig,
    KindredConfigError,
    KindredSecretError,
    build_overrides,
    install_runtime_secrets,
    load_kindred_config,
)
from kindred.observability import DebugDumpConfig, PromptDumper, configure_logging
from kindred.runtime.clock import life_now_iso

SCENARIO_CHOICES: tuple[str, ...] = (
    "act_false",
    "start_activity",
    "advance_activity",
    "failure",
)


def cli_compat(name: str) -> Any:
    """Resolve a facade symbol at call time so existing monkeypatch seams remain live."""
    module = importlib.import_module("kindred.cli")
    return getattr(module, name)


def _load_cli_config(
    *,
    config_path: Path | None = None,
    life_root: Path | None = None,
    db_path: Path | None = None,
    bundle_path: Path | None = None,
    model: str | None = None,
    log_level: str | None = None,
    debug_dump: bool = False,
    debug_dump_dir: Path | None = None,
    web_host: str | None = None,
    web_port: int | None = None,
    load_secrets: bool = True,
) -> KindredConfig:
    # 把 CLI 选项组装成 dotted overrides（唯一程序化入口）。None 自动跳过。
    # debug_dump_dir 副作用：显式给 dir 则自动开 dump_enabled（沿用原 shim 语义）。
    dotted: dict[str, object | None] = {
        "paths.life_root": life_root,
        "paths.db": db_path,
        "paths.context_bundle": bundle_path,
        "llm.model": model,
        "logging.level": log_level,
        "debug.dump_enabled": True if debug_dump else None,
        "web.host": web_host,
        "web.port": web_port,
    }
    if debug_dump_dir is not None:
        dotted["paths.debug_dump_dir"] = debug_dump_dir
        dotted["debug.dump_enabled"] = True
    try:
        config = load_kindred_config(
            config_path,
            overrides=build_overrides(dotted),
            load_secrets=load_secrets,
        )
        if load_secrets:
            install_runtime_secrets(config.resident.secrets_file)
        return config
    except (KindredConfigError, KindredSecretError) as exc:
        raise click.ClickException(f"Kindred 配置错误：{exc}") from exc


def _require_resident_commit(
    config: KindredConfig,
    *,
    validate_secrets: bool = True,
) -> None:
    """OPEN2 配置只有在 marker 提交后才能进入运行入口。"""
    from kindred.resident import ResidentInitError, require_committed_resident

    try:
        require_committed_resident(config, validate_secrets=validate_secrets)
    except ResidentInitError as exc:
        raise click.ClickException(f"Kindred Resident 错误：{exc}") from exc


def _require_mouth_host_binding(config: KindredConfig) -> None:
    """The active host binding must match its committed Resident."""
    from kindred.hermes.binding import HermesBindingError, require_hermes_binding
    from kindred.mouth_host.model import HermesRuntimeModel, OpenClawRuntimeModel
    from kindred.openclaw.binding import OpenClawBindingError, require_openclaw_binding

    try:
        if isinstance(config.mouth_host, OpenClawRuntimeModel):
            require_openclaw_binding(config)
        elif isinstance(config.mouth_host, HermesRuntimeModel):
            require_hermes_binding(config)
        else:
            raise OpenClawBindingError("Mouth host is not configured")
    except (HermesBindingError, OpenClawBindingError) as exc:
        raise click.ClickException(f"Kindred Mouth host 错误：{exc}") from exc


def _require_mouth_host_runtime(config: KindredConfig) -> None:
    """Confirm the active host, binding and packaged Plugin before start."""
    from kindred.hermes.install import (
        HermesInstallError,
        hermes_version_warning,
        require_hermes_runtime,
    )
    from kindred.mouth_host.model import HermesRuntimeModel, OpenClawRuntimeModel
    from kindred.openclaw.binding import OpenClawBindingError
    from kindred.openclaw.install import (
        OpenClawInstallError,
        _version_warning,
        require_openclaw_runtime,
    )

    try:
        if isinstance(config.mouth_host, OpenClawRuntimeModel):
            warning = _version_warning(require_openclaw_runtime(config))
            if warning:
                click.echo(f"WARNING: {warning}")
        elif isinstance(config.mouth_host, HermesRuntimeModel):
            warning = hermes_version_warning(require_hermes_runtime(config))
            if warning:
                click.echo(f"WARNING: {warning}")
        else:
            raise OpenClawInstallError("Mouth host is not configured")
    except (HermesInstallError, OpenClawBindingError, OpenClawInstallError) as exc:
        raise click.ClickException(
            "Kindred Mouth Plugin/binding 不一致；请重新运行 kindred install"
        ) from exc


def _configure_observability(
    config: KindredConfig, *, log_file: Path | None = None
) -> PromptDumper:
    configure_logging(
        config.logging.level,
        log_file=log_file,
        file_max_bytes=config.logging.file_max_bytes,
        file_backup_count=config.logging.file_backup_count,
    )
    return PromptDumper(
        DebugDumpConfig(
            enabled=config.debug.dump_enabled,
            dump_dir=config.debug.dump_dir,
            max_files=config.debug.dump_max_files,
        )
    )


def _now_iso(timezone_name: str) -> str:
    """当前 life 时区 ISO8601 时间戳（tick/dream 的 ``triggered_at``）。"""
    return life_now_iso(timezone_name)


def _require_db(db_path: Path) -> bool:
    """db 文件存在返 True；否则打印 migrate/bootstrap 引导并返 False。"""
    if db_path.exists():
        return True
    click.echo(
        f"ERROR {db_path} 不存在。先运行：\n"
        f"  kindred db migrate --db {db_path}\n"
        f"  kindred db bootstrap --db {db_path}"
    )
    return False


def _echo_cold_start(db_path: Path) -> None:
    """空库（无 state_latest）时的统一引导。"""
    click.echo(
        f"ERROR {db_path} 是空库（无 state_latest）。\n先运行：kindred db bootstrap --db {db_path}"
    )
