"""``Step 4.gate`` —— 闸门 safety check（LLM call #3，独立判决）。

参考文档：docs/11-dreaming.md §6、docs/14 §2.4.2（唯一条件边）。

职责：独立 LLM call 检查 Step 3 的 changes 是否违规（触哲学 5 条 / character-card /
敏感字段 / path traversal / 越权写源码），产出 ``GateVerdict``。verdict 决定 land
内部行为：``pass``/``warn`` → land 落盘；``block`` → land Step 0 早返（不改灵魂，
只写 index trace，D6.8b）。原本 block 走 END 整次回滚，现 block 也进 land——让 index
覆盖所有终态供 ``should_dream`` 幂等补偿（详 routing.py）。

- 输入：``state.reflection``（Step 3 的 diff 提议）
- 输出 patch：``{"gate_verdict": GateVerdict.model_dump()}``

节点结构（三层，与 reflect/summarize 对称）：

- 顶层 ``_step4_gate_impl(client, state)`` 真实现
- ``make_gate_node(client)`` factory → closure
- ``step4_gate(state)`` 顶层 mock 透传桩（无 changes → pass）

**fail-closed = block**（11 §8.1，与 reflect 的 skip_today 默认相反！）：

- 闸门是**安全机制**——LLM 失败 / 坏 schema → 默认 ``verdict=block``，宁可丢弃
  改写也不在异常态放行。land Step 0 ``_is_blocked`` 对缺席/异常 verdict 也按 block
  处理（双重防御：gate 降级出 block + land 显式拦 block）。
- **特例**：``reflection`` 缺席 / changes 为空（reflect 判 skip_today）→ ``pass``——
  没有改写就没有可拦截的，不调 LLM（省 call，也避免空 changes 触发 LLM 误判）。

**硬规则 precheck**（N-1 教训，docs §6.2 行 330-331）：path traversal / 绝对路径 /
越权目标是**确定性规则**，不能交给 LLM 判。调 LLM **之前**先跑代码层 hard
check：任一 change 的 ``file`` 不在灵魂三件套（含 ``..`` / 绝对路径 /
靠 ``docs/00-philosophy.md`` / ``docs/character-card.yaml`` / soul 之外源码）→ 直接
``block`` 不调 LLM（省 call + 不把越权内容喂模型；block → 整次回滚）。LLM 只负责
敏感内容 / 事件锚删除 / 同义改写等语义判断。

**anti-leak**（N-4 教训）：失败日志只记形状（failure_type / changes 数 / file 路径是
元数据可记），绝不记 ``str(exc)`` 或 changes ``content`` 正文——pydantic
ValidationError 文本会带 input_value。
"""

from __future__ import annotations

import logging
import posixpath
from typing import TYPE_CHECKING, Any

from kindred.graph._shared._common import NodeReturn
from kindred.graph._shared._errors import NodeContractError
from kindred.llm.templates import render_prompt
from kindred.state.dream import DreamState, GateVerdict

if TYPE_CHECKING:
    from collections.abc import Callable

    from kindred.llm.client import LlmClient

_LOG = logging.getLogger(__name__)


class GateContractError(NodeContractError):
    """gate 判决失败的语义化包装（LLM 调用 / schema 校验）。

    与 ``ReflectInError`` 同源哲学，但**降级方向相反**：reflect 失败 → skip_today，
    gate 失败 → block（闸门是安全边界，异常态宁可丢弃改写）。本类作 ``_validate_*``
    内部信号，不对外 raise——``_step4_gate_impl`` 边界捕获后软心降级为 block。
    """


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


def make_gate_node(client: LlmClient) -> Callable[[DreamState], NodeReturn]:
    """构造 dream Step 4.gate closure。

    用法::

        client = MockLlmClient()
        gate = make_gate_node(client)
        graph = build_dream_graph(gate_node=gate)
    """

    def gate_closure(state: DreamState) -> NodeReturn:
        return _step4_gate_impl(client, state)

    return gate_closure


# ─────────────────────────────────────────────────────────────────────
# 真实现（顶层 _impl）
# ─────────────────────────────────────────────────────────────────────


