"""Database-management CLI group."""

from __future__ import annotations

from pathlib import Path

import click

from kindred.cli_common import cli_compat
from kindred.config import DEFAULT_CONFIG_PATH
from kindred.db import KindredDB
from kindred.observability import configure_logging


@click.group()
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
    config = cli_compat("_load_cli_config")(
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
    config = cli_compat("_load_cli_config")(
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
    config = cli_compat("_load_cli_config")(
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
