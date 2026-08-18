"""Kindred Portable Capability SPI."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias

from .artifacts import SideEffectFact
from .services import ArtifactReader, ArtifactWriter, SecretResolver, UserMessenger

ToolEffect: TypeAlias = Literal[
    "read_only", "staged_state_event", "external_side_effect", "artifact_write"
]
_EFFECTS = frozenset({"read_only", "staged_state_event", "external_side_effect", "artifact_write"})
_HOST_SERVICES = frozenset({"artifact_writer", "artifact_reader", "user_messenger"})
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def is_safe_name(value: object) -> bool:
    return isinstance(value, str) and _SAFE_NAME.fullmatch(value) is not None


def is_safe_fact_name(value: object) -> bool:
    return isinstance(value, str) and all(is_safe_name(part) for part in value.split("."))


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: Mapping[str, Any]
    effect: ToolEffect
    allow_before_action_lock: bool = False

    def __post_init__(self) -> None:
        if not is_safe_name(self.name):
            raise ValueError(f"invalid tool name: {self.name!r}")
        if not isinstance(self.description, str) or not self.description:
            raise ValueError("tool description must be non-empty")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("tool parameters must be a mapping")
        if self.effect not in _EFFECTS:
            raise ValueError(f"invalid tool effect: {self.effect!r}")
        if type(self.allow_before_action_lock) is not bool:
            raise TypeError("allow_before_action_lock must be a bool")
        if self.allow_before_action_lock and self.effect not in {
            "read_only",
            "staged_state_event",
        }:
            raise ValueError("only preparation tools may run before the action lock")

    def function_declaration(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": _thaw(self.parameters),
        }


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    call_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    name: str
    response: dict[str, Any]
    call_id: str | None = None
    is_error: bool = False
    trace_args: dict[str, Any] | None = field(default=None, compare=False)
    trace_response: dict[str, Any] | None = field(default=None, compare=False)

    @classmethod
    def ok(cls, call: ToolCall, response: Mapping[str, Any]) -> ToolResult:
        return cls(call.name, dict(response), call.call_id)

    @classmethod
    def error(cls, call: ToolCall, *, error_type: str, message: str) -> ToolResult:
        response = {"ok": False, "error_type": error_type, "message": message}
        return cls(call.name, response, call.call_id, True)

    def with_call_id(self, call_id: str | None) -> ToolResult:
        return self if self.call_id == call_id else replace(self, call_id=call_id)

    def with_trace(
        self,
        *,
        args: Mapping[str, Any] | None = None,
        response: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return replace(
            self,
            trace_args=None if args is None else dict(args),
            trace_response=None if response is None else dict(response),
        )


@dataclass(frozen=True)
class FactView:
    name: str
    value: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not is_safe_fact_name(self.name):
            raise ValueError(f"invalid fact view name: {self.name!r}")
        object.__setattr__(self, "value", _freeze(self.value))


class TransientStore:
    """Capability-scoped, tick-local mutable values."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def put(self, key: str, value: Any) -> None:
        if not is_safe_name(key):
            raise ValueError(f"invalid transient key: {key!r}")
        self._values[key] = value

    def setdefault(self, key: str, default: Any) -> Any:
        if not is_safe_name(key):
            raise ValueError(f"invalid transient key: {key!r}")
        return self._values.setdefault(key, default)


@dataclass(frozen=True)
class InvocationContext:
    tick_id: int | str | None
    triggered_at: str | None
    facts: tuple[FactView, ...]
    transient: TransientStore
    artifact_writer: ArtifactWriter | None = None
    artifact_reader: ArtifactReader | None = None
    user_messenger: UserMessenger | None = None

    def fact(self, name: str) -> FactView | None:
        return next((fact for fact in self.facts if fact.name == name), None)


@dataclass(frozen=True)
class CapabilityResult:
    tool_result: ToolResult
    side_effect_facts: tuple[SideEffectFact, ...] = ()


PortableToolHandler: TypeAlias = Callable[[ToolCall, InvocationContext], CapabilityResult]


@dataclass(frozen=True)
class ToolBinding:
    tool_def: ToolDef
    handler: PortableToolHandler


@dataclass(frozen=True)
class CapabilityContribution:
    tool_bindings: tuple[ToolBinding, ...]
    required_host_services: frozenset[str] = frozenset()
    required_fact_views: frozenset[str] = frozenset()
    produced_artifact_profiles: frozenset[str] = frozenset()
    consumed_artifact_profiles: frozenset[str] = frozenset()
    close: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        bindings = tuple(self.tool_bindings)
        object.__setattr__(self, "tool_bindings", bindings)
        for field_name in (
            "required_host_services",
            "required_fact_views",
            "produced_artifact_profiles",
            "consumed_artifact_profiles",
        ):
            object.__setattr__(self, field_name, frozenset(getattr(self, field_name)))
        names = [binding.tool_def.name for binding in bindings]
        if len(names) != len(set(names)):
            raise ValueError("contribution contains duplicate tool names")
        if not self.required_host_services <= _HOST_SERVICES:
            raise ValueError("contribution contains an unsupported host service")
        if any(not is_safe_fact_name(name) for name in self.required_fact_views):
            raise ValueError("contribution contains an invalid grant name")
        profiles = self.produced_artifact_profiles | self.consumed_artifact_profiles
        if any(not isinstance(value, str) or not value.strip() for value in profiles):
            raise ValueError("artifact profile names must be non-empty strings")


class CapabilityFactory(Protocol):
    def __call__(
        self, *, settings: Mapping[str, Any], secrets: SecretResolver
    ) -> CapabilityContribution: ...
