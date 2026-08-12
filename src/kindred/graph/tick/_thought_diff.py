"""Thought diff 应用 helper -- 给 sense_llm 节点用。

参考文档:14-heart-graph.md §3.3「Thought 增删与 Layer 1 重派」/ 09-memory.md §3.2。

本模块只负责一件事:拿 LLM 输出的 ``thought_diff`` dict + 当前 thoughts dict
list(storage form),返回应用 diff 后的新 dict list(storage form)。

为什么独立 helper
=================

sense_llm 节点要把 LLM 返回的
``{"thought_diff": {"add": [...], "remove": [...]}}`` 落到
``next_state.interior.thoughts``。这块逻辑:

- ``add``:dict list 走 ``validate_thought`` 逐条 raise(坏数据立刻冒泡,与
  ``_common.validate_thought`` 不可绕过哲学一致)
- ``remove``:按 ``description`` 字符串匹配(简化版--后续视情扩到三元组
  description+tag+mood_w 匹配)
- 不匹配的 remove 不报错(**软心**):LLM 偶尔 hallucinate 一条不存在的
  thought 想 remove,节点不能因此 raise--日志 warning 跳过即可

抽到独立模块的好处:
1. 单测无需 mock LLM / db,纯 in-memory 验证 diff 行为
2. 节点函数保持「读 → 调 LLM → apply diff → 返 patch」线性骨架,diff 细节
   不污染节点主流程

接口形态:dict-in dict-out
==========================

storage truth 是 dict(02 §3.1)--helper 接 dict 出 dict 与 storage 对称:

- 输入:``list[dict]``(storage form,与 ``next_state.interior.thoughts`` 一致)
- 输出:``list[dict]``(storage form,可直接赋回 ``interior["thoughts"]``)
- 内部:``validate_thought(item).model_dump(mode="json")`` round-trip 一次
  做强校验 + 规范化(保 expire_at 缺省字段不丢、字符串 trim 等)

节点端就一行 ``interior["thoughts"] = apply_thought_diff(prev, diff)``,不用
节点自己 dict↔Thought 来回转。

设计选择(与 ``state/_derive.py`` 对照)
========================================

- ``_derive.py`` 在 ``state/`` 下:纯 state 层 helper(不依赖 LLM / db)
- ``_thought_diff.py`` 在 ``graph/tick/`` 下：consumer 是 sense_llm 节点,
  diff 形态本身是 LLM 输出契约的一部分(不是 state schema 内禀逻辑)

→ 放贴近 consumer。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from kindred.graph._shared._common import validate_thought
from kindred.state._derive_constants import (
    DEFAULT_THOUGHT_TTL_HOURS,
    DEFAULT_TRANSIENT_THOUGHT_TTL_HOURS,
    MAX_INTERIOR_THOUGHTS,
    TRANSIENT_THOUGHT_TAGS,
)

_LOG = logging.getLogger(__name__)


def apply_thought_diff(
    prev: list[dict[str, Any]],
    diff: dict[str, Any],
    *,
    now_iso: str | None = None,
) -> list[dict[str, Any]]:
    """对 ``prev`` 应用 ``diff``(``add`` + ``remove``),返回新 dict list。

    Parameters
    ----------
    prev
        当前 thoughts list(**storage form dict**,与 ``State.interior.thoughts``
        ``model_dump`` 后一致)。
    diff
        LLM 输出的 ``thought_diff``::

            {
                "add": list[dict],     # 每个 dict 走 validate_thought
                "remove": list[str],   # description 字符串列表
            }

        缺 ``add`` 或 ``remove`` 字段视为空 list(**软心**--LLM 可能省)。

    Returns
    -------
    list[dict]
        新 list(**storage form dict**),``prev`` 不被修改。

    Raises
    ------
    pydantic.ValidationError
        ``add`` 中任一 dict 走 ``validate_thought`` 失败立即冒泡。
        **不允许 try/except 默默跳过**(与 ``_common`` 不可绕过哲学一致)--
        坏 thought 漂到 ``next_state.interior.thoughts`` 会污染 ``State``
        校验,调试地狱。

    Notes
    -----
    匹配语义:

    - ``add`` 后 ``remove``--按 14 §3.3 输出顺序就是 add → remove,diff 是
      上一 tick → 下一 tick 的状态变化合集
    - ``remove`` 按 ``description`` 字符串完全匹配第一个;不存在则 warning
      log 跳过(**软心**:LLM 想 remove 不存在的 thought 不致命)
    - 所有匹配/添加在新 list 上完成,``prev`` 永远不被改动
    """
    add_items = diff.get("add") or []
    remove_keys = diff.get("remove") or []

    if not isinstance(add_items, list):
        raise ValueError(
            f"thought_diff.add must be a list, got {type(add_items).__name__}",
        )
    if not isinstance(remove_keys, list):
        raise ValueError(
            f"thought_diff.remove must be a list, got {type(remove_keys).__name__}",
        )

    # ── add:dict → validate_thought raise → 规范化回 dict ──
    # 走 Thought.model_validate 强校验后立即 model_dump 拿规范化 dict。
    # TTL 兜底 + 数量封顶留到最后对整个 result 一次性做（覆盖新 add + legacy 滚进来的
    # null，见下）。
    added: list[dict[str, Any]] = [
        validate_thought(item).model_dump(mode="json") for item in add_items
    ]

    # ── 先 remove 后 add 去重（N-2:保“替换”语义）────────
    # 契约说明 add 后 remove，但净效果是“上一 tick → 下一 tick 的状态变化
    # 合集”。若 add 先去重，会让“remove 旧 a + add 新 a（改 TTL/tag/mood_w）”这种
    # 替换无法表达（新 a 被 prev 里的旧 a 拦下，remove 又把旧 a 删了→变空）。
    # 所以先应用 remove，再拿存活列表做 add 去重。
    result: list[dict[str, Any]] = [dict(t) for t in prev]

    for key in remove_keys:
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                f"thought_diff.remove items must be non-blank strings, got {key!r}",
            )
        idx = next(
            (i for i, t in enumerate(result) if t.get("description") == key),
            None,
        )
        if idx is None:
            _LOG.warning(
                "apply_thought_diff: remove target description=%r not found in thoughts; skipping",
                key,
            )
            continue
        result.pop(idx)

    # ── add 按 description 去重（B:僵尸念头止血）────────
    # remove 已先执行，所以被 remove 掉的 description 现在可以被 add 新版本填回
    # （替换语义保住）；prev 里未被 remove 的同 description 才拦截新 add（不堆叠）。
    existing_descs = {t.get("description") for t in result}
    for item in added:
        desc = item.get("description")
        if desc in existing_descs:
            _LOG.warning(
                "apply_thought_diff: add description=%r already present; skipping (僵尸念头止血 B)",
                desc,
            )
            continue
        result.append(item)
        existing_descs.add(desc)

    # ── TTL 兜底：Layer 4 不留永不过期（治线上一堆 null 念头永久点燃 mood + 滚雪球）──
    # 对**整个 result**（新 add + 上 tick 滚进来的 legacy null）补兜底 TTL：transient
    # tag 走 4h、其余走 12h。真正要永久的记忆归 soul/episode 层，不是 Layer 4 事件流。
    _backfill_thought_ttl(result, now_iso)

    # ── 数量封顶：超出按列表序淘汰最旧（新 add 在尾、旧在头），防 state 无界滚雪球 ──
    if len(result) > MAX_INTERIOR_THOUGHTS:
        dropped = len(result) - MAX_INTERIOR_THOUGHTS
        result = result[-MAX_INTERIOR_THOUGHTS:]
        _LOG.info(
            "apply_thought_diff: thoughts 超上限 %d，淘汰最旧 %d 条",
            MAX_INTERIOR_THOUGHTS,
            dropped,
        )

    return result


def _backfill_thought_ttl(thoughts: list[dict[str, Any]], now_iso: str | None) -> None:
    """对 ``expire_at is None`` 的念头原地补兜底 TTL（transient 4h / 其余 12h）。

    ``now_iso`` 为 None（mock / 测试未传）时保持 None + warn——调用点（sense_llm）应传
    ``triggered_at``。
    """
    for t in thoughts:
        if t.get("expire_at") is not None:
            continue
        if now_iso is None:
            _LOG.warning(
                "apply_thought_diff: thought tag=%r expire_at=None 但无 now_iso 兜底，保持 None",
                t.get("tag"),
            )
            continue
        hours = (
            DEFAULT_TRANSIENT_THOUGHT_TTL_HOURS
            if t.get("tag") in TRANSIENT_THOUGHT_TAGS
            else DEFAULT_THOUGHT_TTL_HOURS
        )
        t["expire_at"] = (datetime.fromisoformat(now_iso) + timedelta(hours=hours)).isoformat()


__all__ = ["apply_thought_diff"]
