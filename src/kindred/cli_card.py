"""Character-card CLI group."""

from __future__ import annotations

from pathlib import Path

import click

from kindred.character_card import CharacterCardError, activate_character_card
from kindred.cli_common import cli_compat
from kindred.config import DEFAULT_CONFIG_PATH
from kindred.observability import configure_logging


@click.group()
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
    config = cli_compat("_load_cli_config")(
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
