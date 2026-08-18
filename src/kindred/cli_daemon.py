"""Daemon, doctor, and platform-service CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import click

from kindred.cli_common import cli_compat
from kindred.config import DEFAULT_CONFIG_PATH, ENV_CONFIG, KindredConfig
from kindred.db import KindredDB


@click.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Kindred YAML 配置路径（默认 {DEFAULT_CONFIG_PATH}，也可用 KINDRED_CONFIG）",
)
@click.option(
    "--life-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="life 根目录",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
    default=None,
    help="覆盖 logging.level",
)
@click.option(
    "--debug-dump-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="开启 prompt debug dump 并写入指定目录（含 SOUL/对话/prompt，敏感）",
)
@click.option(
    "--debug-dump",
    is_flag=True,
    help="开启 prompt debug dump，使用配置中的 dump_dir（默认 life/debug，敏感）",
)
def run(
    config_path: Path | None,
    life_root: Path | None,
    log_level: str | None,
    debug_dump_dir: Path | None,
    debug_dump: bool,
) -> None:
    """启动 daemon（常驻心跳）。

    earlier milestone：纯 in-process 自循环——按 HeartbeatScheduler 节奏（醒 5min / 睡 1h）
    循环 invoke tick_graph，cold_start 首 tick 立即对齐 state，SIGTERM graceful。
    尚不含 watcher（你说话→心感知）/ ws push / dream，见 docs/13 后续刀。
    """
    config = cli_compat("_load_cli_config")(
        config_path=config_path,
        life_root=life_root,
        log_level=log_level,
        debug_dump=debug_dump,
        debug_dump_dir=debug_dump_dir,
    )
    cli_compat("_require_resident_commit")(config)
    cli_compat("_require_mouth_host_runtime")(config)
    cli_compat("_doctor_preflight")(_effective_config_path(config_path))
    cli_compat("_require_relationship_runtime")(config)
    prompt_dumper = cli_compat("_configure_observability")(config, log_file=config.daemon.log_file)
    from kindred.runtime.daemon import run_daemon

    click.echo(
        f"kindred run — heart daemon starting (life_root={config.paths.life_root}, "
        f"pid_file={config.daemon.pid_file}, log_file={config.daemon.log_file})"
    )
    ticks = run_daemon(config, prompt_dumper)
    click.echo(f"kindred run — stopped after {ticks} tick(s)")


def _require_relationship_runtime(config: KindredConfig) -> None:
    from kindred.relationship.preflight import (
        RelationshipPreflightError,
        require_user_relationship,
    )

    try:
        with KindredDB.open_readonly(config.paths.db) as db:
            require_user_relationship(db)
    except RelationshipPreflightError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception:
        raise click.ClickException(
            "relationship preflight path=relationship_profile.user reason=database_unreadable"
        ) from None


def _platform_service(operation: str, **kwargs: Any) -> Any:
    from kindred.runtime import platform_service

    try:
        return getattr(platform_service, operation)(**kwargs)
    except platform_service.PlatformServiceError as exc:
        raise click.ClickException(str(exc)) from exc


def _doctor_preflight(config_path: Path | None) -> None:
    from kindred.openclaw.doctor import DoctorError, require_doctor_ready

    try:
        require_doctor_ready(config_path, online=True)
    except DoctorError as exc:
        raise click.ClickException(str(exc)) from exc


def _effective_config_path(config_path: Path | None) -> Path | None:
    if config_path is not None:
        return config_path
    value = os.environ.get(ENV_CONFIG)
    return Path(value) if value else None


@click.command()
@click.option(
    "--online",
    is_flag=True,
    help="增加 Gateway/LLM 联网兼容性检查（LLM 可能计费）",
)
@click.option("--json", "as_json", is_flag=True, help="输出稳定 JSON 报告")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Kindred YAML 配置路径（默认 {DEFAULT_CONFIG_PATH}，也可用 KINDRED_CONFIG）",
)
def doctor(online: bool, as_json: bool, config_path: Path | None) -> None:
    """诊断本地 Resident；--online 额外验证 Gateway 与 Heart LLM。"""
    from kindred.openclaw.doctor import run_doctor

    report = run_doctor(_effective_config_path(config_path), online=online)
    if as_json:
        click.echo(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        for check in report.checks:
            retry = " retryable=yes" if check["retryable"] else ""
            click.echo(
                f"{str(check['status']).upper():7} {check['check_id']}{retry} - {check['hint']}"
            )
    if report.exit_code:
        raise click.exceptions.Exit(report.exit_code)


@click.command()
def start() -> None:
    """启动 Kindred-owned Heart/Web 平台服务。"""
    config_path = cli_compat("_platform_service")("service_config_path")
    config = cli_compat("_load_cli_config")(config_path=config_path)
    cli_compat("_require_resident_commit")(config)
    cli_compat("_require_mouth_host_runtime")(config)
    cli_compat("_doctor_preflight")(config_path)
    cli_compat("_platform_service")("control_services", config_path=config_path, action="start")


@click.command()
def stop() -> None:
    """停止 Kindred-owned Heart/Web 平台服务。"""
    names = cli_compat("_platform_service")("control_services", action="stop")
    click.echo(f"Kindred services stopped: {', '.join(names) or 'none'}")


@click.command()
def status() -> None:
    """显示 Kindred-owned Heart/Web 平台服务状态。"""
    for name, installed, enabled, active in cli_compat("_platform_service")("service_status"):
        click.echo(
            f"{name} installed={'yes' if installed else 'no'} "
            f"enabled={'yes' if enabled else 'no'} active={'yes' if active else 'no'}"
        )


@click.command()
@click.option("--service", type=click.Choice(("heart", "web")), default="heart")
@click.option("--follow", is_flag=True, help="持续跟随日志")
@click.option("--lines", type=click.IntRange(1, 10000), default=200, show_default=True)
def logs(service: str, follow: bool, lines: int) -> None:
    """读取 Kindred-owned Heart/Web 平台日志。"""
    cli_compat("_platform_service")("show_logs", service=service, follow=follow, lines=lines)
