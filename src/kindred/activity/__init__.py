"""Activity SKILL 模块（docs/15）。"""

from __future__ import annotations

from kindred.activity.action import (
    AtomicAction,
    PlaceSlot,
    is_valid_action_name,
    list_registered_actions,
    load_atomic_action,
    resolve_step_effects,
)
from kindred.activity.skill import (
    MAGNITUDE_REFERENCE,
    ActivitySkill,
    ActivitySkillError,
    ActivityUse,
    LocationBinding,
    StateEffect,
    is_valid_activity_name,
    list_registered_activities,
    load_activity_skill,
)

__all__ = [
    "MAGNITUDE_REFERENCE",
    "ActivitySkill",
    "ActivitySkillError",
    "ActivityUse",
    "AtomicAction",
    "LocationBinding",
    "PlaceSlot",
    "StateEffect",
    "is_valid_action_name",
    "is_valid_activity_name",
    "list_registered_actions",
    "list_registered_activities",
    "load_activity_skill",
    "load_atomic_action",
    "resolve_step_effects",
]
