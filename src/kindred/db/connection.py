"""SQLite 连接与 migrate。

设计：
- 单文件 SQLite 数据库（默认 ``life/data/kindred.db``，沿用 first-party predecessor 路径风格）
- ``schema.sql`` 是声明式幂等（CREATE TABLE IF NOT EXISTS / DROP VIEW IF EXISTS + CREATE）
- ``migrate()`` 把 schema.sql 应用到目标库；可重入安全
- 连接打开时启用 ``foreign_keys=OFF``（02 §3.10.6 心嘴独立 agent 不引入外键）+ JSON
  存为 TEXT（pydantic ``model_dump_json()`` 直接灌入即可）

v0.1：单一 schema 版本（v1）。Phase β 加迁移机制时再扩。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal

from kindred.db import artifacts as _artifacts_mod

_LOG = logging.getLogger(__name__)

# schema.sql 与 connection.py 同目录
_SCHEMA_PATH: Final[Path] = Path(__file__).parent / "schema.sql"
CURRENT_SCHEMA_VERSION: Final[int] = 7
"""当前 schema 版本。

Version History
---------------
* **v1**（2026-06）：初版 schema。
  - tick 表：8 层 State JSON + trigger_source/triggered_at + act_decision/act_result
  - thought 表：软关联 source_tick_id（02 §3.10.6 心嘴独立 agent 不加外键）
  - episode_recall 表：cooldown / last_recalled_ts（同事务写入）
  - state_latest / episode 视图
  - schema_version 表本身
* **v2**（2026-06，earlier milestone）：+ watcher_cursor 表（「心已读书签」持久化）。
* **v3**（2026-07，L3 PlaceStore）：+ place_visits 表（真实到访事件索引）。
* **v4**（2026-07，Inventory I0）：+ inventory_items 运行期私有物品名册。
  纯加表（CREATE TABLE IF NOT EXISTS），既有库 migrate 幂等安全。
* **v5**（2026-07，Artifact Visibility ART1A）：+ artifact_commit Host 读模型。
  首次升级从合法 canonical tick 一次性回填，之后由 T3 同事务副写。
* **v6**（2026-08，Relationship REL1-A）：+ relationship_profile 当前关系权威表。
  纯加表，不回放或修改既有 tick / state_latest。
* **v7**（2026-08，Desktop Spirit V1）：``state_latest`` 改按 ``tick.id DESC``
  选择最新提交，消除秒级时间戳相同导致 Heart 读到旧状态的歧义。迁移仅幂等重建
  view，不回放或修改既有 tick。

