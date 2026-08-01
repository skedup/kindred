"""activity/action capability binding helpers。

这些 helper 只读取 activity 与 action manifest，用来回答“当前 activity 的哪些
action 绑定了某个 capability”。Capability 不应该硬编码 action 名；action 名是
具身动画与状态机节点，capability 才是外部能力契约。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kindred.activity.action import load_atomic_action
from kindred.activity.skill import ActivitySkill, ActivitySkillError, load_activity_skill


def action_names_declaring_capability(
    target: Any,
    capability_name: str,
    *,
    activities_dir: Path | None,
    actions_dir: Path | None,
) -> tuple[str, ...]:
    """返回当前 activity 中绑定了 ``capability_name`` 的 action 名。

    action manifest 加载失败时按“不绑定该 capability”降级。这里不打日志，调用方可以
    根据业务语境决定是否需要 warning。
    """

    if activities_dir is None or actions_dir is None:
        return ()
    skill = _load_activity(target, activities_dir, actions_dir)
    if skill is None:
        return ()
    result: list[str] = []
    for use in skill.uses:
        try:
            action = load_atomic_action(use.action, actions_dir=actions_dir)
        except ActivitySkillError:
            continue
        if capability_name in action.capabilities:
            result.append(use.action)
    return tuple(result)


def activity_declares_capability(
    target: Any,
    capability_name: str,
    *,
    activities_dir: Path | None,
    actions_dir: Path | None,
) -> bool:
    """当前 activity 是否至少有一个 action 绑定了 ``capability_name``。"""

    return bool(
        action_names_declaring_capability(
            target,
            capability_name,
            activities_dir=activities_dir,
            actions_dir=actions_dir,
        )
    )


def declared_capability_names_for_activity(
    target: Any,
    *,
    activities_dir: Path | None,
    actions_dir: Path | None,
) -> tuple[str, ...]:
    """返回当前 activity 通过所有 uses action 绑定的 capability 名集合。"""

    if activities_dir is None or actions_dir is None:
        return ()
    skill = _load_activity(target, activities_dir, actions_dir)
    if skill is None:
        return ()
    capabilities: list[str] = []
    for use in skill.uses:
        try:
            action = load_atomic_action(use.action, actions_dir=actions_dir)
        except ActivitySkillError:
            continue
        for capability_name in action.capabilities:
            if capability_name not in capabilities:
                capabilities.append(capability_name)
    return tuple(capabilities)


def step_is_before_any_action(
    skill: ActivitySkill,
    step: Any,
    action_names: Iterable[str],
) -> bool:
    """``step`` 是否位于给定任一 action 之前。"""

    if not isinstance(step, str) or not step.strip():
        return False
    targets = tuple(action_names)
    if not targets:
        return False
    steps = [use.action for use in skill.uses]
    if step not in steps:
        return False
    step_index = steps.index(step)
    return any(
        action_name in steps and step_index < steps.index(action_name) for action_name in targets
    )


def _load_activity(target: Any, activities_dir: Path, actions_dir: Path) -> ActivitySkill | None:
    if not isinstance(target, str) or not target.strip():
        return None
    try:
        return load_activity_skill(target, activities_dir=activities_dir, actions_dir=actions_dir)
    except ActivitySkillError:
        return None


__all__ = [
    "action_names_declaring_capability",
    "activity_declares_capability",
    "declared_capability_names_for_activity",
    "step_is_before_any_action",
]
