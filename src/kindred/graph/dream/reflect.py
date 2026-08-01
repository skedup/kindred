"""``Step 3.reflect`` —— 反思要不要改灵魂（LLM call #2，做梦核心）。

参考文档：docs/11-dreaming.md §5。

职责：read 当前灵魂（SOUL / IDENTITY / USER）+ Step 1 摘要，用 LLM 产出一份 diff
提议（``ReflectionDiff``）：是否改灵魂、改哪些 section、importance 分级、supersedes 链。
允许 ``decision="skip_today"``——累的夜 / 平淡的夜只总结不改（11 §5.2 / D-010）。

- 输入：``state.messages_summary`` + 灵魂文件（L2 节点本地 read）
- 输出 patch：``{"reflection": ReflectionDiff.model_dump()}``

节点结构（三层，与 summarize 对称）：

- 顶层 ``_step3_reflect_impl(client, state, *, soul/identity/user 路径)`` 真实现
- ``make_reflect_node(client, *, ...)`` factory → closure
- ``step3_reflect(state)`` 顶层 mock 透传（未注 deps 时返 skip_today 桩占位）

**软心降级**（MEMORY R3，不崩心跳）：

- 灵魂文件缺失 → 软心读空串（人格缺失 prompt 仍合法，只是没温度）
- ``messages_summary`` 空 → 仍可反思（但无昨日素材，LLM 多半 skip_today）
- **LLM 失败 / 坏 schema / 坏 operation-section → 降级为 ``skip_today``**（docs §8.1 行
  472：Step 3 反思失败仍完成 stack 重置 + 摘要，只跳过灵魂改写）。不能抛异常
  让 ``graph.invoke`` 崩——那会跳过 gate/land 的安全路径。warning 记形状信息。

**安全默认**：任何异常路径产出的 fallback 都是 ``skip_today``（不提议改写）。

**当前范围**：read 灵魂三件套 + 摘要 → LLM → ReflectionDiff。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kindred.config import (
    DEFAULT_IDENTITY_PATH,
    DEFAULT_SOUL_FULL_PATH,
    DEFAULT_USER_PATH,
)
from kindred.graph._shared._common import NodeReturn
from kindred.graph._shared._errors import NodeContractError
from kindred.llm.templates import render_prompt
from kindred.state.dream import DreamState, ReflectionDiff

if TYPE_CHECKING:
    from collections.abc import Callable

    from kindred.llm.client import LlmClient

_LOG = logging.getLogger(__name__)


class ReflectInError(NodeContractError):
    """reflect 反思失败的语义化包装（LLM 调用 / schema / operation-section 校验）。

    与 ``SummarizeContractError`` 同源哲学。但注意：按 docs/11 §8.1，reflect **不**把
    本异常抩到 graph 外——``_step3_reflect_impl`` 内部捕获后软心降级为 ``skip_today``
    （反思失败仍完成 stack 重置+摘要，只跳过灵魂改写）。本类作为 ``_validate_*``
    的内部信号，不对外 raise。
    """


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


def make_reflect_node(
    client: LlmClient,
    *,
    soul_full_path: Path = DEFAULT_SOUL_FULL_PATH,
    identity_path: Path = DEFAULT_IDENTITY_PATH,
    user_path: Path = DEFAULT_USER_PATH,
) -> Callable[[DreamState], NodeReturn]:
    """构造 dream Step 3.reflect closure。

    用法（详 build_dream_graph / daemon）::

        client = MockLlmClient()
        reflect = make_reflect_node(client)
        graph = build_dream_graph(reflect_node=reflect)

    三个灵魂路径可注入（测试可指 tmp_path）；缺省走 config 默认。
    """

    def reflect_closure(state: DreamState) -> NodeReturn:
        return _step3_reflect_impl(
            client,
            state,
            soul_full_path=soul_full_path,
            identity_path=identity_path,
            user_path=user_path,
        )

    return reflect_closure


# ─────────────────────────────────────────────────────────────────────
# 真实现（顶层 _impl）
# ─────────────────────────────────────────────────────────────────────


def _step3_reflect_impl(
    client: LlmClient,
    state: DreamState,
    *,
    soul_full_path: Path = DEFAULT_SOUL_FULL_PATH,
    identity_path: Path = DEFAULT_IDENTITY_PATH,
    user_path: Path = DEFAULT_USER_PATH,
) -> NodeReturn:
    """Step 3 真实现：read 灵魂三件套 + 摘要 → LLM 反思 → ReflectionDiff。

    流程：

    1. L2 read 灵魂三件套（缺失软心空串）
    2. 渲染 prompt（灵魂 + 摘要）
    3. ``client.complete(role="dream.reflect")``
    4. validate ``ReflectionDiff`` → 返 ``{"reflection": diff.model_dump()}``
    """
    soul = _read_soul_file(soul_full_path)
    identity = _read_soul_file(identity_path)
    user = _read_soul_file(user_path)
    summary = str(state.get("messages_summary") or "").strip()

    prompt = _render_reflect_prompt(soul=soul, identity=identity, user=user, summary=summary)

    # docs/11 §8.1：反思（LLM 调用 / schema / operation-section 校验）失败都软心降级
    # 为 skip_today，不抛异常让 graph 崩——仍走 gate/land（gate 放行空 changes）。只跳过灵魂改写。
    try:
        out = client.complete(prompt, role="dream.reflect")
        diff = _validate_reflection(out)
    except Exception as exc:  # noqa: BLE001 - 反思失败统一软心降级（§8.1）
        # 只记「形状/状态」（失败类型 + 错误个数），绝不记 str(exc)——pydantic v2
        # 会把非法 input_value（即 LLM 拟写入 SOUL/USER 的 content 正文）打进错误文本，
        # 原样入日志会泄露灵魂正文（可观测性边界：常态日志不记正文/prompt/note）。
        _LOG.warning(
            "dream Step 3.reflect: 反思失败 failure_type=%s → 降级 skip_today",
            type(exc).__name__,
        )
        reason = "反思失败软心降级（docs §8.1）：仅跳过灵魂改写。"
        return {"reflection": _skip_today(reason).model_dump()}

    _LOG.info(
        "dream Step 3.reflect: decision=%s changes=%d",
        diff.decision,
        len(diff.changes),
    )
    return {"reflection": diff.model_dump()}


# ─────────────────────────────────────────────────────────────────────
# Helpers（私有）
# ─────────────────────────────────────────────────────────────────────


def _skip_today(reasoning: str) -> ReflectionDiff:
    """造一个 ``skip_today`` diff（不提议任何灵魂改写，最安全默认）。"""
    return ReflectionDiff(decision="skip_today", reasoning=reasoning, changes=[])


def _read_soul_file(path: Path) -> str:
    """L2 read 单个灵魂文件；缺失/读失败 → 软心返 ""（不崩心跳）。"""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _render_reflect_prompt(*, soul: str, identity: str, user: str, summary: str) -> str:
    """把灵魂三件套 + 昨日摘要渲染成 reflect 的 user prompt。"""
    return render_prompt(
        "dream_reflect.md.j2",
        soul=soul or "（空）",
        identity=identity or "（空）",
        user=user or "（空）",
        summary=summary or "（昨日无摘要）",
    )


def _validate_reflection(out: object) -> ReflectionDiff:
    """校 LLM 返回 dict 的 ``ReflectionDiff`` schema（含跨字段 invariant）。

    非 dict / 不合契约一律包成 ``ReflectInError``（供节点边界统一软心降级）。

    **防泄漏**：包装文案只带 ``error_count``，绝不拼 ``str(ValidationError)``——后者
    会把非法 input_value（LLM 拟写入 SOUL/USER 的 ``changes[].content`` 正文）
    嵌进错误文本，被上层 ``except`` 入日志时会泄露灵魂正文。
    """
    from pydantic import ValidationError

    if not isinstance(out, dict):
        raise ReflectInError(
            f"dream Step 3.reflect: LLM 返回顶层非 dict（response_type={type(out).__name__}）",
        )
    try:
        return ReflectionDiff.model_validate(out)
    except ValidationError as exc:
        # 只拿 error_count（不携正文）；from exc 保留 traceback 供调试，但上层不入日志。
        raise ReflectInError(
            f"dream Step 3.reflect: LLM 返回不合 ReflectionDiff 契约（errors={exc.error_count()}）",
        ) from exc


# ─────────────────────────────────────────────────────────────────────
# 顶层 mock 透传（保留 D6.1 桩兼容 / 拓扑测试）
# ─────────────────────────────────────────────────────────────────────


def step3_reflect(state: DreamState) -> NodeReturn:
    """Step 3 顶层 mock 透传（未注 deps 时使用，桩占位）。

    返回 ``skip_today``（最安全默认：不提议任何灵魂改写），让 graph 拓扑能跑通。
    真实路径：``make_reflect_node(client)`` 注入走 ``_step3_reflect_impl``。
    """
    _LOG.debug("dream Step 3.reflect (mock pass-through) → skip_today")
    diff = _skip_today("mock 透传：未注 LLM client，默认不改灵魂。")
    return {"reflection": diff.model_dump()}
