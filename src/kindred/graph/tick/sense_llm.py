"""``T1.sense.llm`` —— 心识感、定意图节点（LLM call）。

参考文档：14-heart-graph.md §3.3

职责（五段 jinja2 prompt + L2 read）
===================================

调 LLM 出 ``note`` / ``significance`` / ``act_decision`` + 应用 ``thought_diff``，
prompt 走 jinja2 模板（``llm/prompts/sense_user.md.j2``）渲染。

**L2 节点本地 read**（read 完渲染进 prompt 即丢，不进 TickState）：

- 最近 N tick 轨迹（``recent_ticks``，SQLite；cold_start N=20 / 平时 N=5；
  db 缺省时空降级）
- SOUL 摘录（``soul_excerpt_path``；cold_start fallback 完整 SOUL；缺失软心空串）
- bundle 高光闪回（``highlights_path``；dream 沉淀产出，早期为空 → 软心，段不渲染）
- 可选活动清单（``activities_dir``，名 + description；闭集约束 target）

L2 文件路径全部 keyword 参数化并贯穿 ``make_sense_llm_node`` →
``_make_t1_sense_llm`` → ``_t1_sense_llm_impl`` →
``_assemble_sense_prompt_context``；纯 ``_render_sense_prompt`` 只消费装配结果，
真实 graph/CLI 可显式指定 life root（不锁死硬编码相对默认路径）。

节点结构（三层，与其它节点一致）：

- 顶层 ``_t1_sense_llm_impl(client, state)`` 是真实现，可独立测
- ``_make_t1_sense_llm(client)`` 是 thin wrapper closure 只绑 client
- ``t1_sense_llm(state)`` 是顶层 mock 透传，未注 deps 时使用

路径用独立 keyword 参数透传；若后续配置字段继续增多，可聚合为
``SenseLlmDeps`` dataclass。

LLM 输出契约
=============

LLM 返 dict 形如::

    {
        "note": str,
        "significance": int (1~10),
        "act_decision": dict,        # validate_act_decision raise on bad
        "thought_diff": {            # apply_thought_diff
            "add": list[dict],
            "remove": list[str],
        },
        "ambience": str,             # 主观氛围，覆写 next_state.environment.ambience
        "observed_user_present": bool | None,
    }

Thought、Needs 与 Affect 是 mood 的 canonical 输入；本节点应用本拍变化后由 Host
统一重派 Layer 1，不接受 LLM 直接提交绝对 mood。
"""

from __future__ import annotations

import logging
import re
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from kindred.activity import (
    ActivitySkillError,
    list_registered_activities,
    load_activity_skill,
)
from kindred.config import (
    DEFAULT_BUNDLE_HIGHLIGHTS_PATH,
    DEFAULT_IDENTITY_PATH,
    DEFAULT_SOUL_EXCERPT_PATH,
    DEFAULT_SOUL_FULL_PATH,
    DEFAULT_USER_PATH,
)
from kindred.graph._shared._common import (
    NodeReturn,
    validate_act_decision,
)
from kindred.graph._shared._errors import NodeContractError
from kindred.graph.tick._affect_event import (
    AffectEventProjectionError,
    apply_affect_event_response,
)
from kindred.graph.tick._thought_diff import apply_thought_diff
from kindred.graph.tick.projection import sense_prompt as _sense_prompt_projection
from kindred.life_assets import ACTIVITIES_DIR
from kindred.llm.schemas import SenseResponse
from kindred.relationship import render_relationship_summary
from kindred.relationship.models import (
    RelationshipFacetProposal,
    RelationshipProfile,
    RelationshipRoleEvent,
)
from kindred.relationship.projector import (
    RelationshipEvidenceContext,
    project_relationship_change,
    project_relationship_evidence,
)
from kindred.state._derive import rederive_layer1
from kindred.state.state import State
from kindred.state.tick import TickState

if TYPE_CHECKING:
    from collections.abc import Callable

    from kindred.db.facade import KindredDB
    from kindred.llm.client import LlmClient
    from kindred.observability import PromptDumper
    from kindred.relationship.preflight import RelationshipReader

_LOG = logging.getLogger(__name__)


class SenseLlmContractError(NodeContractError):
    """sense_llm 上游契约错（``next_state`` / ``interior`` / ``mood`` / ``thoughts``
    缺失或类型错）。

    与 ``sense_io.ColdStartError`` 同源哲学（都继承 ``NodeContractError``）——
    节点 raise 自定义异常让上游 daemon / 集成测试可以语义化 catch，不是裸
    ``RuntimeError`` 后靠字符串匹配 ``args[0]``。
    """