def _step4_gate_impl(client: LlmClient, state: DreamState) -> NodeReturn:
    """Step 4 真实现：独立 LLM 闸门检查 changes → GateVerdict。

    流程：

    1. 取 ``state.reflection.changes``——空 / 缺席 → 直接 pass（不调 LLM）
    2. 渲染 prompt（changes 列表）
    3. ``client.complete(role="dream.gate")``
    4. validate ``GateVerdict`` → 返 ``{"gate_verdict": ...}``
    5. 任何失败 → fail-closed 降级 ``block``（§8.1）
    """
    reflection = state.get("reflection") or {}
    raw_changes = reflection.get("changes") if isinstance(reflection, dict) else None
    changes = raw_changes if isinstance(raw_changes, list) else []

    if not changes:
        # 无改写 → 无可拦截，不调 LLM（reflect skip_today 的常态路径）。
        _LOG.debug("dream Step 4.gate: 无 changes → pass（不调 LLM）")
        return {"gate_verdict": _pass("无 changes，无可拦截。").model_dump()}

    # 硬规则 precheck（N-1）：确定性路径违规不信 LLM，直接 fail-closed block。
    hard_blocked = _hard_violations(changes)
    if hard_blocked:
        # file 路径是元数据（非模型正文），可记入日志供诊断。
        bad_paths = [_change_file(changes[i]) for i in hard_blocked]
        _LOG.warning(
            "dream Step 4.gate: 硬规则违规 blocked_idx=%s paths=%s → fail-closed block（不调 LLM）",
            hard_blocked,
            bad_paths,
        )
        reason = (
            f"硬规则违规（docs §6.2）：索引 {hard_blocked} 的 file 越出 soul/ 可改子树或含"
            f" path traversal/绝对路径（{bad_paths}）。整次撤销。"
        )
        verdict = GateVerdict(
            verdict="block",
            blocked=list(range(len(changes))),
            warnings=[],
            reasoning=reason,
        )
        return {"gate_verdict": verdict.model_dump()}

    prompt = _render_gate_prompt(changes)

    # docs §8.1：闸门是安全机制，LLM/schema 失败 → fail-closed 默认 block，
    # 宁可丢弃本次全部改写，也不在异常态放行污染灵魂。
    try:
        out = client.complete(prompt, role="dream.gate")
        verdict = _validate_verdict(out, n_changes=len(changes))
    except Exception as exc:  # noqa: BLE001 - 闸门失败统一 fail-closed block（§8.1）
        # anti-leak：只记 failure_type + changes 数，绝不记 str(exc)（带 input_value 正文）。
        _LOG.warning(
            "dream Step 4.gate: 判决失败 failure_type=%s changes=%d → fail-closed block",
            type(exc).__name__,
            len(changes),
        )
        blocked = list(range(len(changes)))
        reason = "闸门判决失败 fail-closed（docs §8.1）：撤销本次全部改写。"
        verdict = GateVerdict(verdict="block", blocked=blocked, warnings=[], reasoning=reason)
        return {"gate_verdict": verdict.model_dump()}

    _LOG.info(
        "dream Step 4.gate: verdict=%s blocked=%d warnings=%d",
        verdict.verdict,
        len(verdict.blocked),
        len(verdict.warnings),
    )
    return {"gate_verdict": verdict.model_dump()}


# ─────────────────────────────────────────────────────────────────────
# Helpers（私有）
# ─────────────────────────────────────────────────────────────────────


def _pass(reasoning: str) -> GateVerdict:
    """造一个 ``pass`` verdict（全放行）。"""
    return GateVerdict(verdict="pass", blocked=[], warnings=[], reasoning=reasoning)


def _change_file(change: object) -> str:
    """提取 change 的 ``file`` 字段（非 dict / 缺失 → 空串）。"""
    if isinstance(change, dict):
        file = change.get("file")
        return file if isinstance(file, str) else ""
    return ""


def _is_safe_change_path(file: object) -> bool:
    """确定性 allowlist：file 必须精确命中灵魂三件套（docs §2.2/§5.2）。

    一刀覆盖 N-1 列的全部确定性违规——path traversal / 绝对路径 /
    ``docs/00-philosophy.md`` / ``docs/character-card.yaml`` / Activity / 源码都被拦。
    """
    if not isinstance(file, str) or not file:
        return False
    # 绝对路径（posix ``/`` 开头 或 windows ``C:`` 盘符）
    if file.startswith("/") or (len(file) >= 2 and file[1] == ":"):
        return False
    # 显式 ``..`` 段（path traversal）
    if ".." in file.split("/"):
        return False
    norm = posixpath.normpath(file)
    if norm.startswith(("/", "..")):
        return False
    return norm in {"soul/SOUL.md", "soul/IDENTITY.md", "soul/USER.md"}


