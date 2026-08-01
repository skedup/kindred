"""dream Step 5.land 的文件系统基础设施（纯函数 + 落盘，无 LLM / 无 graph）。

参考文档：docs/11-dreaming.md §7（落地顺序）、§2.2（灵魂层布局）、§2.1（哲学层禁改）。

本模块是 D6.6 land 的第一刀——只做**可独立验证**的底层件，不接 graph：

- ``resolve_change_path``：change 的相对 ``file``（如 ``soul/SOUL.md``）→ 真实落盘
  ``Path``。走 config path 映射（SOUL/IDENTITY/USER），**不假设
  灵魂三件套同目录**（绕过 soul_full=life/SOUL.md 与 data/soul/ 的既有漂移）。
- ``assert_safe_change``：land 落盘前的**二次 path sandbox**（defense in depth，
  不依赖 gate 一道——D6.5 N-1 承诺）。哲学层禁改 + traversal + 越权再校一遍。
- ``write_snapshot``：docs §7.1 步骤 1-2——存三件套快照到
  ``soul-history/snapshots/<timestamp>/``，先存档再改（WAL 思路）。

**安全分层**：gate（Step 4）是第一道；land 落盘前 ``assert_safe_change`` 是第二道。
两道都用三件套 allowlist，确定性规则不信 LLM。
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger(__name__)

# markdown 标题行：1-6 个 # + 空格 + 标题文本（land section 定位用）
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

# 真快照正向哨兵（N-3）：write_snapshot（apply 路径）总会落这个文件，标记「本
# 目录是可回滚的真快照」。trace-only（skip_today / block 只写 dream-*.md、不调
# write_snapshot）永远没这个哨兵。为什么用正向标记而不是「无 *.before」推断：
# 冷启动时灵魂文件本不存在 → write_snapshot 产出的**真快照也是空目录**（无
# *.before），与 trace-only 物理不可区分。正向标记才能区分「真快照（哪怕空）」
# 与「trace-only」。fail-safe 方向：真快照漏标记→不列（顶多不能回滚，但不删灵魂）；
# trace-only 永无标记→永不被当回滚目标。
SNAPSHOT_SENTINEL = ".snapshot"


def is_real_snapshot(snapshot_dir: Path) -> bool:
    """目录是否是可回滚的真快照（N-3：拦 trace-only 目录被当可回滚快照）。

    判据 = ``write_snapshot`` 落的正向哨兵 ``.snapshot`` 存在。哨兵是唯一权威：
    只有 write_snapshot（apply 路径）会落它；skip_today / block 的 trace-only 目录
    永远没。冷启动空真快照也有哨兵（write_snapshot 运行过）→仍可回滚。
    """
    return (snapshot_dir / SNAPSHOT_SENTINEL).exists()


class LandPathError(ValueError):
    """change 的 file 路径不安全 / 无法映射到已知灵魂文件（land 落盘前拦截）。

    land 二次 sandbox 命中即 raise——调用方（Step 5 真实现）应捕获后整次回滚
    （docs §8.1 Step 5 失败 = snapshot 也 rm，下次清晨重试）。
    """


class LandApplyError(ValueError):
    """change 应用失败（section 找不到 / operation 非法 / 字段缺失）。

    与 ``LandPathError`` 区分：路径问题走 ``LandPathError``，内容应用问题走这里。
    两者都是 Step 5 该整次回滚的信号（docs §8.1）。
    """


@dataclass(frozen=True)
class SoulLayout:
    """land 需要的灵魂文件落盘位置（从 config.paths 注入，解耦既有路径漂移）。

    - ``soul_full`` / ``identity`` / ``user``：三件套各自的真实 Path（可能不同目录）
    """

    soul_full: Path
    identity: Path
    user: Path


# change.file 的相对前缀（docs §2.2/§5.2 约定）→ SoulLayout 字段映射
_TOPLEVEL_MAP = {
    "soul/SOUL.md": "soul_full",
    "soul/IDENTITY.md": "identity",
    "soul/USER.md": "user",
}


def _normalized_relpath(file: object) -> str:
    """把 change.file 收敛成规范 posix 相对路径；不安全则 raise ``LandPathError``。

    拦截：非 str / 空 / 绝对路径（posix ``/`` 或 windows 盘符）/ ``..`` 段 /
    normpath 后逃逸。返回 normpath 结果（仍是相对、仍在树内）。
    """
    if not isinstance(file, str) or not file:
        raise LandPathError(f"change.file 必须是非空相对路径（got {type(file).__name__}）")
    if file.startswith("/") or (len(file) >= 2 and file[1] == ":"):
        raise LandPathError("change.file 不能是绝对路径")
    if ".." in file.split("/"):
        raise LandPathError("change.file 不能含 .. 段（path traversal）")
    norm = posixpath.normpath(file)
    if norm.startswith(("/", "..")):
        raise LandPathError("change.file normpath 后逃逸出相对树")
    return norm


def resolve_change_path(file: object, layout: SoulLayout) -> Path:
    """change 的相对 ``file`` → 真实落盘 ``Path``（docs §2.2 灵魂层映射）。

    - ``soul/SOUL.md`` / ``soul/IDENTITY.md`` / ``soul/USER.md`` → 各自 config path
    - 其它一律 ``LandPathError``（不在灵魂可改集合）

    **不假设三件套同目录**——分别走 config，绕过 soul_full=life/SOUL.md 漂移。
    """
    norm = _normalized_relpath(file)

    if norm in _TOPLEVEL_MAP:
        path: Path = getattr(layout, _TOPLEVEL_MAP[norm])
        return path

    raise LandPathError(f"change.file 不在灵魂三件套可改集合：{file!r}")


def assert_safe_change(file: object, layout: SoulLayout) -> Path:
    """land 落盘前的二次 path sandbox（D6.5 N-1 承诺的 defense in depth）。

    复用 ``resolve_change_path`` 的 allowlist——能映射到已知灵魂文件 = 安全；
    映射失败（traversal / 绝对路径 / 越权 / 哲学层）= ``LandPathError``。

    哲学层（``docs/00-philosophy.md`` / ``docs/character-card.yaml``）天然不在
    ``soul/`` 三件套中，会在 ``resolve_change_path`` 被拒，无需单列。

    返回安全的落盘 Path（供调用方直接用）。
    """
    return resolve_change_path(file, layout)


def _fsync_path(path: Path) -> None:
    """fsync 单个文件或目录的内容到磁盘（崩溃持久性）。

    目录 fsync 保证目录项（rename / create / unlink）落盘。某些平台对目录
    fsync 可能 ``EINVAL`` / ``EISDIR``——忽略（尽力语义）。
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_copy(src: Path, dst: Path) -> None:
    """原子拷贝 src→dst（tmpfile + fsync + os.replace + 父目录 fsync）。

    fsync 顺序（docs §7.1 WAL crash-safety）：fsync tmp 内容 → os.replace → fsync
    父目录（让 rename 目录项落盘）。崩溃后要么旧文件要么新文件，不丢不半。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=dst.parent, prefix=dst.name + ".", suffix=".tmp", delete=False
    ) as f:
        tmp = Path(f.name)
    try:
        shutil.copy2(src, tmp)
        _fsync_path(tmp)
        os.replace(tmp, dst)
        _fsync_path(dst.parent)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_snapshot(
    snapshot_dir: Path,
    layout: SoulLayout,
) -> list[str]:
    """docs §7.1 步骤 1-2：存三件套旧版本 ``*.before`` 快照。

    在改任何灵魂文件**之前**调用——先存档再改（WAL）。

    - 三件套各存为 ``<name>.before``（源文件不存在则跳过，记 debug）
    返回实际存档的文件名列表（供 index.json / 日志）。幂等：重复调用覆盖——
    **覆盖一开始先 unlink 旧 ``.snapshot`` 哨兵让目录立即失效**（N-6），全部内容
    重新落盘后才重写哨兵。故覆盖中途失败也只会留「无有效哨兵」的未完成态，不会
    被 list/rollback 当真快照。**源缺失时清旧 artifact**（N-7）：复用同一 snapshot_dir
    时，本轮源灵魂缺失 → 删除同名旧 ``.before``，否则 restore_snapshot 会把
    「本轮做梦前不存在」的文件恢复出来。
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    # N-6：幂等覆盖场景（同一 dream_date 重跑 → 覆盖同名目录）。覆盖**一开始
    # 就先让旧哨兵失效**（删除），再动其他内容。这样「覆盖过程中途崩溃」与
    # 「首次写入中途崩溃」（N-5）同为「无有效哨兵」的未完成态。
    sentinel = snapshot_dir / SNAPSHOT_SENTINEL
    sentinel.unlink(missing_ok=True)
    _fsync_path(snapshot_dir)
    archived: list[str] = []

    for name, src in (
        ("SOUL.md", layout.soul_full),
        ("IDENTITY.md", layout.identity),
        ("USER.md", layout.user),
    ):
        before = snapshot_dir / f"{name}.before"
        if src.exists():
            _atomic_copy(src, before)
            archived.append(f"{name}.before")
        else:
            # N-7：源缺失时必须**删除同名旧 .before**（幂等覆盖复用同一 snapshot_dir，
            # 上轮成功留下的 .before 会残留）。否则 restore_snapshot 会把「本轮做梦前不
            # 存在」的文件恢复出来，违反其双向语义（无 .before = 做梦前不存在）。
            before.unlink(missing_ok=True)
            _LOG.debug("write_snapshot: 源灵魂文件不存在，跳过存档并清旧 .before %s", src)

    # N-3/N-5：正向哨兵是「完成标记」（WAL commit record 思路）——**必须最后写**，
    # 且写后 fsync 哨兵 + 父目录。这样 ``哨兵存在`` ⊙ ``所有 *.before 已落盘``：
    # 若进程在拷贝中途崩溃 / _atomic_copy / copytree 报错留下半成品目录，哨兵还没写
    # （或被开头 unlink 了，N-6）→ is_real_snapshot 返 False → list/rollback 跳过，不会
    # 拿半成品去误 rollback 删灵魂（N-5/N-6）。冷启动空真快照（无 *.before）也走到这里。
    sentinel.write_text("", encoding="utf-8")
    _fsync_path(sentinel)
    _fsync_path(snapshot_dir)

    _LOG.info("write_snapshot: dir=%s archived=%d", snapshot_dir, len(archived))
    return archived


