"""Dream CLI group."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click

from kindred.cli_common import SCENARIO_CHOICES, _now_iso, _require_db, cli_compat
from kindred.config import DEFAULT_CONFIG_PATH
from kindred.db import KindredDB
from kindred.observability import configure_logging

if TYPE_CHECKING:
    from kindred.llm.client import LlmClient
    from kindred.llm.mock import Scenario


@click.group()
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
        "使用真 LLM（按 llm.provider 选：claude_code/anthropic/google/deepseek/openai/xai，"
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

    config = cli_compat("_load_cli_config")(
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
