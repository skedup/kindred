"""``Step 5.land`` —— 落地 snapshot + 应用 changes（代码节点，无 LLM）。

参考文档：docs/11-dreaming.md §7。

三层结构（同 gate.py）：
- ``_step5_land_impl(state, *, layout, snapshot_root, index_path)`` 真实现（WAL 落盘 + 人类可见层）
- ``make_land_node(paths)`` factory → closure（从 config.paths 构 SoulLayout）
- ``step5_land(state)`` 顶层桩（未注 deps 时用，透传 prev_state，不真落盘）

职责（闸门放行后，docs §7.1 严格顺序）：
1. 写 snapshot（旧灵魂存档，WAL）
2. 应用未 block 的 changes 到灵魂文件
3. 写梦日记（§7.3）+ 更新 index.json（§7.2）——人类可见层，落盘副产物
4. 任一灵魂落盘步失败 → 回滚整次（restore_snapshot）+ 软心降级（不崩心跳，MEMORY R3）
   人类可见层（日记/索引）写失败不回滚已成功的灵魂改写，仅软心降级

**所有终态都进本节点**（D6.8b）：pass/warn 正常落盘；block 走 Step 0 早返（不
apply / 不存 snapshot，只写 index trace 供 should_dream 幂等补偿）。``warn`` 放行
（warnings 仅记录）。block 不改灵魂的安全保证收紧在 Step 0（``_is_blocked`` 显式判）。

rollback CLI（§8.3）留 **earlier milestone**。
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kindred.config.soul_history import SoulHistoryLayout
from kindred.graph._shared._common import NodeReturn
from kindred.graph.dream._land_fs import (
    LandApplyError,
    LandPathError,
    SoulLayout,
    apply_change_to_text,
    apply_changes_wal,
    resolve_change_path,
)
from kindred.graph.dream._land_journal import update_index_json, write_dream_journal
from kindred.state.dream import DreamState

if TYPE_CHECKING:
    from kindred.config.schema import KindredPaths
    from kindred.graph.dream.excerpt import ExcerptCompiler

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ExcerptUpdate:
    text: str
    previous: str | None
    source: str
    candidate: str


def make_land_node(
    paths: KindredPaths,
    *,
    excerpt_compiler: ExcerptCompiler | None = None,
) -> Callable[[DreamState], NodeReturn]:
    """构造 dream Step 5.land closure，从 config.paths 取 SoulLayout + snapshot 根。"""
    layout = SoulLayout(
        soul_full=paths.soul_full,
        identity=paths.identity,
        user=paths.user,
    )
    sh = SoulHistoryLayout.from_dir(paths.soul_history_dir)
    snapshot_root = sh.snapshot_root
    index_path = sh.index_path

    def land_closure(state: DreamState) -> NodeReturn:
        return _step5_land_impl(
            state,
            layout=layout,
            snapshot_root=snapshot_root,
            index_path=index_path,
            excerpt_path=paths.soul_excerpt,
            excerpt_compiler=excerpt_compiler,
        )

    return land_closure


def _snapshot_dir_name(state: DreamState) -> str:
    """snapshot 目录名：用 dream_date + T04-00（docs §7.2 时点）。"""
    dream_date = state.get("dream_date") or "unknown"
    return f"{dream_date}T04-00"


def _is_blocked(state: DreamState) -> bool:
    """gate 结论是否 block（含 fail-closed 降级：verdict 缺席 / 非 Mapping / 非法值）。

    与原 ``route_after_gate`` 同源逻辑（fail-closed = block）：只有明确 ``pass``/``warn``
    才不算 block。用于 land Step 0 早返——block 绝不 apply。
    """
    verdict_obj = state.get("gate_verdict")
    if not isinstance(verdict_obj, Mapping):
        return True  # 缺席 / 非 Mapping → fail-closed 当 block
    return verdict_obj.get("verdict") not in ("pass", "warn")


def _surviving_changes(state: DreamState) -> list[object]:
    """取本次应落盘的 changes：reflection.changes 去掉 gate blocked 索引。

    D6.8b：block 也进 land 但在 Step 0 已早返；走到这里的只会是 pass/warn。
    pass/warn 的 blocked 应为空，但仍按 blocked 过滤作纵深防御——万一上游给了
    warn + 非空 blocked，也不落盘那几条。
    """
    reflection = state.get("reflection")
    raw = reflection.get("changes") if isinstance(reflection, Mapping) else None
    changes = list(raw) if isinstance(raw, list) else []

    verdict = state.get("gate_verdict")
    blocked_raw = verdict.get("blocked") if isinstance(verdict, Mapping) else None
    blocked: set[int] = set()
    if isinstance(blocked_raw, list):
        blocked = {i for i in blocked_raw if isinstance(i, int)}

    if not blocked:
        return changes
    return [c for i, c in enumerate(changes) if i not in blocked]


def _step5_land_impl(
    state: DreamState,
    *,
    layout: SoulLayout,
    snapshot_root: Path,
    index_path: Path,
    excerpt_path: Path | None = None,
    excerpt_compiler: ExcerptCompiler | None = None,
) -> NodeReturn:
    """Step 5 真实现：WAL 落盘（snapshot + 应用 changes），失败软心回滚。

    流程：
    0. **block 早返**（D6.8b）：gate verdict=block → 整次回滚语义，**绝不应用任何
       change、绝不存 snapshot**，但仍写人类可见层 trace（index verdict=block /
       changed_files=[]）——让 should_dream 能读到「这天做过梦了（只是被否决）」，
       不重跑（幂等补偿真相源覆盖所有终态，D6.8）。**显式按 verdict 判，不靠
       surviving 是否为空**：fail-closed 降级的 block（verdict=block 但 blocked 字段
       缺失）下 surviving 会是原样 changes，若靠空判会误 apply 被否决的改写。
    1. 取 surviving changes（reflection.changes 去 blocked）
    2. 空 changes（reflect skip_today）→ 不改灵魂，但仍写人类可见层 trace
       （梦日记 changed_files=[] + index 条目）——ta 明天该知道「昨晚做梦了但没改灵魂」
    3. 非空 → apply_changes_wal（先存档再逐条应用，任一失败回滚整次）
    4. WAL 异常 → 软心降级：不崩心跳（MEMORY R3），回填 prev_state，标记落盘失败

    人类可见层（梦日记 + index.json）是 best-effort 副产物（docs §7.1 / §8.1）：
    写失败不回滚已成功的灵魂改写，仅软心降级（MEMORY R3）。

    返回 patch：``{"snapshot_path"（skip_today/block 无）, "dream_journal_path", "next_state"}``。
    """
    dream_date = state.get("dream_date", "<unknown>")
    prev_state = state.get("prev_state") or {}
    snapshot_dir = snapshot_root / _snapshot_dir_name(state)

    # Step 0：block 早返——显式按 verdict 判，不靠 surviving 空判（防 fail-closed
    # 降级的 block 漏 apply）。block = 整次回滚：不 apply / 不存 snapshot，只留 trace。
    if _is_blocked(state):
        _LOG.info(
            "dream Step 5.land: gate block，整次回滚不改灵魂，只写人类可见层 trace date=%s",
            dream_date,
        )
        journal_path = _write_human_layer(state, snapshot_dir, index_path, dream_date, [])
        blocked_patch: NodeReturn = {"next_state": prev_state}
        if journal_path is not None:
            blocked_patch["dream_journal_path"] = str(journal_path)
        return blocked_patch

    surviving = _surviving_changes(state)
    try:
        excerpt_update = _prepare_excerpt(
            surviving,
            layout=layout,
            excerpt_path=excerpt_path,
            excerpt_compiler=excerpt_compiler,
        )
    except Exception as exc:  # noqa: BLE001 - 编译失败必须阻止权威 land
        _LOG.warning(
            "dream Step 5.land: excerpt 编译失败，不落权威灵魂。err_type=%s",
            type(exc).__name__,
        )
        return {"next_state": prev_state}

    if not surviving:
        # skip_today：不改灵魂（无 snapshot 存档），但仍留人类可见 trace。
        _LOG.info(
            "dream Step 5.land: 无 surviving change（skip_today），只写人类可见层 date=%s",
            dream_date,
        )
        if excerpt_update is not None and excerpt_path is not None:
            if not _soul_matches(layout.soul_full, excerpt_update.source):
                _LOG.warning("dream Step 5.land: 权威 SOUL 在 excerpt 编译后变化，跳过派生发布")
                excerpt_update = None
        if excerpt_update is not None and excerpt_path is not None:
            try:
                _atomic_publish_excerpt(excerpt_path, excerpt_update.text)
            except OSError as exc:
                _LOG.warning(
                    "dream Step 5.land: 缺失 excerpt 重建失败。err_type=%s",
                    type(exc).__name__,
                )
        journal_path = _write_human_layer(state, snapshot_dir, index_path, dream_date, [])
        patch: NodeReturn = {"next_state": prev_state}
        if journal_path is not None:
            patch["dream_journal_path"] = str(journal_path)
        return patch

    if (
        excerpt_update is not None
        and excerpt_path is not None
        and not _soul_matches(layout.soul_full, excerpt_update.source)
    ):
        _LOG.warning("dream Step 5.land: 权威 SOUL 在 excerpt 编译后变化，放弃本次 land")
        return {"next_state": prev_state}

    if excerpt_update is not None and excerpt_path is not None:
        try:
            excerpt_path.unlink(missing_ok=True)
        except OSError as exc:
            _LOG.warning(
                "dream Step 5.land: 旧 excerpt 无法失效，不落权威灵魂。err_type=%s",
                type(exc).__name__,
            )
            return {"next_state": prev_state}

    try:
        changed = apply_changes_wal(surviving, layout, snapshot_dir)
    except (LandPathError, LandApplyError, OSError) as exc:
        # 软心降级：落盘失败已 restore_snapshot 回滚；不崩心跳（MEMORY R3）。
        # anti-leak：只记 err_type + 计数，不记 change content。
        _LOG.warning(
            "dream Step 5.land: 落盘失败已回滚，软心降级。err_type=%s n_changes=%d",
            type(exc).__name__,
            len(surviving),
        )
        if excerpt_update is not None and excerpt_path is not None:
            _restore_excerpt(excerpt_path, excerpt_update.previous)
        return {"next_state": prev_state}

    _LOG.info(
        "dream Step 5.land: 落盘成功 date=%s changed_files=%d snapshot=%s",
        dream_date,
        len(changed),
        snapshot_dir,
    )

    if excerpt_update is not None and excerpt_path is not None:
        if not _soul_matches(layout.soul_full, excerpt_update.candidate):
            _LOG.warning("dream Step 5.land: 权威 SOUL 与已编译候选不一致，保持 excerpt 缺失")
            excerpt_update = None
    if excerpt_update is not None and excerpt_path is not None:
        try:
            _atomic_publish_excerpt(excerpt_path, excerpt_update.text)
        except OSError as exc:
            # 权威三件套已经提交；派生投影保持缺失，后续 Dream 可重建。
            try:
                excerpt_path.unlink(missing_ok=True)
            except OSError:
                pass
            _LOG.warning(
                "dream Step 5.land: 权威灵魂已落盘但 excerpt 发布失败。err_type=%s",
                type(exc).__name__,
            )

    # 人类可见层（梦日记 + index.json）是 best-effort 副产物，失败不崩心跳（MEMORY R3）。
    # 灵魂文件已成功写入（apply_changes_wal 过了）；日记/索引写不上不该回滚已成功的改写。
    journal_path = _write_human_layer(state, snapshot_dir, index_path, dream_date, surviving)

    done: NodeReturn = {"snapshot_path": str(snapshot_dir), "next_state": prev_state}
    if journal_path is not None:
        done["dream_journal_path"] = str(journal_path)
    return done


def _prepare_excerpt(
    changes: list[object],
    *,
    layout: SoulLayout,
    excerpt_path: Path | None,
    excerpt_compiler: ExcerptCompiler | None,
) -> _ExcerptUpdate | None:
    """构造最终候选 SOUL；需要更新时在权威落盘前完成窄编译。"""
    if excerpt_path is None or excerpt_compiler is None:
        return None
    previous = _read_excerpt(excerpt_path)
    current = layout.soul_full.read_text(encoding="utf-8") if layout.soul_full.exists() else ""
    candidate = current
    for change in changes:
        destination = resolve_change_path(_change_value(change, "file"), layout)
        if destination != layout.soul_full:
            continue
        operation = _change_value(change, "operation")
        section = _change_value(change, "section")
        content = _change_value(change, "content")
        if not isinstance(operation, str) or not isinstance(content, str):
            raise LandApplyError("SOUL change 缺少 operation/content")
        if section is not None and not isinstance(section, str):
            raise LandApplyError("SOUL change.section 非 str/None")
        candidate = apply_change_to_text(candidate, operation, section, content)
    if candidate == current and previous is not None and previous.strip():
        return None
    return _ExcerptUpdate(
        text=excerpt_compiler(candidate, previous or ""),
        previous=previous,
        source=current,
        candidate=candidate,
    )


def _soul_matches(path: Path, expected: str) -> bool:
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return False
    return current == expected


def _change_value(change: object, key: str) -> object:
    if isinstance(change, Mapping):
        return change.get(key)
    return getattr(change, key, None)


def _read_excerpt(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LandPathError("SOUL excerpt 必须是普通文件")
    return path.read_text(encoding="utf-8")


def _atomic_publish_excerpt(path: Path, excerpt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp = Path(handle.name)
        handle.write(excerpt.strip() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _restore_excerpt(path: Path, previous: str | None) -> None:
    if previous is None:
        return
    try:
        _atomic_publish_excerpt(path, previous)
    except OSError as exc:
        _LOG.warning(
            "dream Step 5.land: 权威回滚后旧 excerpt 恢复失败。err_type=%s",
            type(exc).__name__,
        )


def _write_human_layer(
    state: DreamState,
    snapshot_dir: Path,
    index_path: Path,
    dream_date: str,
    applied_changes: list[object],
) -> Path | None:
    """写梦日记 + 更新 index.json（docs §7.3 / §7.2）。失败软心降级返 None。

    梦日记路径 = ``snapshot_dir/dream-<date>.md``；index.json 的 dream_log 用
    相对 ``soul-history/`` 的路径（``snapshots/<ts>/dream-<date>.md``）。
    """
    reflection = state.get("reflection")
    gate_verdict = state.get("gate_verdict")
    try:
        journal_path = write_dream_journal(
            snapshot_dir, dream_date, reflection, gate_verdict, applied_changes
        )
        # N-3：trace-only 目录（skip_today / block 只写梦日记、不调 write_snapshot）天然
        # 没有 ``.snapshot`` 哨兵→``is_real_snapshot`` 返 False→list_snapshots/rollback 跳过。
        # 无需额外标记（正向哨兵在 write_snapshot 路径才落）。
        dream_log_rel = f"snapshots/{snapshot_dir.name}/{journal_path.name}"
        update_index_json(
            index_path,
            dream_date,
            gate_verdict,
            applied_changes,
            reflection,
            dream_log_rel,
        )
        return journal_path
    except OSError as exc:
        _LOG.warning(
            "dream Step 5.land: 人类可见层写入失败（灵魂已落盘，不回滚）。err_type=%s",
            type(exc).__name__,
        )
        return None


def step5_land(state: DreamState) -> NodeReturn:
    """Step 5 顶层桩（未注 deps 时用）——不真落盘，透传 prev_state 作 next_state。

    真实路径：``make_land_node(paths)`` 注入走 ``_step5_land_impl``（WAL 落盘）。
    """
    dream_date = state.get("dream_date", "<unknown>")
    prev_state = state.get("prev_state") or {}
    _LOG.debug("dream Step 5.land (stub) for date=%s", dream_date)
    return {"next_state": prev_state}