def _section_bounds(lines: list[str], section: str) -> tuple[int, int]:
    """定位 ``section`` 标题在 ``lines`` 中的 [标题行索引, 下一同级或更高级标题索引)。

    匹配规则：标题文本（去 #/空白）等于 ``section``（**也去前导 #/空白后**）。section
    存在多个同名标题取第一个。末尾边界 = 下一个 level ≤ 本节 level 的标题（子节包含在内）。

    找不到 raise ``LandApplyError``。

    为什么 target 也要 ``lstrip("#")``（2026-06-29 实证）：reflect LLM（尤其 gemini）常把
    markdown 前缀写进 ``section`` 字段（``"## 事件锚与关系里程碑"``），而 ``_HEADING_RE``
    的 ``group(2)`` 已不含 ``#``。不归一化则 ``事件锚与关系里程碑`` ≠ ``## 事件锚…`` →
    section 未找到 → land 回滚 + 软降级，**静默丢掉本次灵魂沉淀**（且 anti-leak 日志查不出）。
    剥掉 target 前导 ``#`` + 空白后，``"## X"`` / ``"X"`` 都能命中标题 ``"## X"``。
    """
    target = section.lstrip("#").strip()
    start = -1
    start_level = 0
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and m.group(2).strip() == target:
            start = i
            start_level = len(m.group(1))
            break
    if start < 0:
        raise LandApplyError(f"section 未找到：{section!r}")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = _HEADING_RE.match(lines[j])
        if m and len(m.group(1)) <= start_level:
            end = j
            break
    return start, end


