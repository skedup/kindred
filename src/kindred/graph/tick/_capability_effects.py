"""T2 act 的 Host artifact 与不可撤 side-effect session。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kindred.activity import ActivitySkillError, load_activity_skill
from kindred.activity.tool_binding import (
    action_names_declaring_capability,
    step_is_before_any_action,
)
from kindred.capability_host.artifacts import ArtifactStore, ArtifactStoreError, StagedArtifact
from kindred.capability_host.facts import current_activity_artifacts
from kindred.capability_host.internal import HostToolResult
from kindred.capability_host.messaging import SEND_DELIVERY_FACT_KIND
from kindred_capability_sdk import SideEffectFact, ToolCall, ToolResult

if TYPE_CHECKING:
    from kindred.db.facade import KindredDB

logger = logging.getLogger(__name__)

SEND_CAPABILITY_NAME = "send"
TOOL_SEND_TO_USER = "send_to_user"
OUTBOUND_PROFILE = "kindred.compose.outbound.v1"


@dataclass
class CapabilityEffectSession:
    """一个 tick 内可撤 Artifact 与不可撤发送事实的 Host 边界。"""

    target: Any
    current_step: Any
    kind: str | None
    next_state: dict[str, Any]
    activities_dir: Path
    actions_dir: Path
    db: KindredDB | None = None
    artifact_store: ArtifactStore | None = None
    staged_artifacts: list[StagedArtifact] = field(default_factory=list, init=False)
    send_action_names: tuple[str, ...] = field(default=(), init=False)
    delivered_before: bool = field(default=False, init=False)
    step_before_send: bool = field(default=False, init=False)
    _delivered_artifact_ref: str | None = field(default=None, init=False, repr=False)
    _unknown_before_artifact_ref: str | None = field(default=None, init=False, repr=False)
    _unknown_artifact_ref: str | None = field(default=None, init=False, repr=False)
    _delivery_fact: SideEffectFact | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.send_action_names = _send_action_names(
            self.target,
            activities_dir=self.activities_dir,
            actions_dir=self.actions_dir,
        )
        existing_artifacts = (
            []
            if self.kind == "start_activity"
            else current_activity_artifacts(
                self.next_state,
                self.db,
                activity_name=self.target if isinstance(self.target, str) else None,
            )
        )
        delivered = None
        unknown = None
        for item in existing_artifacts:
            if item.get("profile") != OUTBOUND_PROFILE:
                continue
            if item.get("status") == "delivered":
                delivered = item["artifact_ref"]
            elif item.get("status") == "attempted_unknown":
                unknown = item["artifact_ref"]
        self.delivered_before = delivered is not None
        self._delivered_artifact_ref = delivered
        self._unknown_before_artifact_ref = unknown
        self.step_before_send = _step_is_before_actions(
            self.target,
            self.current_step,
            self.send_action_names,
            activities_dir=self.activities_dir,
            actions_dir=self.actions_dir,
        )

    def consume(self, result: HostToolResult, *, call: ToolCall) -> ToolResult:
        """收集 Portable staged Artifact 与安全 side-effect fact。"""
        tool_result = result.tool_result
        if result.side_effect_facts and not tool_result.is_error:
            for fact in result.side_effect_facts:
                if self._delivery_fact is not None:
                    tool_result = _effect_error(call, "SideEffectBudgetExceeded")
                    break
                if not _valid_delivery_fact(fact):
                    tool_result = _effect_error(call, "UnsupportedSideEffectFact")
                    break
                self._delivery_fact = fact
                self._delivered_artifact_ref = str(fact.data["artifact_ref"])
        if tool_result.is_error:
            if (
                call.name == TOOL_SEND_TO_USER
                and tool_result.response.get("error_type") == "UnknownSideEffect"
            ):
                artifact_ref = call.args.get("artifact_ref")
                if isinstance(artifact_ref, str) and artifact_ref:
                    self._unknown_artifact_ref = artifact_ref
            self._discard_artifacts(result.staged_artifacts)
            return tool_result
        if result.staged_artifacts:
            if self.artifact_store is None:
                self._discard_artifacts(result.staged_artifacts)
                return _effect_error(call, "ArtifactStoreUnavailable")
            if len(result.staged_artifacts) != 1 or self.staged_artifacts:
                self._discard_artifacts(result.staged_artifacts)
                return _effect_error(call, "ArtifactBudgetExceeded")
            self.staged_artifacts.extend(result.staged_artifacts)
        return tool_result

    def commit(self, tool_trace: list[Any]) -> list[dict[str, str]]:
        """提交本拍 staged Artifact，并返回 Host-owned descriptors。"""
        del tool_trace
        if not self.staged_artifacts:
            return []
        if self.artifact_store is None:
            raise ArtifactStoreError("Host artifact store is unavailable")
        artifacts = []
        for staged in self.staged_artifacts:
            descriptor = self.artifact_store.commit(staged)
            artifacts.append(
                {
                    "artifact_ref": descriptor.artifact_ref,
                    "producer": descriptor.producer,
                    "profile": descriptor.profile,
                }
            )
        self.staged_artifacts.clear()
        return artifacts

    def discard(self) -> None:
        self._discard_artifacts(tuple(self.staged_artifacts))
        self.staged_artifacts.clear()

    def _discard_artifacts(self, staged_artifacts: tuple[StagedArtifact, ...]) -> None:
        if self.artifact_store is None:
            return
        for staged in staged_artifacts:
            self.artifact_store.discard(staged)

    def has_successful_send(self, tool_trace: Any) -> bool:
        del tool_trace
        return self._delivery_fact is not None

    def has_irreversible_send_attempt(self) -> bool:
        """本拍已经确认发送，或已进入结果未知的不可重放边界。"""
        return self._delivery_fact is not None or self._unknown_artifact_ref is not None

    def outbound_delivery(self, tool_trace: list[Any]) -> dict[str, Any] | None:
        del tool_trace
        if not self.send_action_names:
            return None
        delivered_this_tick = self._delivery_fact is not None
        delivered = delivered_this_tick or self.delivered_before
        if delivered_this_tick:
            evidence = "current_tick_send_to_user_ok"
            artifact_ref = self._delivered_artifact_ref
        elif self.delivered_before:
            evidence = "recent_send_to_user_ok"
            artifact_ref = self._delivered_artifact_ref
        elif self._unknown_artifact_ref is not None:
            evidence = "current_tick_send_to_user_unknown"
            artifact_ref = self._unknown_artifact_ref
        elif self._unknown_before_artifact_ref is not None:
            evidence = "recent_send_to_user_unknown"
            artifact_ref = self._unknown_before_artifact_ref
        else:
            evidence = "no_successful_send_to_user_trace"
            artifact_ref = None
        return {
            "tool": TOOL_SEND_TO_USER,
            "delivered": delivered,
            "evidence": evidence,
            "current_step": self.current_step if isinstance(self.current_step, str) else None,
            "artifact_ref": artifact_ref,
        }


def _valid_delivery_fact(fact: SideEffectFact) -> bool:
    return (
        fact.kind == SEND_DELIVERY_FACT_KIND
        and fact.data.get("delivered") is True
        and isinstance(fact.data.get("artifact_ref"), str)
        and bool(fact.data["artifact_ref"])
    )


def _effect_error(call: ToolCall, error_type: str) -> ToolResult:
    return ToolResult.error(
        call,
        error_type=error_type,
        message="capability effect was rejected by act kernel",
    ).with_trace(
        args={"arg_keys": sorted(map(str, call.args))},
        response={"ok": False, "error_type": error_type},
    )


def _send_action_names(
    target: Any,
    *,
    activities_dir: Path,
    actions_dir: Path,
) -> tuple[str, ...]:
    if not isinstance(target, str) or not target.strip():
        return ()
    return action_names_declaring_capability(
        target,
        SEND_CAPABILITY_NAME,
        activities_dir=activities_dir,
        actions_dir=actions_dir,
    )


def _step_is_before_actions(
    target: Any,
    current_step: Any,
    action_names: tuple[str, ...],
    *,
    activities_dir: Path,
    actions_dir: Path,
) -> bool:
    if not isinstance(target, str) or not target.strip():
        return False
    if not isinstance(current_step, str) or not current_step.strip():
        return False
    try:
        skill = load_activity_skill(target, activities_dir=activities_dir, actions_dir=actions_dir)
    except ActivitySkillError:
        return False
    return step_is_before_any_action(skill, current_step, action_names)


__all__ = ["CapabilityEffectSession"]
