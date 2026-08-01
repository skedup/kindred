from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from kindred.capability_host.internal import HostTickContext, HostToolHandler, HostToolResult
from kindred_capability_sdk import (
    CapabilityContribution,
    CapabilityResult,
    SideEffectFact,
    ToolBinding,
    ToolCall,
    ToolDef,
    ToolResult,
    is_safe_name,
)

if TYPE_CHECKING:
    from kindred.capability_host.runtime import HostExecutionContext
    from kindred.config import KindredConfig

RegisteredHandler = Callable[[ToolCall, "HostExecutionContext"], HostToolResult]


@dataclass(frozen=True)
class RegisteredTool:
    capability_name: str
    tool_def: ToolDef
    handler: RegisteredHandler


@dataclass(frozen=True)
class InternalBinding:
    capability_name: str
    tool_defs: tuple[ToolDef, ...]
    handler: HostToolHandler

    def registered_tools(self) -> tuple[RegisteredTool, ...]:
        def handle(call: ToolCall, context: HostExecutionContext) -> HostToolResult:
            return self.handler(call, context.tick)

        return tuple(RegisteredTool(self.capability_name, tool, handle) for tool in self.tool_defs)


@dataclass(frozen=True)
class PortableAdapter:
    capability_name: str
    contribution: CapabilityContribution

    def registered_tools(self) -> tuple[RegisteredTool, ...]:
        def adapt(binding: ToolBinding) -> RegisteredHandler:
            def handle(call: ToolCall, context: HostExecutionContext) -> HostToolResult:
                invocation = None
                try:
                    invocation = context.portable_context(
                        self.capability_name,
                        self.contribution.required_fact_views,
                        self.contribution.required_host_services,
                        binding.tool_def.effect,
                        self.contribution.produced_artifact_profiles,
                        self.contribution.consumed_artifact_profiles,
                    )
                    result = binding.handler(call, invocation)
                except Exception:
                    if invocation is not None:
                        _discard_portable_artifacts(context, invocation)
                    return _error(call, "CapabilityExecutionError")
                if not isinstance(result, CapabilityResult):
                    _discard_portable_artifacts(context, invocation)
                    return _error(call, "InvalidCapabilityResult")
                tool_result = result.tool_result
                if (
                    not isinstance(tool_result, ToolResult)
                    or tool_result.name != call.name
                    or not isinstance(tool_result.response, dict)
                ):
                    _discard_portable_artifacts(context, invocation)
                    return _error(call, "InvalidCapabilityResult")
                if not isinstance(result.side_effect_facts, tuple) or any(
                    not isinstance(fact, SideEffectFact) for fact in result.side_effect_facts
                ):
                    _discard_portable_artifacts(context, invocation)
                    return _error(call, "InvalidSideEffectFact")
                if result.side_effect_facts and binding.tool_def.effect != "external_side_effect":
                    _discard_portable_artifacts(context, invocation)
                    return _error(call, "InvalidSideEffectFact")
                try:
                    staged_artifacts = context.finish_artifacts(
                        invocation,
                        accept=not tool_result.is_error,
                    )
                except Exception:
                    return _error(call, "InvalidArtifactStage")
                tool_result = _portable_trace(tool_result.with_call_id(call.call_id), call)
                return HostToolResult(
                    tool_result,
                    staged_artifacts=staged_artifacts,
                    side_effect_facts=result.side_effect_facts,
                )

            return handle

        return tuple(
            RegisteredTool(self.capability_name, binding.tool_def, adapt(binding))
            for binding in self.contribution.tool_bindings
        )


