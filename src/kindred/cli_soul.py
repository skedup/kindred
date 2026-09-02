"""Soul snapshot and rollback CLI group."""

from __future__ import annotations

from pathlib import Path

import click

from kindred.cli_common import cli_compat
from kindred.config import DEFAULT_CONFIG_PATH, KindredConfig, SoulHistoryLayout
from kindred.graph.dream._land_fs import SoulLayout
from kindred.observability import configure_logging


@click.group()
def soul() -> None:
    """灵魂演化管理子命令组（做梦产物：snapshots / rollback）。"""


def _soul_layout(config: KindredConfig) -> SoulLayout:
    return SoulLayout(
        soul_full=config.paths.soul_full,
        identity=config.paths.identity,
        user=config.paths.user,
    )


def _require_local_rollback_layout(config: KindredConfig, life_root: Path | None) -> None:
    if life_root is None:
        return
    root = life_root.expanduser().resolve()
    paths = {
        "SOUL.md": config.paths.soul_full,
        "IDENTITY.md": config.paths.identity,
        "USER.md": config.paths.user,
        "SOUL_excerpt.md": config.paths.soul_excerpt,
        "soul-history": config.paths.soul_history_dir,
    }
    outside = tuple(name for name, path in paths.items() if not path.resolve().is_relative_to(root))
    if outside:
        raise click.ClickException(
            "--life-root 不能让 rollback 组合临时历史与外部 Persona；"
            "请改用一份完整、隔离的 --config。"
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

    config = cli_compat("_load_cli_config")(
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

    config = cli_compat("_load_cli_config")(
        config_path=config_path,
        life_root=life_root,
        load_secrets=False,
    )
    _require_local_rollback_layout(config, life_root)
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
