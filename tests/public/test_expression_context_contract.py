"""Public bounded projection contract for artifact-writing context."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from kindred.capability_host import (
    ArtifactStore,
    CapabilityRegistry,
    HostRuntime,
    HostUserMessenger,
    PortableAdapter,
)
from kindred.capability_host.facts import (
    ACTIVITY_CURRENT_FACT,
    ARTIFACT_EXPLICIT_REFS_FACT,
    build_current_activity,
    build_explicit_artifact_refs,
)
from kindred.config import load_kindred_config
from kindred.graph.tick._expression_context import ExpressionContext, project_expression_context
from kindred.graph.tick.act_llm import _t2_act_llm_impl
from kindred.llm.templates import render_prompt
from kindred.llm.tools import ToolLoopResult
from kindred.state._seed import make_doc_example_state
from kindred_capability_compose import create_capability as create_compose
from kindred_capability_sdk import SecretResolver, ToolDef
from kindred_capability_send import create_capability as create_send


class _NoSecrets(SecretResolver):
    def get(self, name: str) -> str | None:
        del name
        return None


class _CaptureClient:
    def __init__(self) -> None:
        self.prompt = ""
        self.tools: tuple[ToolDef, ...] = ()

    def complete(self, prompt: str, *, role: str) -> dict[str, Any]:
        raise AssertionError((prompt, role))

    def complete_with_tools(
        self,
        prompt: str,
        *,
        role: str,
        tools: tuple[ToolDef, ...],
        handler: object,
        **kwargs: object,
    ) -> ToolLoopResult:
        del handler, kwargs
        assert role == "act.llm"
        self.prompt, self.tools = prompt, tools
        return ToolLoopResult(
            final={"committed": False, "final_state_diff": {}, "failure_reason": "synthetic"},
            tool_events=(),
            rounds=1,
        )


def _host_runtime(tmp_path: Path) -> HostRuntime:
    config = load_kindred_config(env={})
    registry = CapabilityRegistry(config=config, declared_capability_names=config.capabilities)
    secrets = _NoSecrets()
    registry.register(PortableAdapter("compose", create_compose(settings={}, secrets=secrets)))
    registry.register(PortableAdapter("send", create_send(settings={}, secrets=secrets)))
    registry.freeze()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    return HostRuntime(
        registry=registry,
        config=config,
        fact_view_builders={
            ACTIVITY_CURRENT_FACT: build_current_activity,
            ARTIFACT_EXPLICIT_REFS_FACT: build_explicit_artifact_refs,
        },
        host_services={
            "artifact_writer": artifacts,
            "artifact_reader": artifacts,
            "user_messenger": HostUserMessenger(None),
        },
    )


def test_expression_projection_is_bounded_fresh_and_immutable() -> None:
    state = make_doc_example_state(ts="2026-08-08T12:00:00+08:00").model_dump()
    state["environment"].update(
        weather_cached_at=state["time"]["iso"],
        weather_cached_for="synthetic-city",
        weather="雨后晴",
        temperature=28.4,
        feels_like=30.2,
        precip_mm=0.6,
        ambience="潮湿的风吹过屋檐。" * 30,
    )
    before = deepcopy(state)

    context = project_expression_context(
        state,
        triggered_at=state["time"]["iso"],
        soul_excerpt="声线" * 200,
        sense_note="心声" * 100,
        decision_reason="缘由" * 100,
        weather_ttl_minutes=60,
        weather_location="synthetic-city",
    )

    assert state == before
    assert context == project_expression_context(
        state,
        triggered_at=state["time"]["iso"],
        soul_excerpt="声线" * 200,
        sense_note="心声" * 100,
        decision_reason="缘由" * 100,
        weather_ttl_minutes=60,
        weather_location="synthetic-city",
    )
    assert len(context.soul_excerpt) <= 200
    assert len(context.sense_note) <= 130
    assert len(context.decision_reason) <= 130
    assert len(context.ambience) <= 160
    assert any("雨后晴" in line for line in context.scene_lines)
    assert context.activity_line.startswith("- 当前经历：")


def test_expression_template_has_a_fixed_small_envelope() -> None:
    context = ExpressionContext(
        soul_excerpt="x" * 200,
        scene_lines=("x" * 150,),
        possession_lines=("x" * 240,),
        activity_line="x" * 160,
        sense_note="x" * 130,
        decision_reason="x" * 130,
        ambience="x" * 160,
    )

    rendered = render_prompt("expression_context.md.j2", **context.__dict__).strip()

    assert len(rendered) <= 1400
    assert "此刻的生活材料" in rendered


def test_act_writer_consumes_one_bounded_expression_context(tmp_path: Path) -> None:
    excerpt = tmp_path / "SOUL_excerpt.md"
    excerpt.write_text("坦率而敏锐，愿意承认自己真实的偏好。", encoding="utf-8")
    state = make_doc_example_state(ts="2026-08-08T12:00:00+08:00").model_dump()
    tick: dict[str, Any] = {
        "trigger_source": "heartbeat",
        "triggered_at": state["time"]["iso"],
        "prev_state": deepcopy(state),
        "next_state": deepcopy(state),
        "note": "屋檐还在滴水，街上亮起来了。",
        "act_decision": {
            "act": True,
            "kind": "start_activity",
            "target_activity": "reach_out_to_user",
            "reason": "雨停后很想分享空气里的气味",
        },
    }
    client = _CaptureClient()

    _t2_act_llm_impl(
        client,  # type: ignore[arg-type]
        tick,  # type: ignore[arg-type]
        host_runtime=_host_runtime(tmp_path),
        soul_excerpt_path=excerpt,
    )

    assert client.prompt.count("## 此刻的生活材料") == 1
    assert client.prompt.count(tick["note"]) == 1
    assert client.prompt.count(tick["act_decision"]["reason"]) == 1
    assert client.prompt.count(excerpt.read_text(encoding="utf-8")) == 1
    assert "## 这一拍的行动依据" not in client.prompt
    assert [tool.name for tool in client.tools][:2] == ["write_compose", "send_to_user"]