# Pure Sense prompt projection lives in a dependency-leaf module.  These aliases are
# intentionally kept here because the public contract tests and active probes imported
# the historical private names from ``sense_llm`` before RS3-T2.
CHAT_MSG_TRUNC = _sense_prompt_projection.CHAT_MSG_TRUNC
CHAT_SENT_LEN = _sense_prompt_projection.CHAT_SENT_LEN
CHAT_WINDOW_SIZE = _sense_prompt_projection.CHAT_WINDOW_SIZE
MOUTH_CONTEXT_SIZE = _sense_prompt_projection.MOUTH_CONTEXT_SIZE
SETTLE_ACTIVITY_QUIET_TICKS = _sense_prompt_projection.SETTLE_ACTIVITY_QUIET_TICKS
STUCK_REPEAT_THRESHOLD = _sense_prompt_projection.STUCK_REPEAT_THRESHOLD
SenseActivityChoiceContext = _sense_prompt_projection.SenseActivityChoiceContext
SenseCurrentActivityContext = _sense_prompt_projection.SenseCurrentActivityContext
SensePromptContext = _sense_prompt_projection.SensePromptContext
_ChatProjection = _sense_prompt_projection._ChatProjection
_SEND_ACTION_NAME = _sense_prompt_projection._SEND_ACTION_NAME
_act_mark = _sense_prompt_projection._act_mark
_compress_message = _sense_prompt_projection._compress_message
_destination_plan_names = _sense_prompt_projection._destination_plan_names
_format_situation_value = _sense_prompt_projection._format_situation_value
_gauges_oneline = _sense_prompt_projection._gauges_oneline
_has_current_partner_input = _sense_prompt_projection._has_current_partner_input
_has_environment_provider_context = _sense_prompt_projection._has_environment_provider_context
_message_age_seconds = _sense_prompt_projection._message_age_seconds
_project_chat_window = _sense_prompt_projection._project_chat_window
_quiet_tick_identity = _sense_prompt_projection._quiet_tick_identity
_render_activity_choices = _sense_prompt_projection._render_activity_choices
_render_attrs = _sense_prompt_projection._render_attrs
_render_chat_lines = _sense_prompt_projection._render_chat_lines
_render_current_activity = _sense_prompt_projection._render_current_activity
_render_environment_attrs = _sense_prompt_projection._render_environment_attrs
_render_last_note = _sense_prompt_projection._render_last_note
_render_layer1_gauge = _sense_prompt_projection._render_layer1_gauge
_render_presence_before = _sense_prompt_projection._render_presence_before
_render_recent_contact = _sense_prompt_projection._render_recent_contact
_render_recent_life_texture = _sense_prompt_projection._render_recent_life_texture
_render_recent_ticks = _sense_prompt_projection._render_recent_ticks
_render_sense_prompt = _sense_prompt_projection._render_sense_prompt
_render_situation = _sense_prompt_projection._render_situation
_render_stuck_warning = _sense_prompt_projection._render_stuck_warning
_should_render_current_activity = _sense_prompt_projection._should_render_current_activity
_should_render_environment_provider_value = (
    _sense_prompt_projection._should_render_environment_provider_value
)
_split_sentences = _sense_prompt_projection._split_sentences
_step_is_before_action = _sense_prompt_projection._step_is_before_action


def _read_soul_excerpt(
    *,
    is_cold_start: bool,
    excerpt_path: Path = DEFAULT_SOUL_EXCERPT_PATH,
    full_path: Path = DEFAULT_SOUL_FULL_PATH,
) -> str:
    """read SOUL 人格摘录（L2，docs/14 §1.4）。

    - cold_start：fallback 读完整 SOUL（``full_path``）——给冷启动最全人格底色。
    - 平时：读摘录（``excerpt_path``，未来 dream 改写更新）。
    - 文件缺失 → 软心降级返 ""（不 raise；人格缺失 prompt 仍合法，只是没温度）。

    docs/14 §1.4 cold_start A1 兜底语义：摘录文件未由 dream 产出前，cold_start
    读完整 SOUL；平时摘录缺失也不该崩心跳，软心继续。
    """
    target = full_path if is_cold_start else excerpt_path
    try:
        return Path(target).read_text(encoding="utf-8").strip()
    except OSError:
        # 平时摘录缺失时再 fallback 试完整 SOUL（软心二级兜底）
        if not is_cold_start:
            try:
                return Path(full_path).read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return ""


def _read_bundle_highlights(*, path: Path = DEFAULT_BUNDLE_HIGHLIGHTS_PATH) -> str:
    """read bundle 高光闪回（L2，docs/14 §1.4）。

    dream 节点沉淀产出（significance≥7 episode）。早期未沉淀 → 文件不存在 →
    软心返 ""（prompt 不渲染高光段）。这是「软心」设计：没记忆也能活。
    """
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_soul_file(path: Path) -> str:
    """read 灵魂三件套其二（IDENTITY / USER，L2、docs/14 §1.4）。

    与 SOUL 同为人格底色，每 tick 本地 read 渲染进 prompt 即丢。
    文件缺失 → 软心降级返 ""（该段不渲染，不崩心跳）。
    """
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


