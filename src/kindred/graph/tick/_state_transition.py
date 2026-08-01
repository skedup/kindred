"""T2 final_state_diff 到 working state 的原子状态变换。"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from kindred.activity import ActivitySkillError, load_activity_skill
from kindred.activity.action import load_atomic_action
from kindred.activity.skill import MAGNITUDE_RANGE, StateEffect
from kindred.graph.tick._act_contract import (
    ActLlmContractError,
    safe_state_path,
    safe_validation_error_paths,
)
from kindred.graph.tick._effective_action import SETTLE_STEP
from kindred.life_assets import ACTIONS_DIR, ACTIVITIES_DIR
from kindred.state.interior import Affect, Needs
from kindred.state.outward import Activity, Bag, Embodiment, Presence

logger = logging.getLogger(__name__)

_LAYER_KEYS: dict[str, frozenset[str]] = {
    "needs": frozenset(Needs.model_fields),
    "affect": frozenset(Affect.model_fields),
}
_DIRECTIONS = frozenset({"up", "down"})
_CONTEXT_DELTA = 6
_T2_MERGE_LAYERS: dict[str, type[BaseModel]] = {
    "embodiment": Embodiment,
    "bag": Bag,
}
_NODE_OWNED_TOP_LEVEL_FIELDS = frozenset({"interior", "location", "time", "environment"})
_ACTIVITY_FLESH_FIELDS: tuple[str, ...] = ("desc", "engagement", "for_what")
_ACTIVITY_HEART_FIELDS = frozenset((*_ACTIVITY_FLESH_FIELDS, "with_whom"))
_ACTIVITY_NODE_OWNED_FIELDS = frozenset({"name", "started_at", "context", "step"})


@dataclass(frozen=True)
class StateTransitionResult:
    """一次完整状态变换的结果；只在全部校验成功后构造。"""

    next_state: dict[str, Any]
    touched_keys: frozenset[str]
    current_state: str | None
    activity_touched: bool
    with_whom_explicit: bool


@dataclass(frozen=True)
class StateTransitionApplier:
    """在私有副本上应用 LLM state diff，不持有 F1 committed 决策。"""

    target: Any
    kind: Any
    activities_dir: Path = ACTIVITIES_DIR
    actions_dir: Path = ACTIONS_DIR

    def apply(
        self,
        next_state: dict[str, Any],
        final_state_diff: Any,
        *,
        effective_current_state: str | None,
        affect_event_touched: Any = None,
    ) -> StateTransitionResult:
        """应用一次完整变换；失败时调用方输入保持不变。"""
        if not isinstance(final_state_diff, dict):
            raise ActLlmContractError(
                "T2.act.llm: out['final_state_diff'] must be dict, got "
                f"{type(final_state_diff).__name__}",
            )

        working = deepcopy(next_state)
        diff = deepcopy(final_state_diff)
        diff.pop("current_state", None)
        needs_diff = diff.pop("needs", None)
        affect_diff = diff.pop("affect", None)
        presence_diff = diff.pop("presence", None)

        current_state = effective_current_state

        activity_diff = diff.pop("activity", None)
        with_whom_explicit = isinstance(activity_diff, dict) and "with_whom" in activity_diff
        interior_touched = _apply_host_owned_interior(
            working,
            target=self.target,
            kind=self.kind,
            current_state=current_state,
            needs_diff=needs_diff,
            affect_diff=affect_diff,
            affect_event_touched=_validate_affect_event_touched(affect_event_touched),
            activities_dir=self.activities_dir,
            actions_dir=self.actions_dir,
        )

        merged_top_keys: set[str] = set()
        if _apply_presence_diff(working, presence_diff):
            merged_top_keys.add("presence")
        for key, value in diff.items():
            model = _T2_MERGE_LAYERS.get(key)
            if model is None:
                reason = (
                    "node_owned" if key in _NODE_OWNED_TOP_LEVEL_FIELDS else "unknown_top_level"
                )
                _warn_state_diff_rejected(path=key, reason=reason)
                continue
            if _merge_validated_layer(working, layer=key, layer_diff=value, model=model):
                merged_top_keys.add(key)

        activity_touched = _apply_activity(
            working,
            kind=self.kind,
            target=self.target,
            activity_diff=activity_diff,
            current_state=current_state,
        )
        touched = set(merged_top_keys)
        if interior_touched:
            touched.add("interior")
        if activity_touched:
            touched.add("activity")
        return StateTransitionResult(
            next_state=working,
            touched_keys=frozenset(touched),
            current_state=current_state,
            activity_touched=activity_touched,
            with_whom_explicit=with_whom_explicit,
        )


def _warn_state_diff_rejected(*, path: object, reason: str, actual_type: str | None = None) -> None:
    """记录稳定、可聚合且不含字段原值的 rejection。"""
    safe_path = safe_state_path(path)
    if actual_type is None:
        logger.warning("T2.act.llm state_diff_rejected path=%s reason=%s", safe_path, reason)
        return
    logger.warning(
        "T2.act.llm state_diff_rejected path=%s reason=%s actual_type=%s",
        safe_path,
        reason,
        actual_type,
    )


def _merge_validated_layer(
    next_state: dict[str, Any],
    *,
    layer: str,
    layer_diff: Any,
    model: type[BaseModel],
) -> bool:
    """字段级合并一个 T2 所有层；成功校验后才写回 working state。"""
    if layer_diff is None:
        return False
    if not isinstance(layer_diff, dict):
        raise ActLlmContractError(
            f"T2.act.llm: final_state_diff.{layer} 必须是 dict 或 null，"
            f"got {type(layer_diff).__name__}",
        )
    if not layer_diff:
        return False
    existing = next_state.get(layer)
    if not isinstance(existing, dict):
        raise ActLlmContractError(
            f"T2.act.llm: next_state.{layer} 必须是 dict，got {type(existing).__name__}",
        )
    merged = deepcopy(existing)
    merged.update(layer_diff)
    try:
        validated = model.model_validate(merged, strict=True).model_dump()
    except ValidationError as exc:
        raise ActLlmContractError(
            f"T2.act.llm: {layer} 不符合 {model.__name__} schema（fail-fast），"
            f"invalid_paths={safe_validation_error_paths(exc)}",
        ) from None
    next_state[layer] = validated
    return True


def _apply_presence_diff(next_state: dict[str, Any], layer_diff: Any) -> bool:
    """只允许 T2 合并 ``presence.others``；物理在场事实由节点维护。"""
    if layer_diff is None:
        return False
    if not isinstance(layer_diff, dict):
        raise ActLlmContractError(
            "T2.act.llm: final_state_diff.presence 必须是 dict 或 null，"
            f"got {type(layer_diff).__name__}",
        )
    allowed_diff = deepcopy(layer_diff)
    if "user_present" in allowed_diff:
        allowed_diff.pop("user_present")
        _warn_state_diff_rejected(path="presence.user_present", reason="node_owned")
    return _merge_validated_layer(
        next_state,
        layer="presence",
        layer_diff=allowed_diff,
        model=Presence,
    )


def _apply_activity(
    next_state: dict[str, Any],
    *,
    kind: Any,
    target: Any,
    activity_diff: Any,
    current_state: str | None,
) -> bool:
    if activity_diff is None:
        diff: dict[str, Any] = {}
    elif isinstance(activity_diff, dict):
        diff = activity_diff
    else:
        raise ActLlmContractError(
            "T2.act.llm: final_state_diff.activity 必须是 dict 或 null，got "
            f"{type(activity_diff).__name__}",
        )

    def _validated(activity: dict[str, Any]) -> dict[str, Any]:
        try:
            return Activity.model_validate(activity, strict=True).model_dump()
        except ValidationError as exc:
            raise ActLlmContractError(
                "T2.act.llm: activity 不符合 Activity schema（fail-fast），"
                f"invalid_paths={safe_validation_error_paths(exc)}",
            ) from None

    if kind == "start_activity":
        if not isinstance(target, str) or not target.strip():
            raise ActLlmContractError(
                "T2.act.llm: start_activity 缺合法 target_activity（骨架 name 来源）",
            )
        time_layer = next_state.get("time")
        started_at = time_layer.get("iso") if isinstance(time_layer, dict) else None
        if not isinstance(started_at, str) or not started_at.strip():
            raise ActLlmContractError(
                "T2.act.llm: start_activity 拿不到 next_state.time.iso（骨架 "
                "started_at 来源）——T1 sense 必须先填 time",
            )
        unknown_fields = set(diff) - _ACTIVITY_HEART_FIELDS - _ACTIVITY_NODE_OWNED_FIELDS
        if unknown_fields:
            raise ActLlmContractError(
                "T2.act.llm: final_state_diff.activity 包含未知字段，"
                f"invalid_paths={sorted(f'activity.{key}' for key in unknown_fields)}",
            )
        for field_name in sorted(_ACTIVITY_NODE_OWNED_FIELDS.intersection(diff)):
            _warn_state_diff_rejected(path=f"activity.{field_name}", reason="node_owned")

        activity: dict[str, Any] = {"name": target, "started_at": started_at}
        for field_name in _ACTIVITY_FLESH_FIELDS:
            if field_name not in diff:
                raise ActLlmContractError(
                    "T2.act.llm: start_activity 时 final_state_diff.activity 缺血肉字段 "
                    f"{field_name!r}（desc/engagement/for_what 由心给）",
                )
            activity[field_name] = diff[field_name]
        if "with_whom" in diff:
            activity["with_whom"] = diff["with_whom"]
        if current_state is not None:
            activity["step"] = current_state
        next_state["activity"] = _validated(activity)
        return True

    existing = next_state.get("activity")
    if not isinstance(existing, dict):
        if diff or current_state is not None:
            logger.warning(
                "T2.act.llm: kind=%r 但 next_state 无现有 activity 可更新（上游在无 "
                "activity 时不应决定 %r），降级跳过。",
                kind,
                kind,
            )
        return False
    touched = False
    for key, value in diff.items():
        if key in _ACTIVITY_NODE_OWNED_FIELDS:
            _warn_state_diff_rejected(path=f"activity.{key}", reason="node_owned")
            continue
        existing[key] = value
        touched = True
    if current_state is not None:
        existing["step"] = current_state
        touched = True
    if touched:
        next_state["activity"] = _validated(existing)
    return touched


def project_arrival_presence(
    next_state: dict[str, Any],
    *,
    with_whom_explicit: bool,
    arrival_applied: bool,
) -> bool:
    """按已应用的到达事实与本拍显式同行决定派生物理离场。"""
    if not arrival_applied or not with_whom_explicit:
        return False
    presence = next_state.get("presence")
    if not isinstance(presence, dict) or presence.get("user_present") is not True:
        return False
    activity = next_state.get("activity")
    if not isinstance(activity, dict):
        return False
    with_whom = activity.get("with_whom")
    if not isinstance(with_whom, list) or "user" in with_whom:
        return False
    presence["user_present"] = False
    return True


def _apply_host_owned_interior(
    next_state: dict[str, Any],
    *,
    target: Any,
    kind: Any,
    current_state: Any,
    needs_diff: Any,
    affect_diff: Any,
    affect_event_touched: frozenset[str],
    activities_dir: Path,
    actions_dir: Path,
) -> bool:
    """先落 Action 确定性 effect，再落最多两项情境方向。"""
    interior = next_state.get("interior")
    if not isinstance(interior, dict):
        raise ActLlmContractError(
            f"T2.act.llm: next_state.interior 必须是 dict，got {type(interior).__name__}",
        )

    contextual = {
        "needs": _validate_contextual_directions("needs", needs_diff),
        "affect": _validate_contextual_directions("affect", affect_diff),
    }
    contextual_keys = {key for layer_diff in contextual.values() for key in layer_diff}
    if len(contextual_keys) > 2:
        raise ActLlmContractError(
            "T2.act.llm: needs/affect 情境调整合计最多两项，invalid_paths=['needs','affect']"
        )
    for key in sorted(affect_event_touched.intersection(contextual["affect"])):
        contextual["affect"].pop(key)
        _warn_state_diff_rejected(
            path=f"affect.{key}",
            reason="partner_event_already_applied",
        )
    contextual_keys = {key for layer_diff in contextual.values() for key in layer_diff}

    state_effects: dict[str, StateEffect] = {}
    if kind in {"start_activity", "advance_activity"}:
        if not isinstance(target, str) or not target.strip():
            raise ActLlmContractError("T2.act.llm: committed start/advance 缺合法 target_activity")
        if current_state is None and not contextual_keys:
            # 生产 start/advance 在进入本对象前已由 resolve_effective_action 强制解析；
            # None 仅供本模块的非 Action 层投影测试，不代表 committed proposal 可省略 step。
            return False
        if not isinstance(current_state, str) or not current_state.strip():
            raise ActLlmContractError("T2.act.llm: committed start/advance 缺合法 current_state")
        try:
            skill = load_activity_skill(
                target,
                activities_dir=activities_dir,
                actions_dir=actions_dir,
            )
            uses = [use for use in skill.uses if use.action == current_state]
            if len(uses) != 1:
                raise ActLlmContractError(
                    "T2.act.llm: current_state 必须唯一匹配当前 activity uses，"
                    "invalid_paths=['current_state']"
                )
            action = load_atomic_action(current_state, actions_dir=actions_dir)
        except ActivitySkillError:
            raise ActLlmContractError(
                "T2.act.llm: 无法解析当前 activity/action package，"
                "invalid_paths=['target_activity','current_state']"
            ) from None
        state_effects = dict(action.state_effects)
        state_effects.update(uses[0].state_effects)
    elif kind != "end_activity":
        raise ActLlmContractError("T2.act.llm: committed proposal 的 kind 非法")

    overlap = contextual_keys.intersection(state_effects)
    if overlap:
        raise ActLlmContractError(
            "T2.act.llm: 情境调整不得重复当前 Action effect，"
            f"invalid_paths={sorted(_effect_path(key) for key in overlap)}"
        )

    for key, effect in state_effects.items():
        _apply_delta(interior, key, effect.direction, _magnitude_midpoint(effect.magnitude))
    for layer, layer_diff in contextual.items():
        for key, direction in layer_diff.items():
            _apply_delta(interior, key, direction, _CONTEXT_DELTA, expected_layer=layer)

    logger.debug(
        "T2.act.llm state_authority action_effect_keys=%s contextual_keys=%s",
        sorted(state_effects),
        sorted(contextual_keys),
    )
    return bool(state_effects or contextual_keys)


def _validate_affect_event_touched(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or any(
        type(key) is not str or key not in _LAYER_KEYS["affect"] for key in value
    ):
        raise ActLlmContractError("T2.act.llm: affect_event_touched shape invalid")
    return frozenset(value)


def _validate_contextual_directions(layer: str, layer_diff: Any) -> dict[str, str]:
    if layer_diff is None:
        return {}
    if not isinstance(layer_diff, dict):
        raise ActLlmContractError(
            f"T2.act.llm: final_state_diff.{layer} 必须是 dict 或 null，"
            f"got {type(layer_diff).__name__}"
        )
    allowed = _LAYER_KEYS[layer]
    normalized: dict[str, str] = {}
    for key, direction in layer_diff.items():
        if key not in allowed:
            raise ActLlmContractError(
                f"T2.act.llm: final_state_diff.{layer} 包含未知字段，"
                f"invalid_paths=['{safe_state_path(f'{layer}.{key}')}']"
            )
        if not isinstance(direction, str) or direction not in _DIRECTIONS:
            raise ActLlmContractError(
                f"T2.act.llm: final_state_diff.{layer}.{key} 只接受 strict up/down，"
                f"got {type(direction).__name__}"
            )
        normalized[key] = direction
    return normalized


def _apply_delta(
    interior: dict[str, Any],
    key: str,
    direction: str,
    delta: int,
    *,
    expected_layer: str | None = None,
) -> None:
    layer = expected_layer or ("needs" if key in _LAYER_KEYS["needs"] else "affect")
    bucket = interior.get(layer)
    if not isinstance(bucket, dict):
        raise ActLlmContractError(
            f"T2.act.llm: next_state.interior.{layer} 必须是 dict，got {type(bucket).__name__}"
        )
    current = bucket.get(key)
    if not isinstance(current, int) or isinstance(current, bool):
        raise ActLlmContractError(
            f"T2.act.llm: next_state.interior.{layer}.{key} 必须是 int，"
            f"got {type(current).__name__}"
        )
    signed_delta = delta if direction == "up" else -delta
    bucket[key] = max(0, min(100, current + signed_delta))


def _magnitude_midpoint(magnitude: str) -> int:
    low, high = MAGNITUDE_RANGE[magnitude]
    return (low + high) // 2


def _effect_path(key: str) -> str:
    layer = "needs" if key in _LAYER_KEYS["needs"] else "affect"
    return f"{layer}.{key}"


__all__ = [
    "SETTLE_STEP",
    "StateTransitionApplier",
    "StateTransitionResult",
    "project_arrival_presence",
]