def apply_change_to_text(text: str, operation: str, section: str | None, content: str) -> str:
    """纯函数：把一条 change 应用到文件文本，返回新文本（docs §5.3 operation 语义）。

    - ``append_to_section``：在 ``section`` 末尾（下一同级/更高级标题前）追加 ``content``
    - ``replace_section``：用 ``标题行 + content`` 替换整节（保留原标题行）
    - ``append_file``：在文件末尾追加 ``content``（``section`` 必为 None）

    不合法 operation / section 缺失 raise ``LandApplyError``。不处理路径（调用方已 sandbox）。
    """
    if operation == "append_file":
        if section is not None:
            raise LandApplyError("append_file 不应带 section")
        sep = "" if text.endswith("\n") or not text else "\n"
        body = content if content.endswith("\n") else content + "\n"
        return text + sep + body

    if section is None or not section.strip():
        raise LandApplyError(f"operation={operation!r} 需非空 section")

    lines = text.splitlines(keepends=True)
    start, end = _section_bounds(lines, section)
    block = content if content.endswith("\n") else content + "\n"

    if operation == "append_to_section":
        # 插在节末（end 前）；保证与前文隔一空行不强求，直接插入
        new_lines = lines[:end] + [block] + lines[end:]
        return "".join(new_lines)
    if operation == "replace_section":
        # 保留标题行（start），替掉 (start, end) 的正文
        heading = lines[start]
        if not heading.endswith("\n"):
            heading += "\n"
        new_lines = lines[:start] + [heading, block] + lines[end:]
        return "".join(new_lines)

    raise LandApplyError(f"未知 operation：{operation!r}")