def make_sense_llm_node(
    client: LlmClient,
    db: KindredDB | None = None,
    *,
    recent_ticks_limit: int = 5,
    activities_dir: Path = ACTIVITIES_DIR,
    soul_excerpt_path: Path = DEFAULT_SOUL_EXCERPT_PATH,
    soul_full_path: Path = DEFAULT_SOUL_FULL_PATH,
    identity_path: Path = DEFAULT_IDENTITY_PATH,
    user_path: Path = DEFAULT_USER_PATH,
    highlights_path: Path = DEFAULT_BUNDLE_HIGHLIGHTS_PATH,
    prompt_dumper: PromptDumper | None = None,
    relationship_reader: RelationshipReader | None = None,
) -> Callable[[TickState], NodeReturn]:
    """构造 T1.sense.llm closure。

    用法（详 build.py / cli.py）::

        client = MockLlmClient(scenario="start_activity")
        sense_llm = make_sense_llm_node(client, db)
        graph = build_tick_graph(sense_llm_node=sense_llm, ...)

    ``client`` 类型为 ``LlmClient`` Protocol——任何提供
    ``complete(prompt, *, role) -> dict`` 的实现都可注入（mock 或真 LLM），
    节点零改动。

    ``db`` 可选：传入时节点本地 read 最近 N 条轨迹（L2）渲染进 prompt，给心
    「刚经历了什么」的连续感。不传（mock / 拓扑测）时轨迹段为空，prompt 仍
    合法（降级，不报错）。
    """
    return _make_t1_sense_llm(
        client,
        db,
        recent_ticks_limit=recent_ticks_limit,
        activities_dir=activities_dir,
        soul_excerpt_path=soul_excerpt_path,
        soul_full_path=soul_full_path,
        identity_path=identity_path,
        user_path=user_path,
        highlights_path=highlights_path,
        prompt_dumper=prompt_dumper,
        relationship_reader=relationship_reader,
    )


# ─────────────────────────────────────────────────────────────────────
# 真实现（顶层 _impl）
# ─────────────────────────────────────────────────────────────────────


