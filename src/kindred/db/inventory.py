import json
import sqlite3

from pydantic import ValidationError

from kindred.inventory.catalog import InventoryItem

_COLUMNS = "item_key, name, home_pool, kind, equip_to_json, description"


class InventoryCatalogDataError(RuntimeError):
    """SQLite Catalog 行违反严格契约。"""


class InventoryImportConflictError(RuntimeError):
    """非空 Catalog 与初始化文件不完全一致。"""


class InventoryAddConflictError(RuntimeError):
    """增量输入与 Catalog 中已有同 key 物品不一致。"""


def _row_to_item(row: sqlite3.Row) -> InventoryItem:
    try:
        raw = dict(row)
        raw["equip_to"] = json.loads(raw.pop("equip_to_json"))
        return InventoryItem.model_validate(raw)
    except (json.JSONDecodeError, TypeError, UnicodeError, ValidationError):
        raise InventoryCatalogDataError("inventory catalog row validation_failed") from None


def get_inventory_item(conn: sqlite3.Connection, item_key: str) -> InventoryItem | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM inventory_items WHERE item_key = ?", (item_key,)
    ).fetchone()
    return None if row is None else _row_to_item(row)


def list_inventory_items(conn: sqlite3.Connection) -> list[InventoryItem]:
    rows = conn.execute(f"SELECT {_COLUMNS} FROM inventory_items ORDER BY item_key").fetchall()
    return [_row_to_item(row) for row in rows]


def count_inventory_items(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0])


def _insert_inventory_items(conn: sqlite3.Connection, items: list[InventoryItem]) -> None:
    conn.executemany(
        f"INSERT INTO inventory_items ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                item.item_key,
                item.name,
                item.home_pool,
                item.kind,
                json.dumps(item.equip_to, separators=(",", ":")),
                item.description,
            )
            for item in items
        ],
    )


def _import_inventory_items_impl(conn: sqlite3.Connection, items: list[InventoryItem]) -> bool:
    ordered = sorted(items, key=lambda item: item.item_key)
    existing = list_inventory_items(conn)
    if existing:
        if existing == ordered:
            return False
        raise InventoryImportConflictError("inventory import catalog_conflict")
    if not ordered:
        return False
    _insert_inventory_items(conn, ordered)
    return True


def _add_inventory_items_impl(
    conn: sqlite3.Connection,
    items: list[InventoryItem],
) -> tuple[int, int]:
    """增量追加物品；返回 ``(added, unchanged)``。"""
    if len({item.item_key for item in items}) != len(items):
        raise InventoryAddConflictError("inventory add duplicate_item_key")

    existing = {item.item_key: item for item in list_inventory_items(conn)}
    pending: list[InventoryItem] = []
    unchanged = 0
    for item in sorted(items, key=lambda candidate: candidate.item_key):
        current = existing.get(item.item_key)
        if current is None:
            pending.append(item)
        elif current == item:
            unchanged += 1
        else:
            raise InventoryAddConflictError("inventory add item_conflict")

    _insert_inventory_items(conn, pending)
    return len(pending), unchanged