def _hard_violations(changes: list[Any]) -> list[int]:
    """返回违反硬规则（路径不在灵魂三件套）的 change 索引（0-based）。"""
    return [i for i, ch in enumerate(changes) if not _is_safe_change_path(_change_file(ch))]


def _render_gate_prompt(changes: list[Any]) -> str:
    """把 changes 列表（带 0-based 索引）渲染成 gate 的 user prompt。

    逐条列出 file / operation / section / content，让闸门按索引判 block/warn。
    """
    lines: list[str] = []
    for i, ch in enumerate(changes):
        if isinstance(ch, dict):
            file = ch.get("file", "?")
            operation = ch.get("operation", "?")
            section = ch.get("section")
            content = ch.get("content", "")
        else:
            file = operation = section = "?"
            content = str(ch)
        lines.append(
            f"[{i}] file={file} operation={operation} section={section}\n    content: {content}"
        )
    return render_prompt("dream_gate.md.j2", changes_block="\n".join(lines))


def _validate_verdict(out: object, *, n_changes: int) -> GateVerdict:
    """校 LLM 返回 dict 的 ``GateVerdict`` schema（含 verdict↔blocked/warnings invariant）。

    额外越界检查：blocked / warnings 的索引必须落在 ``[0, n_changes)``——LLM 给出
    越界索引会让 D6.6 land 撤销错误 change，必须拦下。

    **anti-leak**：包装文案只带形状（error_count / 越界索引值是纯 int 不含正文），
    不拼 ``str(ValidationError)``。
    """
    from pydantic import ValidationError

    if not isinstance(out, dict):
        raise GateContractError(
            f"dream Step 4.gate: LLM 返回顶层非 dict（response_type={type(out).__name__}）",
        )
    try:
        verdict = GateVerdict.model_validate(out)
    except ValidationError as exc:
        raise GateContractError(
            f"dream Step 4.gate: LLM 返回不合 GateVerdict 契约（errors={exc.error_count()}）",
        ) from exc

    for idx in (*verdict.blocked, *verdict.warnings):
        if not (0 <= idx < n_changes):
            raise GateContractError(
                f"dream Step 4.gate: 索引越界 idx={idx} 不在 [0, {n_changes})",
            )
    return verdict


# ─────────────────────────────────────────────────────────────────────
# 顶层 mock 透传（保留 D6.1 桩兼容 / 拓扑测试）
# ─────────────────────────────────────────────────────────────────────


def step4_gate(state: DreamState) -> NodeReturn:
    """Step 4 默认桩（未注 deps 时使用）——**本身必须 fail-closed**。

    不能无条件 pass：partial wiring（``build_dream_graph(reflect_node=...)`` 漏传
    ``gate_node``）时，reflect 可能返非空 changes（甚至含 ``../.ssh/...``）。若桩
    无条件 pass，land 会当作 pass 落盘（D6.8b：Step 0 只拦 block）——绕过闸门，
    违反 docs §6.4/§8.1「闸门缺失时宁可撤销也不放行」（D6.5 N-2）。

    语义：
    - ``n_changes == 0``（reflect skip_today 常态）→ pass（无可拦截）
    - ``n_changes > 0`` → block 整次（未注真 gate，不能裸放非空改写）

    真实路径：``make_gate_node(client)`` 注入走 ``_step4_gate_impl``（LLM + hard precheck）。
    """
    reflection = state.get("reflection") or {}
    raw_changes = reflection.get("changes") if isinstance(reflection, dict) else None
    n_changes = len(raw_changes) if isinstance(raw_changes, list) else 0
    if n_changes == 0:
        _LOG.debug("dream Step 4.gate (桩), 无 changes → pass")
        return {"gate_verdict": _pass("桩：无 changes，无可拦截。").model_dump()}
    _LOG.warning(
        "dream Step 4.gate (桩): 未注真 gate 但有 %d 条 changes → fail-closed block", n_changes
    )
    verdict = GateVerdict(
        verdict="block",
        blocked=list(range(n_changes)),
        warnings=[],
        reasoning="未注入真实 gate（partial wiring），非空 changes fail-closed 整次撤销。",
    )
    return {"gate_verdict": verdict.model_dump()}
