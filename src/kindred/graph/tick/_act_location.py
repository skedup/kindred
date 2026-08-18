"""T2.act.llm 的地点状态机 session。

本模块承载 T2.act.llm 的地点 prompt 与状态机 session（第五趴 §7 步骤 5~10）：

- binding 已有跨 tick 目的地计划（``activity.context.destinations``）→ 渲染计划，
  **不再调 Provider**（消掉 earlier review 遗留的「advance 每拍重查」）。
- binding 未满足 → prompt 只渲染 ``find_places(binding_id=...)`` 查询入口；
  候选由 read-only tool 的结构化结果承载，同一 tick 内按 binding 缓存。
- ``end_activity`` → 不发起新查询；已有计划仅作收尾上下文渲染。

候选不进入 ``TickState``，也不直接写 ``state.location``——地点选择/到达/放弃
由 choose_destination / arrive / abandon staged tools 表达；session 只暂存事件，最终是否
应用仍由 act orchestrator 的 F1 committed 边界决定。
"""

from __future__ import annotations

import logging
import math
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from kindred.activity import ActivitySkillError, load_activity_skill
from kindred.activity.skill import ActivitySkill, LocationBinding
from kindred.character_card import CHARACTER_CARD_PLACE_SOURCE
from kindred.graph.tick._act_contract import ActLlmContractError
from kindred.llm.schemas import (
    DestinationAbandonedEvent,
    DestinationChoiceEvent,
    LocationArrivalEvent,
)
from kindred.location.candidates import LocationCandidateResolver
from kindred.location.context import (
    as_text,
    binding_accepts_home,
    current_home_satisfied_bindings,
    current_location_satisfied_bindings,
    destination_plans_from_state,
    location_origin_from_state,
)
from kindred.state._types import is_safe_name
from kindred.state.outward import Activity, Location
from kindred_capability_sdk import ToolCall, ToolDef, ToolResult

if TYPE_CHECKING:
    from kindred.character_card import HomeProfile

logger = logging.getLogger(__name__)

_HOME_SOURCE = CHARACTER_CARD_PLACE_SOURCE
_TOOL_CHOOSE_DESTINATION = "choose_destination"
_TOOL_ARRIVE = "arrive"
_TOOL_ABANDON = "abandon"

LOCATION_KERNEL_TOOL_DEFS: tuple[ToolDef, ...] = (
    ToolDef(
        name=_TOOL_CHOOSE_DESTINATION,
        description=(
            "Stage a destination plan using only a candidate_ref returned by find_places. "
            "The Host restores the canonical place identity. Choosing records a cross-tick "
            "plan and never means the character has arrived."
        ),
        effect="staged_state_event",
        allow_before_action_lock=True,
        parameters={
            "type": "OBJECT",
            "properties": {"candidate_ref": {"type": "STRING"}},
            "required": ["candidate_ref"],
        },
    ),
    ToolDef(
        name=_TOOL_ARRIVE,
        description=(
            "Stage a real arrival at a planned place, or at a same-tick candidate when "
            "candidate_ref is supplied. This is the only location-changing place event. "
            "No separate arrival signal will appear: use chosen_at, Host-derived "
            "en_route_minutes, available distance_km, and the current context without a fixed "
            "minute threshold. Do not call while still en route."
        ),
        effect="staged_state_event",
        parameters={
            "type": "OBJECT",
            "properties": {
                "binding_id": {"type": "STRING"},
                "candidate_ref": {"type": "STRING"},
            },
            "required": ["binding_id"],
        },
    ),
    ToolDef(
        name=_TOOL_ABANDON,
        description=(
            "Stage abandoning an existing destination plan for one binding when the character "
            "has genuinely stopped going or turned back; keep the final activity description "
            "consistent with that fact."
        ),
        effect="staged_state_event",
        parameters={
            "type": "OBJECT",
            "properties": {
                "binding_id": {"type": "STRING"},
                "reason": {"type": "STRING", "nullable": True},
            },
            "required": ["binding_id"],
        },
    ),
)


@dataclass
class LocationApplyResult:
    """一次 committed 地点事件应用的结果与审计出口。"""

    activity_touched: bool = False
    location_touched: bool = False
    environment_touched: bool = False
    choice_record: dict[str, Any] | None = None
    abandoned_record: dict[str, Any] | None = None
    arrival_record: dict[str, Any] | None = None