def _t1_sense_llm_impl(
    client: LlmClient,
    state: TickState,
    *,
    db: KindredDB | None = None,
    recent_ticks_limit: int = 5,
    activities_dir: Path = ACTIVITIES_DIR,
    soul_excerpt_path: Path = DEFAULT_SOUL_EXCERPT_PATH,
    soul_full_path: Path = DEFAULT_SOUL_FULL_PATH,
    identity_path: Path = DEFAULT_IDENTITY_PATH,
    user_path: Path = DEFAULT_USER_PATH,
    highlights_path: Path = DEFAULT_BUNDLE_HIGHLIGHTS_PATH,
    prompt_dumper: PromptDumper | None = None,
    relationship_reader: RelationshipReader | None = None,
) -> NodeReturn:
    """T1.sense.llm 真实现。

    14 §3.3 五段流程：

    1. L2 节点本地 read（recent_ticks / SOUL 摘录 / bundle 高光 / 活动清单）
       + jinja2 渲染五段 prompt
    2. ``client.complete(prompt, role="sense.llm")`` → dict
    3. ``validate_act_decision(out["act_decision"])`` raise on bad schema
    4. 应用到 ``next_state.interior`` / ``next_state.environment``：
       - ``thoughts = apply_thought_diff(prev_thoughts, diff)``
       - 从最终 Needs/Affect/Thought 统一重派 Layer 1
       - ``environment.ambience = ambience``（主观氛围）
    5. 返 patch::

        {
            "next_state": {... interior/environment 已更，Presence 实际迁移层按需加入 ...},
            "note": str,
            "significance": int,
            "act_decision": dict,
        }

    入参契约：``state`` 应已含 ``next_state``（T1.sense.io 填）+ interior 已经
    derive 衰减+级联+Layer 1 重派。缺 ``next_state`` 时 raise，不软兜底——上游
    契约错应立即暴露。
    """
    started = time.perf_counter()
    next_state = state.get("next_state")
    if next_state is None:
        raise SenseLlmContractError(
            "T1.sense.llm: state['next_state'] missing; "
            "T1.sense.io / T1.sense.derive must run first.",
        )

    # 节点只动 next_state；deepcopy interior/environment 防上游传进来的 next_state 与 prev_state
    # 共享子层引用。Presence projector 稍后在完整私有副本上运行，不会污染调用方输入。
    next_state = dict(next_state)  # 顶层拷贝
    next_state["interior"] = deepcopy(next_state.get("interior"))
    next_state["environment"] = deepcopy(next_state.get("environment"))

    # ─── 1. jinja2 分段 prompt + L2 read ─────────────
    # docs/14 §3.3 五段结构。L2（节点本地 read，不进 state）：
    #   - 最近 N tick 轨迹（recent_ticks，SQLite；db 缺省时空降级）
    #   - SOUL 摘录（人格底色；cold_start fallback 完整 SOUL）
    #   - bundle 高光闪回（dream 沉淀；早期为空 → 软心）
    #   - 可选活动清单（名 + desc，闭集约束）
    # cold_start 时 recent_ticks 取更多（N=20），soul 读完整版。
    is_cold_start = state.get("trigger_source") == "cold_start"
    limit = 20 if is_cold_start else recent_ticks_limit
    recent_ticks = db.get_recent_ticks(limit=limit) if db is not None else []
    recent_activity_rows: list[dict[str, Any]] = []
    time_raw = next_state.get("time")
    now_iso = _as_str_or_none(time_raw.get("iso")) if isinstance(time_raw, dict) else None
    if db is not None and now_iso is not None:
        try:
            datetime.fromisoformat(now_iso)
            recent_activity_rows = db.get_recent_activity_rows(until=now_iso)
        except Exception as exc:  # noqa: BLE001 - 独立 L2 read 失败只降级本段
            _LOG.warning("recent life texture unavailable error_type=%s", type(exc).__name__)
    chat_projection = _project_chat_window(
        state.get("chat_window"),
        triggered_at=_as_str_or_none(state.get("triggered_at")),
    )
    relationship_profile = _read_relationship_profile(relationship_reader, node="T1.sense.llm")
    relationship_summary = (
        render_relationship_summary(relationship_profile)
        if relationship_profile is not None
        else ""
    )
    relationship_evidence = project_relationship_evidence(
        chat_projection.partner_lines,
        recent_ticks[0] if recent_ticks else None,
    )
    _LOG.debug(
        "T1.sense.llm start cold_start=%s recent_ticks_count=%d trigger_source=%s",
        is_cold_start,
        len(recent_ticks),
        state.get("trigger_source"),
    )
    prompt_context = _assemble_sense_prompt_context(
        state,
        next_state,
        recent_ticks=recent_ticks,
        recent_activity_rows=recent_activity_rows,
        activities_dir=activities_dir,
        is_cold_start=is_cold_start,
        soul_excerpt_path=soul_excerpt_path,
        soul_full_path=soul_full_path,
        identity_path=identity_path,
        user_path=user_path,
        highlights_path=highlights_path,
        chat_projection=chat_projection,
        relationship_summary=relationship_summary,
        relationship_evidence=(relationship_evidence if relationship_profile is not None else None),
    )
    prompt = _render_sense_prompt(prompt_context)

    # ─── 2. 调 LLM（mock 或 真）────────────────────────────────
    # client.complete 的运行时异常（如 LlmClientError：HTTP 错/坏 JSON）不在
    # NodeContractError 谱系。按 LlmClient Protocol 约定，调用节点应在边界包装为
    # SenseLlmContractError——让 daemon 一行 ``except NodeContractError`` 兜底，
    # 不漏接 LLM runtime failure。不 import real_client（层次倒置 + 保 Protocol
    # 解耦）：宽 catch 但排除已是 NodeContractError 的（防御性保留）。
    try:
        out = client.complete(prompt, role="sense.llm")
    except NodeContractError:
        raise
    except Exception as exc:  # noqa: BLE001 - 边界包装 client 任意运行时异常
        if prompt_dumper is not None:
            prompt_dumper.dump(
                role="sense.llm",
                prompt=prompt,
                response={"error_type": type(exc).__name__},
                triggered_at=_as_str_or_none(state.get("triggered_at")),
                tick_id=state.get("tick_id"),
                raw_text=getattr(exc, "raw_text", None),
            )
        raise SenseLlmContractError(
            f"T1.sense.llm: LLM client.complete 失败（{type(exc).__name__}）",
        ) from exc

    # ─── 3. validate 真 LLM 返回 schema（顶层门收完整）────
    # _validate_sense_response 一次性校 note/significance/act_decision/
    # thought_diff 的顶层存在+类型——避免真 LLM 缺字段裸
    # KeyError 逃出 NodeContractError 谱系。
    observed_status = _observed_user_present_status(out)
    affect_event_status = _affect_event_response_status(out)
    relationship_proposal_status = _relationship_proposal_status(out)
    out = _validate_sense_response(out)
    if not _has_current_partner_input(chat_projection):
        if out["observed_user_present"] is not None:
            observed_status = "normalized"
        out["observed_user_present"] = None
    if prompt_dumper is not None:
        prompt_dumper.dump(
            role="sense.llm",
            prompt=prompt,
            response=out,
            triggered_at=_as_str_or_none(state.get("triggered_at")),
            tick_id=state.get("tick_id"),
        )
    act_decision_dict = out["act_decision"]
    note = out["note"]
    significance = out["significance"]
    ambience = out["ambience"]
    observed_user_present = out["observed_user_present"]
    affect_event_response = out["affect_event_response"]
    relationship_proposals = [
        RelationshipFacetProposal.model_validate(item) for item in out["relationship_changes"]
    ]
    relationship_role_event = (
        RelationshipRoleEvent.model_validate(out["relationship_role_event"])
        if out["relationship_role_event"] is not None
        else None
    )
    relationship_change = (
        project_relationship_change(
            relationship_profile,
            relationship_evidence,
            relationship_proposals,
            relationship_role_event,
        )
        if relationship_profile is not None
        else None
    )

    # act_decision 内部结构细校（顶层 dict 门已在上面过）。
    # pydantic ValidationError 包装为 SenseLlmContractError。
    try:
        validate_act_decision(act_decision_dict)  # raise on bad
    except ValidationError as exc:
        raise SenseLlmContractError(f"T1.sense.llm: act_decision schema invalid: {exc}") from exc

    kind = act_decision_dict.get("kind")
    if kind in {"advance_activity", "end_activity"}:
        current = next_state.get("activity")
        current_name = current.get("name") if isinstance(current, dict) else None
        current_step = current.get("step") if isinstance(current, dict) else None
        if (
            not isinstance(current_name, str)
            or current_step == "settle"
            or act_decision_dict.get("target_activity") != current_name
        ):
            raise SenseLlmContractError(
                "T1.sense.llm: act_decision kind/target_activity 与当前 Activity 生命周期冲突",
            )

    # ─── 3b. target_activity 注册表闭集硬门 ──────
    # prompt 约束（_render_activity_choices）只降低模型犯错概率，不堵死——真模型
    # 仍可能返清单外 target（如中文「觅食」）。这里在节点边界用注册表做硬校验：
    # act=true 时 target_activity 必须 ∈ 已注册活动，否则 raise，绝不让幽灵活动
    # 写进 state（否则 T2 读不到 SKILL 降级 / 后续 advance/校验全废）。
    # raise SenseLlmContractError（非降级 act=false）——与本节点其他校验失败一致，
    # daemon `except NodeContractError` 兜底不崩进程；非法 target 是契约错该暴露。
    # 只校 start_activity（启动新活动必须从注册表选）；advance_activity 是「推进
    # 当前正在做的活动」，其 target 指向进行中的活动而非新启动，不受注册表闭集约束。
    if act_decision_dict.get("act") is True and act_decision_dict.get("kind") == "start_activity":
        target = act_decision_dict.get("target_activity")
        registered = list_registered_activities(activities_dir=activities_dir)
        if target not in registered:
            raise SenseLlmContractError(
                f"T1.sense.llm: start_activity 的 target_activity={target!r} 不在"
                f"注册表 {registered}——拒绝写入幽灵活动"
                f"（prompt 约束被模型绕过，节点硬门拦截）",
            )

    # ─── 4. 应用到 next_state.interior / environment ──────────
    interior = _require_interior(next_state)
    environment = _require_environment(next_state)

    # 4a. thoughts diff（apply_thought_diff 是 dict-in dict-out，与 storage truth
    # 对称，节点不需要 dict↔Thought↔dict round-trip）
    prev_thoughts = interior.get("thoughts", []) or []
    if not isinstance(prev_thoughts, list):
        raise SenseLlmContractError(
            f"T1.sense.llm: next_state.interior.thoughts must be list, "
            f"got {type(prev_thoughts).__name__}",
        )
    diff = out.get("thought_diff") or {"add": [], "remove": []}
    # apply_thought_diff 内部抛 ValueError / pydantic ValidationError（坏
    # thought_diff.add/remove 结构），也要包成 SenseLlmContractError，否则
    # daemon except NodeContractError 漏接。
    try:
        interior["thoughts"] = apply_thought_diff(
            prev_thoughts,
            diff,
            now_iso=_as_str_or_none(state.get("triggered_at")),
        )
    except (ValueError, ValidationError) as exc:
        raise SenseLlmContractError(
            f"T1.sense.llm: thought_diff 应用失败（{type(exc).__name__}）：{exc}",
        ) from exc

    # 4b. ambience = 心的主观处境感。EnvironmentProvider 只产天气等被给予事实；
    # 氛围不是 Provider 事实，必须由 sense.llm 结合地点/天气/时间/内在状态主动刷新。
    environment["ambience"] = ambience
    # 4c. Host 从可空物理事实派生 Presence 迁移，并在 T1 私有 working state 上投影。完整 State
    # 校验成功后才把实际改变的层放进 patch；失败时不会返回半成品。
    next_state, presence_touched, presence_observation = _apply_observed_user_present(
        next_state,
        observed_user_present,
    )
    try:
        next_state, affect_event_touched = apply_affect_event_response(
            next_state,
            affect_event_response,
        )
    except ValidationError as exc:
        raise SenseLlmContractError(
            "T1.sense.llm: Affect event response 后完整 State 校验失败，"
            f"invalid_paths={_safe_schema_paths(exc)}",
        ) from None
    except AffectEventProjectionError as exc:
        raise SenseLlmContractError(
            f"T1.sense.llm: Affect event response 投影失败，reason={exc}",
        ) from None
    next_state = _rederive_final_layer1(next_state)
    _LOG.debug(
        "T1.sense.llm done cold_start=%s recent_ticks_count=%d act=%s kind=%s "
        "target=%s significance=%s thought_diff_add=%d "
        "thought_diff_remove=%d partner_input_count=%d mouth_context_count=%d "
        "partner_input_max_age_s=%s observed_user_present_status=%s "
        "observed_user_present=%s presence_observation=%s presence_touched=%s "
        "affect_event_status=%s affect_event_keys=%s affect_event_touched=%s "
        "relationship_proposal_status=%s relationship_change_present=%s "
        "relationship_role_touched=%s relationship_facet_keys=%s elapsed_ms=%.1f",
        is_cold_start,
        len(recent_ticks),
        act_decision_dict.get("act"),
        act_decision_dict.get("kind"),
        act_decision_dict.get("target_activity"),
        significance,
        _diff_count(diff, "add"),
        _diff_count(diff, "remove"),
        len(chat_projection.partner_lines),
        len(chat_projection.mouth_lines),
        chat_projection.partner_input_max_age_s,
        observed_status,
        observed_user_present,
        presence_observation,
        sorted(presence_touched),
        affect_event_status,
        sorted(affect_event_response),
        sorted(affect_event_touched),
        (
            "ignored_no_evidence"
            if not relationship_evidence.available
            else relationship_proposal_status
        ),
        relationship_change is not None,
        relationship_change is not None and relationship_change.target_role is not None,
        sorted(facet for facet, _ in relationship_change.facet_deltas)
        if relationship_change is not None
        else [],
        (time.perf_counter() - started) * 1000,
    )

    # ─── 5. 返 patch ──────────────────────────────────────────
    # 只交出本节点改的层（deepcopy→patch：reducer 浅 merge，不再整包覆盖）。
    next_state_patch = {
        "interior": next_state["interior"],
        "environment": next_state["environment"],
    }
    for layer in presence_touched:
        next_state_patch[layer] = next_state[layer]
    patch: NodeReturn = {
        "next_state": next_state_patch,
        "note": note,
        "significance": significance,
        "act_decision": act_decision_dict,
        "affect_event_touched": sorted(affect_event_touched),
    }
    if relationship_change is not None:
        patch["relationship_change"] = relationship_change
    return patch