Phase β 加 ALTER 类迁移时，需配合 connection.migrate() 重构为
逐语句 execute（详见 migrate docstring "事务语义限制" 节）。
"""


def get_schema_sql() -> str:
    """读取 schema.sql 内容（用于 migrate 和测试）。"""
    return _SCHEMA_PATH.read_text(encoding="utf-8")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开一个 SQLite 连接。

    - ``row_factory = sqlite3.Row``：可按列名访问字段
    - ``isolation_level="DEFERRED"``：sqlite3 默认事务模式。
      **必须这样**：``with conn:`` 才能起 transaction COMMIT/ROLLBACK
      语义。若设 ``isolation_level=None``（autocommit）则每条 SQL 立即
      提交，``with conn:`` 不起作用——episode_recall 同事务会失效。
    - ``foreign_keys = OFF``：02 §3.10.6 心嘴独立 agent 不引入外键
    - ``busy_timeout = 5000``：心 daemon 与自建聊天 API（earlier milestone）会**并发写同一库**——
      SQLite 单写者，无 busy_timeout 时第二个写者立刻 ``database is locked`` 报错。设
      5s 忙等让并发写者排队重试而非直接失败（WAL 下读不阻塞，只写者间需要）。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _LOG.debug("db.connect path=%s", path)
    # isolation_level="DEFERRED"（sqlite3 默认）不是 autocommit。
    # 详见 https://docs.python.org/3/library/sqlite3.html#sqlite3-controlling-transactions
    conn = sqlite3.connect(str(path), isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """打开一个**真只读**的 SQLite 连接（``mode=ro`` URI）。

    与 :func:`connect` 的关键区别：

    - **绝不建文件 / 绝不建父目录**：``mode=ro`` 下文件不存在 → SQLite 直接
      raise ``OperationalError``。调用方应在 connect 前自行判断文件存在性。
    - **绝不允许写**：任何 DDL（CREATE/DROP）或 DML（INSERT/UPDATE）都会被
      SQLite 引擎拒绝（``readonly database``）。这是引擎级保证，不靠纪律。

    用途：web 可视化等"观察窗"场景——观察旧库 / 备份库 / 只读挂载库时，
    GET 请求绝不能改写被观察对象（earlier milestone codex review N-2）。

    注意：只读连接**不 migrate**。若库未建表，读视图会 raise
    ``OperationalError``；调用方可先用 :func:`get_schema_version`
    （表不存在返回 None）探测，未迁移库当空库处理。
    """
    path = Path(db_path)
    # mode=ro：只读打开；文件不存在直接报错（不像 connect 会 mkdir + 建库）。
    uri = f"file:{path}?mode=ro"
    # Read-only observation can be exposed remotely; do not put resident paths
    # into request-adjacent DEBUG logs.
    _LOG.debug("db.connect_readonly")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """把 schema.sql 应用到 conn。

    幂等（CREATE ... IF NOT EXISTS / DROP VIEW IF EXISTS）。

    事务语义限制（重要）
    --------------------------
    ``executescript`` 本身**不是事务化的**：它在执行前会
    隐式发 COMMIT（[Python sqlite3 文档][1]）。这意味着：即使本函数
    包在 ``with conn:`` 里，若 schema.sql 中间某句 SQL 失败 raise，
    **前面已 CREATE 的表不会被 ROLLBACK**。

    当前 schema.sql 所有 CREATE 都用 ``IF NOT EXISTS``，下次 migrate 能补齐
    这次失败的部分——依赖“幂等 + 下次补齐”不是“事务原子性”保证。
    Phase ß 如需引入复杂迁移语句（e.g. ALTER 中间步骤）时需重构：
    拆 schema.sql 成多个语句逐条 ``conn.execute(stmt)``，这样 ``with conn:`` 才能真正起作用。

    v5 backfill 与 schema_version 在 ``executescript`` 后的同一事务中提交。

    [1]: https://docs.python.org/3/library/sqlite3.html#sqlite3.Cursor.executescript

    Returns
    -------
    int
        应用后的 schema_version。
    """
    old_version = get_schema_version(conn) or 0
    sql = get_schema_sql()
    # `with conn:` 在块结束时 COMMIT，避免 deferred 模式下忘记提交。
    # executescript 后的 backfill/version 由同一个事务提交。
    with conn:
        conn.executescript(sql)
        if old_version < 5:
            _artifacts_mod.backfill_artifact_commits(conn)
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (CURRENT_SCHEMA_VERSION, now_iso),
        )
    _LOG.debug("db.migrate schema_version=%d", CURRENT_SCHEMA_VERSION)
    return CURRENT_SCHEMA_VERSION


def get_schema_version(conn: sqlite3.Connection) -> int | None:
    """查询当前已应用的最高 schema_version；未迁移返回 None。"""
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        # 表还不存在
        return None
    if row is None or row["v"] is None:
        return None
    version = row["v"]
    if not isinstance(version, int):
        raise TypeError(f"schema_version.version expected int, got {type(version).__name__}")
    return version


# count_* 三函数共享同一个实现。表名用 Literal 钉死为枚举（不是拼任意 SQL），
# noqa: S608 是合理豁免。
CountableTable = Literal[
    "tick",
    "thought",
    "episode_recall",
    "main_session_messages",
    "place_visits",
    "schema_version",
]


def count(conn: sqlite3.Connection, table: CountableTable) -> int:
    """返回表总行数。表名仅 Literal 枚举中之一，不接受任意字符串。"""
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # noqa: S608
    return int(row["n"])
