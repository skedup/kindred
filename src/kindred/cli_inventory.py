"""Private runtime-inventory CLI group."""

from __future__ import annotations

from pathlib import Path

import click

from kindred.cli_common import cli_compat
from kindred.db import InventoryCatalogDataError, InventoryImportConflictError, KindredDB
from kindred.observability import configure_logging


@click.group()
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

    config = cli_compat("_load_cli_config")(
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

    config = cli_compat("_load_cli_config")(
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