# ─────────────────────────────────────────────────────────────────────
# Helpers（私有，非节点）
# ─────────────────────────────────────────────────────────────────────


def _load_activity_choice_context(
    activities_dir: Path,
) -> tuple[SenseActivityChoiceContext, ...]:
    """一次性读取 Activity 注册表，投影为 renderer 可消费的数据。"""
    choices: list[SenseActivityChoiceContext] = []
    for name in list_registered_activities(activities_dir=activities_dir):
        try:
            description = load_activity_skill(name, activities_dir=activities_dir).description
        except (ActivitySkillError, OSError) as exc:
            _LOG.warning(
                "activity_choice_load_failed activity=%s activities_dir=%s error_type=%s",
                name,
                activities_dir,
                type(exc).__name__,
            )
            description = None
        choices.append(SenseActivityChoiceContext(name=name, description=description))
    return tuple(choices)


def _load_current_activity_context(
    next_state: dict[str, Any],
    *,
    activities_dir: Path,
) -> SenseCurrentActivityContext:
    """读取当前 Activity package，并只保留 Sense prompt 所需字段。"""
    activity = next_state.get("activity") if isinstance(next_state, dict) else None
    if not isinstance(activity, dict) or not activity.get("name"):
        return SenseCurrentActivityContext(
            name=None,
            step=None,
            description="",
            engagement=None,
            destination_names=(),
        )

    name = activity.get("name")
    step = activity.get("step")
    description = activity.get("desc") or ""
    terminal_when: str | None = None
    step_before_send = False
    if isinstance(name, str) and name and step != "settle":
        try:
            skill = load_activity_skill(name, activities_dir=activities_dir)
        except ActivitySkillError:
            pass
        else:
            terminal_when = skill.terminal_when
            step_before_send = _step_is_before_action(skill, step, _SEND_ACTION_NAME)

    return SenseCurrentActivityContext(
        name=name,
        step=step,
        description=str(description),
        engagement=activity.get("engagement"),
        destination_names=tuple(_destination_plan_names(activity)),
        terminal_when=terminal_when,
        step_before_send=step_before_send,
    )


