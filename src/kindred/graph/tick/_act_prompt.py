"""T2 act 的只读 prompt context 与纯渲染。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kindred.activity import ActivitySkillError, load_activity_skill, resolve_step_effects
from kindred.activity.action import load_atomic_action
from kindred.graph.tick._state_transition import SETTLE_STEP
from kindred.llm.templates import render_prompt
from kindred.state.interior import Needs

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocationBindingPromptContext:
    """activity 地点 binding 的只读 prompt 投影。"""

    binding_id: str
    query: str
    categories: tuple[str, ...]
    radius_min_km: float | None
    radius_max_km: float | None
    limit: int | None


@dataclass(frozen=True)
class PlaceBindingPromptContext:
    """action place slot 到 activity binding 的只读投影。"""

    slot_id: str
    binding_id: str
    needed_for: str | None
    description: str | None
    query: str | None


@dataclass(frozen=True)
class ActionPromptContext:
    """一个 activity use 及其 action 客观契约。"""

    name: str
    intent: str
    effects: tuple[tuple[str, str, str], ...]
    description: str | None
    applies_when: str | None
    place_bindings: tuple[PlaceBindingPromptContext, ...]


@dataclass(frozen=True)
class ActivityPromptContext:
    """activity package 的完整只读 prompt 投影。"""

    requested_target: str | None
    name: str | None = None
    description: str | None = None
    duration_hint: str | None = None
    produces: str | None = None
    location_bindings: tuple[LocationBindingPromptContext, ...] = ()
    actions: tuple[ActionPromptContext, ...] = ()
    terminal_when: str | None = None
    requires: tuple[str, ...] = ()
    method: str | None = None


@dataclass(frozen=True)
class OutboundFactContext:
    """外部发送事实的只读 prompt 投影。"""

    enabled: bool
    delivered_before: bool
    step_before_send: bool
    kind: str | None


@dataclass(frozen=True)
class PresencePromptContext:
    """物理在场与当前活动参与者的只读 prompt 投影。"""

    user_present: bool
    activity_name: str | None
    with_whom: tuple[str, ...] | None


@dataclass(frozen=True)
class ActPromptContext:
    """act user prompt 的全部结构化输入。"""

    triggered_at: Any
    kind: Any
    target: Any
    current_step: Any
    activity: ActivityPromptContext
    presence: PresencePromptContext
    location_section: str
    outbound: OutboundFactContext
    en_route_destinations: tuple[str, ...]
    committed_artifacts: tuple[tuple[str, str], ...] = ()
    possession_facts_section: str = ""
    expression_context_section: str = ""
    relationship_summary: str = ""
    sense_note: str = ""
    current_engagement: float | None = None


def load_activity_prompt_context(
    target: Any,
    *,
    activities_dir: Path,
    actions_dir: Path,
) -> ActivityPromptContext:
    """读取一次 activity/action package，生成与文件系统解耦的 prompt 数据。"""

    if not isinstance(target, str) or not target:
        return ActivityPromptContext(requested_target=None)
    try:
        skill = load_activity_skill(target, activities_dir=activities_dir, actions_dir=actions_dir)
    except ActivitySkillError as exc:
        logger.warning("T2.act.llm: activity SKILL 加载失败，降级：%s", exc)
        return ActivityPromptContext(requested_target=target)

    bindings = tuple(
        LocationBindingPromptContext(
            binding_id=binding_id,
            query=binding.query,
            categories=tuple(binding.categories),
            radius_min_km=binding.radius_min_km,
            radius_max_km=binding.radius_max_km,
            limit=binding.limit,
        )
        for binding_id, binding in skill.location_bindings.items()
    )
    actions: list[ActionPromptContext] = []
    for use in skill.uses:
        effects = tuple(
            (name, effect.direction, effect.magnitude)
            for name, effect in resolve_step_effects(
                skill,
                use.action,
                actions_dir=actions_dir,
            ).items()
        )
        try:
            atomic = load_atomic_action(use.action, actions_dir=actions_dir)
        except ActivitySkillError:
            atomic = None
        place_bindings: list[PlaceBindingPromptContext] = []
        for slot_id, binding_id in use.bind_places.items():
            slot = atomic.place_slots.get(slot_id) if atomic is not None else None
            binding = skill.location_bindings.get(binding_id)
            place_bindings.append(
                PlaceBindingPromptContext(
                    slot_id=slot_id,
                    binding_id=binding_id,
                    needed_for=slot.needed_for if slot is not None else None,
                    description=slot.description if slot is not None else None,
                    query=binding.query if binding is not None else None,
                )
            )
        actions.append(
            ActionPromptContext(
                name=use.action,
                intent=use.intent,
                effects=effects,
                description=atomic.desc if atomic is not None else None,
                applies_when=atomic.applies_when if atomic is not None else None,
                place_bindings=tuple(place_bindings),
            )
        )
    return ActivityPromptContext(
        requested_target=target,
        name=skill.name,
        description=skill.description,
        duration_hint=skill.duration_hint,
        produces=skill.produces,
        location_bindings=bindings,
        actions=tuple(actions),
        terminal_when=skill.terminal_when,
        requires=tuple(skill.requires),
        method=skill.method,
    )


def render_act_prompt(context: ActPromptContext) -> str:
    """只消费结构化 context 渲染 prompt，不读取 activity/action package。"""

    skill_section = render_activity_prompt_context(
        context.activity,
        end_activity=context.kind == "end_activity",
        current_step=context.current_step,
    )
    action_names = {action.name for action in context.activity.actions}
    possession_facts_section = ""
    if action_names & {
        "change_outfit",
        "pack_bag",
        "makeup",
        "remove_makeup",
    }:
        possession_facts_section = context.possession_facts_section
    presence_section = _render_presence_fact_section(context.presence)
    outbound_section = _render_outbound_fact_section(context.outbound)
    artifact_section = _render_artifact_section(context.committed_artifacts)
    decision_handoff_section = _render_decision_handoff(context)
    engagement_section = _render_engagement_section(context)
    logger.debug(
        "prompt_sections role=act.llm activated_skill_chars=%d reason=activity_selected "
        "relationship_context_chars=%d relationship_reason=user_tone_only "
        "phase_context_chars=%d "
        "phase_scope=location_presence_outbound_decision_handoff phase_reason=%s",
        len(skill_section),
        len(context.relationship_summary),
        len(context.location_section)
        + len(presence_section)
        + len(outbound_section)
        + len(artifact_section)
        + len(possession_facts_section)
        + len(context.expression_context_section)
        + len(decision_handoff_section)
        + len(engagement_section),
        context.kind,
    )
    return render_prompt(
        "act_user.md.j2",
        triggered_at=context.triggered_at,
        kind=context.kind,
        target=context.target,
        skill_section=skill_section,
        presence_section=presence_section,
        current_step=context.current_step,
        location_section=context.location_section,
        possession_facts_section=possession_facts_section,
        expression_context_section=context.expression_context_section,
        relationship_summary=context.relationship_summary,
        outbound_fact_section=outbound_section,
        artifact_section=artifact_section,
        decision_handoff_section=decision_handoff_section,
        engagement_section=engagement_section,
        en_route_destinations=context.en_route_destinations,
    )


def _render_artifact_section(artifacts: tuple[tuple[str, str], ...]) -> str:
    if not artifacts:
        return ""
    lines = [
        "## 当前活动已提交的作品",
        "只可在后续动作中显式使用这里列出的 artifact_ref；正文仍由 Host 保管。",
        "调用工具时逐字复制引号内的完整值；`artifact:` 前缀是 artifact_ref 的一部分。",
    ]
    lines.extend(
        f'- artifact_ref="{artifact_ref}"（profile={profile}）'
        for artifact_ref, profile in artifacts
    )
    return "\n".join(lines)


def _render_decision_handoff(context: ActPromptContext) -> str:
    """把 Sense 已形成的本拍心声紧凑交给 Act，不重复投影原始处境。"""
    lines: list[str] = []
    if context.sense_note:
        lines.append(f"- 本拍心声：{context.sense_note}")
    return "## 这一拍的行动依据\n" + "\n".join(lines) if lines else ""


def _render_engagement_section(context: ActPromptContext) -> str:
    value = context.current_engagement
    if context.kind not in {"advance_activity", "end_activity"} or value is None:
        return ""
    return f"## 当前活动投入感\n- engagement={value:g}（上一拍软事实；有体验差异时用合法 delta）"


def render_activity_prompt_context(
    context: ActivityPromptContext,
    *,
    end_activity: bool = False,
    current_step: Any = None,
) -> str:
    """渲染已投影的 activity 数据；收尾拍省略已无关的机器执行契约。"""

    if context.requested_target is None:
        return "（无 target activity）"
    if context.name is None:
        return f"（activity SKILL 未找到：{context.requested_target}）"

    lines = [f"## activity: {context.name}（高级意图 / 状态机）"]
    if not end_activity:
        lines.extend(
            (
                f"描述：{context.description}",
                f"时长参考：{context.duration_hint}　产出倾向：{context.produces}",
            )
        )
    if context.location_bindings and not end_activity:
        lines.append(
            "地点需求（activity 级查询语义；pre-act 后续可据此查候选，"
            "这里不是已选择 / 已到达的地点）："
        )
        for binding in context.location_bindings:
            categories = "、".join(binding.categories) if binding.categories else "（不限定）"
            radius = _format_radius(binding.radius_min_km, binding.radius_max_km)
            limit = f"，limit={binding.limit}" if binding.limit is not None else ""
            lines.append(
                f"  - {binding.binding_id}: query={binding.query}；categories={categories}；"
                f"radius={radius}{limit}"
            )
    if not end_activity:
        lines.append(
            "原子动作（step 只能从这几个选；intent 是该动作在本 activity 的味道；"
            "effect 的 direction/magnitude 是通常体验软先验，不是本次硬结算）："
        )
    for action in context.actions if not end_activity else ():
        effects = (
            "、".join(
                f"{_effect_path(name)} {direction} {magnitude}"
                for name, direction, magnitude in action.effects
            )
            or "（无）"
        )
        lines.append(f"  - {action.name}：{action.intent}｜{effects}")
        if action.description:
            lines.append(f"      是什么：{action.description}")
        if action.applies_when:
            lines.append(f"      何时能做（applies_when）：{action.applies_when}")
        if action.place_bindings:
            lines.append("      地点槽位绑定：")
            for place_binding in action.place_bindings:
                details: list[str] = []
                if place_binding.needed_for is not None:
                    details.append(f"needed_for={place_binding.needed_for}")
                if place_binding.description is not None:
                    details.append(f"description={place_binding.description}")
                if place_binding.query is not None:
                    details.append(f"query={place_binding.query}")
                suffix = f"（{'；'.join(details)}）" if details else ""
                lines.append(
                    f"        - {action.name}.{place_binding.slot_id} -> "
                    f"{place_binding.binding_id}{suffix}"
                )
    if not end_activity:
        if context.requested_target and current_step is None:
            lines.append(
                "这是新 Activity run：实际选中的 Action 是 entry；看见 Outcome 后可按本拍具体经历"
                "提交合法 signed delta，省略即不改变数值。"
            )
        elif isinstance(current_step, str):
            lines.append(
                f"上一拍 step={current_step}：继续同一 step 不是新的 entry，不会自动重放通常体验"
                "先验；若本拍出现新的具体体验，可按 1..10 小幅提交任意合法轴，否则省略。"
                "切换到不同 Action 时，新 Action 是 entry，看见 Outcome 后可按 1..80 表达本拍经历。"
            )
    if end_activity and isinstance(current_step, str):
        lines.append(
            f"上一 Action={current_step}：end 不会自动重放它的通常体验先验；若收尾时新形成了"
            "具体回味，可按 1..10 小幅提交任意合法轴，否则省略。"
        )
    lines.append(f"终止条件（心仲裁何时 end_activity）：{context.terminal_when}")
    lines.append(
        "收尾（end_activity）：满足终止条件就 end_activity。end 不是清空，是转进一拍"
        f"「{SETTLE_STEP}（谢幕回望）」：回顾这一程的感受、结这程总账（只做小幅 "
        "Needs/Affect signed delta 回味，不写 mood）、该记的内容落盘。settle 是谢幕的一拍，"
        "不是歇脚的沙发——回望过下个 tick 就去开始下一件事，世界很大，别赖在 settle 里。"
    )
    if context.requires and not end_activity:
        lines.append(f"需要：{'、'.join(context.requires)}")
    lines.extend(("", context.method or ""))
    return "\n".join(lines)


def _effect_path(name: str) -> str:
    return f"{'needs' if name in Needs.model_fields else 'affect'}.{name}"


def _render_outbound_fact_section(context: OutboundFactContext) -> str:
    if not context.enabled:
        return ""
    if context.delivered_before:
        return "\n".join(
            [
                "## 外部发送事实",
                "- 最近轨迹里已有 send_to_user.ok=true：这次主动联系已经真实发出过。",
                "- 若本 tick 是 end_activity，可以把它写成“话已发出后的收尾”。",
            ]
        )

    lines = [
        "## 外部发送事实",
        "- 到本 tick 开始前，没有看到 send_to_user.ok=true；系统事实是：还没有消息成功发出。",
    ]
    if context.step_before_send:
        lines.append(
            "- 当前还在 send 之前。你可以选择 end_activity 把这件事收住/取消，"
            "但不能写成“话已发出/已经传达”。"
        )
    elif context.kind == "start_activity":
        lines.append("- 这是刚 start 的新一轮意图；不要继承旧活动实例里的发送结果。")
    elif context.kind == "end_activity":
        lines.append(
            "- 如果你要在此刻收尾，请只按未确认发送来写收尾；不要把没有 trace 的外部效果"
            "当成已经发生。"
        )
    return "\n".join(lines)


def _render_presence_fact_section(context: PresencePromptContext) -> str:
    lines = [
        "## 当前物理共处事实",
        f"- user 此刻物理在场：{'是' if context.user_present else '否'}。",
    ]
    if context.with_whom is not None:
        activity_name = context.activity_name or "当前活动"
        participants = "、".join(context.with_whom) or "（没有记录共同参与者）"
        lines.append(f"- 当前活动「{activity_name}」的共同参与者：{participants}。")
    return "\n".join(lines)


def _format_radius(radius_min_km: float | None, radius_max_km: float | None) -> str:
    if radius_min_km is None and radius_max_km is None:
        return "（默认）"
    if radius_min_km is None:
        return f"<= {radius_max_km:g}km"
    if radius_max_km is None:
        return f">= {radius_min_km:g}km"
    return f"{radius_min_km:g}-{radius_max_km:g}km"


__all__ = [
    "ActPromptContext",
    "ActivityPromptContext",
    "OutboundFactContext",
    "PresencePromptContext",
    "load_activity_prompt_context",
    "render_activity_prompt_context",
    "render_act_prompt",
]
