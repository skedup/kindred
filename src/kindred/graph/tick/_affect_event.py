"""T1 partner 事件 Affect 方向的 Host-owned 投影。"""

from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from kindred.activity.skill import MAGNITUDE_RANGE
from kindred.graph.tick._act_contract import safe_validation_error_paths
from kindred.state._derive import derive_body, derive_inner_pulse
from kindred.state.state import State

_EVENT_DELTA = sum(MAGNITUDE_RANGE["small"]) // 2


class AffectEventProjectionError(ValueError):
    pass


def _validate_state(value: object) -> State:
    try:
        return State.model_validate(value)
    except ValidationError as exc:
        raise AffectEventProjectionError(
            f"invalid_paths={safe_validation_error_paths(exc)}"
        ) from None


def apply_affect_event_response(
    next_state: dict[str, Any],
    response: dict[str, str],
) -> tuple[dict[str, Any], frozenset[str]]:
    """在私有副本应用 fixed-small delta，并只重派 body/inner_pulse。"""
    working = _validate_state(deepcopy(next_state))
    interior = working.interior
    updates = {}
    touched = set()
    for key, direction in response.items():
        before = getattr(interior.affect, key)
        after = max(0, min(100, before + (_EVENT_DELTA if direction == "up" else -_EVENT_DELTA)))
        updates[key] = after
        if after != before:
            touched.add(key)
    if touched:
        affect = interior.affect.model_copy(update=updates)
        interior = interior.model_copy(
            update={
                "affect": affect,
                "body": derive_body(interior.needs, affect),
                "inner_pulse": derive_inner_pulse(interior.needs, affect),
            }
        )
        working = working.model_copy(update={"interior": interior})
    return _validate_state(working.model_dump()).model_dump(), frozenset(touched)
