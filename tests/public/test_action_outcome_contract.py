"""Public contract for same-tick Action identity and Outcome resolution."""

from __future__ import annotations

from typing import Any

from kindred.graph.tick._act_outcome import (
    ACTION_OUTCOME_TOOL_DEFS,
    ActionOutcomeKernelSession,
)
from kindred.life_assets import ACTIONS_DIR, ACTIVITIES_DIR
from kindred_capability_sdk import ToolCall

_STARTED_AT = "2026-08-04T10:00:00+08:00"


def _session(*, kind: str = "start_activity", prior_step: str | None = None):
    current: dict[str, Any] = {}
    if kind == "advance_activity":
        current = {
            "name": "dine_out",
            "started_at": _STARTED_AT,
            "step": prior_step,
        }
    return ActionOutcomeKernelSession(
        kind=kind,
        activity_name="create_picture" if kind == "start_activity" else "dine_out",
        triggered_at="2026-08-04T10:10:00+08:00",
        current_activity=current,
        activities_dir=ACTIVITIES_DIR,
        actions_dir=ACTIONS_DIR,
    )


def _call(name: str, args: dict[str, Any], call_id: str) -> ToolCall:
    return ToolCall(name, args, call_id)


def test_action_entry_locks_once_and_resolves_one_safe_outcome() -> None:
    session = _session()

    locked = session.handle(_call("lock_action", {"action_step": "draw"}, "lock"))
    resolved = session.handle(_call("resolve_action_outcome", {}, "outcome"))

    assert [tool.name for tool in ACTION_OUTCOME_TOOL_DEFS] == [
        "lock_action",
        "resolve_action_outcome",
    ]
    assert all(tool.effect == "read_only" for tool in ACTION_OUTCOME_TOOL_DEFS)
    assert locked.response == {"status": "locked", "action_step": "draw", "entry": True}
    assert resolved.response == {"status": "resolved", "action_step": "draw", "grade": "C"}
    assert resolved.trace_response == {"status": "attempted"}
    assert "grade" not in str(resolved.trace_response)
    session.validate_committed({"current_state": "draw"})


def test_same_step_does_not_fabricate_a_second_outcome() -> None:
    session = _session(kind="advance_activity", prior_step="eat")

    locked = session.handle(_call("lock_action", {"action_step": "eat"}, "lock"))
    outcome = session.handle(_call("resolve_action_outcome", {}, "outcome"))

    assert locked.response["entry"] is False
    assert outcome.is_error is True
    assert outcome.response["error_type"] == "NotActionEntry"
    session.validate_committed({"current_state": "eat", "affect": {"stress": -3}})


def test_locked_action_controls_capability_and_location_binding() -> None:
    draw = _session()
    artifact = _call("draw_image", {"prompt": "synthetic scene"}, "draw")
    rejected = draw.guard_tool(artifact, owner="draw", effect="artifact_write")
    assert rejected is not None and rejected.response["error_type"] == "ActionLockRequired"

    draw.handle(_call("lock_action", {"action_step": "draw"}, "lock"))
    assert draw.guard_tool(artifact, owner="draw", effect="artifact_write") is None

    walk = _session(kind="advance_activity", prior_step="makeup")
    walk.handle(_call("lock_action", {"action_step": "walk"}, "lock"))
    assert (
        walk.guard_tool(
            _call("arrive", {"binding_id": "meal_place"}, "arrive"),
            owner="location_kernel",
            binding_id="meal_place",
        )
        is None
    )
    mismatch = walk.guard_tool(
        _call("arrive", {"binding_id": "other"}, "mismatch"),
        owner="location_kernel",
        binding_id="other",
    )
    assert mismatch is not None and mismatch.response["error_type"] == "ActionBindingMismatch"