@dataclass
class _StagedLocationEvents:
    choice: DestinationChoiceEvent | None = None
    abandoned: DestinationAbandonedEvent | None = None
    arrival: LocationArrivalEvent | None = None


@dataclass
class LocationKernelSession:
    """一个 tick 内地点状态机的完整运行上下文。"""

    kind: str | None
    skill: ActivitySkill
    next_state: dict[str, Any]
    home: HomeProfile | None
    resolver: LocationCandidateResolver
    prompt_now_iso: str | None = None
    _staged: _StagedLocationEvents = field(default_factory=_StagedLocationEvents, init=False)

    @classmethod
    def create(
        cls,
        target: str | None,
        next_state: dict[str, Any],
        *,
        kind: str | None,
        activities_dir: Path,
        actions_dir: Path,
        resolver: LocationCandidateResolver,
        home: HomeProfile | None = None,
        prompt_now_iso: str | None = None,
    ) -> LocationKernelSession | None:
        """按 effective activity 创建 session；无有效 binding 时不创建空壳。"""
        if not target:
            return None
        try:
            skill = load_activity_skill(
                target,
                activities_dir=activities_dir,
                actions_dir=actions_dir,
            )
        except ActivitySkillError as exc:
            logger.warning(
                "T2.act.llm: 地点 session 无法加载 activity %r，按无地点降级：%s", target, exc
            )
            return None
        if not skill.location_bindings:
            return None
        return cls(
            kind=kind,
            skill=skill,
            next_state=next_state,
            home=home,
            resolver=resolver,
            prompt_now_iso=prompt_now_iso,
        )

    @property
    def tool_defs(self) -> tuple[ToolDef, ...]:
        """收尾拍只清计划，不暴露任何地点事件工具。"""
        return () if self.kind == "end_activity" else LOCATION_KERNEL_TOOL_DEFS

    def handles(self, tool_name: str) -> bool:
        return any(tool.name == tool_name for tool in LOCATION_KERNEL_TOOL_DEFS)

    def binding_id_for_call(self, call: ToolCall) -> str | None:
        """Return a valid model-selected binding for Action ownership checks."""
        if call.name == _TOOL_CHOOSE_DESTINATION:
            candidate_ref = call.args.get("candidate_ref")
            if not isinstance(candidate_ref, str) or not candidate_ref:
                return None
            try:
                candidate = self.resolver.resolve(candidate_ref)
            except KeyError:
                return None
            value = candidate.get("binding_id")
        else:
            value = call.args.get("binding_id")
        return value if isinstance(value, str) and value else None

    def prompt_section(self) -> str:
        plans = self.active_destination_plans()
        plan_lines = _render_destination_plans(
            plans,
            is_end=self.kind == "end_activity",
            now_iso=self.prompt_now_iso,
        )
        if self.kind == "end_activity":
            return "\n".join(plan_lines)

        if self.kind == "start_activity":
            satisfied = current_home_satisfied_bindings(
                self.next_state,
                self.skill,
                home=self.home,
            )
        else:
            satisfied = current_location_satisfied_bindings(self.next_state, self.skill)
        unmet = {
            binding_id: binding
            for binding_id, binding in self.skill.location_bindings.items()
            if binding_id not in plans and binding_id not in satisfied
        }
        lines = list(plan_lines)
        satisfied_lines = _render_current_location_satisfied_bindings(satisfied)
        if satisfied_lines:
            if lines:
                lines.append("")
            lines.extend(satisfied_lines)
        if unmet:
            if lines:
                lines.append("")
            lines.extend(_render_find_places_tool_section(unmet, self.next_state, home=self.home))
        return "\n".join(lines)

    def active_destination_plans(self) -> dict[str, dict[str, Any]]:
        if self.kind == "start_activity":
            return {}
        return destination_plans_from_state(self.next_state, self.skill)

    def en_route_destinations(self) -> list[str]:
        return [
            plan["name"].strip()
            for plan in self.active_destination_plans().values()
            if isinstance(plan.get("name"), str) and plan["name"].strip()
        ]

    def handle(self, call: ToolCall) -> ToolResult:
        """校验并暂存地点事件；不在工具环内改 canonical state。"""
        if self.kind == "end_activity":
            return _safe_location_error(call, "ToolUnavailableDuringEndActivity")
        if call.name == _TOOL_CHOOSE_DESTINATION:
            return self._handle_choose(call)
        if call.name == _TOOL_ARRIVE:
            return self._handle_arrive(call)
        if call.name == _TOOL_ABANDON and not set(call.args).issubset({"binding_id", "reason"}):
            return _safe_location_error(call, "InvalidArgs")
        event = _parse_location_tool_event(call)
        if isinstance(event, ToolResult):
            return event
        binding_error = self._validate_binding(call, event.binding_id)
        if binding_error is not None:
            return binding_error
        self._staged.abandoned = event
        response = {
            "ok": True,
            "staged_event": "destination_abandoned",
            "binding_id": event.binding_id,
        }
        return ToolResult.ok(
            call,
            response,
        ).with_trace(
            args=event.model_dump(exclude_unset=True),
            response=response,
        )

    def _handle_choose(self, call: ToolCall) -> ToolResult:
        if set(call.args) != {"candidate_ref"}:
            return _safe_location_error(call, "InvalidArgs")
        candidate = self._resolve_candidate(call, call.args.get("candidate_ref"))
        if isinstance(candidate, ToolResult):
            return candidate
        try:
            event = DestinationChoiceEvent.model_validate(candidate)
        except ValidationError:
            return _safe_location_error(call, "InvalidCandidate")
        binding_error = self._validate_binding(call, event.binding_id)
        if binding_error is not None:
            return binding_error
        self._staged.choice = event
        return _place_event_result(
            call,
            event,
            "destination_choice",
            "plan",
            _preview_destination_plan(event, self.next_state, self.home),
        )

    def _handle_arrive(self, call: ToolCall) -> ToolResult:
        if not set(call.args).issubset({"binding_id", "candidate_ref"}):
            return _safe_location_error(call, "InvalidArgs")
        binding_id = call.args.get("binding_id")
        if not isinstance(binding_id, str) or not binding_id.strip():
            return _safe_location_error(call, "InvalidArgs")
        binding_id = binding_id.strip()
        binding_error = self._validate_binding(call, binding_id)
        if binding_error is not None:
            return binding_error

        candidate_ref = call.args.get("candidate_ref")
        if candidate_ref is not None:
            resolved = self._resolve_candidate(call, candidate_ref)
            if isinstance(resolved, ToolResult):
                return resolved
            if resolved.get("binding_id") != binding_id:
                return _safe_location_error(call, "CandidateBindingMismatch")
            candidate = resolved
        else:
            arrival_source = self._arrival_source(binding_id)
            if arrival_source is None:
                return _safe_location_error(call, "MissingArrivalSource")
            candidate = arrival_source
        try:
            event = LocationArrivalEvent.model_validate(_event_payload(candidate))
        except ValidationError:
            return _safe_location_error(call, "InvalidCandidate")
        self._staged.arrival = event
        return _place_event_result(
            call,
            event,
            "location_arrival",
            "location",
            _preview_arrival_location(event, self.next_state, self.home),
        )

    def _resolve_candidate(self, call: ToolCall, value: Any) -> dict[str, Any] | ToolResult:
        if not isinstance(value, str) or not value:
            return _safe_location_error(call, "InvalidCandidateRef")
        try:
            return self.resolver.resolve(value)
        except KeyError:
            return _safe_location_error(call, "InvalidCandidateRef")

    def _arrival_source(self, binding_id: str) -> dict[str, Any] | None:
        choice = self._staged.choice
        if choice is not None and choice.binding_id == binding_id:
            return choice.model_dump()
        plan = self.active_destination_plans().get(binding_id)
        if not isinstance(plan, dict):
            return None
        return {**_event_payload(plan), "binding_id": binding_id}

    def apply(self, next_state: dict[str, Any]) -> LocationApplyResult:
        """在私有副本应用暂存事件，全部成功后才回填节点 working state。"""
        self._validate_staged()
        working_state = deepcopy(next_state)
        result = _apply_location_events(
            working_state,
            kind=self.kind,
            skill=self.skill,
            choice=self._staged.choice,
            abandoned=self._staged.abandoned,
            arrival=self._staged.arrival,
            home=self.home,
        )
        for layer, touched in (
            ("activity", result.activity_touched),
            ("location", result.location_touched),
            ("environment", result.environment_touched),
        ):
            if touched:
                next_state[layer] = working_state[layer]
        return result

    def _validate_binding(self, call: ToolCall, binding_id: str) -> ToolResult | None:
        if not is_safe_name(binding_id):
            return _safe_location_error(call, "InvalidBinding")
        if binding_id not in self.skill.location_bindings:
            return _safe_location_error(call, "InvalidBinding")
        return None

    def _validate_staged(self) -> None:
        choice = self._staged.choice
        abandoned = self._staged.abandoned
        arrival = self._staged.arrival
        if (
            choice is not None
            and abandoned is not None
            and choice.binding_id == abandoned.binding_id
        ):
            raise ActLlmContractError(
                "choose_destination 与 abandon 不能指向同一 binding_id"
                "（同 tick 对同一目的地又选又弃自相矛盾）"
            )
        if (
            abandoned is not None
            and arrival is not None
            and abandoned.binding_id == arrival.binding_id
        ):
            raise ActLlmContractError(
                "abandon 与 arrive 不能指向同一 binding_id（同 tick 对同一目的地又弃又到自相矛盾）"
            )


