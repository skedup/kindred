"""Kindred CLI 入口。

子命令：

- ``kindred tick``        跑一次 tick（真 LLM / mock fixture / 仅拓扑）
- ``kindred run``         启动 daemon（尚未实现）
- ``kindred db migrate``  建 SQLite schema
- ``kindred db status``   查询数据库状态
- ``kindred db bootstrap`` 种 1 行 cold_start tick（避 ``ColdStartError``）
- ``kindred inventory import`` 从私有文件一次性初始化运行期物品名册
- ``kindred inventory add`` 从私有文件向运行期物品名册增量追加
- ``kindred card activate`` 激活一张 card 的 runtime manifest（home 等实例级设置）
- ``kindred soul list-snapshots`` 列出可用灵魂快照
- ``kindred soul rollback --to <ts>`` 回滚灵魂到某次做梦前（不依赖 git）

``tick`` 三种模式（按优先级）：

- ``--real``                真 LLM（按 ``llm.provider`` 选）+ 真 db 走完整 graph
- ``--mock --scenario=X``   ``MockLlmClient`` 喂剧本 + 真 db 走完整 graph
- ``--mock`` / ``--dry-run`` 顶层 mock 透传，仅验拓扑（无 db / 无 LLM，最快）
- ``--act`` / ``--target-activity`` 仅作用于顶层 mock 透传路径
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from kindred import __version__
from kindred.character_card import CharacterCardError, activate_character_card
from kindred.config import (
    DEFAULT_CONFIG_PATH,
    KindredConfig,
    KindredConfigError,
    KindredSecretError,
    SoulHistoryLayout,
    build_overrides,
    install_runtime_secrets,
    load_kindred_config,
)
from kindred.db import (
    InventoryCatalogDataError,
    InventoryImportConflictError,
    KindredDB,
)
from kindred.graph.dream._land_fs import SoulLayout
from kindred.observability import DebugDumpConfig, PromptDumper, configure_logging
from kindred.openclaw.install import openclaw_cli
from kindred.runtime.clock import life_now_iso

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from kindred.llm.client import LlmClient
    from kindred.llm.mock import Scenario
    from kindred.state.tick import TickState

SCENARIO_CHOICES: tuple[str, ...] = (
    "act_false",
    "start_activity",
    "advance_activity",
    "failure",
)


@click.group()
@click.version_option(__version__, prog_name="kindred")
def cli() -> None:
    """Kindred — 一个能呼吸的 AI 伴侣框架。

    呼吸 = SQLite 里真的有一行 tick 数据。
    """


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


def _require_resident_commit(config: KindredConfig) -> None:
    """OPEN2 配置只有在 marker 提交后才能进入运行入口。"""
    from kindred.resident import ResidentInitError, require_committed_resident

    try:
        require_committed_resident(config)
    except ResidentInitError as exc:
        raise click.ClickException(f"Kindred Resident 错误：{exc}") from exc


def _require_openclaw_binding(config: KindredConfig) -> None:
    """OPEN3 配置已有 wire 时，Mouth binding 必须存在并一致。"""
    from kindred.openclaw.binding import OpenClawBindingError, require_openclaw_binding

    try:
        require_openclaw_binding(config)
    except OpenClawBindingError as exc:
        raise click.ClickException(f"Kindred OpenClaw 错误：{exc}") from exc


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


# ─────────────────────────────────────────────────────────────────────
# kindred tick
# ─────────────────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--mock",
    is_flag=True,
    help=(
        "使用 mock client（无真 LLM）。配合 --scenario 走真 db；"
        "不带 --scenario 时走顶层 mock 透传（最快）"
    ),
)
@click.option("--dry-run", is_flag=True, help="跑 graph 但不写 db（与 --mock 行为相同）")
@click.option(
    "--real",
    is_flag=True,
    help=(
        "使用真 LLM（按 llm.provider 选：claude_code/anthropic/google/deepseek/openai，"
        "默认 google）"
        "+ 真 db 跑完整 graph"
    ),
)
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
    help="life 根目录；派生 db/context bundle/SOUL/activity/debug 路径",
)
@click.option(
    "--model",
    "model",
    default=None,
    help=(
        "（--real 专用）覆盖 llm.model；claude_code/anthropic 如 haiku/sonnet，"
        "google 如 gemini-2.5-flash"
    ),
)
@click.option(
    "--scenario",
    type=click.Choice(SCENARIO_CHOICES),
    default=None,
    help="MockLlmClient 场景：act_false / start_activity / advance_activity / failure",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="SQLite 数据库路径（CLI override，优先级最高）",
)
@click.option(
    "--bundle-path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="context bundle 文件输出路径（CLI override，优先级最高）",
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
@click.option(
    "--act",
    is_flag=True,
    help="（顶层 mock 路径专用）强制 act_decision.act=True，走完整分支",
)
@click.option(
    "--target-activity",
    default="placeholder_activity",
    show_default=True,
    help="（顶层 mock 路径专用）--act 时填入 act_decision.target_activity",
)
def tick(
    mock: bool,
    dry_run: bool,
    real: bool,
    config_path: Path | None,
    life_root: Path | None,
    model: str | None,
    scenario: str | None,
    db_path: Path | None,
    bundle_path: Path | None,
    log_level: str | None,
    debug_dump_dir: Path | None,
    debug_dump: bool,
    act: bool,
    target_activity: str,
) -> None:
    """跑一次 tick（一次性命令）。

    三种模式（按优先级）：

    1. ``--real``：真 LLM（按 ``llm.provider`` 选）+ 真 db 走完整 graph
    2. ``--mock --scenario=<name>``：``MockLlmClient`` + 真 db 走完整 graph
    3. ``--mock`` / ``--dry-run``：顶层 mock 透传（无 db / 无 LLM，仅验拓扑）

    cold-start 处理：模式 1/2 下若 db 为空，先跑 ``kindred db bootstrap``
    种 1 行 cold_start tick，再 ``tick``。
    """
    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
        db_path=db_path,
        bundle_path=bundle_path,
        model=model,
        log_level=log_level,
        debug_dump=debug_dump,
        debug_dump_dir=debug_dump_dir,
    )
    prompt_dumper = _configure_observability(config)

    if real:
        _run_real_llm_tick(config, prompt_dumper=prompt_dumper)
        return

    if not (mock or dry_run):
        click.echo(
            "kindred tick 需要 --real / --mock / --dry-run 之一"
            "（--real 真 LLM；--mock --scenario 走 mock fixture；--dry-run 仅拓扑）"
        )
        return

    if mock and scenario is not None:
        _run_scenario_tick(scenario, config, prompt_dumper=prompt_dumper)
        return

    _run_mock_tick(
        act=act,
        target_activity=target_activity,
        timezone_name=config.world.timezone,
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


def _build_client_graph(
    client: LlmClient,
    db: KindredDB,
    *,
    config: KindredConfig,
    prompt_dumper: PromptDumper,
    resource_stack: ExitStack,
) -> CompiledStateGraph[TickState, Any, Any, Any]:
    """薄 wrapper——复用 runtime 共享工厂（earlier milestone N-2 去重）。

    真正装配逻辑在 ``kindred.runtime.tick_graph.build_client_tick_graph``，
    CLI 和 daemon 共用，避免两套组合点漂移。
    """
    from kindred.llm.client import ToolCapableLlmClient
    from kindred.runtime.tick_graph import build_client_tick_graph

    if not isinstance(client, ToolCapableLlmClient):
        raise KindredConfigError(
            "tick graph requires a tool-capable LLM provider for act.llm; "
            "set llm.provider=google/deepseek/openai or implement ToolCapableLlmClient."
        )
    return build_client_tick_graph(
        client,
        db,
        config=config,
        prompt_dumper=prompt_dumper,
        resource_stack=resource_stack,
    )


def _echo_tick_result(
    header: str,
    *,
    db_path: Path,
    bundle_path: Path,
    triggered_at: str,
    final: dict[str, Any],
) -> None:
    """打印一次完整 tick 的结果摘要（mock / 真 LLM 共用）。"""
    click.echo(header)
    click.echo(f"db:             {db_path}")
    click.echo(f"bundle:         {bundle_path}")
    click.echo(f"triggered_at:   {triggered_at}")
    click.echo(f"tick_id:        {final.get('tick_id')}")
    click.echo(f"act_decision:   {final.get('act_decision')}")
    if "act_result" in final:
        click.echo(f"act_result:     {final['act_result']}")
    else:
        click.echo("act_result:     (T2 skipped, act=False)")
    click.echo(f"note:           {final.get('note')}")
    click.echo(f"significance:   {final.get('significance')}")


def _run_scenario_tick(
    scenario_str: str,
    config: KindredConfig,
    *,
    prompt_dumper: PromptDumper,
) -> None:
    """``--mock --scenario=<name>``：MockLlmClient + 真 db 走完整 graph。"""
    from typing import cast

    from kindred.graph.errors import NodeContractError
    from kindred.graph.tick.sense_io import ColdStartError
    from kindred.llm.mock import MockLlmClient

    scenario = cast("Scenario", scenario_str)
    db_path = config.paths.db
    bundle_path = config.paths.context_bundle
    if not _require_db(db_path):
        return

    client = MockLlmClient(scenario=scenario)
    triggered_at = _now_iso(config.world.timezone)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    with ExitStack() as resources:
        db = resources.enter_context(KindredDB.open(db_path))
        graph = _build_client_graph(
            client,
            db,
            config=config,
            prompt_dumper=prompt_dumper,
            resource_stack=resources,
        )
        try:
            final = graph.invoke({"trigger_source": "heartbeat", "triggered_at": triggered_at})
        except ColdStartError:
            _echo_cold_start(db_path)
            return
        except NodeContractError as exc:
            click.echo(
                f"ERROR 节点契约错：{type(exc).__name__}: {exc}\n"
                f"      （这通常是 mock fixture 输出不符 schema，或 sense.llm 返坏结构）"
            )
            return

    _echo_tick_result(
        f"=== kindred tick (mock --scenario={scenario}) ===",
        db_path=db_path,
        bundle_path=bundle_path,
        triggered_at=triggered_at,
        final=final,
    )


def _run_real_llm_tick(
    config: KindredConfig,
    *,
    prompt_dumper: PromptDumper,
) -> None:
    """``--real``：真 LLM client（按 ``llm.provider`` 选）+ 真 db 走完整 graph。

    与 ``_run_scenario_tick``（MockLlmClient）同构，只是换 client。tick graph 的 act
    节点要求 ``ToolCapableLlmClient``；不具备工具环能力的 provider 在装配期 fail-fast。
    **走 ``build_llm_client(config)`` 与 daemon 同一入口**（earlier review N-3）：YAML 切
    ``llm.provider`` 后 tick --real 与 daemon 行为一致。

    fail-fast：LLM/配置挂了 raise，本函数 catch 后友好提示。
    """
    from kindred.config import KindredConfigError
    from kindred.graph.errors import NodeContractError
    from kindred.graph.tick.sense_io import ColdStartError
    from kindred.llm.client import LlmClientError
    from kindred.llm.factory import build_llm_client

    db_path = config.paths.db
    bundle_path = config.paths.context_bundle
    if not _require_db(db_path):
        return

    try:
        client = build_llm_client(config)
    except (LlmClientError, KindredConfigError) as exc:
        click.echo(f"ERROR 无法初始化真 LLM client：{exc}")
        return

    triggered_at = _now_iso(config.world.timezone)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with ExitStack() as resources:
            db = resources.enter_context(KindredDB.open(db_path))
            graph = _build_client_graph(
                client,
                db,
                config=config,
                prompt_dumper=prompt_dumper,
                resource_stack=resources,
            )
            try:
                final = graph.invoke({"trigger_source": "heartbeat", "triggered_at": triggered_at})
            except ColdStartError:
                _echo_cold_start(db_path)
                return
            except (NodeContractError, LlmClientError) as exc:
                click.echo(f"ERROR 真 LLM tick 失败（fail-fast）：{type(exc).__name__}: {exc}")
                return
    finally:
        client.close()

    _echo_tick_result(
        "=== kindred tick (--real) ===",
        db_path=db_path,
        bundle_path=bundle_path,
        triggered_at=triggered_at,
        final=final,
    )


def _run_mock_tick(*, act: bool, target_activity: str, timezone_name: str) -> None:
    """顶层 mock 透传路径——只验拓扑，不接 db / LLM。"""
    from kindred.graph import build_tick_graph

    graph = build_tick_graph()
    initial_state: dict[str, object] = {
        "trigger_source": "heartbeat",
        "triggered_at": _now_iso(timezone_name),
    }
    if act:
        initial_state["act_decision"] = {
            "act": True,
            "kind": "start_activity",
            "target_activity": target_activity,
            "reason": f"cli --act 强制走 T2 分支（mock 透传，target={target_activity}）",
        }

    final_state = graph.invoke(initial_state)

    fallback = "(mock 透传: T1.sense.llm 未写)"
    click.echo("=== kindred tick (mock) ===")
    click.echo(f"act_decision:   {final_state.get('act_decision')}")
    if "act_result" in final_state:
        click.echo(f"act_result:     {final_state['act_result']}")
    else:
        click.echo("act_result:     (T2 skipped, act=False)")
    click.echo(f"note:           {final_state.get('note', fallback)}")
    click.echo(f"significance:   {final_state.get('significance', fallback)}")


# ─────────────────────────────────────────────────────────────────────
# kindred run
# ─────────────────────────────────────────────────────────────────────


@cli.command()
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
    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
        log_level=log_level,
        debug_dump=debug_dump,
        debug_dump_dir=debug_dump_dir,
    )
    _require_resident_commit(config)
    _require_openclaw_binding(config)
    prompt_dumper = _configure_observability(config, log_file=config.daemon.log_file)
    from kindred.runtime.daemon import run_daemon

    click.echo(
        f"kindred run — heart daemon starting (life_root={config.paths.life_root}, "
        f"pid_file={config.daemon.pid_file}, log_file={config.daemon.log_file})"
    )
    ticks = run_daemon(config, prompt_dumper)
    click.echo(f"kindred run — stopped after {ticks} tick(s)")


def _platform_service(operation: str, **kwargs: Any) -> Any:
    from kindred.runtime import platform_service

    try:
        return getattr(platform_service, operation)(**kwargs)
    except platform_service.PlatformServiceError as exc:
        raise click.ClickException(str(exc)) from exc


def _doctor_preflight(config_path: Path) -> None:
    from kindred.openclaw.doctor import DoctorError, require_doctor_ready

    try:
        require_doctor_ready(config_path, online=True)
    except DoctorError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.option("--online", is_flag=True, help="增加无副作用 Gateway/LLM 联网检查")
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

    report = run_doctor(config_path, online=online)
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


@cli.command()
def start() -> None:
    """启动 Kindred-owned Heart/Web 平台服务。"""
    config_path = _platform_service("service_config_path")
    config = _load_cli_config(config_path=config_path)
    _require_resident_commit(config)
    _require_openclaw_binding(config)
    _doctor_preflight(config_path)
    _platform_service("control_services", config_path=config_path, action="start")


@cli.command()
def stop() -> None:
    """停止 Kindred-owned Heart/Web 平台服务。"""
    names = _platform_service("control_services", action="stop")
    click.echo(f"Kindred services stopped: {', '.join(names) or 'none'}")


@cli.command()
def status() -> None:
    """显示 Kindred-owned Heart/Web 平台服务状态。"""
    for name, installed, enabled, active in _platform_service("service_status"):
        click.echo(
            f"{name} installed={'yes' if installed else 'no'} "
            f"enabled={'yes' if enabled else 'no'} active={'yes' if active else 'no'}"
        )


@cli.command()
@click.option("--service", type=click.Choice(("heart", "web")), default="heart")
@click.option("--follow", is_flag=True, help="持续跟随日志")
@click.option("--lines", type=click.IntRange(1, 10000), default=200, show_default=True)
def logs(service: str, follow: bool, lines: int) -> None:
    """读取 Kindred-owned Heart/Web 平台日志。"""
    _platform_service("show_logs", service=service, follow=follow, lines=lines)


# ─────────────────────────────────────────────────────────────────────
# kindred db ...
# ─────────────────────────────────────────────────────────────────────


@cli.group()
def db() -> None:
    """数据库管理子命令组。"""


@db.command(name="migrate")
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
    help="life 根目录；未显式 --db 时派生 SQLite 路径",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="SQLite 数据库路径（CLI override，优先级最高）。",
)
def db_migrate(
    config_path: Path | None,
    life_root: Path | None,
    db_path: Path | None,
) -> None:
    """应用 schema.sql 到数据库（幂等）。

    首次运行：建 tick / thought / schema_version 表与 state_latest /
    episode 视图。重入运行：安全无副作用。
    """
    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
        db_path=db_path,
        load_secrets=False,
    )
    configure_logging(config.logging.level)
    db_path = config.paths.db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with KindredDB.open(db_path) as db:
        version = db.get_schema_version()
        click.echo(f"OK    migrated {db_path} -> schema v{version}")


@db.command(name="status")
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
    help="life 根目录；未显式 --db 时派生 SQLite 路径",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="SQLite 数据库路径（CLI override，优先级最高）。",
)
def db_status(
    config_path: Path | None,
    life_root: Path | None,
    db_path: Path | None,
) -> None:
    """查询数据库状态（schema 版本 + tick / thought / episode_recall / place_visits 计数）。

    注意：``KindredDB.open()`` 会自动 migrate，所以 ``schema_version`` 从不为空
    ——不存在“db 文件存在但未 migrate”这种中间态。
    """
    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
        db_path=db_path,
        load_secrets=False,
    )
    configure_logging(config.logging.level)
    db_path = config.paths.db
    if not db_path.exists():
        click.echo(f"ERROR {db_path} 不存在（运行 `kindred db migrate` 创建）")
        return
    with KindredDB.open(db_path) as db:
        click.echo(f"db_path:        {db_path}")
        click.echo(f"schema_version: {db.get_schema_version()}")
        click.echo(f"tick:           {db.count_ticks()} rows")
        click.echo(f"thought:        {db.count_thoughts()} rows")
        click.echo(f"episode_recall: {db.count_episode_recalls()} rows")
        click.echo(f"place_visits:   {db.count_place_visits()} rows")


@db.command(name="bootstrap")
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
    help="life 根目录；未显式 --db 时派生 SQLite 路径",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="SQLite 数据库路径（CLI override，优先级最高）。",
)
@click.option(
    "--force",
    is_flag=True,
    help="即使 db 已有 tick 行也覆盖写入 cold_start tick",
)
def db_bootstrap(
    config_path: Path | None,
    life_root: Path | None,
    db_path: Path | None,
    *,
    force: bool,
) -> None:
    """种 1 行 cold_start tick——给空库一个起点。

    这是开发期的最小占位：用 ``kindred.state._seed.make_doc_example_state()``
    当 seed state。未来真 daemon 会有 daemon-driven bootstrap（character card
    加载 + 真 prompt + LLM 生成 first state）替换它。

    使用顺序::

        kindred db migrate --db /path/to/kindred.db
        kindred db bootstrap --db /path/to/kindred.db
        kindred tick --mock --scenario=start_activity --db /path/to/kindred.db
    """
    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
        db_path=db_path,
        load_secrets=False,
    )
    configure_logging(config.logging.level)
    db_path = config.paths.db
    if not db_path.exists():
        click.echo(f"ERROR {db_path} 不存在（先运行 `kindred db migrate`）")
        return

    with KindredDB.open(db_path) as db_inst:
        if not force and db_inst.count_ticks() > 0:
            click.echo(
                f"WARN  {db_path} 已有 {db_inst.count_ticks()} 行 tick，跳过 bootstrap。\n"
                f"      若要强制重种，加 --force（仅开发期使用，会污染 state_latest）"
            )
            return

        # cli 是产品代码，必须走 facade 公开 API（db.insert_tick(TickWriteParams)）
        # 而不是 _conn 私有访问，以保全事务边界保护。
        from kindred.db import TickWriteParams
        from kindred.state._seed import make_doc_example_state

        seed_state = make_doc_example_state()
        cold_start_ad = {
            "act": False,
            "reason": "cli db bootstrap: cold_start seed (placeholder)",
        }
        with db_inst.transaction():
            tick_id = db_inst.insert_tick(
                TickWriteParams(
                    state=seed_state,
                    trigger_source="cold_start",
                    triggered_at=seed_state.time.iso,
                    act_decision=cold_start_ad,
                )
            )
        click.echo(
            f"OK    bootstrapped {db_path} with seed cold_start tick (id={tick_id}, "
            f"state.time.iso={seed_state.time.iso})"
        )


# ───────────────────────────────────────────────────────────────
# kindred inventory ...
# ───────────────────────────────────────────────────────────────


@cli.group()
def inventory() -> None:
    """运行期私有物品名册管理。"""


@inventory.command(name="import")
@click.argument("private_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--life-root", type=click.Path(file_okay=False, path_type=Path))
def inventory_import(
    private_file: Path,
    config_path: Path | None,
    life_root: Path | None,
) -> None:
    """从私有 YAML 向空 Catalog 做一次原子初始化。"""
    from kindred.inventory.catalog import InventoryCatalogError, load_inventory_import

    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
    )
    configure_logging(config.logging.level)
    try:
        document = load_inventory_import(private_file)
        with KindredDB.open(config.paths.db) as db_inst:
            with db_inst.transaction():
                imported = db_inst.import_inventory_items(document.items)
    except (InventoryCatalogError, InventoryCatalogDataError, InventoryImportConflictError) as exc:
        raise click.ClickException(str(exc)) from None

    counts = {pool: 0 for pool in ("wardrobe", "bedside", "storage")}
    for item in document.items:
        counts[item.home_pool] += 1
    status = "imported" if imported else "no-op"
    click.echo(
        f"OK    status={status} total={len(document.items)} "
        f"wardrobe={counts['wardrobe']} bedside={counts['bedside']} storage={counts['storage']}"
    )


@inventory.command(name="add")
@click.argument("private_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--life-root", type=click.Path(file_okay=False, path_type=Path))
def inventory_add(
    private_file: Path,
    config_path: Path | None,
    life_root: Path | None,
) -> None:
    """从私有 YAML 向现有 Catalog 原子追加物品。"""
    from kindred.db import InventoryAddConflictError
    from kindred.inventory.catalog import InventoryCatalogError, load_inventory_import

    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
    )
    configure_logging(config.logging.level)
    try:
        document = load_inventory_import(private_file)
        with KindredDB.open(config.paths.db) as db_inst:
            with db_inst.transaction():
                added, unchanged = db_inst.add_inventory_items(document.items)
    except (InventoryCatalogError, InventoryCatalogDataError, InventoryAddConflictError) as exc:
        raise click.ClickException(str(exc)) from None

    counts = {pool: 0 for pool in ("wardrobe", "bedside", "storage")}
    for item in document.items:
        counts[item.home_pool] += 1
    status = "added" if added else "no-op"
    click.echo(
        f"OK    status={status} total={len(document.items)} added={added} unchanged={unchanged} "
        f"wardrobe={counts['wardrobe']} bedside={counts['bedside']} storage={counts['storage']}"
    )


# ───────────────────────────────────────────────────────────────
# kindred card ...
# ───────────────────────────────────────────────────────────────


@cli.group()
def card() -> None:
    """Character Card 管理子命令组。"""


@card.command(name="activate")
@click.argument(
    "card_dir",
    type=click.Path(exists=True, path_type=Path),
)
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
    help="life 根目录；未显式配置时派生 runtime character-card 路径",
)
@click.option(
    "--force",
    is_flag=True,
    help="覆盖已有 runtime character-card（不会覆盖 SOUL/DB）",
)
def card_activate(
    card_dir: Path,
    config_path: Path | None,
    life_root: Path | None,
    *,
    force: bool,
) -> None:
    """激活 card manifest，让 home 等实例级设置进入 runtime。

    本命令只复制 ``card.yaml`` 到 ``paths.character_card``，用于把 full card 的
    ``home`` 暴露给 LocationProvider / act prompt。它**不**覆盖 SOUL、不写 DB、
    不处理 seed_state；完整搬家 install 另行实现。
    """
    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
    )
    configure_logging(config.logging.level)
    try:
        manifest = activate_character_card(
            card_dir,
            target_path=config.paths.character_card,
            force=force,
        )
    except CharacterCardError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"OK    activated card {manifest.name!r} ({manifest.form}) -> {config.paths.character_card}"
    )
    if manifest.home is not None:
        click.echo(
            "home: "
            f"name={manifest.home.name} "
            f"city={manifest.home.city or '（空）'} "
            f"address={manifest.home.address or '（空）'} "
            f"place_key={manifest.home.place_key}"
        )
    else:
        click.echo("home: （无；rest 不会获得 character-card home 候选）")


# ───────────────────────────────────────────────────────────────
# kindred soul ...
# ───────────────────────────────────────────────────────────────


@cli.group()
def soul() -> None:
    """灵魂演化管理子命令组（做梦产物：snapshots / rollback）。"""


def _soul_layout(config: KindredConfig) -> SoulLayout:
    return SoulLayout(
        soul_full=config.paths.soul_full,
        identity=config.paths.identity,
        user=config.paths.user,
    )


@soul.command(name="list-snapshots")
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
    "--include-emergency",
    is_flag=True,
    help="一并列出 emergency/ 下的回滚前备份（可用 rollback --to-emergency 恢复）",
)
def soul_list_snapshots(
    config_path: Path | None,
    life_root: Path | None,
    *,
    include_emergency: bool,
) -> None:
    """列出可用的灵魂快照时间戳（按时间序）。"""
    from kindred.graph.dream._rollback import list_snapshots

    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
        load_secrets=False,
    )
    configure_logging(config.logging.level)
    sh = SoulHistoryLayout.from_dir(config.paths.soul_history_dir)
    snapshot_root = sh.snapshot_root
    snaps = list_snapshots(snapshot_root)
    if not snaps:
        click.echo(f"（无快照）snapshot_root={snapshot_root}")
    else:
        click.echo(f"可用灵魂快照（{snapshot_root}）：")
        for s in snaps:
            click.echo(f"  {s}")
    if include_emergency:
        emergency_root = sh.emergency_root
        emgs = list_snapshots(emergency_root)
        if not emgs:
            click.echo(f"（无 emergency 备份）emergency_root={emergency_root}")
        else:
            click.echo(f"emergency 备份（{emergency_root}，用 --to-emergency 恢复）：")
            for s in emgs:
                click.echo(f"  {s}")


@soul.command(name="rollback")
@click.option("--to", "target_ts", default=None, help="目标做梦快照时间戳，如 2026-05-27T04-00")
@click.option(
    "--to-emergency",
    "emergency_ts",
    default=None,
    help="恢复某次回滚前的 emergency 备份（用 list-snapshots --include-emergency 查时间戳）",
)
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
    "--yes",
    is_flag=True,
    help="跳过交互确认（回滚会覆盖当前灵魂，仅在确信时用）",
)
def soul_rollback(
    target_ts: str | None,
    emergency_ts: str | None,
    config_path: Path | None,
    life_root: Path | None,
    *,
    yes: bool,
) -> None:
    """把灵魂回滚到某次做梦前的状态（不依赖 git，docs §8.3）。

    回滚前会把当前灵魂备份到 ``soul-history/emergency/<now>/``（安全网），
    然后从目标恢复。index.json 会追加一条 rollback 事件。

    二选一：``--to <ts>``（做梦快照）或 ``--to-emergency <ts>``（回滚前备份）。

    使用::

        kindred soul list-snapshots [--include-emergency]
        kindred soul rollback --to 2026-05-27T04-00
        kindred soul rollback --to-emergency 2026-06-12T20-15-30-123456
    """
    from kindred.graph.dream._rollback import RollbackError, rollback_soul

    if (target_ts is None) == (emergency_ts is None):
        raise click.UsageError("请二选一：--to <ts> 或 --to-emergency <ts>")
    target_id = target_ts if target_ts is not None else emergency_ts
    assert target_id is not None  # 上面互斥校验保证
    target_kind = "snapshot" if target_ts is not None else "emergency"

    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
        load_secrets=False,
    )
    configure_logging(config.logging.level)
    layout = _soul_layout(config)
    soul_history_dir = config.paths.soul_history_dir
    sh = SoulHistoryLayout.from_dir(soul_history_dir)
    snapshot_root = sh.snapshot_root
    emergency_root = sh.emergency_root
    index_path = sh.index_path

    if not yes:
        click.echo(
            f"即将把灵魂回滚到 {target_id}（{target_kind}）。\n"
            f"当前灵魂会先备份到 {emergency_root}/<now>/。"
        )
        if not click.confirm("确认回滚？"):
            click.echo("已取消。")
            return

    try:
        emergency_dir = rollback_soul(
            target_id,
            target_kind=target_kind,  # type: ignore[arg-type]
            layout=layout,
            snapshot_root=snapshot_root,
            emergency_root=emergency_root,
            index_path=index_path,
            soul_history_dir=soul_history_dir,
            excerpt_path=config.paths.soul_excerpt,
        )
    except RollbackError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"OK    灵魂已回滚到 {target_id}（{target_kind}）")
    click.echo(
        f"      当前灵魂已备份到 {emergency_dir}\n"
        f"      （如需恢复：kindred soul rollback --to-emergency {emergency_dir.name}）"
    )


# ───────────────────────────────────────────────────────────────
# kindred dream ...
# ───────────────────────────────────────────────────────────────


@cli.group()
def dream() -> None:
    """做梦子命令组（清晨 04:00 daemon 触发；run 用于手动测试）。"""


def _yesterday_date(triggered_at: str) -> str:
    """从触发时刻算被总结的「昨日」（YYYY-MM-DD）。清晨做梦总结的是前一天。"""
    from datetime import datetime, timedelta

    dt = datetime.fromisoformat(triggered_at)
    return (dt - timedelta(days=1)).strftime("%Y-%m-%d")


def _validate_dream_date(
    ctx: click.Context,  # noqa: ARG001 - click callback 签名
    param: click.Parameter,  # noqa: ARG001
    value: str | None,
) -> str | None:
    """校验 ``--dream-date`` 为严格 YYYY-MM-DD（N-2）。

    坏日期会被底层 day_window 软降级放过，但 land 仍用原字符串生成
    ``snapshots/<bad>T04-00/`` / ``dream-<bad>.md`` / index timestamp，制造不可
    恢复的坏历史记录。在 CLI 边界 fail-closed。
    """
    if value is None:
        return None
    from datetime import datetime

    # N-3：strptime("%Y-%m-%d") 会接受非零填充的 2026-6-1，仍会打散 soul-history
    # 时间戳/目录规范。用 round-trip（strftime == value）卡死严格 YYYY-MM-DD：
    # 既拒非零填充又拒非法日期，一道门覆盖 N-2 + N-3。
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")  # noqa: DTZ007 - 仅校验格式
    except ValueError as exc:
        raise click.BadParameter(
            f"日期格式需为严格 YYYY-MM-DD（零填充），得到 {value!r}", param=param
        ) from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise click.BadParameter(
            f"日期需严格零填充 YYYY-MM-DD（如 2026-06-01），得到 {value!r}", param=param
        )
    return value


@dream.command(name="run")
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
    "--model",
    "model",
    default=None,
    help="（--real 专用）覆盖默认模型 id",
)
@click.option(
    "--dream-date",
    default=None,
    callback=_validate_dream_date,
    help="被总结的昨日 YYYY-MM-DD（不填由 triggered_at 算前一天）",
)
@click.option(
    "--real",
    is_flag=True,
    help=(
        "使用真 LLM（按 llm.provider 选：claude_code/anthropic/google/deepseek/openai，"
        "默认 google）"
        "；不加走 mock fixture"
    ),
)
@click.option(
    "--scenario",
    type=click.Choice(SCENARIO_CHOICES),
    default="start_activity",
    show_default=True,
    help="（mock 默认）MockLlmClient 场景",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
    default=None,
    help="覆盖 logging.level",
)
def dream_run(
    config_path: Path | None,
    life_root: Path | None,
    model: str | None,
    dream_date: str | None,
    real: bool,
    scenario: str,
    log_level: str | None,
) -> None:
    """手动跑一次做梦（总结昨日 + 重置 stack + 反思 + 闸门 + 落盘）。

    为接 daemon 清晨触发前的手动验证入口（docs §13 Phase 1）。两种模式：

    - ``--real``：真 LLM + 真 db 走完整 dream graph
    - 默认（mock）：MockLlmClient + 真 db
    """
    from typing import cast

    from kindred.graph.errors import NodeContractError
    from kindred.runtime.dream_graph import build_client_dream_graph

    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
        model=model,
        log_level=log_level,
    )
    configure_logging(config.logging.level)
    db_path = config.paths.db
    if not _require_db(db_path):
        return

    triggered_at = _now_iso(config.world.timezone)
    resolved_date = dream_date or _yesterday_date(triggered_at)

    client: LlmClient
    if real:
        # build_llm_client：按 llm.provider 选，与 daemon 同入口（earlier review N-3）。
        from kindred.config import KindredConfigError
        from kindred.llm.client import LlmClientError
        from kindred.llm.factory import build_llm_client

        try:
            client = build_llm_client(config)
        except (LlmClientError, KindredConfigError) as exc:
            click.echo(f"ERROR 无法初始化真 LLM client：{exc}")
            return
    else:
        from kindred.llm.mock import MockLlmClient

        client = MockLlmClient(scenario=cast("Scenario", scenario))

    try:
        with KindredDB.open(db_path) as db:
            prev_state = db.get_state_latest() or {}
            graph = build_client_dream_graph(client, db, config=config)
            try:
                final = graph.invoke(
                    {
                        "triggered_at": triggered_at,
                        "dream_date": resolved_date,
                        "prev_state": prev_state,
                    }
                )
            except (NodeContractError, LlmClientError) as exc:
                click.echo(f"ERROR dream 失败（fail-fast）：{type(exc).__name__}: {exc}")
                return
    finally:
        if real:
            client.close()  # type: ignore[union-attr]

    click.echo(f"=== kindred dream run ({'--real' if real else 'mock'}) ===")
    click.echo(f"dream_date:        {resolved_date}")
    click.echo(f"pruned_count:      {final.get('pruned_count')}")
    verdict = final.get("gate_verdict") or {}
    click.echo(f"gate_verdict:      {verdict.get('verdict') if verdict else '(未走闸门)'}")
    click.echo(f"snapshot_path:     {final.get('snapshot_path', '(未落盘 / 被撤销)')}")
    click.echo(f"dream_journal_path: {final.get('dream_journal_path', '(无)')}")


@cli.command(name="serve")
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
    help="life 根目录；未显式 --db 时派生 SQLite 路径",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="SQLite 数据库路径（CLI override，优先级最高）。",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="监听地址")
@click.option("--port", default=8787, show_default=True, type=int, help="监听端口")
def serve(
    config_path: Path | None,
    life_root: Path | None,
    db_path: Path | None,
    host: str,
    port: int,
) -> None:
    """启动 web 后端：只读可视化（观察 ta 的生活）。

    挂只读投影 ``/healthz`` / ``/now`` / ``/stream`` / ``/episodes`` /
    ``/interior/history`` / ``/artifacts``，绝不写库（``mode=ro``）。
    需要 web extra：``pip install kindred[web]``。
    """
    config = _load_cli_config(
        config_path=config_path,
        life_root=life_root,
        db_path=db_path,
    )
    _require_resident_commit(config)
    _require_openclaw_binding(config)
    configure_logging(config.logging.level)
    resolved_db = config.paths.db

    try:
        import uvicorn

        from kindred.web.app import create_app
        from kindred.web.assets import WebAssetError, resolve_web_dist
    except ModuleNotFoundError as exc:  # pragma: no cover - 取决于是否装 extra
        raise click.ClickException(
            "缺少 web 依赖。安装：pip install 'kindred[web]'（fastapi + uvicorn）"
        ) from exc

    try:
        static_dir = resolve_web_dist()
    except WebAssetError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Kindred 可视化（只读）：http://{host}:{port}/")
    click.echo(f"db: {resolved_db}（{'存在' if resolved_db.exists() else '尚未创建—空态'}）")
    # N-7：把已解析的 config 注入 app，别让 create_app 二次 load 丢掉 --config 的 web 设置。
    # 显式 --db 是只看任意 SQLite 的调试入口，不猜配套 ArtifactStore。
    # config/life-root 正常入口由 create_app 使用同一 config 装配 db + artifacts/。
    app = create_app(
        config=config,
        db_path=resolved_db if db_path is not None else None,
        static_dir=static_dir,
    )
    uvicorn.run(app, host=host, port=port)


cli.add_command(openclaw_cli)


def main() -> None:
    """Entry point for `python -m kindred` and `kindred` console script."""
    cli()


if __name__ == "__main__":
    main()