def _assemble_sense_prompt_context(
    state: TickState,
    next_state: dict[str, Any],
    *,
    recent_ticks: list[dict[str, Any]] | None = None,
    recent_activity_rows: list[dict[str, Any]] | None = None,
    activities_dir: Path = ACTIVITIES_DIR,
    is_cold_start: bool = False,
    soul_excerpt_path: Path = DEFAULT_SOUL_EXCERPT_PATH,
    soul_full_path: Path = DEFAULT_SOUL_FULL_PATH,
    identity_path: Path = DEFAULT_IDENTITY_PATH,
    user_path: Path = DEFAULT_USER_PATH,
    highlights_path: Path = DEFAULT_BUNDLE_HIGHLIGHTS_PATH,
    chat_projection: _ChatProjection | None = None,
    relationship_summary: str = "",
    relationship_evidence: RelationshipEvidenceContext | None = None,
) -> SensePromptContext:
    """完成 Sense prompt 的全部 L2/package I/O，返回只读渲染输入。"""
    ticks = recent_ticks or []
    show_current_activity = _should_render_current_activity(next_state, ticks)
    projection = chat_projection or _project_chat_window(
        state.get("chat_window"),
        triggered_at=_as_str_or_none(state.get("triggered_at")),
    )
    activity_choices = _load_activity_choice_context(activities_dir)
    current_activity = (
        _load_current_activity_context(
            next_state,
            activities_dir=activities_dir,
        )
        if show_current_activity
        else None
    )
    soul_excerpt = _read_soul_excerpt(
        is_cold_start=is_cold_start,
        excerpt_path=soul_excerpt_path,
        full_path=soul_full_path,
    )
    identity = _read_soul_file(identity_path)
    user = _read_soul_file(user_path)
    bundle_highlights = _read_bundle_highlights(path=highlights_path)
    return SensePromptContext(
        state=state,
        next_state=next_state,
        recent_ticks=ticks,
        recent_activity_rows=recent_activity_rows or [],
        soul_excerpt=soul_excerpt,
        identity=identity,
        user=user,
        bundle_highlights=bundle_highlights,
        activity_choices=activity_choices,
        show_current_activity=show_current_activity,
        current_activity=current_activity,
        chat_projection=projection,
        relationship_summary=relationship_summary,
        relationship_evidence=relationship_evidence,
    )


