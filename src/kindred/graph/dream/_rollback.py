"""``kindred soul rollback`` 的核心逻辑 —— 不依赖 git 恢复某天的旧灵魂（docs §8.3）。

user 想把灵魂恢复到某次做梦前的状态时用。流程（§8.3）：

1. 把 ``--to`` 当 **snapshot id 而非路径**校验收敛（N-1 path sandbox：拒 ``/`` ``\\`` ``..``
   / 绝对路径，再 ``resolve().relative_to(root)`` 纵深防御），定位
   ``soul-history/snapshots/<id>/`` 或 ``emergency/<id>/``（不存在 → 报错列可用）
2. **回滚前把当前灵魂备份到 ``soul-history/emergency/<now>/``**（安全网：万一回滚错了
   能 ``rollback --to-emergency <ts>`` 找回；N-3 目录名带微秒 + 独占创建防同秒覆盖）
3. 从目标快照恢复（复用 ``_land_fs.restore_snapshot`` 的双向恢复语义）
4. index.json 追加一条 ``rollback`` 事件（emergency_backup 存**相对 soul-history** 路径，
   避免绝对路径写进可迁移历史）

快照存的是**那夜做梦前**的灵魂三件套（``*.before``）。所以 rollback --to <id>
= 把灵魂恢复到那次做梦发生前的样子。复用 restore_snapshot 保证「那夜本不存在的文件
回滚后也不存在」的双向语义一致。emergency 备份与 snapshot 同构，故可作为另一组可回滚目标。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from kindred.graph.dream._land_fs import (
    SoulLayout,
    _atomic_write_text,
    is_real_snapshot,
    restore_snapshot,
    write_snapshot,
)

_LOG = logging.getLogger(__name__)

# rollback 目标来源：做梦快照 / 回滚前的 emergency 备份。
TargetKind = Literal["snapshot", "emergency"]


class RollbackError(Exception):
    """rollback 前置校验失败（id 非法 / 目标不存在）——CLI 捕获后友好提示。"""


def _validate_snapshot_id(target_id: str) -> None:
    """把 ``--to`` 当 snapshot id 校验（N-1 path sandbox）：拒路径分隔符 / .. / 绝对路径。"""
    if not target_id:
        raise RollbackError("快照 id 不能为空")
    if "/" in target_id or "\\" in target_id or ".." in target_id:
        raise RollbackError(
            f"非法快照 id：{target_id!r}（不能含 / \\ ..；--to 是 snapshot 时间戳，不是路径）"
        )
    if Path(target_id).is_absolute():
        raise RollbackError(f"非法快照 id：{target_id!r}（不能是绝对路径）")


def _resolve_target(root: Path, target_id: str) -> Path:
    """校验 id + 纵深防御：确认 ``root/target_id`` 真在 root 内、且是已存在目录。"""
    _validate_snapshot_id(target_id)
    candidate = root / target_id
    # 纵深防御：即便上面漏了，resolve 后必须仍在 root 子树内
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RollbackError(f"非法快照 id：{target_id!r}（逃出 {root}）") from exc
    if not candidate.is_dir():
        available = list_snapshots(root)
        hint = "、".join(available) if available else "（无）"
        raise RollbackError(f"目标不存在：{candidate}\n可用：{hint}")
    # N-3：即使 user 手动输了 trace-only 目录 id（无灵魂快照）也拒绝——回滚到
    # 它会 restore_snapshot 删当前灵魂。只允许可回滚的真快照。
    if not is_real_snapshot(candidate):
        available = list_snapshots(root)
        hint = "、".join(available) if available else "（无）"
        raise RollbackError(
            f"目标不是可回滚快照（trace-only，无灵魂快照内容）：{target_id}\n可用：{hint}"
        )
    return candidate


def list_snapshots(root: Path) -> list[str]:
    """列出某根目录下**可回滚的真快照**目录名，按字典序（= 时间序）。

    通用于 snapshots/ 与 emergency/。不存在 / 空 → 返空列表。

    **N-3**：只认 ``is_real_snapshot`` 为真的目录——trace-only（skip_today / block
    只写梦日记、无 ``*.before``）不列为可回滚目标。否则
    rollback 到它 → restore_snapshot 把缺失的 ``.before`` 当「做梦前不存在」→
    unlink/rmtree 当前灵魂（灾难）。emergency 备份是 write_snapshot 产出的真快照，
    不受影响。
    """
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and is_real_snapshot(p))


def _now_stamp() -> str:
    """emergency 备份目录名：本地时区紧凑 ISO + 微秒（去冒号，文件系统安全）。

    N-3：到微秒级，配合独占创建（``_unique_emergency_dir``）防同秒连续 rollback 覆盖。
    """
    return datetime.now(tz=timezone.utc).astimezone().strftime("%Y-%m-%dT%H-%M-%S-%f")


def _unique_emergency_dir(emergency_root: Path) -> Path:
    """独占创建一个不与已有 emergency 备份碰撞的目录（N-3 防覆盖）。

    用 ``mkdir(exist_ok=False)`` 独占；极小概率同微秒碰撞则加 ``-N`` 后缀重试。
    """
    base = _now_stamp()
    emergency_root.mkdir(parents=True, exist_ok=True)
    for suffix in ("", *(f"-{i}" for i in range(1, 1000))):
        candidate = emergency_root / f"{base}{suffix}"
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RollbackError(f"无法创建唯一 emergency 目录（{base} 碰撞超 1000 次）")


def _append_rollback_event(
    index_path: Path,
    target_id: str,
    target_kind: TargetKind,
    emergency_relpath: str,
) -> None:
    """index.json 追加一条 rollback 事件（trigger=rollback，区别于做梦 snapshot 条目）。

    损坏 / 不存在 → 当空 ``{"snapshots": []}`` 起步（fail-safe，与 update_index_json 一致）。
    emergency_relpath 是相对 soul-history/ 的路径（N-2：不写绝对路径进可迁移历史）。
    """
    index: dict[str, Any] = {"snapshots": []}
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping) and isinstance(loaded.get("snapshots"), list):
                index = {"snapshots": list(loaded["snapshots"])}
        except (json.JSONDecodeError, OSError):
            _LOG.warning("rollback: index.json 损坏 / 不可读，重建 %s", index_path)

    event = {
        "timestamp": _now_stamp(),
        "trigger": "rollback",
        "rollback_to": target_id,
        "rollback_from_kind": target_kind,
        "emergency_backup": emergency_relpath,
    }
    snapshots: list[Any] = index["snapshots"]
    snapshots.append(event)
    index["snapshots"] = snapshots
    text = json.dumps(index, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(index_path, text)
    _LOG.info("rollback: index.json 追加 rollback 事件 -> %s (%s)", target_id, target_kind)


def rollback_soul(
    target_id: str,
    *,
    target_kind: TargetKind = "snapshot",
    layout: SoulLayout,
    snapshot_root: Path,
    emergency_root: Path,
    index_path: Path,
    soul_history_dir: Path,
    excerpt_path: Path | None = None,
) -> Path:
    """把灵魂回滚到 ``target_id``（做梦快照或 emergency 备份），返回本次 emergency 备份目录。

    raise ``RollbackError``：id 非法 / 目标不存在（消息列可用目标供 user 选）。
    """
    source_root = snapshot_root if target_kind == "snapshot" else emergency_root
    target_dir = _resolve_target(source_root, target_id)

    # 1. 安全网：回滚前把当前灵魂整体存到 emergency/<now>/（独占创建防同秒覆盖，N-3）
    emergency_dir = _unique_emergency_dir(emergency_root)
    write_snapshot(emergency_dir, layout)
    _LOG.warning("rollback: 当前灵魂已备份到 emergency=%s", emergency_dir)

    # 2. excerpt 是可重建投影，不随权威快照回滚。先使其失效，避免恢复旧 SOUL
    #    后 Heart 继续读取与权威不一致的摘要。
    if excerpt_path is not None:
        try:
            excerpt_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RollbackError("无法使 SOUL excerpt 失效，未执行灵魂回滚") from exc

    # 3. 从目标恢复（双向语义：那夜本不存在的文件回滚后也不存在）
    restore_snapshot(target_dir, layout)

    # 4. index.json 记 rollback 事件（best-effort：失败不该让已完成的回滚算失败）
    #    emergency_backup 存相对 soul-history/ 的路径（N-2 可迁移）
    try:
        emergency_rel = emergency_dir.relative_to(soul_history_dir).as_posix()
    except ValueError:
        emergency_rel = emergency_dir.name  # 兜底：至少存目录名
    try:
        _append_rollback_event(index_path, target_id, target_kind, emergency_rel)
    except OSError as exc:
        _LOG.warning(
            "rollback: index.json 记事件失败（回滚已生效，不影响灵魂）。err_type=%s",
            type(exc).__name__,
        )

    _LOG.warning(
        "rollback: 灵魂已回滚到 %s (%s)（emergency 备份 %s）",
        target_id,
        target_kind,
        emergency_dir,
    )
    return emergency_dir