# ─────────────────────────────────────────────────────────────────────
# 进行中目的地计划（第五趴 §7 步骤 6：计划在手不重查）
# ─────────────────────────────────────────────────────────────────────


def _render_destination_plans(
    plans: dict[str, dict[str, Any]],
    *,
    is_end: bool,
    now_iso: str | None,
) -> list[str]:
    if not plans:
        return []
    lines = ["## 目的地计划（你此前已选中，跨 tick 持续）"]
    if is_end:
        lines.append(
            "活动正在收尾：下面的计划会随收尾自动清除。没去成就在 desc 里如实收束"
            "——不要调用 arrive 工具假装到达。"
        )
    else:
        lines.append(
            "这是进行中的计划，不是新候选，不必重选。真实到达时只调用 "
            "arrive(binding_id=...)，地点事实由代码从本计划恢复；"
            "改主意不去了调用 abandon 工具；还在路上就不调用地点事件工具，保持当前"
            "移动动作继续走（在路上不算原地踏步）。"
        )
    for binding_id, plan in plans.items():
        name = as_text(plan.get("name")) or "（未命名）"
        if is_end:
            lines.append(f"- 「{name}」")
            continue
        facts = [f"place_key={as_text(plan.get('place_key')) or '（缺）'}"]
        plan_type = as_text(plan.get("type"))
        if plan_type:
            facts.append(f"type={plan_type}")
        plan_city = as_text(plan.get("city"))
        if plan_city:
            facts.append(f"city={plan_city}")
        facts.append(f"source={as_text(plan.get('source')) or '（缺）'}")
        chosen_at = as_text(plan.get("chosen_at"))
        if chosen_at:
            facts.append(f"chosen_at={chosen_at}")
            elapsed = _elapsed_minutes(chosen_at, now_iso)
            if elapsed is not None:
                facts.append(f"en_route_minutes={elapsed}")
        snapshot = plan.get("candidate_snapshot")
        if isinstance(snapshot, dict):
            distance_km = snapshot.get("distance_km")
            if (
                isinstance(distance_km, (int, float))
                and not isinstance(distance_km, bool)
                and math.isfinite(float(distance_km))
            ):
                if distance_km >= 0:
                    facts.append(f"distance_km={distance_km:g}")
        lines.append(f"- binding {binding_id}: 「{name}」（{'；'.join(facts)}）")
        address = as_text(plan.get("address"))
        if address:
            lines.append(f"  address={address}")
    return lines


