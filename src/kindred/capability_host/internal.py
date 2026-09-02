"""Host internal tool execution types."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kindred.capability_host.artifacts import StagedArtifact
from kindred.config import KindredConfig
from kindred_capability_sdk import SideEffectFact, ToolCall, ToolResult


@dataclass(frozen=True)
class ArtifactProfileRoute:
    """Package-owned selector for an Artifact producer profile."""

    producer_capability: str
    selector_capability: str
    profile: str
    member_paths: Mapping[str, str] = field(default_factory=dict)
    source_ref_kinds: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HostToolResult:
    """Result returned by a Host internal tool handler."""

    tool_result: ToolResult
    staged_artifacts: tuple[StagedArtifact, ...] = ()
    side_effect_facts: tuple[SideEffectFact, ...] = ()


@dataclass(frozen=True)
class HostTickContext:
    """Host-private context shared by one act tool loop.

    Portable packages only receive the SDK ``InvocationContext`` and cannot
    access this State, DB, provider handle, or runtime path container.
    """

    config: KindredConfig
    anchor_activity: str | None = None
    act_kind: str | None = None
    tick_id: int | str | None = None
    triggered_at: str | None = None
    target: Any = None
    next_state: Mapping[str, Any] = field(default_factory=dict)
    activities_dir: Path | None = None
    actions_dir: Path | None = None
    db: Any = None
    home: Any = None
    caches: MutableMapping[str, Any] = field(default_factory=dict)
    provider_handles: Mapping[str, Any] = field(default_factory=dict)


HostToolHandler = Callable[[ToolCall, HostTickContext], HostToolResult]


__all__ = [
    "ArtifactProfileRoute",
    "HostTickContext",
    "HostToolHandler",
    "HostToolResult",
]
