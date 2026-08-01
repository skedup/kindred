"""dream Step 5.land 的人类可见层 —— 梦日记（§7.3）+ index.json（§7.2）。

参考文档：docs/11-dreaming.md §7.2（index.json 元数据）、§7.3（梦日记人类可读）。

land 落盘成功（snapshot + change 应用）后调用，产出两份**给人看**的东西：

- ``write_dream_journal``：``snapshot_dir/dream-<date>.md`` —— ta 自己明天醒来读、user 也能读
- ``update_index_json``：``soul-history/index.json`` —— user ``jq`` 浏览全部演化历史，不需 git

两者都不参与 graph state，是落盘的副产物。失败不应崩心跳（调用方软心降级）。
原子写复用 ``_land_fs._atomic_write_text``（fsync 文件 + 父目录，crash-safety）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kindred.graph.dream._land_fs import _atomic_write_text

_LOG = logging.getLogger(__name__)

# index.json 时点：dream_date + 04:00（docs §9.1 默认做梦时点）+ 内网时区
_DREAM_HOUR = "04:00:00"
_DREAM_TZ_OFFSET = "+08:00"


def _change_field(change: object, key: str) -> Any:
    """从 change（dict 或对象）取字段，缺失返 None。"""
    if isinstance(change, Mapping):
        return change.get(key)
    return getattr(change, key, None)


def _verdict_field(verdict: object, key: str) -> Any:
    if isinstance(verdict, Mapping):
        return verdict.get(key)
    return getattr(verdict, key, None)


def _render_change_line(idx: int, change: object) -> str:
    """渲染梦日记「改了什么」里的一行（人类可读）。"""
    file = _change_field(change, "file") or "?"
    section = _change_field(change, "section")
    if section:
        return f"{idx}. {file} 「{section}」"
    return f"{idx}. {file}（追加到文件末尾）"


def render_dream_journal(
    dream_date: str,
    reflection: object,
    gate_verdict: object,
    applied_changes: Sequence[object],
) -> str:
    """渲染梦日记 markdown（docs §7.3 结构）。纯函数，便于单测。

    结构：标题 + 今晨的我（reasoning）+ 改了什么（applied_changes）+ 闸门怎么说
    （verdict / warnings）。``applied_changes`` 为空（skip_today）时「改了什么」写「没改」。
    """
    reasoning = _change_field(reflection, "reasoning") or "（无反思说明）"
    verdict = _verdict_field(gate_verdict, "verdict") or "unknown"
    warnings = _verdict_field(gate_verdict, "warnings")
    warnings_list = (
        list(warnings)
        if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes))
        else []
    )

    lines: list[str] = [f"# 梦 · {dream_date} 04:00", "", "## 今晨的我", reasoning, ""]

    lines.append("## 改了什么")
    if applied_changes:
        for i, change in enumerate(applied_changes, start=1):
            lines.append(_render_change_line(i, change))
    else:
        lines.append("（今晨没有改灵魂——累的夜 / 平淡的夜，只总结昨天）")
    lines.append("")

    lines.append("## 闸门怎么说")
    lines.append(f"verdict: {verdict}")
    if warnings_list:
        lines.append(f"warnings: {warnings_list}")
    else:
        lines.append("warnings: []")
    lines.append("")
    lines.append("🍷")
    lines.append("")
    return "\n".join(lines)


def write_dream_journal(
    snapshot_dir: Path,
    dream_date: str,
    reflection: object,
    gate_verdict: object,
    applied_changes: Sequence[object],
) -> Path:
    """写梦日记到 ``snapshot_dir/dream-<date>.md``，返回路径。"""
    path = snapshot_dir / f"dream-{dream_date}.md"
    text = render_dream_journal(dream_date, reflection, gate_verdict, applied_changes)
    _atomic_write_text(path, text)
    _LOG.info("write_dream_journal: %s", path)
    return path


def _soul_root_relpath(file: str | None) -> str | None:
    """把 reflection change 的 ``soul/...`` 路径归一成 soul-root 相对路径（docs §7.2）。

    index.json / snapshot 布局用的是 soul-root 相对路径（如 ``SOUL.md``），
    而 reflection change.file 带 ``soul/`` 前缀
    （``soul/SOUL.md``）。这里剥掉前缀让 index 路径与 snapshot 布局一致——否则 user
    通过 index 浏览演化历史会看到一套和 snapshots/ 不一致的路径。
    """
    if file is None:
        return None
    return file[len("soul/") :] if file.startswith("soul/") else file


def _build_index_entry(
    dream_date: str,
    gate_verdict: object,
    applied_changes: Sequence[object],
    dream_log_relpath: str,
    reasoning: str,
) -> dict[str, Any]:
    """构造 index.json 里一条 snapshot 元数据（docs §7.2 结构）。"""
    changed_files: list[dict[str, Any]] = []
    for change in applied_changes:
        entry: dict[str, Any] = {"file": _soul_root_relpath(_change_field(change, "file"))}
        section = _change_field(change, "section")
        if section is not None:
            entry["section"] = section
        importance = _change_field(change, "importance")
        if importance is not None:
            entry["importance"] = importance
        supersedes = _change_field(change, "supersedes")
        if supersedes is not None:
            entry["supersedes"] = supersedes
        sb = _change_field(change, "summary_before")
        if sb is not None:
            entry["summary_before"] = sb
        sa = _change_field(change, "summary_after")
        if sa is not None:
            entry["summary_after"] = sa
        changed_files.append(entry)

    verdict = _verdict_field(gate_verdict, "verdict") or "unknown"
    return {
        "timestamp": f"{dream_date}T{_DREAM_HOUR}{_DREAM_TZ_OFFSET}",
        "trigger": "morning_dream",
        "verdict": verdict,
        "changed_files": changed_files,
        "reasoning": reasoning,
        "dream_log": dream_log_relpath,
    }


def update_index_json(
    index_path: Path,
    dream_date: str,
    gate_verdict: object,
    applied_changes: Sequence[object],
    reflection: object,
    dream_log_relpath: str,
) -> Path:
    """把本次做梦的元数据 append 到 ``index.json`` 的 ``snapshots`` 列表，返回路径。

    幂等保护：若已有同 timestamp 条目，**替换**而非重复 append（同夜重跑做梦不产生
    重复条目）。损坏 / 不存在的 index.json → 当空 ``{"snapshots": []}`` 起步（fail-safe，
    不让一次坏文件阻断落盘；旧条目丢失风险低于做梦整体崩溃）。
    """
    reasoning = _change_field(reflection, "reasoning") or ""
    entry = _build_index_entry(
        dream_date, gate_verdict, applied_changes, dream_log_relpath, reasoning
    )

    index: dict[str, Any] = {"snapshots": []}
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping) and isinstance(loaded.get("snapshots"), list):
                index = {"snapshots": list(loaded["snapshots"])}
        except (json.JSONDecodeError, OSError):
            _LOG.warning("update_index_json: index.json 损坏 / 不可读，重建空索引 %s", index_path)

    snapshots: list[Any] = index["snapshots"]
    ts = entry["timestamp"]
    snapshots = [s for s in snapshots if not (isinstance(s, Mapping) and s.get("timestamp") == ts)]
    snapshots.append(entry)
    index["snapshots"] = snapshots

    text = json.dumps(index, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(index_path, text)
    _LOG.info("update_index_json: %s snapshots=%d", index_path, len(snapshots))
    return index_path
