"""Public-safe deterministic checks for subjective dynamics behavior."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from kindred.activity.action import load_atomic_action
from kindred.capability_host import CapabilityRegistry, HostRuntime
from kindred.config import load_kindred_config
from kindred.graph.tick._act_prompt import (
    ActPromptContext,
    OutboundFactContext,
    PresencePromptContext,
    load_activity_prompt_context,
    render_act_prompt,
)
from kindred.graph.tick._state_transition import StateTransitionApplier
from kindred.graph.tick.act_llm import _t2_act_llm_impl
from kindred.graph.tick.sense_llm import (
    _load_current_activity_context,
    _render_current_activity,
)
from kindred.life_assets import ACTIONS_DIR, ACTIVITIES_DIR
from kindred.llm.mock import MockLlmClient
from kindred.llm.real_client import act_tool_loop_system_prompt_for, system_prompt_for
from kindred.llm.tools import ToolLoopResult
from kindred.state._seed import make_doc_example_state


class _CaptureClient:
    def __init__(self) -> None:
        self.prompt = ""

    def complete(self, prompt: str, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("act must use the tool loop")

    def complete_with_tools(self, prompt: str, **_kwargs: Any) -> ToolLoopResult:
        self.prompt = prompt
        return ToolLoopResult(
            final={"committed": False, "final_state_diff": {}, "failure_reason": "fixture"},
            tool_events=(),
            rounds=1,
        )


def _state(*, step: str = "walk") -> dict[str, Any]:
    return {
        "interior": {
            "needs": {
                "hunger": 80,
                "energy": 70,
                "fatigue": 20,
                "comfort": 50,
                "social": 50,
                "stimulation": 50,
                "aesthetic": 50,
            },
            "affect": {"stress": 50, "focus": 50, "arousal": 20, "clarity": 50},
        },
        "activity": {
            "name": "dine_out",
            "desc": "在路上",
            "started_at": "2026-08-04T10:00:00+08:00",
            "engagement": 0.37,
            "with_whom": [],
            "for_what": "想吃一顿饭",
            "step": step,
            "context": None,
        },
        "time": {
            "iso": "2026-08-04T10:05:00+08:00",
            "phase": "morning",
            "weekday": "Tuesday",
        },
    }


def _apply(state: dict[str, Any], diff: dict[str, Any], *, kind: str = "advance_activity") -> Any:
    current = "settle" if kind == "end_activity" else diff.get("current_state")
    return StateTransitionApplier(target="dine_out", kind=kind).apply(
        state,
        diff,
        effective_current_state=current,
    )


def test_signed_delta_entry_same_step_and_end_cadence() -> None:
    source = _state()
    entered = _apply(source, {"current_state": "eat", "needs": {"hunger": -25}})

    assert entered.next_state["interior"]["needs"]["hunger"] == 55
    assert entered.next_state["interior"]["needs"]["comfort"] == 50
    assert entered.next_state["interior"]["needs"]["stimulation"] == 50
    before_same_step = deepcopy(entered.next_state["interior"])
    repeated = _apply(entered.next_state, {"current_state": "eat"})
    assert repeated.next_state["interior"] == before_same_step
    ended = _apply(repeated.next_state, {"affect": {"stress": -4}}, kind="end_activity")
    assert ended.next_state["interior"]["needs"] == repeated.next_state["interior"]["needs"]
    assert ended.next_state["interior"]["affect"]["stress"] == 46


def test_prompt_and_fixture_expose_engagement_and_signed_delta_contract() -> None:
    activity = load_activity_prompt_context(
        "dine_out", activities_dir=ACTIVITIES_DIR, actions_dir=ACTIONS_DIR
    )
    prompt = render_act_prompt(
        ActPromptContext(
            triggered_at="2026-08-04T10:05:00+08:00",
            kind="advance_activity",
            target="dine_out",
            current_step="walk",
            activity=activity,
            presence=PresencePromptContext(False, "dine_out", ()),
            location_section="",
            outbound=OutboundFactContext(False, False, False, "advance_activity"),
            en_route_destinations=(),
            current_engagement=0.37,
        )
    )
    sense_activity = _render_current_activity(
        _load_current_activity_context(_state(), activities_dir=ACTIVITIES_DIR)
    )
    sense_contract = system_prompt_for("sense.llm")
    act_contract = act_tool_loop_system_prompt_for(())
    mock_end = MockLlmClient(scenario="end_activity").complete("fixture", role="act.llm")

    assert prompt.count("engagement=0.37") == 1
    assert sense_activity.count("engagement：0.37") == 1
    assert "没有 partner 也可形成" in sense_contract
    assert "只接受 nonzero strict integer delta" in act_contract
    assert mock_end["final_state_diff"]["affect"] == {"clarity": 4}


def test_act_node_projects_current_engagement_into_its_prompt() -> None:
    current = make_doc_example_state().model_dump()
    current["activity"].update({"name": "dine_out", "step": "walk", "engagement": 0.37})
    config = load_kindred_config(env={})
    registry = CapabilityRegistry(config=config, declared_capability_names=config.capabilities)
    registry.freeze()
    client = _CaptureClient()

    _t2_act_llm_impl(
        client,
        {
            "triggered_at": current["time"]["iso"],
            "prev_state": deepcopy(current),
            "next_state": current,
            "act_decision": {
                "act": True,
                "kind": "advance_activity",
                "target_activity": "dine_out",
                "reason": "fixture",
            },
        },
        host_runtime=HostRuntime(registry, config),
    )

    assert client.prompt.count("engagement=0.37") == 1


def test_draw_declares_both_experience_effects() -> None:
    effects = load_atomic_action("draw").state_effects

    assert {
        ("aesthetic", "up", "large"),
        ("stimulation", "up", "small"),
    } <= {(key, value.direction, value.magnitude) for key, value in effects.items()}
