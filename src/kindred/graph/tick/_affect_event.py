"""T1 当拍事件 Affect delta 的 Host-owned 投影。"""

from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from kindred.graph.tick._act_contract import safe_validation_error_paths
from kindred.state._derive import derive_body, derive_inner_pulse
from kindred.state.state import State


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
    response: dict[str, int],
) -> tuple[dict[str, Any], frozenset[str]]:
    """在私有副本应用已校验 signed delta，并只重派 body/inner_pulse。"""
    working = _validate_state(deepcopy(next_state))
    interior = working.interior
    updates = {}
    touched = set()
    for key, delta in response.items():
        before = getattr(interior.affect, key)
        after = max(0, min(100, before + delta))
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
