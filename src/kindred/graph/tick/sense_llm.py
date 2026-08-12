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
``_make_t1_sense_llm`` → ``_t1_sense_llm_impl`` → ``_render_sense_prompt``，
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from kindred.activity import (
    ActivitySkill,
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
from kindred.graph.tick._possession_narrative import render_current_possession_facts
from kindred.graph.tick._thought_diff import apply_thought_diff
from kindred.life_assets import ACTIVITIES_DIR
from kindred.llm.schemas import SenseResponse
from kindred.llm.templates import render_prompt
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
from kindred.state._types import is_safe_name
from kindred.state.state import State
from kindred.state.tick import RecentContactContext, TickState

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


# sense user prompt 模板（共享加载器，llm/templates.py）
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
    "feels_like",
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
    prompt = _render_sense_prompt(
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


def _render_sense_prompt(
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
) -> str:
    """渲染 sense.llm 的 user prompt（jinja2 五段，docs/14 §3.3）。

    五段：人格（SOUL）+ 高光（bundle）+ 当下 state（mood/needs/affect/念头）
    + 最近对话 + 轨迹 + 可选活动 + 任务收尾。模板：``llm/prompts/sense_user.md.j2``。

    L2 节点本地 read（read 完渲染进 prompt 即丢，不进 TickState）：

    - ``recent_ticks``（L2 docs §1.4）：最近 N 条连续 tick，给心「刚经历了什么」
      的连续感。None / 空 → 轨迹段「（无历史）」。
    - ``soul_excerpt_path`` / ``soul_full_path``：SOUL 人格摘录；cold_start
      fallback 读完整 SOUL（``soul_full_path``）。缺失 → 软心空串，人格段不渲染。
    - ``highlights_path``：bundle 高光闪回（dream 沉淀产出）。早期缺失 → 软心空串，
      高光段不渲染。
    - ``activities_dir``：注入「可选活动清单」（名 + description）。target_activity
      闭集约束从「自由发挥」收成「只能从注册名选」；空注册表 → 「（暂无可选活动）」，
      此时心只能 act=false。单个 SKILL 解析失败 → 只列名。

    L2 文件路径均为 keyword 参数（上层贯穿），真实 graph/CLI 可显式指定 life root。
    """
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

    if chat_projection is None:
        chat_projection = _project_chat_window(
            state.get("chat_window"),
            triggered_at=_as_str_or_none(state.get("triggered_at")),
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
    activities_line = _render_activity_choices(activities_dir)
    show_current_activity = _should_render_current_activity(next_state, recent_ticks or [])
    current_activity_line = (
        _render_current_activity(next_state, activities_dir) if show_current_activity else ""
    )
    settled_activity_exit_line = (
        "上一程已经结束并从当前生活内容中退场；现在要做候选清单里的另一件事时，"
        "使用 start_activity，否则 act=false。"
        if not show_current_activity
        else ""
    )
    # L2 节点本地 read——read 完渲染进 prompt 即丢，不进 TickState。
    soul_excerpt = _read_soul_excerpt(
        is_cold_start=is_cold_start,
        excerpt_path=soul_excerpt_path,
        full_path=soul_full_path,
    )
    identity = _read_soul_file(identity_path)
    user = _read_soul_file(user_path)
    bundle_highlights = _read_bundle_highlights(path=highlights_path)
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
        soul_excerpt=soul_excerpt,
        identity=identity,
        user=user,
        bundle_highlights=bundle_highlights,
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
    source = "对方" if actor == "partner" else "你"
    age_text = "刚刚" if age < 60 else f"距今约 {age // 60} 分钟"
    return f"【最近的联系】\n- 你们刚有过联系，最近一次表达来自{source}，{age_text}。"


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
    if field in {"temperature", "feels_like"} and isinstance(value, (int, float)):
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
    activities_dir: Path,
) -> str:
    """把已注册 activity 名渲染成「可选活动清单」。

    枚举 ``activities_dir`` 下所有已注册 activity 名（``list_registered_activities``），
    多行列出供心选 target_activity。空注册表 → 降级为「（暂无可选活动）」——
    此时配合 prompt schema「act=true 必填 target」，心只能 act=false（无可动作）。

    把 target 从「自由发挥」收成「闭集选择」，防止模型返回注册表外的活动名
    被降级成幽灵活动。
    """
    names = list_registered_activities(activities_dir=activities_dir)
    if not names:
        return "（暂无可选活动）"
    lines: list[str] = []
    for n in names:
        # 名后挂 description（让心知道每个活动是干嘛的，挑得更准）。
        # 单个 SKILL 解析失败不该拖垮整张清单——降级只列名（软心）。
        try:
            desc = load_activity_skill(n, activities_dir=activities_dir).description
            lines.append(f"  - {n}：{desc}")
        except (ActivitySkillError, OSError) as exc:
            _LOG.warning(
                "activity_choice_load_failed activity=%s activities_dir=%s error_type=%s",
                n,
                activities_dir,
                type(exc).__name__,
            )
            lines.append(f"  - {n}")
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


def _render_current_activity(next_state: dict[str, Any], activities_dir: Path) -> str:
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
    activity = next_state.get("activity") if isinstance(next_state, dict) else None
    if not isinstance(activity, dict) or not activity.get("name"):
        return "（上一拍你没有进行中的活动——可以 start 一个新活动）"
    name = activity.get("name")
    step = activity.get("step")
    desc = activity.get("desc") or ""
    engagement = activity.get("engagement")
    dest_names = _destination_plan_names(activity)
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
        try:
            skill = load_activity_skill(name, activities_dir=activities_dir)
        except ActivitySkillError:
            return line  # SKILL 缺失/坏 → 降级：只渲 name/step（运行期数据问题，不崩）
        if skill.terminal_when:
            # terminal_when 是多行 YAML（`|`）——逐行补缩进，别让内部换行顶格
            # 脱离「上一拍状态」块（原来只第一行有缩进，后续行顶格会被弱模型
            # 误读成 prompt 顶层段落，与当前活动解绑）。
            tw_indented = "\n".join("    " + ln for ln in skill.terminal_when.strip().splitlines())
            line += f"\n  何时该收（terminal_when，达成就 end_activity）：\n{tw_indented}"
        if _step_is_before_action(skill, step, _SEND_ACTION_NAME):
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
