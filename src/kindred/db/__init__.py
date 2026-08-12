"""持久化层 — SQLite 读写。

参考文档：

- docs/02-state-system.md §3.10（tick 表 + state_latest / episode 视图）
- docs/09-memory.md §3.2（thought 表）+ §4.4.4.1（episode_recall 表）
- docs/14-heart-graph.md §3.5（T3.persist.write_state 同事务写两表）

依赖：state/

模块：

- schema.sql        SQLite schema（02 §3.10 + 09 §3.2 + 09 §4.4.4.1）
- connection.py     建库 / 连接 / migrate
- ticks.py          tick / state_latest / episode 读写 + episode_recall / place_visits 同事务写入
- thoughts.py       thought 表（含 expire_at 过滤）
- places.py         place_visits 表（PlaceStore 到访事件索引，第四趴 / L3）
- _types.py         入参聚合体 ``TickWriteParams``

公共 API（其它层应只 import 这里）：
"""

from __future__ import annotations

from kindred.db._types import TickWriteParams
from kindred.db.connection import (
    connect,
    connect_readonly,
    count,
    get_schema_sql,
    get_schema_version,
    migrate,
)
from kindred.db.facade import (
    KindredDB,
    NestedTransactionError,
    TransactionRequiredError,
    TransactionStateError,
)
from kindred.db.inventory import (
    InventoryAddConflictError,
    InventoryCatalogDataError,
    InventoryImportConflictError,
)
from kindred.db.messages import (
    BUSINESS_TO_PROTOCOL,
    PROTOCOL_TO_BUSINESS,
    ROLE_MY_HEART,
    ROLE_MY_VOICE,
    ROLE_PARTNER,
    MainSessionMessage,
    build_from_gateway,
    count_messages,
    extract_text_summary,
    get_after_cursor,
    get_between_ms,
    get_by_msg_id,
    get_latest,
    get_latest_visible_contact_expression,
    get_max_cursor,
    get_max_seq,
    get_max_ts_ms,
    get_recent,
    get_since_ms,
    protocol_role_to_business,
    prune_between_ms,
    prune_keep_recent,
    prune_older_than_ms,
    upsert,
    upsert_many,
)
from kindred.db.places import (
    PlaceVisitParams,
    PlaceVisitStats,
    derive_place_visit,
    get_visit_stats,
    rebuild_place_visits,
)
from kindred.db.relationships import RelationshipDataError
from kindred.db.thoughts import (
    count_thoughts,
    insert_thoughts,
    query_active_thoughts,
)
from kindred.db.ticks import (
    STATE_LAYERS,
    _insert_tick_impl,
    count_episode_recalls,
    count_ticks,
    decay_episode_recall,
    get_episodes,
    get_episodes_page,
    get_highlight_episodes,
    get_interior_history,
    get_recent_activity_rows,
    get_recent_ticks,
    get_state_latest,
    get_ticks_for_bundle,
    get_ticks_page,
    insert_tick,
)

__all__ = [
    # impl: 新代码首选（接受 TickWriteParams 实例）
    "_insert_tick_impl",
    # connection
    "connect",
    "connect_readonly",
    "count",
    "count_episode_recalls",
    # ticks
    "count_ticks",
    "count_thoughts",
    "decay_episode_recall",
    # messages
    "BUSINESS_TO_PROTOCOL",
    "MainSessionMessage",
    "PROTOCOL_TO_BUSINESS",
    "ROLE_MY_HEART",
    "ROLE_MY_VOICE",
    "ROLE_PARTNER",
    "count_messages",
    "build_from_gateway",
    "extract_text_summary",
    "get_between_ms",
    "get_by_msg_id",
    "get_latest",
    "get_latest_visible_contact_expression",
    "get_after_cursor",
    "get_max_cursor",
    "get_max_seq",
    "get_max_ts_ms",
    "get_recent",
    "get_since_ms",
    "protocol_role_to_business",
    "prune_between_ms",
    "prune_keep_recent",
    "prune_older_than_ms",
    "upsert",
    "upsert_many",
    "get_episodes",
    "get_episodes_page",
    "get_highlight_episodes",
    "get_interior_history",
    "get_recent_activity_rows",
    "get_recent_ticks",
    "get_ticks_for_bundle",
    "get_ticks_page",
    "get_schema_sql",
    "get_schema_version",
    "get_state_latest",
    # places（PlaceStore，L3）
    "PlaceVisitParams",
    "PlaceVisitStats",
    "derive_place_visit",
    "get_visit_stats",
    "rebuild_place_visits",
    # thoughts
    "insert_thoughts",
    # ticks: backward-compat thin wrapper
    "insert_tick",
    "migrate",
    "query_active_thoughts",
    # facade
    "KindredDB",
    "NestedTransactionError",
    "TransactionRequiredError",
    "TransactionStateError",
    "InventoryAddConflictError",
    "InventoryCatalogDataError",
    "InventoryImportConflictError",
    "RelationshipDataError",
    # ticks: 跨层 reuse 的 8 层字段顺序常量（L-1）
    "STATE_LAYERS",
    # _types
    "TickWriteParams",
]