def _elapsed_minutes(chosen_at: str, now_iso: str | None) -> int | None:
    if not now_iso:
        return None
    try:
        elapsed_seconds = (
            datetime.fromisoformat(now_iso) - datetime.fromisoformat(chosen_at)
        ).total_seconds()
    except (TypeError, ValueError):
        return None
    return int(elapsed_seconds // 60) if elapsed_seconds >= 0 else None


# ─────────────────────────────────────────────────────────────────────
# find_places 查询入口渲染（未满足 binding；第五趴 §7 步骤 7）
# ─────────────────────────────────────────────────────────────────────


def _render_find_places_tool_section(
    bindings: dict[str, LocationBinding],
    next_state: dict[str, Any],
    *,
    home: HomeProfile | None = None,
) -> list[str]:
    origin = location_origin_from_state(next_state, home=home)
    lines = [
        "## 地点查询工具（find_places）",
        "下面这些 binding 还没有目的地计划。需要真实候选时，先调用 "
        "find_places(binding_id=...)；不要自己编地点、距离或身份。工具返回的 "
        "places[].candidate_ref 可直接用于 choose_destination(candidate_ref=...)；"
        "若本拍已经真实到达，可调用 arrive(binding_id=..., candidate_ref=...)。",
        "如果 find_places 返回 places 为空或 status=no_places，就说明本 tick 没有可落地"
        "目的地：不要 choose_destination/arrive，不要把到店、坐下、吃上等写进 desc；"
        "继续寻找、如实收束或 committed=false。",
        (
            "origin="
            f"name={origin.name or '（空）'}；"
            f"address={origin.address or '（空）'}；"
            f"city={origin.city or '（空）'}"
        ),
    ]
    for binding_id, binding in bindings.items():
        facts = [
            f"query={binding.query}",
            f"categories={','.join(binding.categories) if binding.categories else 'any'}",
        ]
        if binding.radius_min_km is not None:
            facts.append(f"radius_min_km={binding.radius_min_km:g}")
        if binding.radius_max_km is not None:
            facts.append(f"radius_max_km={binding.radius_max_km:g}")
        if binding.limit is not None:
            facts.append(f"limit={binding.limit}")
        if binding_accepts_home(binding):
            facts.append("includes_character_card_home=true")
        lines.append(f"- binding {binding_id}: {'；'.join(facts)}")
    return lines


def _render_current_location_satisfied_bindings(
    satisfied: dict[str, dict[str, Any]],
) -> list[str]:
    if not satisfied:
        return []
    lines = [
        "## 当前地点已满足地点绑定",
        "下面这些 binding 已由当前 state.location 满足：不要重新调用 find_places，"
        "不要重新 choose_destination，也不要再次 arrive。继续推进到使用该地点的动作即可。",
    ]
    for binding_id, location in satisfied.items():
        facts = [f"type={location['type']}", f"arrived_at={location['arrived_at']}"]
        city = as_text(location.get("city"))
        if city:
            facts.append(f"city={city}")
        lines.append(f"- binding {binding_id}: 当前在「{location['name']}」（{'；'.join(facts)}）")
        address = as_text(location.get("address"))
        if address:
            lines.append(f"  address={address}")
    return lines


def _parse_location_tool_event(
    call: ToolCall,
) -> DestinationAbandonedEvent | ToolResult:
    try:
        if call.name == _TOOL_ABANDON:
            return DestinationAbandonedEvent.model_validate(call.args)
    except ValidationError:
        return _safe_location_error(call, "InvalidArgs")
    return _safe_location_error(call, "UnknownTool")


def _event_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(record[key]) for key in LocationArrivalEvent.model_fields if key in record
    }


