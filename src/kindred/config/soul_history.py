"""soul-history 目录的内部布局子路径（单一真相源）。

``soul_history_dir`` 之下的三个子路径（snapshots/ 目录、emergency/ 目录、
index.json 文件）是做梦 land / rollback / 梦日记三处共用的**内部布局约定**——
不是 user 可配项（改了三处会对不上），但必须集中定义，杜绝字面量散落漂移。

参考：docs/16 §3.2（派生子路径集中到 config 层）、docs/11 §7.2（snapshot 布局）。

逻辑边界：本模块只定义「``soul_history_dir`` → 子路径」的派生，不碰 ``soul_history_dir``
本身的值（那是 ``KindredPaths.soul_history_dir``，走四层 config merge）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 子路径名常量（内部布局约定，非 user 可配）
SOUL_HISTORY_SNAPSHOTS_SUBDIR = "snapshots"
SOUL_HISTORY_EMERGENCY_SUBDIR = "emergency"
SOUL_HISTORY_INDEX_FILE = "index.json"


@dataclass(frozen=True)
class SoulHistoryLayout:
    """从 ``soul_history_dir`` 派生的做梦演化历史布局。

    land / rollback / 梦日记三处统一从这里取 snapshot_root / emergency_root /
    index_path，而不是各自 ``soul_history_dir / "snapshots"`` 拼字面量。
    """

    soul_history_dir: Path
    snapshot_root: Path
    emergency_root: Path
    index_path: Path

    @classmethod
    def from_dir(cls, soul_history_dir: Path) -> SoulHistoryLayout:
        """从 ``soul_history_dir`` 派生全套子路径。"""
        return cls(
            soul_history_dir=soul_history_dir,
            snapshot_root=soul_history_dir / SOUL_HISTORY_SNAPSHOTS_SUBDIR,
            emergency_root=soul_history_dir / SOUL_HISTORY_EMERGENCY_SUBDIR,
            index_path=soul_history_dir / SOUL_HISTORY_INDEX_FILE,
        )
