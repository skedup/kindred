"""``T2.act.llm`` —— 落实意图、改事件级 state 节点（LLM call）。

参考文档：14-heart-graph.md §3.4 + §2.3.2 (T2 单节点 errata)、docs/15

职责
====

调 LLM 出行动结果 + 把 ``final_state_diff`` 应用到 ``next_state``，再自己
构造 ``act_result`` 元信息。与 sense_llm 对偶：同 factory pattern、同
``deepcopy`` 双保险、同自定义 ``ActLlmContractError``、同 mock 兼容路径。

节点结构（三层，与 sense_llm / sense_io 一致）：

- 顶层 ``_t2_act_llm_impl(client, state, host_runtime=...)`` 是真实现
- ``_make_t2_act_llm(client, host_runtime=...)`` 是 thin wrapper closure，
  绑定 client 与能力运行时
- ``t2_act_llm(state)`` 是顶层 mock 透传，未注 deps 时使用

prompt 会 read target activity package（docs/15）渲染进去：``manifest.yaml``
提供 activity 描述 / uses / bindings 等机器契约，``SKILL.md`` 提供 method
技能叙事。package 缺失时降级不 raise（运行时数据问题，非契约错）。

LLM 最终 JSON 契约（14 §3.4）
============================

LLM 顶层返 dict 形如::

    {
        "final_state_diff": dict,      # state 变更包（按子层：activity /
                                       # current_state / needs / affect / ...）
        "committed": bool,             # 原子性是否走完
        "failure_reason": str | None,  # committed=False 时填原因
    }

地点事件、compose 与 send 均由 tool_loop 工具调用表达。节点把 ``final_state_diff``
应用进 next_state（确定性 Action effect + 情境方向 + 落 step），再由
``LocationKernelSession`` 从 staged
地点工具事件派生 ``activity.context.destinations`` 与 ``state.location``，再按
committed location arrival 与显式同行决定派生 ``presence.user_present``，对合成结果执行完整
``State.model_validate``，最后才提交可撤 artifact，
再**自己构造** ``act_result`` 元信息（贴 02 §563）::

    {kind, target, current_state, tool_trace, state_diff_keys,
     committed, failure_reason,
     destination_choice, destination_abandoned, location_arrival}

关键：act_result 不是 LLM 产出的 diff 包。state 变更走顶层 final_state_diff；
act_result 是节点从实际改的 key 算出的 trace 元信息。

**failure 不 raise 哲学**（与 sense_llm 不同！）：
``committed=False + failure_reason`` 是合法输出 —— ta 想做但没做的歧义对 ta
自己也是有意义的 tick（下次 sense 会读到，做主观判断）。StateTransition、Location
apply 或完整 State 校验拒绝 proposal 时同样收口成可持久化的 ``committed=False``；
仅上游路由错误、LLM 顶层结构错等无法形成合法 act_result 的错误继续 raise。

工具协议：act 使用有界工具环，``find_places`` 是 read_only，地点事件工具是
staged_state_event，``write_compose`` 是 artifact_write，``send_to_user`` 是
external_side_effect；``tool_trace`` 由真实工具调用生成，不再接受最终 JSON 声明式
代执行。
``end_activity`` **不清空** activity，而是由节点强制落谢幕终态 step
``settle``（§0.4 / SETTLE_STEP）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from kindred.activity import (
    ActivitySkillError,
    list_registered_activities,
    load_activity_skill,
)
from kindred.activity.action import (
    list_registered_actions,
    load_atomic_action,
)
from kindred.activity.tool_binding import (
    declared_capability_names_for_activity,
)
from kindred.capability_host import CapabilityRegistry, HostExecutionContext, HostRuntime
from kindred.capability_host.artifacts import ArtifactStoreError
from kindred.capability_host.facts import anchor_activity_for_act, current_activity_artifacts
from kindred.capability_host.internal import HostTickContext
from kindred.graph._shared._common import NodeReturn
from kindred.graph._shared._errors import NodeContractError
from kindred.graph.tick._act_contract import ActLlmContractError, safe_validation_error_paths
from kindred.graph.tick._act_location import (
    LOCATION_KERNEL_TOOL_DEFS,
    LocationApplyResult,
    LocationKernelSession,
    apply_location_commit,
)
from kindred.graph.tick._act_prompt import (
    ActPromptContext,
    OutboundFactContext,
    PresencePromptContext,
    load_activity_prompt_context,
    render_act_prompt,
)
from kindred.graph.tick._capability_effects import CapabilityEffectSession
from kindred.graph.tick._effective_action import resolve_effective_action
from kindred.graph.tick._inventory_transition import resolve_inventory_diff
from kindred.graph.tick._possession_narrative import render_current_possession_facts
from kindred.graph.tick._state_transition import (
    SETTLE_STEP,
    StateTransitionApplier,
    project_arrival_presence,
)
from kindred.life_assets import ACTIONS_DIR, ACTIVITIES_DIR
from kindred.llm.client import ToolCapableLlmClient, ToolLoopError
from kindred.llm.schemas import ActResponse
from kindred.llm.tools import ToolEvent
from kindred.location.candidates import (
    LOCATION_CANDIDATE_RESOLVER_KEY,
    LocationCandidateResolver,
)
from kindred.observability import redact_observability_secrets
from kindred.state.state import State
from kindred.state.tick import TickState
from kindred_capability_sdk import ToolCall, ToolDef, ToolResult

if TYPE_CHECKING:
    from kindred.character_card import HomeProfile
    from kindred.db.facade import KindredDB
    from kindred.observability import PromptDumper

logger = logging.getLogger(__name__)

_ACT_TOOL_LOOP_MAX_ROUNDS = 6
_LOCATION_CAPABILITY = "location"


# ─────────────────────────────────────────────────────────────────────
# Factory 公开入口
# ─────────────────────────────────────────────────────────────────────


def make_act_llm_node(
    client: ToolCapableLlmClient,
    *,
    host_runtime: HostRuntime,
    activities_dir: Path = ACTIVITIES_DIR,
    actions_dir: Path = ACTIONS_DIR,
    prompt_dumper: PromptDumper | None = None,
    db: KindredDB | None = None,
    home: HomeProfile | None = None,
) -> Callable[[TickState], NodeReturn]:
    """构造绑了 ``client`` 的 T2.act.llm closure，给 ``build.py`` add_node。

    与 ``make_sense_llm_node`` 同设计——单参 client。M4.5 后 act 只支持
    ``ToolCapableLlmClient``，不再保留单发 JSON 兼容路径；sense/dream 仍可使用基础
    ``LlmClient.complete``。

    ``host_runtime``：runtime composition root 构造的能力平面装配对象，统一
    持有 registry、配置与 provider/io handles。act 节点只按 tick 补充 state、activity、
    artifact readers 等短生命周期上下文，不认识具体 provider dependency 名。

    ``prompt_dumper``（earlier review step 2）：注入后，与 sense.llm 对称——成功 tick 落
    prompt+parsed response、失败时把模型**原始未解析文本**（client 异常的 ``raw_text``）
    落进 gated + 0600 的 dump 工件。为 ``None`` 时不 dump（mock / 拓扑测）。

    ``db``（L3 PlaceStore）：注入后，pre-act 给地点候选补「本地经验」行
    （place_visits 按 place_key exact lookup，与 sense_llm 的 db 注入同款可选依赖）。
    为 ``None`` 时不渲染本地经验（mock / 未接库环境照跑）。**只读**——act 节点
    不写 DB（第四趴 §3 写入纪律）。

    ``home``：runtime 层解析后的设置级居住锚点；pre-act 只把它作为已知固定地点
    参与显式 ``home`` category 的 binding，不读取 character-card 文件、不写 state。

    ``host_runtime`` 为真实 act 节点装配必需项，避免 prompt 已引导模型调用
    capability tool、但 ToolDef 未注册的半接线状态静默运行。
    """
    return _make_t2_act_llm(
        client,
        activities_dir=activities_dir,
        actions_dir=actions_dir,
        host_runtime=host_runtime,
        prompt_dumper=prompt_dumper,
        db=db,
        home=home,
    )


# ─────────────────────────────────────────────────────────────────────
# 真实现（顶层 _impl）
# ─────────────────────────────────────────────────────────────────────


def _t2_act_llm_impl(
    client: ToolCapableLlmClient,
    state: TickState,
    *,
    activities_dir: Path = ACTIVITIES_DIR,
    actions_dir: Path = ACTIONS_DIR,
    host_runtime: HostRuntime,
    prompt_dumper: PromptDumper | None = None,
    db: KindredDB | None = None,
    home: HomeProfile | None = None,
) -> NodeReturn:
    """T2.act.llm 真实现。

    14 §3.4 三段流程：

    1. render prompt（含 target activity SKILL 段；尚未升 jinja2 step 模板）
    2. ``client.complete_with_tools(...)`` → final dict + tool trace
    3. 应用 ``final_state_diff`` 到 ``next_state``：
       - ``committed=True``：StateTransition → Location apply → 完整 State 校验 →
         artifact commit；任一 state proposal 校验失败都转为 ``committed=False``
       - ``committed=False``：不写 diff，仅记录 ``act_result``（ta 想做但没做）
    4. 返 patch::

        {
            "next_state": {... diff 已应用 ...},  # committed=True 才有
            "act_result": dict,                     # 总是有
        }

    入参契约：``state`` 应含 ``next_state``（T1.sense.io / T1.sense.derive /
    T1.sense.llm 已填）+ ``act_decision``（T1.sense.llm 填，且
    ``act_decision.act == True``——条件边路由保证）。

    缺 ``next_state`` / ``act_decision`` raise ``ActLlmContractError`` ——
    上游契约错应立即暴露（与 sense_llm 同款 fail-fast）。

    路由防御反向 testbed：
        ``MockLlmClient`` 在 ``act_false + act.llm`` 组合自己 raise——节点
        不需要额外防御 ``act_decision.act == False`` 的情况（路由保证不到）。
    """
    started = time.perf_counter()
    next_state = state.get("next_state")
    if next_state is None:
        raise ActLlmContractError(
            "T2.act.llm: state['next_state'] missing; T1 sense chain must run first.",
        )

    act_decision = state.get("act_decision")
    if not isinstance(act_decision, dict):
        raise ActLlmContractError(
            "T2.act.llm: state['act_decision'] missing or not dict; T1.sense.llm must run first.",
        )

    # 条件边路由保证 act=True 才到本节点；本地浅 assert 双保险（与 sense_llm 一致）
    if not act_decision.get("act"):
        raise ActLlmContractError(
            "T2.act.llm: routed in but act_decision.act is False/missing—routing bug",
        )

    # 节点只动 next_state；deepcopy 防上游传进来的 next_state 与 prev_state
    # 共享子层引用（双保险——sense_io 真实路径已 deepcopy，但节点不应隐式
    # 依赖上游。拷贝代价：单个 next_state dict，可忽略）。
    next_state = deepcopy(next_state)

    # ─── 1. render prompt：read target activity SKILL 渲染进 prompt（L2）─
    # mock client 不解析 prompt，但 read 是真的——让 --real 真 LLM 读到 activity
    # 是什么、涉及哪些基础动作、状态影响基准档位。SKILL 缺失降级（运行时数据
    # 问题，非契约错，不 raise）。
    triggered_at = state.get("triggered_at")
    kind = act_decision.get("kind")
    target = act_decision.get("target_activity")
    # advance/end 操作的是**现有 activity**（推进它），SKILL 渲染 / current_state
    # Action 解析与确定性 effect 都应针对现有 activity，而非 sense 决策的 target——多 SKILL 时
    # target≠现有 activity 会按 target 的 states 校验通过却把 state 写到另一个
    # activity 上。start 时 effective=target（还没现有 activity）。
    effective_activity = target
    current_step = None
    if kind != "start_activity":
        existing = next_state.get("activity")
        if isinstance(existing, dict) and isinstance(existing.get("name"), str):
            effective_activity = existing["name"]
            current_step = existing.get("step")
    # 硬兜底：对已在 settle 的活动重复 end_activity 是空转（实证：dine_out 在 settle
    # 上被弱模型连判 4 次 end）。settle 是谢幕终态，再 end 不会推进只会原地重复谢幕。
    # sense 侧已加软引导（system prompt + current_activity_line），这里是结构兜底。
    #
    # ⚠️ N-1（codex）：不能纠偏成 advance 后**继续调 LLM**——advance prompt 会让弱模型
    # 从 uses 推进到下一原子动作（如 current_state="eat"），状态变换随后会把已 settle 的
    # step 改回原子动作 → settle 活动被「复活」，违反
    # 「停在 settle 不动」目标和 docs/15 end_activity→settle 隐式终态约束，根本没兜住。
    # 故做成**确定性 no-op**：不调 LLM、不应用任何 final_state_diff，直接 early return
    # 一个可审计的 act_result（corrected_from=end_activity / state_diff_keys=[] /
    # committed=False），activity.step 原样保持 settle。下一拍 sense 再决定 start 新活动。
    # 不 raise（运行期弱模型行为，不该崩 tick）。
    if kind == "end_activity" and current_step == SETTLE_STEP:
        logger.warning(
            "T2.act.llm: kind=end_activity 但上一拍已在 step=settle（已谢幕）——"
            "对已谢幕活动重复 end 是空转，确定性 no-op（停在 settle，不调 LLM）。target=%r",
            effective_activity,
        )
        act_result = {
            "kind": "advance_activity",
            "corrected_from": "end_activity",
            "target": effective_activity,
            "current_state": SETTLE_STEP,
            "tool_trace": [],
            "state_diff_keys": [],
            "committed": False,
            "failure_reason": None,
            "destination_choice": None,
            "destination_abandoned": None,
            "location_arrival": None,
            "artifacts": [],
        }
        # next_state patch 为空 → activity 等层保持上游 derive 值（step 仍 settle）。
        return {"next_state": {}, "act_result": act_result}
    # ⑥-act：渲染「你此刻在哪一 step + 该不该推进」——sense 侧「你正在做」的孪生补丁。
    # 不告诉心当前 step，它每拍只看到 SKILL 菜单（walk/eat 都能选），会惯性反复选同一个
    # step（实证：dine_out 卡 step=walk 9 tick 到不了 eat，越走越饿、energy=0 干耗）。
    # step 引导文案在 act_user.md.j2 按 kind/current_step 分支渲染。
    location_target = effective_activity if isinstance(effective_activity, str) else None
    location_kind = kind if isinstance(kind, str) else None
    location_triggered_at = triggered_at if isinstance(triggered_at, str) else None
    location_resolver = LocationCandidateResolver()
    location_session = LocationKernelSession.create(
        location_target,
        next_state,
        kind=location_kind,
        activities_dir=activities_dir,
        actions_dir=actions_dir,
        resolver=location_resolver,
        home=home,
    )
    location_section = location_session.prompt_section() if location_session is not None else ""
    # L5：有进行中目的地计划的拍子，step 引导切 en route 分支——「在路上停在移动
    # 动作是正确的」，替换通用的「别原地反复同一个 step」（后者会催心没到就切 eat）。
    en_route_destinations = (
        location_session.en_route_destinations() if location_session is not None else []
    )
    effect_session = CapabilityEffectSession(
        target=effective_activity,
        current_step=current_step,
        kind=location_kind,
        next_state=next_state,
        activities_dir=activities_dir,
        actions_dir=actions_dir,
        db=db,
        artifact_store=host_runtime.artifact_store(),
    )
    presence_state = next_state.get("presence")
    activity_state = next_state.get("activity")
    current_activity = (
        activity_state if kind != "start_activity" and isinstance(activity_state, dict) else None
    )
    decision_reason_raw = act_decision.get("reason")
    sense_note_raw = state.get("note")
    prompt_context = ActPromptContext(
        triggered_at=triggered_at,
        kind=kind,
        target=effective_activity,
        current_step=current_step,
        activity=load_activity_prompt_context(
            effective_activity,
            activities_dir=activities_dir,
            actions_dir=actions_dir,
        ),
        presence=PresencePromptContext(
            user_present=isinstance(presence_state, dict)
            and presence_state.get("user_present") is True,
            activity_name=(current_activity.get("name") if current_activity is not None else None),
            with_whom=(
                tuple(current_activity.get("with_whom", ()))
                if current_activity is not None
                else None
            ),
        ),
        location_section=location_section,
        outbound=OutboundFactContext(
            enabled=bool(effect_session.send_action_names),
            delivered_before=effect_session.delivered_before,
            step_before_send=effect_session.step_before_send,
            kind=effect_session.kind,
        ),
        en_route_destinations=tuple(en_route_destinations),
        committed_artifacts=(
            tuple(
                (artifact["artifact_ref"], artifact["profile"])
                for artifact in current_activity_artifacts(
                    next_state,
                    db,
                    activity_name=(
                        effective_activity if isinstance(effective_activity, str) else None
                    ),
                )
                if artifact.get("status") == "available"
            )
            if kind == "advance_activity"
            else ()
        ),
        possession_facts_section=render_current_possession_facts(next_state),
        decision_reason=(
            decision_reason_raw.strip() if isinstance(decision_reason_raw, str) else ""
        ),
        sense_note=sense_note_raw.strip() if isinstance(sense_note_raw, str) else "",
        current_engagement=(
            float(current_activity["engagement"])
            if current_activity is not None
            and isinstance(current_activity.get("engagement"), (int, float))
            and not isinstance(current_activity.get("engagement"), bool)
            and 0 <= current_activity["engagement"] <= 1
            else None
        ),
    )
    prompt = render_act_prompt(prompt_context)

    # ─── 2. 调 LLM 工具环（mock 或 真）──────────────────────────
    # client.complete_with_tools 的运行时异常（GeminiLlmClientError / ToolLoopError 等，均
    # LlmClientError 子类）不在 NodeContractError 谱系。与 sense_llm 一致：边界包装成
    # ActLlmContractError，让 daemon / CLI 一行 ``except NodeContractError`` 兜底，不漏接
    # LLM runtime failure。宽 catch 但放行已是 NodeContractError 的。
    dump_triggered_at = triggered_at if isinstance(triggered_at, str) else None
    tool_loop_trace: list[Any] = []
    tool_loop_debug_events: list[dict[str, Any]] = []
    try:
        out, tool_loop_trace, tool_loop_debug_events = _complete_act_with_tools(
            client,
            prompt,
            target=effective_activity,
            prev_state=state.get("prev_state"),
            next_state=next_state,
            activities_dir=activities_dir,
            actions_dir=actions_dir,
            home=home,
            kind=location_kind,
            tick_id=state.get("tick_id"),
            triggered_at=location_triggered_at,
            db=db,
            location_session=location_session,
            location_resolver=location_resolver,
            effect_session=effect_session,
            host_runtime=host_runtime,
        )
    except ToolLoopError as exc:
        # M2 契约：工具环可能已经发生不可撤外部效果，不能把 tick raise 掉。
        # 本期地点工具都是 staged_state_event，不 apply；trace 仍完整进 act_result。
        tool_trace = _tool_events_to_trace(
            exc.tool_events,
        )
        tool_debug_events = _tool_events_to_debug_dump(
            exc.tool_events,
        )
        logger.info(
            "T2.act.llm: tool_loop failed rounds=%s tools=%s error_types=%s",
            exc.rounds,
            _tool_event_names(exc.tool_events),
            _tool_event_error_types(exc.tool_events),
        )
        if prompt_dumper is not None:
            prompt_dumper.dump(
                role="act.llm",
                prompt=prompt,
                response={
                    "error_type": type(exc).__name__,
                    "rounds": exc.rounds,
                    "tool_events": tool_trace,
                    "tool_events_raw": tool_debug_events,
                },
                triggered_at=dump_triggered_at,
                tick_id=state.get("tick_id"),
                raw_text=getattr(exc, "raw_text", None),
            )
        return _failed_tool_loop_patch(
            kind=kind,
            target=effective_activity,
            failure_reason=str(exc),
            tool_trace=tool_trace,
            effect_session=effect_session,
        )
    except NodeContractError:
        effect_session.discard()
        raise
    except Exception as exc:  # noqa: BLE001 - 边界包装 client 任意运行时异常
        # 与 sense.llm 对称：失败时把模型原始未解析文本（client 异常的 raw_text）落进
        # gated + 0600 dump 工件——这是失败全文的唯一出口（异常 msg 只带不含正文的指纹）。
        if prompt_dumper is not None:
            prompt_dumper.dump(
                role="act.llm",
                prompt=prompt,
                response={"error_type": type(exc).__name__},
                triggered_at=dump_triggered_at,
                tick_id=state.get("tick_id"),
                raw_text=getattr(exc, "raw_text", None),
            )
        effect_session.discard()
        raise ActLlmContractError(
            f"T2.act.llm: LLM client.complete_with_tools 失败（{type(exc).__name__}）",
        ) from exc

    # ─── 3. validate LLM 顶层输出结构（坏 schema 立即 raise）─
    # 14 §3.4：LLM 最终 JSON 只输出 final_state_diff（state 变更）+ committed /
    # failure_reason。tool_trace 与地点事件由真实工具调用生成，act_result 由节点构造。
    try:
        response = _validate_act_output_schema(out)
    except ActLlmContractError as exc:
        if effect_session.has_irreversible_send_attempt():
            return _failed_tool_loop_patch(
                kind=kind,
                target=effective_activity,
                failure_reason=f"tool_loop final response rejected: {exc}",
                tool_trace=tool_loop_trace,
                effect_session=effect_session,
            )
        effect_session.discard()
        raise
    # 成功 tick 也 dump（gated；与 sense.llm 对称，闭合「dump 只覆盖 sense」的历史缺口）。
    if prompt_dumper is not None:
        dump_response = dict(out)
        dump_response["tool_events_raw"] = tool_loop_debug_events
        prompt_dumper.dump(
            role="act.llm",
            prompt=prompt,
            response=dump_response,
            triggered_at=dump_triggered_at,
            tick_id=state.get("tick_id"),
        )
    committed = response.committed
    failure_reason = response.failure_reason
    tool_trace = tool_loop_trace

    # ─── 4. F1：StateTransition → Location → Presence → State 校验 → artifact commit ───
    # 14 §3.4：LLM 顶层 final_state_diff 是 state 变更包（按子层：activity /
    # current_state / needs / affect / ...）。节点应用进 next_state + Host-owned effect
    # + 落 step，并自动算 state_diff_keys 供 act_result。
    state_diff_keys: list[str] = []
    artifacts: list[dict[str, str]] = []
    current_state = None
    events_outcome = LocationApplyResult()
    if committed:
        try:
            final_state_diff = response.final_state_diff
            if final_state_diff is None:
                raise ActLlmContractError("T2.act.llm: committed=true requires final_state_diff")
            effective_current_state = resolve_effective_action(
                effective_activity,
                kind,
                final_state_diff,
                activities_dir=activities_dir,
                actions_dir=actions_dir,
            )
            resolved_state_diff = resolve_inventory_diff(
                next_state,
                final_state_diff,
                effective_action=effective_current_state,
                target=effective_activity,
                activities_dir=activities_dir,
                actions_dir=actions_dir,
                reader=db,
            )
            transition = StateTransitionApplier(
                target=effective_activity,
                kind=kind,
                activities_dir=activities_dir,
                actions_dir=actions_dir,
            ).apply(
                next_state,
                resolved_state_diff,
                effective_current_state=effective_current_state,
                affect_event_touched=state.get("affect_event_touched"),
            )
            next_state = transition.next_state
            current_state = transition.current_state

            # 地点三事件派生（第五趴 §6/§7 步骤 10）：choice → 写 context 计划；
            # abandoned → 移除计划；arrival → 唯一更新 state.location 的路径 + 关闭计划
            # （有匹配计划时以计划归一化持久化事件，N-2）；end_activity → 清全部计划
            # （到达前活动结束，计划随活动死亡）。
            events_outcome = apply_location_commit(
                location_session,
                next_state,
                kind=location_kind,
            )

            presence_touched = project_arrival_presence(
                next_state,
                with_whom_explicit=transition.with_whom_explicit,
                arrival_applied=events_outcome.arrival_record is not None,
            )

            # S2 / D-019：完整 State 校验是 F1 的最后一道 state 闸门。必须看到
            # StateTransition 与 Location apply 的合成结果，并且先于任何可撤 artifact commit。
            _validate_complete_state(next_state)

            # state_diff_keys = 节点**实际改写的 State 顶层字段**（供审计/重放），
            # 不是 LLM raw final_state_diff 的 key。raw key 里 current_state→
            # activity.step、needs/affect→interior，都不是顶层字段。所以这里 apply
            # 后收集：浅合并的顶层 key + interior（若 Action effect / 情境方向生效）+
            # activity（若 start
            # 建 / advance 更新 / step 落 / 地点事件改了 context）+ location（若 arrival）。
            touched = set(transition.touched_keys)
            if events_outcome.activity_touched:
                touched.add("activity")
            if events_outcome.location_touched:
                touched.add("location")
            if events_outcome.environment_touched:
                touched.add("environment")
            if presence_touched:
                touched.add("presence")
                logger.info("T2.act.llm: arrival_presence_projected transition=true_to_false")
            state_diff_keys = sorted(touched)

            artifacts = effect_session.commit(tool_trace)
        except (ActLlmContractError, ArtifactStoreError) as exc:
            logger.info(
                "T2.act.llm: state proposal rejected error_type=%s send_already_delivered=%s",
                type(exc).__name__,
                effect_session.has_successful_send(tool_trace),
            )
            return _failed_tool_loop_patch(
                kind=kind,
                target=effective_activity,
                failure_reason=f"state proposal rejected: {exc}",
                tool_trace=tool_trace,
                effect_session=effect_session,
            )
    else:
        # committed=False：ta 想做但没做。不应用 state 变更，仅记元信息。
        # logger 给 daemon / debug 一个明显信号（不是 raise，是观察点）
        logger.info(
            "T2.act.llm: committed=False, skipping state diff apply; failure_reason_present=%s",
            failure_reason is not None,
        )
        effect_session.discard()

    if (
        committed
        and effect_session.has_successful_send(tool_trace)
        and (
            not isinstance(current_state, str)
            or current_state not in effect_session.send_action_names
        )
    ):
        logger.warning(
            "T2.act.llm: send_to_user 已成功执行，但 final current_state=%r，"
            "不是声明 send_to_user 的 action。",
            current_state,
        )

    outbound_delivery = effect_session.outbound_delivery(tool_trace)

    # ─── 5. 节点构造 act_result 元信息（贴 02 §563，不是 LLM 产出 diff 包）─
    # 02 §563 设计是 step_index（数字序号），但 step=原子状态（walk/eat）非序号，
    # 所以用 current_state 记 ta 落在哪个原子状态。
    # target 记**实际操作对象** effective_activity（advance 时是现有 activity，非
    # sense 给的 target）——让 trace 与 state_diff_keys / next_state 实际改的 activity
    # 一致，不会「说操作 study_tech 但实改 explore_food」。
    # 三个地点事件也进 act_result（committed 才记——与 final_state_diff 同命；
    # committed=False 时事件整体视为无效，schema 交叉校验也只在 committed=True 收紧）。
    # destination_choice / location_arrival 都记**实际应用**的归一化事件（home 等
    # node-held 事实、匹配计划的 snapshot 不靠 LLM 复读）：L3 PlaceStore 的派生源是
    # act_result + committed state diff（第五趴 §9），这里是持久化出口。
    act_result = {
        "kind": kind,
        "target": effective_activity,
        "current_state": current_state,
        "tool_trace": tool_trace,
        "state_diff_keys": state_diff_keys,
        "committed": committed,
        "failure_reason": failure_reason,
        "destination_choice": events_outcome.choice_record,
        "destination_abandoned": events_outcome.abandoned_record,
        "location_arrival": events_outcome.arrival_record,
        "outbound_delivery": outbound_delivery,
        "artifacts": artifacts,
    }
    logger.debug(
        "T2.act.llm done kind=%s target=%s committed=%s current_state=%s "
        "state_diff_keys=%s elapsed_ms=%.1f",
        kind,
        effective_activity,
        committed,
        current_state,
        state_diff_keys,
        (time.perf_counter() - started) * 1000,
    )

    # ─── 6. 返 patch ──────────────────────────────────────────
    # deepcopy→patch：只交出本节点真改的层（state_diff_keys 已精确收集）。
    # committed=False 时 state_diff_keys 为空 → next_state patch 为空字典——activity
    # 等层保持上游（derive）的值，这正是 act:false「诚实的平稳继续」（§0.2）。
    next_state_patch = {key: next_state[key] for key in state_diff_keys}
    return {
        "next_state": next_state_patch,
        "act_result": act_result,
    }


# ─────────────────────────────────────────────────────────────────────
# Helpers（私有，非节点）
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _ActToolContext:
    """act 工具分发上下文。

    这是 M4 的最小 adapter 接缝：注册表按 tool 名分发，具体 handler 自己处理
    read_only / staged_state_event / external_side_effect / artifact_write 的语义。
    """

    kind: str | None
    capability_registry: CapabilityRegistry
    capability_context: HostExecutionContext
    authorized_capability_names: frozenset[str]
    authorized_tool_names: frozenset[str]
    location_session: LocationKernelSession | None
    effect_session: CapabilityEffectSession


def _complete_act_with_tools(
    client: ToolCapableLlmClient,
    prompt: str,
    *,
    target: Any,
    prev_state: Any,
    next_state: dict[str, Any],
    activities_dir: Path,
    actions_dir: Path,
    home: HomeProfile | None,
    kind: str | None,
    tick_id: int | str | None,
    triggered_at: str | None,
    db: KindredDB | None,
    location_session: LocationKernelSession | None,
    location_resolver: LocationCandidateResolver,
    effect_session: CapabilityEffectSession,
    host_runtime: HostRuntime,
) -> tuple[dict[str, Any], list[Any], list[dict[str, Any]]]:
    """运行 act 工具环；按 ToolDef.effect 执行/暂存，不直接绕过 F1 写 state。"""
    if not isinstance(client, ToolCapableLlmClient):
        raise ActLlmContractError(
            "T2.act.llm: act requires ToolCapableLlmClient",
        )

    capability_registry = host_runtime.registry
    host_tick_context = HostTickContext(
        config=host_runtime.config,
        anchor_activity=anchor_activity_for_act(
            prev_state if isinstance(prev_state, Mapping) else None,
            kind,
            target,
        ),
        act_kind=kind,
        tick_id=tick_id,
        triggered_at=triggered_at,
        target=target,
        next_state=next_state,
        activities_dir=activities_dir,
        actions_dir=actions_dir,
        db=db,
        home=home,
        caches={LOCATION_CANDIDATE_RESOLVER_KEY: location_resolver},
        provider_handles=host_runtime.provider_handles,
    )
    capability_context = host_runtime.execution_context(host_tick_context)
    authorized_tools = _authorized_act_tool_defs(
        target,
        activities_dir=activities_dir,
        actions_dir=actions_dir,
        for_end_activity=kind == "end_activity",
        capability_registry=capability_registry,
        capability_context=capability_context,
        location_session=location_session,
    )
    context = _ActToolContext(
        kind=kind,
        capability_registry=capability_registry,
        capability_context=capability_context,
        authorized_capability_names=frozenset(
            _authorized_act_capability_names(
                target,
                activities_dir=activities_dir,
                actions_dir=actions_dir,
                for_end_activity=kind == "end_activity",
            )
        ),
        authorized_tool_names=frozenset(tool.name for tool in authorized_tools),
        location_session=location_session,
        effect_session=effect_session,
    )
    logger.debug(
        "T2.act.llm: authorized act tools target=%r kind=%r tools=%s",
        target,
        kind,
        [tool.name for tool in authorized_tools],
    )
    result = client.complete_with_tools(
        prompt,
        role="act.llm",
        tools=authorized_tools,
        handler=lambda call: _handle_act_tool(call, context=context),
        max_rounds=_ACT_TOOL_LOOP_MAX_ROUNDS,
    )
    return (
        result.final,
        _tool_events_to_trace(result.tool_events),
        _tool_events_to_debug_dump(result.tool_events),
    )


def _handle_act_tool(
    call: ToolCall,
    *,
    context: _ActToolContext,
) -> ToolResult:
    """act tool adapter registry：核心节点只按名字分发，效果语义由 handler 落地。"""
    if context.location_session is not None and context.location_session.handles(call.name):
        if context.kind == "end_activity":
            return ToolResult.error(
                call,
                error_type="ToolUnavailableDuringEndActivity",
                message="act tools are unavailable while ending an activity",
            )
        if call.name not in context.authorized_tool_names:
            return ToolResult.error(
                call,
                error_type="UnauthorizedTool",
                message="tool is not authorized for the current activity",
            )
        return context.location_session.handle(call)
    return context.effect_session.consume(
        context.capability_registry.dispatch(
            call,
            authorized_capability_names=context.authorized_capability_names,
            context=context.capability_context,
        ),
        call=call,
    )


def _authorized_act_tool_defs(
    target: Any,
    *,
    activities_dir: Path,
    actions_dir: Path,
    for_end_activity: bool = False,
    capability_registry: CapabilityRegistry,
    capability_context: HostExecutionContext,
    location_session: LocationKernelSession | None,
) -> tuple[ToolDef, ...]:
    authorized_capabilities = _authorized_act_capability_names(
        target,
        activities_dir=activities_dir,
        actions_dir=actions_dir,
        for_end_activity=for_end_activity,
    )
    capability_tools = capability_registry.visible_tool_defs(
        authorized_capability_names=authorized_capabilities,
        context=capability_context,
    )
    kernel_tools = location_session.tool_defs if location_session is not None else ()
    _ensure_distinct_tool_names(capability_tools, kernel_tools)
    return capability_tools + kernel_tools


def _authorized_act_capability_names(
    target: Any,
    *,
    activities_dir: Path,
    actions_dir: Path,
    for_end_activity: bool = False,
) -> tuple[str, ...]:
    if not isinstance(target, str) or not target.strip():
        return ()
    try:
        skill = load_activity_skill(target, activities_dir=activities_dir, actions_dir=actions_dir)
    except ActivitySkillError as exc:
        logger.warning("T2.act.llm: 工具授权无法加载 activity %r，按无工具降级：%s", target, exc)
        return ()

    if for_end_activity:
        return ()
    names = list(
        declared_capability_names_for_activity(
            target,
            activities_dir=activities_dir,
            actions_dir=actions_dir,
        )
    )
    if skill.location_bindings and not for_end_activity:
        names.append(_LOCATION_CAPABILITY)
    return tuple(names)


def _ensure_distinct_tool_names(*groups: tuple[ToolDef, ...]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for tools in groups:
        for tool in tools:
            if tool.name in seen:
                duplicates.add(tool.name)
            seen.add(tool.name)
    if duplicates:
        raise ActLlmContractError(
            f"T2.act.llm: kernel/capability tool names overlap: {sorted(duplicates)}"
        )


def _tool_events_to_trace(
    events: tuple[ToolEvent, ...],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        item = {
            "tool": event.call.name,
            "effect": event.effect,
            "round": event.round_index,
            "args": deepcopy(
                event.result.trace_args if event.result.trace_args is not None else event.call.args
            ),
            "ok": not event.result.is_error,
            "result": deepcopy(
                event.result.trace_response
                if event.result.trace_response is not None
                else event.result.response
            ),
        }
        result.append(cast("dict[str, Any]", redact_observability_secrets(item)))
    return result


def _tool_events_to_debug_dump(
    events: tuple[ToolEvent, ...],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        result.append(
            cast(
                "dict[str, Any]",
                redact_observability_secrets(
                    {
                        "tool": event.call.name,
                        "effect": event.effect,
                        "round": event.round_index,
                        "args": deepcopy(event.call.args),
                        "ok": not event.result.is_error,
                        "result": deepcopy(event.result.response),
                    }
                ),
            )
        )
    return result


def _tool_event_names(events: tuple[ToolEvent, ...]) -> list[str]:
    return [event.call.name for event in events]


def _tool_event_error_types(events: tuple[ToolEvent, ...]) -> list[str]:
    result: list[str] = []
    for event in events:
        if not event.result.is_error:
            continue
        error_type = event.result.response.get("error_type")
        result.append(error_type if isinstance(error_type, str) else "UnknownError")
    return result


def _failed_tool_loop_act_result(
    *,
    kind: Any,
    target: Any,
    failure_reason: str,
    tool_trace: list[Any],
    outbound_delivery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "target": target,
        "current_state": None,
        "tool_trace": tool_trace,
        "state_diff_keys": [],
        "committed": False,
        "failure_reason": failure_reason,
        "destination_choice": None,
        "destination_abandoned": None,
        "location_arrival": None,
        "outbound_delivery": outbound_delivery,
        "artifacts": [],
    }


def _failed_tool_loop_patch(
    *,
    kind: Any,
    target: Any,
    failure_reason: str,
    tool_trace: list[Any],
    effect_session: CapabilityEffectSession,
) -> NodeReturn:
    effect_session.discard()
    return {
        "next_state": {},
        "act_result": _failed_tool_loop_act_result(
            kind=kind,
            target=target,
            failure_reason=failure_reason,
            tool_trace=tool_trace,
            outbound_delivery=effect_session.outbound_delivery(tool_trace),
        ),
    }


def _validate_complete_state(next_state: dict[str, Any]) -> None:
    """验证 State 全层与跨层 invariant；错误只暴露安全 schema path。"""
    try:
        State.model_validate(next_state)
    except ValidationError as exc:
        raise ActLlmContractError(
            "T2.act.llm: 完整 State schema 校验失败，"
            f"invalid_paths={safe_validation_error_paths(exc)}",
        ) from None


def _validate_act_output_schema(out: dict[str, Any]) -> ActResponse:
    """校验 LLM 顶层输出（14 §3.4）必含字段 + 类型，违约抛 ``ActLlmContractError``。

    顶层字段 / 类型 + ``committed`` ↔ ``final_state_diff`` 的交叉约束，单一真相源是
    ``ActResponse``（见 ``kindred.llm.schemas``）。本 helper 只把 ``ValidationError``
    包成 ``ActLlmContractError``，保 daemon ``except NodeContractError`` 兜底语义。

    ``failure_reason`` 不强制与 committed 互斥（failure scenario 给 failure_reason +
    committed=False，其他 scenario failure_reason=None + committed=True）；只查类型。

    返回已校验的 ``ActResponse``——三个地点事件（destination_choice 等）经 pydantic
    解析成 model，下游 state applier 直接消费，不再裸摸 dict。
    """
    try:
        return ActResponse.model_validate(out)
    except ValidationError as exc:
        raise ActLlmContractError(
            f"T2.act.llm: LLM 输出顶层 schema 不合契约：{exc}",
        ) from exc


# ─────────────────────────────────────────────────────────────────────
# Factory thin wrapper
# ─────────────────────────────────────────────────────────────────────


def _make_t2_act_llm(
    client: ToolCapableLlmClient,
    *,
    host_runtime: HostRuntime,
    activities_dir: Path = ACTIVITIES_DIR,
    actions_dir: Path = ACTIONS_DIR,
    prompt_dumper: PromptDumper | None = None,
    db: KindredDB | None = None,
    home: HomeProfile | None = None,
) -> Callable[[TickState], NodeReturn]:
    """绑定 client、HostRuntime 与 tick 依赖，返回 act closure。"""
    if not isinstance(client, ToolCapableLlmClient):
        raise ActLlmContractError(
            "T2.act.llm: act requires ToolCapableLlmClient",
        )
    _validate_capability_assembly(
        host_runtime.registry,
        activities_dir=activities_dir,
        actions_dir=actions_dir,
    )

    def t2_act_llm_closure(state: TickState) -> NodeReturn:
        return _t2_act_llm_impl(
            client,
            state,
            activities_dir=activities_dir,
            actions_dir=actions_dir,
            host_runtime=host_runtime,
            prompt_dumper=prompt_dumper,
            db=db,
            home=home,
        )

    return t2_act_llm_closure


def _validate_capability_assembly(
    capability_registry: CapabilityRegistry,
    *,
    activities_dir: Path,
    actions_dir: Path,
) -> None:
    """装配期校验 capability 声明和 kernel/capability 工具命名空间。"""

    _ensure_distinct_tool_names(
        capability_registry.tool_defs(),
        LOCATION_KERNEL_TOOL_DEFS,
    )
    required: set[str] = set()
    package_errors: list[str] = []
    for action_name in list_registered_actions(actions_dir=actions_dir):
        try:
            action = load_atomic_action(action_name, actions_dir=actions_dir)
        except ActivitySkillError as exc:
            package_errors.append(str(exc))
            continue
        required.update(action.capabilities)
    for activity_name in list_registered_activities(activities_dir=activities_dir):
        try:
            skill = load_activity_skill(
                activity_name,
                activities_dir=activities_dir,
                actions_dir=actions_dir,
            )
        except ActivitySkillError as exc:
            package_errors.append(str(exc))
            continue
        if skill.location_bindings:
            required.add(_LOCATION_CAPABILITY)
    if package_errors:
        details = "\n".join(f"- {message}" for message in package_errors)
        raise ActLlmContractError(
            "T2.act.llm: action/activity package validation failed:\n" + details
        )
    missing = required - capability_registry.declared_capability_names()
    if missing:
        raise ActLlmContractError(
            f"T2.act.llm: action/activity packages require unregistered capabilities: "
            f"{sorted(missing)}"
        )


# ─────────────────────────────────────────────────────────────────────
# 顶层 mock（保留兼容 build.py 默认装配 / 拓扑测试）
# ─────────────────────────────────────────────────────────────────────


def t2_act_llm(state: TickState) -> NodeReturn:
    """T2 唯一节点：mock 透传 + 占位 act_result（仅当调用方未注入时）。

    默认占位 ``{'mock': True, 'committed': True}``，保拓扑测试 + cli --mock 仍跑。
    真实路径：runtime 通过 ``make_act_llm_node(client, host_runtime=...)`` 注入
    client 与 capability wiring 后走真实现，详 ``_t2_act_llm_impl`` docstring。
    """
    return {"act_result": {"mock": True, "committed": True}}


__all__ = [
    "ActLlmContractError",
    "make_act_llm_node",
    "t2_act_llm",
]