class CapabilityRegistry:
    """Own authorization, effect gate, end filtering and dispatch."""

    def __init__(
        self,
        *,
        config: KindredConfig | None = None,
        declared_capability_names: Collection[str] = (),
    ) -> None:
        self._config = config
        self._declared = set(declared_capability_names)
        self._capabilities: set[str] = set()
        self._tools: dict[str, RegisteredTool] = {}
        self._frozen = False

    def register(self, binding: InternalBinding | PortableAdapter) -> None:
        name = binding.capability_name
        if self._frozen:
            raise RuntimeError("capability registry is frozen")
        if not is_safe_name(name):
            raise ValueError(f"invalid capability name: {name!r}")
        if name in self._capabilities:
            raise ValueError(f"duplicate capability name: {name}")
        tools = binding.registered_tools()
        incoming = [item.tool_def.name for item in tools]
        if len(incoming) != len(set(incoming)) or any(name in self._tools for name in incoming):
            raise ValueError("duplicate tool name")
        if any(item.tool_def.effect == "staged_state_event" for item in tools):
            raise ValueError("staged_state_event tools must stay in act kernel")
        self._capabilities.add(name)
        self._declared.add(name)
        for item in tools:
            frozen = RegisteredTool(name, _freeze_tool(item.tool_def), item.handler)
            self._tools[frozen.tool_def.name] = frozen

    def freeze(self) -> None:
        self._frozen = True

    def visible_tool_defs(
        self,
        *,
        authorized_capability_names: Collection[str],
        context: HostExecutionContext,
    ) -> tuple[ToolDef, ...]:
        if context.tick.act_kind == "end_activity":
            return ()
        allowed = frozenset(authorized_capability_names)
        return tuple(
            item.tool_def
            for item in self._tools.values()
            if item.capability_name in allowed and not self._side_effect_denied(item, context.tick)
        )

    def dispatch(
        self,
        call: ToolCall,
        *,
        authorized_capability_names: Collection[str],
        context: HostExecutionContext,
    ) -> HostToolResult:
        item = self._tools.get(call.name)
        if item is None:
            return _error(call, "UnknownTool")
        if context.tick.act_kind == "end_activity":
            return _error(call, "CapabilityUnavailableDuringEndActivity")
        if item.capability_name not in authorized_capability_names:
            return _error(call, "UnauthorizedTool")
        if self._side_effect_denied(item, context.tick):
            return _error(call, "UnauthorizedSideEffect")
        return item.handler(call, context)

    def capability_names(self) -> frozenset[str]:
        return frozenset(self._capabilities)

    def declared_capability_names(self) -> frozenset[str]:
        return frozenset(self._declared)

    def tool_defs(self) -> tuple[ToolDef, ...]:
        return tuple(item.tool_def for item in self._tools.values())

    def _side_effect_denied(self, item: RegisteredTool, context: HostTickContext) -> bool:
        if item.tool_def.effect != "external_side_effect":
            return False
        policy = (
            self._config.capabilities.get(item.capability_name)
            if self._config is not None
            else None
        )
        allowed = policy.side_effect_activities if policy else ()
        return context.anchor_activity is None or context.anchor_activity not in allowed


def _error(call: ToolCall, error_type: str) -> HostToolResult:
    result = ToolResult.error(
        call, error_type=error_type, message="capability dispatch rejected"
    ).with_trace(
        args={"arg_keys": sorted(map(str, call.args))},
        response={"ok": False, "error_type": error_type},
    )
    return HostToolResult(result)


def _discard_portable_artifacts(
    context: HostExecutionContext,
    invocation: Any,
) -> None:
    try:
        context.finish_artifacts(invocation, accept=False)
    except Exception:
        pass


def _portable_trace(result: ToolResult, call: ToolCall) -> ToolResult:
    trace_args = {"arg_keys": sorted(str(key) for key in call.args)}
    trace_response: dict[str, Any] = {"ok": not result.is_error}
    error_type = result.response.get("error_type")
    if isinstance(error_type, str):
        trace_response["error_type"] = error_type
    return result.with_trace(args=trace_args, response=trace_response)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in copy.deepcopy(dict(value)).items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _freeze_tool(tool: ToolDef) -> ToolDef:
    return ToolDef(tool.name, tool.description, _freeze(tool.parameters), tool.effect)