def _change_field(change: object, key: str) -> object:
    """从 change（dict 或对象）取字段，缺失返 None。"""
    if isinstance(change, dict):
        return change.get(key)
    return getattr(change, key, None)


def apply_change(change: object, layout: SoulLayout) -> Path:
    """应用单条 change 到落盘文件（二次 sandbox + 读-改-原子写）。

    步骤：``assert_safe_change`` 二次路径沙箱 → 读现文（不存在当空串，支持首次
    创建 append_file）→ ``apply_change_to_text`` → 原子写回。返回实际写入 Path。

    路径不安全 raise ``LandPathError``；内容应用失败 raise ``LandApplyError``。
    """
    file = _change_field(change, "file")
    dst = assert_safe_change(file, layout)
    operation = _change_field(change, "operation")
    section = _change_field(change, "section")
    content = _change_field(change, "content")
    if not isinstance(operation, str):
        raise LandApplyError(f"change.operation 缺失或非 str：{operation!r}")
    if not isinstance(content, str):
        raise LandApplyError(f"change.content 缺失或非 str：{type(content).__name__}")
    if section is not None and not isinstance(section, str):
        raise LandApplyError(f"change.section 非 str/None：{type(section).__name__}")

    old = dst.read_text(encoding="utf-8") if dst.exists() else ""
    new_text = apply_change_to_text(old, operation, section, content)
    _atomic_write_text(dst, new_text)
    return dst


def _atomic_write_text(path: Path, text: str) -> None:
    """tmpfile + fsync + os.replace + 父目录 fsync 原子写文本（WAL crash-safety）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
        tmp_path = Path(f.name)
    os.replace(tmp_path, path)
    _fsync_path(path.parent)


def restore_snapshot(snapshot_dir: Path, layout: SoulLayout) -> None:
    """WAL 回滚：把灵魂恢复到做梦前状态（docs §7.1 “回滚到上个 fsync 点”、§6.4
    “SOUL/IDENTITY/USER 保持原样”）。

    「快照即真相」推断：``write_snapshot`` 对做梦前**不存在**的源跳过存档（无
    ``*.before``）。所以回滚要双向恢复：

    - 有 ``*.before`` → 覆盖回（恢复旧内容）
    - **无 ``*.before`` 但 live 文件存在 → unlink**（删掉本轮 append_file 新建的文件，N-1）

    旧 snapshot 中可能存在的 ``activities/`` 保持原样，但不读取、不迁移、不清理。
    """
    for name, dst in (
        ("SOUL.md", layout.soul_full),
        ("IDENTITY.md", layout.identity),
        ("USER.md", layout.user),
    ):
        before = snapshot_dir / f"{name}.before"
        if before.exists():
            _atomic_copy(before, dst)
        elif dst.exists():
            # 做梦前本不存在（无 before）→ 本轮新建的，删回去
            dst.unlink()
            _fsync_path(dst.parent)
    _LOG.warning("restore_snapshot: 已回滚灵魂到 snapshot=%s", snapshot_dir)


def apply_changes_wal(changes: list[object], layout: SoulLayout, snapshot_dir: Path) -> list[str]:
    """WAL 式批量应用 changes：先存快照 → 逐条 apply；任一失败回滚全部后 raise。

    docs §7.1：先存快照再写新版，任一步失败回滚到上个 fsync 点。返回实际改动
    的文件相对名列表（去重，供梦日记 / index.json）。

    空 changes 直接返空（仍会先 ``write_snapshot`` 由调用方决定）。
    """
    write_snapshot(snapshot_dir, layout)
    changed: list[str] = []
    try:
        for idx, change in enumerate(changes):
            apply_change(change, layout)
            file = _change_field(change, "file")
            if isinstance(file, str) and file not in changed:
                changed.append(file)
            _LOG.debug("apply_changes_wal: applied #%d file=%s", idx, file)
    except (LandPathError, LandApplyError, OSError) as exc:
        _LOG.warning("apply_changes_wal: 应用失败，回滚整次。err_type=%s", type(exc).__name__)
        restore_snapshot(snapshot_dir, layout)
        raise
    return changed
