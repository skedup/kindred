"""Public deterministic checks for life continuity and derived-state behavior."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from kindred.activity.skill import load_activity_skill
from kindred.graph.tick.sense_llm import (
    _render_recent_life_texture,
    _should_render_current_activity,
    _t1_sense_llm_impl,
)
from kindred.life_assets import ACTIVITIES_DIR
from kindred.llm.mock import MockLlmClient
from kindred.state._derive import (
    ComfortInputs,
    apply_cascade_thresholds,
    decay_interior,
    rederive_layer1,
)
from kindred.state._seed import make_doc_example_state
from kindred.state.interior import Interior


def test_settled_activity_leaves_prompt_after_three_quiet_ticks() -> None:
    state = make_doc_example_state().model_dump()
    state["activity"].update({"name": "dine_out", "step": "settle"})
    activity = state["activity"]
    quiet = {
        "activity": dict(activity),
        "act_decision": {"act": False},
    }

    assert _should_render_current_activity(state, [quiet, quiet]) is True
    assert _should_render_current_activity(state, [quiet, quiet, quiet]) is False


def test_recent_life_texture_excludes_the_current_run_and_preserves_order() -> None:
    current = {
        "name": "rest",
        "started_at": "2026-08-10T12:00:00+08:00",
        "step": "sleep",
    }
    rows = [
        {"activity": dict(current)},
        {"activity": {"name": "dine_out", "started_at": "run-2"}},
        {"activity": {"name": "create_picture", "started_at": "run-1"}},
    ]

    rendered = _render_recent_life_texture(rows, current_activity=current)

    assert "rest" not in rendered
    assert rendered.endswith("create_picture → dine_out")


def test_arousal_reaches_its_baseline_without_oscillation() -> None:
    original = make_doc_example_state().interior
    interior = original.model_copy(
        update={"affect": original.affect.model_copy(update={"arousal": 30})}
    )
    current = datetime.fromisoformat("2026-08-10T10:00:00+08:00")
    values = [interior.affect.arousal]

    for _ in range(60):
        next_time = current + timedelta(minutes=5)
        interior = decay_interior(
            interior,
            prev_iso=current.isoformat(),
            curr_iso=next_time.isoformat(),
            comfort_inputs=ComfortInputs(),
            arousal_baseline=40,
        )
        current = next_time
        values.append(interior.affect.arousal)
        if values[-1] == 40:
            break

    assert values[-1] == 40
    assert values == sorted(values)


def test_high_arousal_focus_cascade_is_idempotent_and_not_user_bound() -> None:
    original = make_doc_example_state().interior
    interior = original.model_copy(
        update={"affect": original.affect.model_copy(update={"arousal": 85, "focus": 50})}
    )
    once = apply_cascade_thresholds(
        interior,
        curr_iso="2026-08-10T10:00:00+08:00",
    )
    twice = apply_cascade_thresholds(
        once,
        curr_iso="2026-08-10T10:01:00+08:00",
    )

    assert once.affect.focus == twice.affect.focus == 35
    bodily = [thought for thought in twice.thoughts if thought.tag == "cascade_arousal_high"]
    assert len(bodily) == 1
    assert all(marker not in bodily[0].description for marker in ("user", "主人", "你"))


def test_public_life_assets_keep_choice_and_reach_semantics() -> None:
    eat = load_activity_skill("eat_at_home", activities_dir=ACTIVITIES_DIR)
    reach = load_activity_skill("reach_out_to_user", activities_dir=ACTIVITIES_DIR)

    actions = [use.action for use in eat.uses]
    assert {"prepare_food", "eat"} <= set(actions)
    assert actions.index("prepare_food") < actions.index("eat")
    assert "具体" in reach.method
    assert "沉默" in reach.method


def test_sense_consumer_rederives_final_layer1_from_host_owned_state() -> None:
    state = make_doc_example_state(ts="2026-08-08T12:00:00+08:00").model_dump()
    next_state = deepcopy(state)
    next_state["interior"]["body"] = {"value": 1, "description": "stale"}
    next_state["interior"]["mood"] = {"value": 1, "description": "stale"}
    next_state["interior"]["inner_pulse"] = {"value": 1, "description": "stale"}
    tick: dict[str, Any] = {
        "trigger_source": "heartbeat",
        "triggered_at": state["time"]["iso"],
        "prev_state": deepcopy(state),
        "next_state": next_state,
    }

    patch = _t1_sense_llm_impl(
        MockLlmClient(scenario="start_activity"),
        tick,  # type: ignore[arg-type]
    )
    interior = Interior.model_validate(patch["next_state"]["interior"])
    expected = rederive_layer1(interior)

    assert interior.body == expected.body
    assert interior.mood == expected.mood
    assert interior.inner_pulse == expected.inner_pulse
    assert interior.body.description != "stale"
    assert interior.mood.description != "stale"
    assert interior.inner_pulse.description != "stale"
    assert any(thought.description == "肚子饿了" for thought in interior.thoughts)