def _safe_location_error(
    call: ToolCall,
    error_type: str,
) -> ToolResult:
    return ToolResult.error(
        call, error_type=error_type, message="location tool request rejected"
    ).with_trace(
        args={"arg_keys": sorted(call.args)},
        response={"ok": False, "error_type": error_type},
    )


def _place_event_result(
    call: ToolCall,
    event: DestinationChoiceEvent | LocationArrivalEvent,
    event_name: str,
    detail_name: str,
    detail: dict[str, Any],
) -> ToolResult:
    response = {"ok": True, "staged_event": event_name}
    return ToolResult.ok(
        call,
        {**response, "binding_id": event.binding_id, "name": event.name},
    ).with_trace(
        args=event.model_dump(),
        response={**response, detail_name: detail},
    )


def _preview_destination_plan(
    event: DestinationChoiceEvent,
    next_state: dict[str, Any],
    home: HomeProfile | None,
) -> dict[str, Any]:
    record = _normalize_home_place_event(event.model_dump(), home)
    return {
        "status": "planned",
        "place_key": record["place_key"],
        "name": record["name"],
        "address": record.get("address"),
        "city": record.get("city"),
        "type": record.get("type"),
        "chosen_at": _time_iso_or_none(next_state),
        "source": record["source"],
        "candidate_snapshot": dict(record.get("candidate_snapshot") or {}),
    }


