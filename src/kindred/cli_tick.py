"""The `kindred tick` command and its runtime helpers."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from kindred.cli_common import (
    SCENARIO_CHOICES,
    _echo_cold_start,
    _now_iso,
    _require_db,
    cli_compat,
)
from kindred.config import DEFAULT_CONFIG_PATH, KindredConfig, KindredConfigError
from kindred.db import KindredDB
from kindred.observability import PromptDumper

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from kindred.llm.client import LlmClient
    from kindred.llm.mock import Scenario
    from kindred.state.tick import TickState


@click.command()
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
        "使用真 LLM（按 llm.provider 选：claude_code/anthropic/google/deepseek/openai/xai，"
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
    config = cli_compat("_load_cli_config")(
        config_path=config_path,
        life_root=life_root,
        db_path=db_path,
        bundle_path=bundle_path,
        model=model,
        log_level=log_level,
        debug_dump=debug_dump,
        debug_dump_dir=debug_dump_dir,
    )
    prompt_dumper = cli_compat("_configure_observability")(config)

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
            "set llm.provider=google/deepseek/openai/xai or implement ToolCapableLlmClient."
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
        from kindred.runtime.observed_invoke import (
            create_runtime_telemetry,
            observed_invoke,
        )

        telemetry = create_runtime_telemetry(config)
        resources.callback(telemetry.close)
        db = resources.enter_context(KindredDB.open(db_path))
        graph = _build_client_graph(
            client,
            db,
            config=config,
            prompt_dumper=prompt_dumper,
            resource_stack=resources,
        )
        try:
            final = observed_invoke(
                lambda: graph.invoke({"trigger_source": "heartbeat", "triggered_at": triggered_at}),
                telemetry=telemetry,
                run_kind="tick",
                execution_mode="mock",
                trigger_source="heartbeat",
            )
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
            from kindred.runtime.observed_invoke import (
                create_runtime_telemetry,
                observed_invoke,
            )

            telemetry = create_runtime_telemetry(config)
            resources.callback(telemetry.close)
            db = resources.enter_context(KindredDB.open(db_path))
            graph = _build_client_graph(
                client,
                db,
                config=config,
                prompt_dumper=prompt_dumper,
                resource_stack=resources,
            )
            try:
                final = observed_invoke(
                    lambda: graph.invoke(
                        {"trigger_source": "heartbeat", "triggered_at": triggered_at}
                    ),
                    telemetry=telemetry,
                    run_kind="tick",
                    execution_mode="real",
                    trigger_source="heartbeat",
                )
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