def _require_interior(next_state: dict[str, Any]) -> dict[str, Any]:
    interior = next_state.get("interior")
    if not isinstance(interior, dict):
        raise SenseLlmContractError(
            f"T1.sense.llm: next_state.interior must be dict, got {type(interior).__name__}",
        )
    return interior


def _require_environment(next_state: dict[str, Any]) -> dict[str, Any]:
    environment = next_state.get("environment")
    if not isinstance(environment, dict):
        raise SenseLlmContractError(
            f"T1.sense.llm: next_state.environment must be dict, got {type(environment).__name__}",
        )
    return environment


def _diff_count(diff: object, key: str) -> int:
    if not isinstance(diff, dict):
        return 0
    value = diff.get(key)
    return len(value) if isinstance(value, list) else 0


def _as_str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _apply_observed_user_present(
    next_state: dict[str, Any],
    observed_user_present: bool | None,
) -> tuple[dict[str, Any], frozenset[str], str]:
    """从可空物理事实派生迁移，在私有副本上投影并返回实际改变的顶层。"""
    working = deepcopy(next_state)
    touched: set[str] = set()
    presence = working.get("presence")
    activity = working.get("activity")
    before = presence.get("user_present") if isinstance(presence, dict) else None
    observation = "no_change"

    if isinstance(presence, dict) and before is False and observed_user_present is True:
        observation = "arrived"
        presence["user_present"] = True
        touched.add("presence")
    elif observed_user_present is False:
        if before is True and isinstance(presence, dict):
            observation = "left"
            presence["user_present"] = False
            touched.add("presence")
        if isinstance(activity, dict) and isinstance(activity.get("with_whom"), list):
            participants = activity["with_whom"]
            without_user = [name for name in participants if name != "user"]
            if without_user != participants:
                activity["with_whom"] = without_user
                touched.add("activity")

    try:
        validated = State.model_validate(working).model_dump()
    except ValidationError as exc:
        raise SenseLlmContractError(
            "T1.sense.llm: Presence observation 后完整 State 校验失败，"
            f"invalid_paths={_safe_schema_paths(exc)}",
        ) from None
    return validated, frozenset(touched), observation


def _safe_schema_paths(exc: ValidationError) -> list[str]:
    """只保留固定 schema path，避免 Pydantic 错误携带输入原值。"""
    paths: set[str] = set()
    for error in exc.errors():
        parts = [str(part) for part in error["loc"]]
        safe_parts = [
            part if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*|\d+", part) else "<invalid>"
            for part in parts
        ]
        paths.add(".".join(safe_parts) or "<root>")
    return sorted(paths)


def _rederive_final_layer1(next_state: dict[str, Any]) -> dict[str, Any]:
    """从本拍最终 lower layers 重派 Layer 1，并只在完整 State 合法时返回。"""
    try:
        working = State.model_validate(deepcopy(next_state))
        working = working.model_copy(
            update={"interior": rederive_layer1(working.interior)},
        )
        return State.model_validate(working).model_dump()
    except ValidationError as exc:
        raise SenseLlmContractError(
            "T1.sense.llm: Layer 1 重派后完整 State 校验失败，"
            f"invalid_paths={_safe_schema_paths(exc)}",
        ) from None


# ─────────────────────────────────────────────────────────────────────
# Factory thin wrapper
# ─────────────────────────────────────────────────────────────────────


def _validate_sense_response(out: object) -> dict[str, Any]:
    """校真 LLM 返回 dict 的顶层 schema 门，统一抛 ``SenseLlmContractError``。

    顶层字段 / 类型 / 范围规则的单一真相源是 ``SenseResponse``（见
    ``kindred.llm.schemas``）；本 helper 只做两件事：

    1. 把非 dict 输入挡在 pydantic 之前给出干净报错；
    2. 把 ``SenseResponse`` 的 ``ValidationError`` 包成 ``SenseLlmContractError``，
       让 daemon ``except NodeContractError`` 兜底接住（避免裸下标在真 LLM 缺字段
       时裸 ``KeyError`` 逃出谱系）。

    返回原 dict 的浅副本，但用 schema 归一化后的 ``observed_user_present`` 覆盖
    对应字段；``act_decision`` / ``thought_diff`` 的深层结构仍由下游细校。
    """
    if not isinstance(out, dict):
        raise SenseLlmContractError(
            f"T1.sense.llm: LLM 返回必须是 dict，got {type(out).__name__}",
        )
    candidate = dict(out)
    candidate.setdefault("observed_user_present", None)
    candidate.setdefault("affect_event_response", {})
    try:
        validated = SenseResponse.model_validate(candidate)
    except ValidationError as exc:
        raise SenseLlmContractError(
            f"T1.sense.llm: LLM 返回顶层 schema 不合契约：{exc}",
        ) from exc
    normalized = dict(out)
    normalized["observed_user_present"] = validated.observed_user_present
    normalized["affect_event_response"] = validated.affect_event_response
    normalized["relationship_changes"] = [
        item.model_dump(mode="json") for item in validated.relationship_changes or []
    ]
    normalized["relationship_role_event"] = (
        validated.relationship_role_event.model_dump(mode="json")
        if validated.relationship_role_event is not None
        else None
    )
    return normalized