def _preview_arrival_location(
    event: LocationArrivalEvent,
    next_state: dict[str, Any],
    home: HomeProfile | None,
) -> dict[str, Any]:
    record = _normalize_home_place_event(event.model_dump(), home)
    return {
        "name": record["name"],
        "address": record.get("address"),
        "city": record.get("city"),
        "type": record.get("type"),
        "arrived_at": _time_iso_or_none(next_state),
    }


def _time_iso_or_none(next_state: dict[str, Any]) -> str | None:
    time_layer = next_state.get("time")
    value = time_layer.get("iso") if isinstance(time_layer, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _apply_location_events(
    next_state: dict[str, Any],
    *,
    kind: str | None,
    skill: ActivitySkill,
    choice: DestinationChoiceEvent | None,
    abandoned: DestinationAbandonedEvent | None,
    arrival: LocationArrivalEvent | None,
    home: HomeProfile | None,
) -> LocationApplyResult:
    """从 committed 地点事件派生目的地计划、当前位置与环境城市。"""
    has_events = choice is not None or abandoned is not None or arrival is not None
    if not has_events and kind != "end_activity":
        return LocationApplyResult()

    activity = next_state.get("activity")
    if not isinstance(activity, dict):
        if has_events:
            logger.warning(
                "T2.act.llm: 有地点事件但 next_state 无现有 activity 可挂 context，跳过。"
            )
        return LocationApplyResult()

    for event_name, binding_id in (
        ("destination_choice", choice.binding_id if choice else None),
        ("destination_abandoned", abandoned.binding_id if abandoned else None),
        ("location_arrival", arrival.binding_id if arrival else None),
    ):
        if binding_id is None:
            continue
        if not is_safe_name(binding_id):
            raise ActLlmContractError(
                f"T2.act.llm: {event_name}.binding_id={binding_id!r} 非法"
                "（须匹配 ^[a-z][a-z0-9_]*$，拒空白、/ 与 ..）"
            )
        if binding_id not in skill.location_bindings:
            raise ActLlmContractError(
                f"T2.act.llm: {event_name}.binding_id={binding_id!r} 不在 activity "
                f"{skill.name!r} 声明的 location_bindings "
                f"{sorted(skill.location_bindings)} 里（心给了本活动未声明的地点绑定）"
            )

    now_iso = _time_iso_or_none(next_state)
    if (choice is not None or arrival is not None) and now_iso is None:
        raise ActLlmContractError(
            "T2.act.llm: 地点事件拿不到 next_state.time.iso（chosen_at/arrived_at "
            "来源）——T1 sense 必须先填 time"
        )

    context = activity.get("context")
    destinations: dict[str, Any] = {}
    if isinstance(context, dict) and isinstance(context.get("destinations"), dict):
        destinations = deepcopy(context["destinations"])
    original = deepcopy(destinations)

    choice_record: dict[str, Any] | None = None
    if choice is not None:
        choice_record = _normalize_home_place_event(choice.model_dump(), home)
        destinations[choice.binding_id] = {
            "status": "planned",
            "place_key": choice_record["place_key"],
            "name": choice_record["name"],
            "address": choice_record.get("address"),
            "city": choice_record.get("city"),
            "type": choice_record.get("type"),
            "chosen_at": now_iso,
            "source": choice_record["source"],
            "candidate_snapshot": dict(choice_record.get("candidate_snapshot") or {}),
        }

    if abandoned is not None and destinations.pop(abandoned.binding_id, None) is None:
        logger.warning(
            "T2.act.llm: destination_abandoned binding=%r 但无对应计划（no-op）。",
            abandoned.binding_id,
        )

    location_touched = False
    environment_touched = False
    arrival_record: dict[str, Any] | None = None
    if arrival is not None:
        plan = destinations.pop(arrival.binding_id, None)
        arrival_record = _normalize_home_place_event(arrival.model_dump(), home)
        if plan is not None and plan.get("place_key") == arrival.place_key:
            arrival_record.update(
                name=plan.get("name") or arrival.name,
                address=plan.get("address"),
                city=plan.get("city"),
                type=plan.get("type"),
                source=plan.get("source") or arrival.source,
                candidate_snapshot=deepcopy(plan.get("candidate_snapshot") or {}),
            )
            arrival_record = _normalize_home_place_event(arrival_record, home)
        elif plan is not None:
            logger.warning(
                "T2.act.llm: location_arrival.place_key=%r 与计划 place_key=%r 不一致，"
                "以事件为准并关闭计划。",
                arrival.place_key,
                plan.get("place_key"),
            )
        try:
            next_state["location"] = Location(
                name=arrival_record["name"],
                address=arrival_record["address"],
                city=arrival_record.get("city"),
                type=arrival_record["type"],
                arrived_at=str(now_iso),
            ).model_dump()
        except ValidationError as exc:
            raise ActLlmContractError(
                f"T2.act.llm: location_arrival 不符合 Location schema（fail-fast）：{exc}"
            ) from exc
        location_touched = True
        city = arrival_record.get("city")
        if isinstance(city, str) and city.strip():
            environment_touched = _sync_environment_city(next_state, city)

    if kind == "end_activity" and destinations:
        logger.debug("T2.act.llm: end_activity 清除 %d 条未完成目的地计划。", len(destinations))
        destinations = {}

    activity_touched = destinations != original
    if activity_touched:
        activity["context"] = {"destinations": destinations}
        try:
            next_state["activity"] = Activity.model_validate(activity).model_dump()
        except ValidationError as exc:
            raise ActLlmContractError(
                f"T2.act.llm: 地点事件派生后 activity 不符合 Activity schema（fail-fast）：{exc}"
            ) from exc
    return LocationApplyResult(
        activity_touched=activity_touched,
        location_touched=location_touched,
        environment_touched=environment_touched,
        choice_record=choice_record,
        abandoned_record=abandoned.model_dump() if abandoned is not None else None,
        arrival_record=arrival_record,
    )


def _normalize_home_place_event(
    record: dict[str, Any],
    home: HomeProfile | None,
) -> dict[str, Any]:
    if home is None or record.get("place_key") != home.place_key:
        return record
    normalized = dict(record)
    normalized.update(
        name=home.name,
        address=home.address,
        city=home.city,
        type="home",
        source=_HOME_SOURCE,
    )
    snapshot = dict(normalized.get("candidate_snapshot") or {})
    snapshot.setdefault("known", "fixed_home")
    normalized["candidate_snapshot"] = snapshot
    return normalized


def _sync_environment_city(next_state: dict[str, Any], city: str) -> bool:
    env = next_state.get("environment")
    if not isinstance(env, dict):
        return False
    city = city.strip()
    if not city or env.get("city") == city:
        return False
    next_state["environment"] = {**env, "city": city}
    return True


def apply_location_commit(
    session: LocationKernelSession | None,
    next_state: dict[str, Any],
    *,
    kind: str | None,
) -> LocationApplyResult:
    """应用一次 committed 地点变更，并兼容清理无 session 的历史计划。

    ``session=None`` 仍表示当前 activity 没有地点状态机；但历史 state 可能来自旧版
    package。活动收尾时必须无条件清掉遗留 destinations，不能让计划越过 settle。
    """
    if session is not None:
        return session.apply(next_state)
    if kind != "end_activity":
        return LocationApplyResult()
    return _clear_destination_plans(next_state)


def _clear_destination_plans(next_state: dict[str, Any]) -> LocationApplyResult:
    activity = next_state.get("activity")
    if not isinstance(activity, dict):
        return LocationApplyResult()
    context = activity.get("context")
    destinations = context.get("destinations") if isinstance(context, dict) else None
    if not isinstance(destinations, dict) or not destinations:
        return LocationApplyResult()

    cleaned = deepcopy(activity)
    cleaned["context"] = {"destinations": {}}
    try:
        validated = Activity.model_validate(cleaned).model_dump()
    except ValidationError as exc:
        raise ActLlmContractError(
            f"T2.act.llm: end_activity 清理地点计划后 activity 不符合 schema（fail-fast）：{exc}"
        ) from exc
    next_state["activity"] = validated
    logger.debug("T2.act.llm: end_activity 清除 %d 条历史目的地计划。", len(destinations))
    return LocationApplyResult(activity_touched=True)


__all__ = [
    "LOCATION_KERNEL_TOOL_DEFS",
    "LocationApplyResult",
    "LocationKernelSession",
    "apply_location_commit",
]
