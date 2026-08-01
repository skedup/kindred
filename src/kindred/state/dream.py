"""DreamState —— dream graph 在 5 步节点间传递的运行时状态。

参考文档：docs/11-dreaming.md（5 步 + 1 闸门）、docs/14 §2.4（dream graph 拓扑）。

设计原则（earlier milestone 拍板 B，与 TickState 并列）：
- DreamState 是 **TypedDict**（同 TickState：LangGraph 节点返回 partial dict 即 patch）
- 与 TickState **共享 state 8 层数据结构**（``prev_state`` / ``next_state`` 以
  ``State.model_dump()`` 的 dict 形态承载，写库前用 ``State`` 校验），但**流程字段
  独立**——dream 的流水线（摘要 → 反思 diff → 闸门 verdict → 落地 snapshot）跟 tick
  的流水线（note / act_decision / act_result）是两条不同的链，不挤一个 TypedDict。
- DreamState 不背 errors 元数据——失败由 daemon / 节点降级处理（11 §8 失败模式）。

字段分组（与 11 §3 5 步 pipeline 对齐）：
- § A. trigger 上下文：triggered_at / dream_date（daemon 选 dream entry 时注入）
- § B. ta 的完整 state：prev_state / next_state（跨 graph 共享，02 §3 8 层结构）
- § C. Step 1 总结昨日：messages_summary
- § D. Step 3 反思：reflection（diff 提议）
- § E. Step 4 闸门：gate_verdict
- § F. Step 5 落地：snapshot_path / dream_journal_path

字段填写时机详见 11 §3 ~ §7。
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import Field, field_validator, model_validator

from kindred.state._base import StrictBase

# ─────────────────────────────────────────────────────────────────────
# 子结构（pydantic model，便于校验；DreamState 内仍以 dict 形式承载）
# ─────────────────────────────────────────────────────────────────────


ReflectionOperation = Literal["append_to_section", "replace_section", "append_file"]


class SoulExcerptResponse(StrictBase):
    """从最终候选 SOUL 派生出的有界 Heart 投影。"""

    soul_excerpt: str = Field(strict=True, min_length=1, max_length=200)

    @field_validator("soul_excerpt")
    @classmethod
    def _trim_excerpt(cls, value: str) -> str:
        excerpt = value.strip()
        if not excerpt:
            raise ValueError("soul_excerpt must not be blank")
        return excerpt


class ReflectionChange(StrictBase):
    """Step 3 反思输出的单条灵魂改写提议（11 §5.2）。

    一条 change = 对某个灵魂文件某个 section 的一次增量改写。importance 用于
    淡化分级（11 §5.2），supersedes 串旧快照锚点形成 supersedes 链。
    """

    file: str = Field(description="灵魂文件相对路径，如 soul/SOUL.md")
    operation: ReflectionOperation = Field(description="改写操作类型")
    section: str | None = Field(
        default=None,
        description="目标 section 标题（append_file 时为 None）",
    )
    importance: int = Field(ge=1, le=10, description="重要度 1-10，用于淡化分级")
    supersedes: str | None = Field(
        default=None,
        description="被本次改写取代的旧快照锚点（snapshot://...），形成 supersedes 链",
    )
    summary_before: str | None = Field(default=None, description="改写前一句话概括")
    summary_after: str | None = Field(default=None, description="改写后一句话概括")
    content: str = Field(description="实际写入内容")

    @model_validator(mode="after")
    def _check_operation_section(self) -> ReflectionChange:
        """operation 与 section 的交叉约束（docs/11 §5.3 行 296-299）。

        - ``append_to_section`` / ``replace_section`` ⊙ section 必填（去空白后非空）
          ——要改某节必须指得出是哪节，否则 D6.6 land 拿到不可应用的 diff。
        - ``append_file`` ⊙ section 必为 None（追加到文件末尾，不针对某节）。
        """
        has_section = bool(self.section and self.section.strip())
        if self.operation in ("append_to_section", "replace_section") and not has_section:
            raise ValueError(f"operation={self.operation!r} requires non-empty section")
        if self.operation == "append_file" and self.section is not None:
            raise ValueError("operation='append_file' requires section=None")
        return self


class ReflectionDiff(StrictBase):
    """Step 3 反思的完整输出 —— 一份 diff 提议（11 §5.2）。

    ``decision == "skip_today"``：累的夜 / 平淡的夜，只总结不改灵魂
    （11 §5.2 / D-010「是否每天都改灵魂 = 否」）。此时 ``changes`` 应为空。
    """

    decision: Literal["do_change", "skip_today"] = Field(description="今晨是否改灵魂")
    reasoning: str = Field(description="自由文本：反思对本决策的自我说明")
    changes: list[ReflectionChange] = Field(
        default_factory=list,
        description="灵魂改写提议列表；skip_today 时为空",
    )

    @model_validator(mode="after")
    def _check_decision_consistency(self) -> ReflectionDiff:
        """decision 与 changes 跟字段 invariant（docs/11 §5.2 行 286-287）。

        - ``skip_today`` ⟹ changes 必须为空（不改就不能携提议）
        - ``do_change`` ⟹ changes 至少一条（要改就得真有东西改，否则
          该规范化为 skip_today）
        """
        if self.decision == "skip_today" and self.changes:
            raise ValueError("decision='skip_today' requires empty changes")
        if self.decision == "do_change" and not self.changes:
            raise ValueError("decision='do_change' requires at least one change")
        return self


class GateVerdict(StrictBase):
    """Step 4 闸门输出（11 §6.3）。

    闸门是独立第二个 LLM call，不依赖反思节点自觉。
    - ``pass``：全部 changes 放行
    - ``block``：``blocked`` 列出的 change 索引被撤销（11 §6.4：block → 整次灵魂
      改写回滚，但 stack 仍按 Step 2 重置）
    - ``warn``：``warnings`` 列出的 change 索引放行但记录告警
    """

    verdict: Literal["pass", "block", "warn"] = Field(description="闸门裁决")
    blocked: list[int] = Field(
        default_factory=list,
        description="被撤销的 change 索引（仅 verdict=block 时可非空）",
    )
    warnings: list[int] = Field(
        default_factory=list,
        description="告警但放行的 change 索引（仅 verdict=warn 时可非空）",
    )
    reasoning: str = Field(description="闸门裁决理由")

    @model_validator(mode="after")
    def _check_verdict_consistency(self) -> GateVerdict:
        """verdict 与 blocked/warnings 跟字段 invariant（防自相矛盾闸门结果）。

        闸门是独立安全边界，下游（routing / Step 5）只看 ``verdict``，若
        允许 ``pass`` 带 blocked 或 ``block`` 带 warnings，会按 verdict 落盘而
        静默丢掉矛盾信息，击穿闸门意义：

        - ``pass`` ⟹ blocked 与 warnings 均为空（全放行，无可拦截/告警）
        - ``warn`` ⟹ blocked 为空（告警不拦截）
        - ``block`` ⟹ warnings 为空（撤销走 blocked，不走 warn）
        """
        if self.verdict == "pass" and (self.blocked or self.warnings):
            raise ValueError("verdict='pass' requires empty blocked and warnings")
        if self.verdict == "warn" and self.blocked:
            raise ValueError("verdict='warn' requires empty blocked")
        if self.verdict == "block" and self.warnings:
            raise ValueError("verdict='block' requires empty warnings")
        return self


# ─────────────────────────────────────────────────────────────────────
# DreamState（TypedDict） —— dream graph 节点签名直接用这个
# ─────────────────────────────────────────────────────────────────────


class DreamState(TypedDict, total=False):
    """单次做梦生命周期内 dream graph 节点间传递的 dict。

    11 §3：做梦不是一个 tick，是状态机的另一条路径。DreamState 承载这条路径
    5 步的运行时——trigger 上下文、Step1-5 各步产物。

    total=False：所有字段可选——LangGraph 节点函数返回 partial dict 即 patch。
    daemon 选 dream entry 时通过 initial state 注入 triggered_at / dream_date /
    prev_state；其余字段在 5 步流转中由各节点填写。
    """

    # § A. trigger 上下文（daemon 选 dream entry 时注入）
    triggered_at: str  # ISO8601，做梦实际触发时刻
    dream_date: str  # 被总结的「昨日」日期 YYYY-MM-DD

    # § B. ta 的完整 state（跨 graph 共享，02 §3 8 层结构，dict 承载）
    prev_state: dict[str, object]  # = State.model_dump() 的形态
    next_state: dict[str, object]  # Dream 不改运行状态；Step 5 透传 prev_state

    # § C. Step 1 总结昨日 messages → markdown 摘要（11 §4.2）
    messages_summary: str

    # § C′. Step 2 prune 昨日已总结消息的删除条数（观测用，11 §3 Step 2）
    pruned_count: int

    # § D. Step 3 反思 → diff 提议（= ReflectionDiff.model_dump() 的形态）
    reflection: dict[str, object]

    # § E. Step 4 闸门 → verdict（= GateVerdict.model_dump() 的形态）
    gate_verdict: dict[str, object]

    # § F. Step 5 落地输出
    snapshot_path: str  # 本次做梦 snapshot 目录（11 §7.2）
    dream_journal_path: str  # 梦日记 markdown 路径（11 §7.3）