def _observed_user_present_status(out: object) -> str:
    """区分模型显式输出、本地补缺与非法值归一化。"""
    if not isinstance(out, dict) or "observed_user_present" not in out:
        return "missing"
    value = out["observed_user_present"]
    if value is None or type(value) is bool:
        return "explicit"
    return "normalized"


def _affect_event_response_status(out: object) -> str:
    if not isinstance(out, dict) or "affect_event_response" not in out:
        return "missing"
    candidate = dict(out)
    candidate.setdefault("observed_user_present", None)
    try:
        validated = SenseResponse.model_validate(candidate).affect_event_response
    except ValidationError:
        return "normalized"
    return "explicit" if out["affect_event_response"] == validated else "normalized"


def _relationship_proposal_status(out: object) -> str:
    if not isinstance(out, dict) or not {
        "relationship_changes",
        "relationship_role_event",
    }.intersection(out):
        return "missing"
    candidate = dict(out)
    candidate.setdefault("observed_user_present", None)
    candidate.setdefault("affect_event_response", {})
    try:
        validated = SenseResponse.model_validate(candidate)
    except ValidationError:
        return "normalized"
    raw_changes = out.get("relationship_changes")
    normalized_changes = [
        item.model_dump(mode="json") for item in validated.relationship_changes or []
    ]
    if raw_changes is not None and raw_changes != normalized_changes:
        return "normalized"
    raw_role = out.get("relationship_role_event")
    normalized_role = (
        validated.relationship_role_event.model_dump(mode="json")
        if validated.relationship_role_event is not None
        else None
    )
    if raw_role is not None and raw_role != normalized_role:
        return "normalized"
    return "explicit"


def _make_t1_sense_llm(
    client: LlmClient,
    db: KindredDB | None = None,
    *,
    recent_ticks_limit: int = 5,
    activities_dir: Path = ACTIVITIES_DIR,
    soul_excerpt_path: Path = DEFAULT_SOUL_EXCERPT_PATH,
    soul_full_path: Path = DEFAULT_SOUL_FULL_PATH,
    identity_path: Path = DEFAULT_IDENTITY_PATH,
    user_path: Path = DEFAULT_USER_PATH,
    highlights_path: Path = DEFAULT_BUNDLE_HIGHLIGHTS_PATH,
    prompt_dumper: PromptDumper | None = None,
    relationship_reader: RelationshipReader | None = None,
) -> Callable[[TickState], NodeReturn]:
    """绑 client (+ 可选 db / activities_dir / L2 路径) 返回 closure（LangGraph add_node 接口）。"""

    def t1_sense_llm_closure(state: TickState) -> NodeReturn:
        return _t1_sense_llm_impl(
            client,
            state,
            db=db,
            recent_ticks_limit=recent_ticks_limit,
            activities_dir=activities_dir,
            soul_excerpt_path=soul_excerpt_path,
            soul_full_path=soul_full_path,
            identity_path=identity_path,
            user_path=user_path,
            highlights_path=highlights_path,
            prompt_dumper=prompt_dumper,
            relationship_reader=relationship_reader,
        )

    return t1_sense_llm_closure


def _read_relationship_profile(
    reader: RelationshipReader | None, *, node: str
) -> RelationshipProfile | None:
    if reader is None:
        return None
    from kindred.relationship.preflight import (
        RelationshipPreflightError,
        require_user_relationship,
    )

    try:
        return require_user_relationship(reader)
    except RelationshipPreflightError as exc:
        raise SenseLlmContractError(f"{node}: {exc}") from None


# ─────────────────────────────────────────────────────────────────────
# 顶层 mock（保留兼容 build.py 默认装配 / 拓扑测试）
# ─────────────────────────────────────────────────────────────────────


def t1_sense_llm(state: TickState) -> NodeReturn:
    """T1 第 3 节点：mock 透传 + 默认 act_decision（仅当调用方未注入时）。

    默认 dict 走 ``ActDecision.model_validate`` 可过（含 ``reason``），保拓扑
    测试 + cli --mock 仍跑。真实路径：``make_sense_llm_node(client)`` 注入
    client 走真实现，详 ``_t1_sense_llm_impl`` docstring。
    """
    if "act_decision" not in state:
        return {
            "act_decision": {
                "act": False,
                "kind": None,
                "target_activity": None,
                "reason": "mock: T1.sense.llm pass-through default",
            },
        }
    return {}


__all__ = [
    "SenseLlmContractError",
    "make_sense_llm_node",
    "t1_sense_llm",
]
