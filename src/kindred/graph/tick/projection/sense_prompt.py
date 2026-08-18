"""Pure, deterministic Sense prompt projection.

This module consumes an already assembled :class:`SensePromptContext`; it does not
read the repository, life root, database, or network.  Keeping it below ``tick`` as
a dependency leaf lets ``sense_llm`` own orchestration and side effects without an
import cycle.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from kindred.graph.tick._possession_narrative import render_current_possession_facts
from kindred.llm.templates import render_prompt
from kindred.relationship.projector import RelationshipEvidenceContext
from kindred.state._types import is_safe_name
from kindred.state.tick import RecentContactContext, TickState

if TYPE_CHECKING:
    from kindred.activity import ActivitySkill

# Preserve the established observability channel after moving the pure renderer.
_LOG = logging.getLogger("kindred.graph.tick.sense_llm")

_SENSE_USER_TEMPLATE = "sense_user.md.j2"

_LIFE_TEXTURE_HEADER = (
    "近期生活纹理（过去事实，不是完成证明、配额或建议；重复、新意图和安静不行动都合法）："
)

_ENVIRONMENT_LEADING_FIELDS: Final[tuple[str, ...]] = (
    "city",
    "weather",
    "temperature",
)
_ENVIRONMENT_PROVIDER_FIELDS: Final[tuple[str, ...]] = (
    "humidity",
    "wind",
    "moon_phase",
    "sunrise",
    "sunset",
    "uv_index",
    "precip_mm",
)
_ENVIRONMENT_TRAILING_FIELDS: Final[tuple[str, ...]] = ("ambience",)
_SEND_ACTION_NAME: Final[str] = "send"
_NEEDS_LABELS: Final[dict[str, str]] = {
    "hunger": "饥饿程度",
    "energy": "行动余力",
    "fatigue": "疲惫程度",
    "comfort": "舒适满足",
    "social": "连接满足",
    "stimulation": "新鲜感满足",
    "aesthetic": "审美满足",
}
_AFFECT_LABELS: Final[dict[str, str]] = {
    "stress": "压力",
    "focus": "专注",
    "arousal": "身心唤起",
    "clarity": "清晰",
}
_SATISFACTION_DEFICITS: Final[dict[str, str]] = {
    "comfort": "舒适欲求较明显",
    "social": "连接欲求较明显",
    "stimulation": "新鲜欲求较明显",
    "aesthetic": "审美欲求较明显",
}


@dataclass(frozen=True)
class SenseActivityChoiceContext:
    """一个已加载 Activity 在 Sense 候选清单中的只读投影。"""

    name: str
    description: str | None


@dataclass(frozen=True)
class SenseCurrentActivityContext:
    """当前 Activity 对 Sense renderer 可见的只读投影。"""

    name: object
    step: object
    description: str
    engagement: object
    destination_names: tuple[str, ...]
    terminal_when: str | None = None
    step_before_send: bool = False


@dataclass(frozen=True)
class SensePromptContext:
    """Sense user prompt 的完整只读输入；renderer 不从仓库或 life root 读盘。"""

    state: TickState
    next_state: dict[str, Any]
    recent_ticks: list[dict[str, Any]]
    recent_activity_rows: list[dict[str, Any]]
    soul_excerpt: str
    identity: str
    user: str
    bundle_highlights: str
    activity_choices: tuple[SenseActivityChoiceContext, ...]
    show_current_activity: bool
    current_activity: SenseCurrentActivityContext | None
    chat_projection: _ChatProjection
    relationship_summary: str = ""
    relationship_evidence: RelationshipEvidenceContext | None = None


def _render_sense_prompt(
    context: SensePromptContext,
) -> str:
    """渲染 sense.llm 的 user prompt（jinja2 五段，docs/14 §3.3）。

    五段：人格（SOUL）+ 高光（bundle）+ 当下 state（mood/needs/affect/念头）
    + 最近对话 + 轨迹 + 可选活动 + 任务收尾。模板：``llm/prompts/sense_user.md.j2``。

    ``SensePromptContext`` 已在装配阶段完成 L2 与 Activity package 读取；此函数只做
    确定性投影和模板渲染，不访问文件系统。
    """
    state = context.state
    next_state = context.next_state
    recent_ticks = context.recent_ticks
    recent_activity_rows = context.recent_activity_rows
    chat_projection = context.chat_projection
    relationship_summary = context.relationship_summary
    relationship_evidence = context.relationship_evidence
    interior_raw = next_state.get("interior") if isinstance(next_state, dict) else None
    interior = interior_raw if isinstance(interior_raw, dict) else {}

    mood_raw = interior.get("mood")
    mood = mood_raw if isinstance(mood_raw, dict) else {}
    mood_val = mood.get("value")
    mood_desc = mood.get("description")
    body_line = _render_layer1_gauge(interior.get("body"))
    inner_pulse_line = _render_layer1_gauge(interior.get("inner_pulse"))

    needs_raw = interior.get("needs")
    needs = needs_raw if isinstance(needs_raw, dict) else {}
    affect_raw = interior.get("affect")
    affect = affect_raw if isinstance(affect_raw, dict) else {}
    needs_line = _gauges_oneline(
        needs,
        labels=_NEEDS_LABELS,
        deficit_hints=_SATISFACTION_DEFICITS,
    )
    affect_line = _gauges_oneline(affect, labels=_AFFECT_LABELS)

    raw_thoughts = interior.get("thoughts")
    thoughts = raw_thoughts if isinstance(raw_thoughts, list) else []
    thoughts_line = (
        "、".join(str(t.get("description", "")) for t in thoughts if isinstance(t, dict))
        or "（暂无）"
    )

    recent_contact_line = _render_recent_contact(state.get("recent_contact"))

    triggered_at = state.get("triggered_at")
    situation_block = _render_situation(next_state)
    possession_facts_section = render_current_possession_facts(next_state)
    last_note_line = _render_last_note(recent_ticks or [])
    traj_line = _render_recent_ticks(recent_ticks or [])
    life_texture_line = _render_recent_life_texture(
        recent_activity_rows or [], current_activity=next_state.get("activity")
    )
    stuck_warning_line = _render_stuck_warning(recent_ticks or [])
    activities_line = _render_activity_choices(context.activity_choices)
    current_activity_line = (
        _render_current_activity(context.current_activity)
        if context.show_current_activity and context.current_activity is not None
        else ""
    )
    settled_activity_exit_line = (
        "上一程已经结束并从当前生活内容中退场；现在要做候选清单里的另一件事时，"
        "使用 start_activity，否则 act=false。"
        if not context.show_current_activity
        else ""
    )
    current_partner_input = _render_chat_lines(chat_projection.partner_lines)
    mouth_context = _render_chat_lines(chat_projection.mouth_lines)
    unknown_chat_context = _render_chat_lines(chat_projection.unknown_lines, empty="")

    _LOG.debug(
        "prompt_sections role=sense.llm skill_catalog_chars=%d reason=activity_selection "
        "current_activity_chars=%d current_reason=continue_end_switch life_texture_chars=%d "
        "relationship_context_chars=%d relationship_reason=user_tone_only "
        "phase_context_chars=%d "
        "phase_scope=situation_partner_mouth phase_reason=sense_tick",
        len(activities_line),
        len(current_activity_line) + len(stuck_warning_line),
        len(life_texture_line),
        len(relationship_summary),
        len(situation_block)
        + len(possession_facts_section)
        + len(current_partner_input)
        + len(mouth_context),
    )
    return render_prompt(
        _SENSE_USER_TEMPLATE,
        soul_excerpt=context.soul_excerpt,
        identity=context.identity,
        user=context.user,
        bundle_highlights=context.bundle_highlights,
        triggered_at=triggered_at,
        situation_block=situation_block,
        possession_facts_section=possession_facts_section,
        body_line=body_line,
        mood_val=mood_val,
        mood_desc=mood_desc,
        inner_pulse_line=inner_pulse_line,
        needs_line=needs_line,
        affect_line=affect_line,
        thoughts_line=thoughts_line,
        last_note_line=last_note_line,
        current_partner_input=current_partner_input,
        relationship_summary=relationship_summary,
        relationship_evaluation_enabled=(
            relationship_evidence is not None and relationship_evidence.available
        ),
        relationship_previous_experience=(
            relationship_evidence.previous_experience if relationship_evidence is not None else ""
        ),
        mouth_context=mouth_context,
        unknown_chat_context=unknown_chat_context,
        presence_before_line=_render_presence_before(next_state),
        recent_contact_line=recent_contact_line,
        traj_line=traj_line,
        life_texture_line=life_texture_line,
        stuck_warning_line=stuck_warning_line,
        activities_line=activities_line,
        current_activity_line=current_activity_line,
        settled_activity_exit_line=settled_activity_exit_line,
    )


def _render_recent_life_texture(rows: list[dict[str, Any]], *, current_activity: object) -> str:
    """把 newest-first Activity 行折叠为 oldest-first 的近期非当前 run。"""
    active_identity: tuple[str, str] | None = None
    if isinstance(current_activity, dict) and current_activity.get("step") != "settle":
        name = current_activity.get("name")
        started_at = current_activity.get("started_at")
        if isinstance(name, str) and isinstance(started_at, str):
            active_identity = (name, started_at)

    seen: set[tuple[str, str]] = set()
    newest_names: list[str] = []
    for row in rows:
        activity = row.get("activity") if isinstance(row, dict) else None
        if not isinstance(activity, dict):
            continue
        name = activity.get("name")
        started_at = activity.get("started_at")
        if (
            not isinstance(name, str)
            or len(name) > 64
            or not is_safe_name(name)
            or not isinstance(started_at, str)
        ):
            continue
        identity = (name, started_at)
        if identity in seen:
            continue
        seen.add(identity)
        if identity == active_identity:
            continue
        newest_names.append(name)
        if len(newest_names) == 6:
            break

    return f"{_LIFE_TEXTURE_HEADER}\n{' → '.join(reversed(newest_names))}" if newest_names else ""


def _render_recent_contact(raw: object) -> str:
    """渲染近期联系事实；窗口外或 projection unavailable 时整段缺席。"""
    if raw is None:
        return ""
    context = RecentContactContext.model_validate(raw)
    age = context.recent_exchange_age_seconds
    actor = context.recent_actor
    if not context.available or age is None or actor is None:
        return ""
    age_text = "刚刚" if age < 60 else f"距今约 {age // 60} 分钟"
    partner_age = context.recent_partner_message_age_seconds
    if actor == "partner":
        detail = f"对方最近一次发来消息，{age_text}"
    else:
        detail = f"最近一次表达来自你，{age_text}"
        if partner_age is not None:
            partner_age_text = "刚刚" if partner_age < 60 else f"距今约 {partner_age // 60} 分钟"
            detail += f"；对方最近一次发来消息，{partner_age_text}"
    return f"【最近的联系】\n- {detail}。"


def _render_situation(next_state: dict[str, Any]) -> str:
    """处境块：**location 属性块 + environment 属性块分开**。

    「能呼吸的世界」③处境维进**心的感知**——让天气/位置参与 mood/念头/氛围，而不只
    显在嘴侧 bundle。各字段缺测则跳过（向后兼容旧 state / virtual 未填全）。
    """
    lines: list[str] = []
    location_lines = _render_attrs(
        next_state.get("location"),
        fields=("name", "address", "type", "arrived_at"),
    )
    if location_lines:
        lines.append("- location:")
        lines.extend(f"  - {line}" for line in location_lines)
    environment_lines = _render_environment_attrs(next_state.get("environment"))
    if environment_lines:
        lines.append("- environment:")
        lines.extend(f"  - {line}" for line in environment_lines)
    return "\n".join(lines) if lines else "- （处境未知）"


def _render_presence_before(next_state: dict[str, Any]) -> str:
    presence = next_state.get("presence")
    if isinstance(presence, dict) and isinstance(presence.get("user_present"), bool):
        value = "是" if presence["user_present"] else "否"
        return f"user_present={value}"
    return "（未知）"


def _render_environment_attrs(raw: Any) -> list[str]:
    """Render environment while avoiding legacy default rich-weather sentinels.

    ``Environment`` gives rich weather fields zero/empty defaults so old states keep
    loading. Those defaults are compatibility sentinels, not facts. Once
    ``weather_cached_for`` is present, the provider has refreshed the weather and
    zero values such as night ``uv_index=0`` or no-rain ``precip_mm=0`` become real
    facts worth showing to the prompt.
    """
    if not isinstance(raw, dict):
        return []

    lines = _render_attrs(raw, fields=_ENVIRONMENT_LEADING_FIELDS)
    has_provider_context = _has_environment_provider_context(raw)
    for field in _ENVIRONMENT_PROVIDER_FIELDS:
        value = raw.get(field)
        if not _should_render_environment_provider_value(
            value, has_provider_context=has_provider_context
        ):
            continue
        lines.append(f"{field}: {_format_situation_value(field, value)}")
    lines.extend(_render_attrs(raw, fields=_ENVIRONMENT_TRAILING_FIELDS))
    return lines


def _has_environment_provider_context(raw: dict[str, Any]) -> bool:
    cached_for = raw.get("weather_cached_for")
    return isinstance(cached_for, str) and cached_for.strip() != ""


def _should_render_environment_provider_value(value: object, *, has_provider_context: bool) -> bool:
    if value is None or value == "" or isinstance(value, bool):
        return False
    if has_provider_context:
        return True
    if isinstance(value, (int, float)) and value == 0:
        return False
    return True


def _render_attrs(raw: Any, *, fields: tuple[str, ...]) -> list[str]:
    """Render selected state-layer attributes as stable ``key: value`` lines."""
    if not isinstance(raw, dict):
        return []
    lines: list[str] = []
    for field in fields:
        value = raw.get(field)
        if value is None or value == "":
            continue
        lines.append(f"{field}: {_format_situation_value(field, value)}")
    return lines


def _format_situation_value(field: str, value: object) -> str:
    if field == "temperature" and isinstance(value, (int, float)):
        return f"{value:g}°C"
    if field == "humidity" and isinstance(value, int):
        return f"{value}%"
    if field == "precip_mm" and isinstance(value, (int, float)):
        return f"{value:g}mm"
    return str(value)


def _render_layer1_gauge(raw: object) -> str:
    if not isinstance(raw, dict):
        return "（无）"
    value = raw.get("value")
    description = raw.get("description")
    if isinstance(value, bool) or not isinstance(value, int):
        return "（无）"
    if isinstance(description, str) and description.strip():
        return f"{value}（{description.strip()}）"
    return str(value)


def _gauges_oneline(
    gauges: dict[str, Any],
    *,
    labels: dict[str, str],
    deficit_hints: dict[str, str] | None = None,
) -> str:
    """把 Needs / Affect 子层拼成带稳定方向语义的一行。

    needs / affect 子层是 **plain int** 字段（hunger=88 这种，见
    state.interior.Needs / Affect）——不是 GaugeWithDescription 嵌套。
    同时容忍 `{'value': int}` 嵌套形式（防御；万一将来某子层升为
    GaugeWithDescription）。

    满足度字段较低时追加对应欲求提示，避免模型把低 stimulation/social
    误读成低驱动力。阈值只控制自然语言投影，不参与 Activity 选择。
    """
    deficit_hints = deficit_hints or {}
    parts: list[str] = []
    for name, g in gauges.items():
        if isinstance(g, bool):
            continue  # bool 不是有效 gauge 值
        value: int | None = None
        if isinstance(g, int):
            value = g
        elif (
            isinstance(g, dict)
            and not isinstance(g.get("value"), bool)
            and isinstance(g.get("value"), int)
        ):
            value = g["value"]
        if value is None:
            continue
        label = labels.get(name, name)
        hint = deficit_hints.get(name) if value < 40 else None  # noqa: PLR2004
        parts.append(f"{label}={value}" + (f"（{hint}）" if hint else ""))
    return "、".join(parts) or "（无）"


def _act_mark(act_decision: Any, act_result: Any) -> str:
    """轨迹行的行动三态。

    ``act_decision.act`` 是 sense 的**意图**，不是执行结果——只看它会把
    「想动但没做成」误渲染成「行动」，让 LLM 误读上一轮真做成了。
    所以区分三态（依据 act_result.committed）：

    - act!=True                              → 「未动」
    - act=True 且 act_result.committed=True  → 「行动」
    - act=True 且 committed!=True / 缺 result → 「想动未落实（动作没有发生）」
    """
    acted_intent = isinstance(act_decision, dict) and act_decision.get("act") is True
    if not acted_intent:
        return "未动"
    committed = isinstance(act_result, dict) and act_result.get("committed") is True
    return "行动" if committed else "想动未落实（动作没有发生）"


def _render_last_note(recent_ticks: list[dict[str, Any]]) -> str:
    """把**上一拍**的 note 单独拎出来（连续意识的接力棒）。

    note 是上一 tick 对自己进展/心境的**第一人称内心独白**——是这一拍决策的
    直接前因，不是"历史档案的一行"。之前它被埋在 _render_recent_ticks 的轨迹
    列表里平铺，最新那条只是排第一行，身份被淹没。心读的时候抓不住"这是我上
    一拍刚想完的话"，连续感断裂。

    单拎出来摆在「当前状态」附近，让心明确接上上一拍的意识流。recent_ticks 按
    ts DESC、id DESC（最新在前），取第一条。空 → 降级「（无上一拍记录）」。
    """
    for t in recent_ticks:
        if not isinstance(t, dict):
            continue
        note = t.get("note")
        if note:
            return str(note)
        return "（上一拍没留下内心独白）"
    return "（无上一拍记录——这可能是你醒来的第一拍）"


def _quiet_tick_identity(tick: dict[str, Any]) -> tuple[str, str, str | None] | None:
    """返回可压缩静止 tick 的 Activity run 身份；边界不完整时保守保留。"""
    decision = tick.get("act_decision")
    significance = tick.get("significance")
    activity = tick.get("activity")
    if (
        not isinstance(decision, dict)
        or decision.get("act") is not False
        or tick.get("act_result") is not None
        or type(significance) is not int
        or not 1 <= significance < 7
        or not isinstance(activity, dict)
    ):
        return None

    name = activity.get("name")
    started_at = activity.get("started_at")
    step = activity.get("step")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(started_at, str)
        or not started_at
        or (step is not None and (not isinstance(step, str) or not step))
    ):
        return None
    return name, started_at, step


def _render_recent_ticks(recent_ticks: list[dict[str, Any]]) -> str:
    """把最近 N 条轨迹渲染成多行摘要（L2）。

    每条取 ts / note / significance + 行动三态（_act_mark）。最新一拍的 note 已由
    ``_render_last_note`` 在尾部单列，因此这里只省略那一条重复文本；时间、行动三态和
    significance 仍保留。连续三拍静止后，同一 activity/step 下更早的低重要度静止 note
    也会省略，减少旧叙事的重复曝光。空列表降级为「（无历史）」——cold_start 后第一次
    tick 或 db 未传时。

    recent_ticks 按 ts DESC、id DESC（最新在前，同 get_recent_ticks 契约）。
    """
    if not recent_ticks:
        return "（无历史）"

    quiet_run = 0
    quiet_activity: tuple[str, str, str | None] | None = None
    for tick in recent_ticks:
        if not isinstance(tick, dict):
            break
        activity_key = _quiet_tick_identity(tick)
        if activity_key is None or (quiet_activity is not None and activity_key != quiet_activity):
            break
        quiet_activity = activity_key
        quiet_run += 1

    compress_quiet_notes = quiet_run >= SETTLE_ACTIVITY_QUIET_TICKS
    lines: list[str] = []
    first_tick = True
    rendered_index = 0
    for t in recent_ticks:
        if not isinstance(t, dict):
            continue
        ts = t.get("ts", "?")
        note = t.get("note") or "（无记录）"
        sig = t.get("significance")
        act_mark = _act_mark(t.get("act_decision"), t.get("act_result"))
        sig_part = f" ⭐{sig}" if isinstance(sig, int) else ""
        omit_quiet_note = compress_quiet_notes and 0 < rendered_index < quiet_run
        note_part = "" if (first_tick and t.get("note")) or omit_quiet_note else f" {note}"
        lines.append(f"  [{ts}] {act_mark}{sig_part}{note_part}")
        first_tick = False
        rendered_index += 1
    return "\n".join(lines) or "（无历史）"


#: settle 后连续安静三拍，下一拍不再向 Sense 披露已经结束的 Activity。
SETTLE_ACTIVITY_QUIET_TICKS: Final[int] = 3

#: 连续卡同一 activity+step 多少拍起开始严厉提示（死循环阈值）。
#: 3 = 三拍一样就够可疑（~10min 同 step），再多就是真卡住了。
STUCK_REPEAT_THRESHOLD: Final[int] = 3


def _destination_plan_names(activity: Any) -> list[str]:
    """从 activity dict 取进行中目的地计划的地点名（无 context/计划返回空）。

    L5 en route：sense 侧只做展示与措辞分支，不校验 binding 归属（那是 act 节点
    写入时的事）——这里读到什么计划就展示什么。
    """
    if not isinstance(activity, dict):
        return []
    context = activity.get("context")
    if not isinstance(context, dict):
        return []
    destinations = context.get("destinations")
    if not isinstance(destinations, dict):
        return []
    names: list[str] = []
    for plan in destinations.values():
        if isinstance(plan, dict):
            name = plan.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return names


def _render_stuck_warning(recent_ticks: list[dict[str, Any]]) -> str:
    """检测「连续多拍卡在同一 activity+step」→ 生成严厉提示块（治死循环）。

    背景（skedush 2026-06-22 实证）：dine_out 在 hunger=0 后连续 17 拍
    advance_activity，note 几乎一字不变「再坐一会儿」——心陷进叙事惯性出
    不来。terminal_when 是软条件，LLM 可以不听；prev 轨迹只是平铺不点破。
    这里主动识别「原地打转」并在 prompt 里插入醒目警告，逆向迫心打破现状。

    识别：recent_ticks 按 ts DESC、id DESC（最新在前），从首条起数连续多少条
    activity.name+step 完全相同。达阈则返警告块，否则空串（模板段降级）。
    settle 是谢幕终态，其披露由 _should_render_current_activity 单独控制，这里不重复告警。

    L5 en route 分支：有进行中目的地计划的连拍**不是**经典死循环（路上要花时间，
    停在移动动作是正确的）——警告改成指向 arrival / abandon 出口的提醒，而不是
    吼「必须改变现状 / end」把在路上的活动催死。
    """
    if not recent_ticks:
        return ""

    def _name_step(t: object) -> tuple[str, str] | None:
        if not isinstance(t, dict):
            return None
        act = t.get("activity")
        if not isinstance(act, dict):
            return None
        name = act.get("name")
        step = act.get("step")
        if not name:
            return None
        return (str(name), str(step) if step else "")

    head = _name_step(recent_ticks[0])
    if head is None or head[1] == "settle":
        return ""
    streak = 0
    for t in recent_ticks:
        if _name_step(t) == head:
            streak += 1
        else:
            break
    if streak < STUCK_REPEAT_THRESHOLD:
        return ""

    name, step = head
    step_part = f"/{step}" if step else ""
    dest_names = _destination_plan_names(recent_ticks[0].get("activity"))
    if dest_names:
        dest_part = "」「".join(dest_names)
        return (
            f"🧭 在路上提醒：你已经连续 {streak} 个心跳停在 {name}{step_part}"
            f"（去「{dest_part}」的路上）。路上花时间不算卡住；但按现实节奏想想——\n"
            "  系统不会另发外部到达信号。仍愿意前往就选择 advance，让行动侧依据目的地计划、"
            "在途时长和已有处境判断本拍应 arrive 还是继续移动；不想去了也选择 advance，"
            "由行动侧 abandon。这里不要预先宣告已经到达，也别永远等待信号。"
        )
    return (
        f"⚠️ 原地打转警告：你已经连续 {streak} 个心跳停在 {name}{step_part}，"
        "独白几乎一字不变。这是「卡住了」的信号。别再重复同一句话、同一个动作。\n"
        "  现在**必须改变现状**：检查你的 needs/terminal_when——该收就 end_activity，"
        "想换事就 start 别的。不要再用「再待会儿」「再坐一会儿」这种叙事继续拖下去。"
    )


def _render_activity_choices(
    choices: tuple[SenseActivityChoiceContext, ...],
) -> str:
    """把已装配的 Activity 候选投影成「名 + description」清单。"""
    if not choices:
        return "（暂无可选活动）"
    lines = [
        f"  - {choice.name}：{choice.description}"
        if choice.description is not None
        else f"  - {choice.name}"
        for choice in choices
    ]
    return "\n".join(lines)


def _step_is_before_action(skill: ActivitySkill, step: Any, action_name: str) -> bool:
    if not isinstance(step, str) or not step.strip():
        return False
    steps = [use.action for use in skill.uses]
    if action_name not in steps or step not in steps:
        return False
    return steps.index(step) < steps.index(action_name)


def _should_render_current_activity(
    next_state: dict[str, Any],
    recent_ticks: list[dict[str, Any]],
) -> bool:
    """settle 最多陪伴三次连续安静拍，之后从 Sense prompt 中退场。"""
    activity = next_state.get("activity") if isinstance(next_state, dict) else None
    if not isinstance(activity, dict) or activity.get("step") != "settle":
        return True

    run_key = (activity.get("name"), activity.get("started_at"))
    quiet_ticks = 0
    for tick in recent_ticks:
        if not isinstance(tick, dict):
            break
        tick_activity = tick.get("activity")
        if not isinstance(tick_activity, dict):
            break
        if (
            tick_activity.get("name"),
            tick_activity.get("started_at"),
        ) != run_key or tick_activity.get("step") != "settle":
            break

        decision = tick.get("act_decision")
        if isinstance(decision, dict) and decision.get("act") is False:
            quiet_ticks += 1
            if quiet_ticks >= SETTLE_ACTIVITY_QUIET_TICKS:
                return False
            continue

        result = tick.get("act_result")
        return (
            isinstance(decision, dict)
            and decision.get("act") is True
            and decision.get("kind") == "end_activity"
            and isinstance(result, dict)
            and result.get("committed") is True
        )

    return True


def _render_current_activity(activity: SenseCurrentActivityContext) -> str:
    """渲染「上一拍你处在的状态」（上一拍 activity + step + 终止条件）。

    ⚠ 措辞纠正（skedush 2026-06-21 指出）：sense 跨在 act 之前跑，next_state 是
    ``dict(prev_state)`` 浅拷——activity 字段原样继承自**上一 tick**，act 还没跑、
    还没改它。所以这里渲的 activity.name/step **本质是「上一拍你停在的状态」，
    不是「你此刻正在进行的动作」**。旧措辞"你正在做"制造了虚假进行时：心读到
    "我正在 walk"就当成"一个动作正在推进=平稳"顺着判 act:false 干耗。改成「上一拍你
    停在」——把进行时改成起点态，心才把它当成一个**待裁决的局面**（继续？end？换？）。

    ⑥ prompt 引导：之前 prompt 只给 needs，不告诉心「上一拍你停在什么活动、走到哪一
    step」——心看不到自己正卡在哪，就容易退化成 act:false 干耗（死循环软侧根因）。
    把上一拍的 activity.name/step/desc 明确渲染出来，心才能判断「该继续推进 / 该 end
    收尾 / 该换个活动」。

    **terminal_when（终止/中断条件）也一起渲染**：「该不该 end_activity」是 sense 决策，
    但之前 terminal_when 只在 act prompt 里、sense 看不到——心只知道「我在做啥」却不知
    道「啥时该停」，只能用表层的「活动在推进=平稳」填 act:false 干耗。把出口条件摆在
    sense 面前，心才有依据把「我在走」重新判成「不合时宜了、该 end」。

    无 activity（理论上不该发生，settle 后 activity 始终不空）→ 降级提示。
    """
    if not activity.name:
        return "（上一拍你没有进行中的活动——可以 start 一个新活动）"
    name = activity.name
    step = activity.step
    desc = activity.description
    engagement = activity.engagement
    dest_names = activity.destination_names
    parts = [f"活动：{name}"]
    if step:
        # settle 是谢幕终态——特别点明：已经 end 过了，不能再 end_activity（实证
        # dine_out 在 settle 上被弱模型连判 4 次 end_activity 空转）。settle 上的合法
        # 出路只有两条：start 新活动 / act=false 静一拍。再 end 是对已谢幕的活动重复
        # 收尾，无意义。
        if step == "settle":
            parts.append(
                "step：settle（这一程**已经谢幕收尾**了——回味过就该往前走。"
                "⚠️ 不要再 end_activity（已经 end 过了，对已谢幕的活动再 end 是空转）；"
                "现在只有两条路：start 一件新活动，或 act=false 静一拍。别赖在 settle 上）"
            )
        else:
            parts.append(f"step：{step}")
    # L5 en route：进行中目的地计划渲进「上一拍状态」——sense 判 advance/end 才有
    # 依据（在去某地的路上 end 掉 = 计划作废；继续 advance 才可能真实到达）。
    if dest_names and step != "settle":
        parts.append(f"目的地计划：去「{'」「'.join(dest_names)}」（已选中、还没到）")
    if desc:
        parts.append(f"（{desc}）")
    if (
        isinstance(engagement, (int, float))
        and not isinstance(engagement, bool)
        and 0 <= engagement <= 1
    ):
        parts.append(f"engagement：{engagement:g}（上一拍投入程度，仅作软事实）")
    line = "｜".join(parts)
    # 渲 terminal_when（end_activity 出口条件）——sense 据此判「该完成/中断/继续」。
    # settle 是框架隐式终态（不属任何 activity 的 uses），无 terminal_when，不渲。
    if name and isinstance(name, str) and step != "settle":
        if activity.terminal_when:
            # terminal_when 是多行 YAML（`|`）——逐行补缩进，别让内部换行顶格
            # 脱离「上一拍状态」块（原来只第一行有缩进，后续行顶格会被弱模型
            # 误读成 prompt 顶层段落，与当前活动解绑）。
            tw_indented = "\n".join(
                "    " + line for line in activity.terminal_when.strip().splitlines()
            )
            line += f"\n  何时该收（terminal_when，达成就 end_activity）：\n{tw_indented}"
        if activity.step_before_send:
            line += (
                "\n  外部发送事实：当前还在 send 之前，系统没有发送任何消息。"
                "如果你选择 end_activity，这是把想说的话收住/取消，不是已经发出。"
            )
    return line


#: 对方新表达与嘴侧背景各自限量，互不挤占。
CHAT_WINDOW_SIZE: Final[int] = 6
MOUTH_CONTEXT_SIZE: Final[int] = 3
#: 单条消息 content 超过该长度才压缩成「首句 … 末句」（skedush 拍：200）。
CHAT_MSG_TRUNC: Final[int] = 200
#: 压缩后首/末句各自上限（防单句本身超长）。
CHAT_SENT_LEN: Final[int] = 100
#: 句末标点（中英）——移植自 first-party predecessor segments.py。
_SENTENCE_END_RE = re.compile(r"([。！？!?])")


def _split_sentences(text: str) -> list[str]:
    """按句末标点切句（标点保留在上一句末）。移植自 first-party predecessor。"""
    if not text:
        return []
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        buf = ""
        for token in _SENTENCE_END_RE.split(line):
            if not token:
                continue
            buf += token
            if _SENTENCE_END_RE.fullmatch(token):
                if buf.strip():
                    out.append(buf.strip())
                buf = ""
        if buf.strip():
            out.append(buf.strip())
    return out


def _compress_message(text: str) -> str:
    """单条对话压缩：超 CHAT_MSG_TRUNC → 「首句 … 末句」（中间省略）。

    移植自 first-party predecessor segments.py 的首末句思路（但作用在单条消息而非「段」：
    kindred 的 ChatTurn 是单条消息，不带 tool 调用）。skedush 2026-06-22 拍：
    长回复的上下文在首句、结论在末句，都保住，中间叙事省掉——避免长消息
    （我的大段回复 / Compaction 整块）卡爆 prompt 、淹没状态信号。

    短消息（≤ CHAT_MSG_TRUNC）原样返回。首/末句各自过长再截 CHAT_SENT_LEN。
    只有一句（末句==首句）只显一份。
    """
    text = text.strip()
    if len(text) <= CHAT_MSG_TRUNC:
        return text
    sents = _split_sentences(text)
    if len(sents) >= 2:
        # 多句：首句 … 末句（各自超长再截）
        head = sents[0]
        if len(head) > CHAT_SENT_LEN:
            head = head[:CHAT_SENT_LEN] + "…"
        tail = sents[-1]
        if len(tail) > CHAT_SENT_LEN:
            tail = "…" + tail[-CHAT_SENT_LEN:]
        return f"{head} … {tail}"
    # 0 或 1 句但整体超长（无句末标点 / 单巨句，如 Compaction 整块）：
    # 首尾硬切 head … tail，两端都保住。
    return f"{text[:CHAT_SENT_LEN]} … {text[-CHAT_SENT_LEN:]}"


# chat_window role 分边：partner 侧 = ta 对你说的（标 [对方]、完整保留、可回应）；
# mouth 侧 = 你 push / 嘴转达的（标 [你/嘴]、可压缩、只是背景）。主路径 sense_io
# 只产 user/mouth；这里同时兼容业务层 partner/my_voice/my_heart。
# 未知 role **绝不默认归 mouth 侧**——那会把真·对方消息当背景 → 反向漏听。
_CHAT_PARTNER_ROLES: frozenset[str] = frozenset({"user", "partner"})
_CHAT_MOUTH_ROLES: frozenset[str] = frozenset({"mouth", "my_voice", "my_heart", "assistant"})


@dataclass(frozen=True)
class _ChatProjection:
    partner_lines: tuple[str, ...]
    mouth_lines: tuple[str, ...]
    unknown_lines: tuple[str, ...]
    partner_input_max_age_s: int | None


def _project_chat_window(raw_chat: object, *, triggered_at: str | None) -> _ChatProjection:
    """按事实来源投影本拍聊天；partner 与 mouth 各自限量且互不重复。"""
    partner: list[tuple[str, str | None]] = []
    mouth: list[str] = []
    unknown: list[str] = []
    messages = raw_chat if isinstance(raw_chat, list) else []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or message.get("from") or "?")
        content = message.get("text") or message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role in _CHAT_PARTNER_ROLES:
            if message.get("is_new") is not False:
                ts = message.get("ts")
                partner.append((f"  [对方] {content}", ts if isinstance(ts, str) else None))
        elif role in _CHAT_MOUTH_ROLES:
            mouth.append(f"  [你/嘴] {_compress_message(content)}")
        else:
            unknown.append(f"  [未知:{role}] {content}")

    selected_partner = partner[-CHAT_WINDOW_SIZE:]
    ages = [
        age
        for _, ts in selected_partner
        if (age := _message_age_seconds(ts, triggered_at)) is not None
    ]
    return _ChatProjection(
        partner_lines=tuple(line for line, _ in selected_partner),
        mouth_lines=tuple(mouth[-MOUTH_CONTEXT_SIZE:]),
        unknown_lines=tuple(unknown[-MOUTH_CONTEXT_SIZE:]),
        partner_input_max_age_s=max(ages, default=None),
    )


def _message_age_seconds(message_ts: str | None, triggered_at: str | None) -> int | None:
    if message_ts is None or triggered_at is None:
        return None
    try:
        age = (
            datetime.fromisoformat(triggered_at).timestamp()
            - datetime.fromisoformat(
                message_ts,
            ).timestamp()
        )
    except (ValueError, OSError):
        return None
    return max(0, int(age))


def _render_chat_lines(lines: tuple[str, ...], *, empty: str = "（暂无）") -> str:
    return "\n".join(lines) or empty


def _has_current_partner_input(projection: _ChatProjection) -> bool:
    """Presence gate 与 Prompt 共用同一份 partner 投影。

    显式 ``is_new=False`` 是背景；缺字段时保留历史兼容语义。
    """
    return bool(projection.partner_lines)
