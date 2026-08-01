"""Minimal Host composition for internal and Portable bindings."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from kindred.capability_host.artifacts import (
    ArtifactStore,
    ArtifactStoreError,
    ScopedArtifactWriter,
    StagedArtifact,
)
from kindred.capability_host.discovery import discover_enabled_contributions
from kindred.capability_host.facts import FactViewBuilder
from kindred.capability_host.internal import HostTickContext
from kindred.capability_host.registry import (
    CapabilityRegistry,
    InternalBinding,
    PortableAdapter,
)
from kindred_capability_sdk import FactView, InvocationContext, ToolEffect, TransientStore

if TYPE_CHECKING:
    from kindred.config import KindredConfig


@dataclass
class HostExecutionContext:
    tick: HostTickContext
    fact_builders: Mapping[str, FactViewBuilder] = field(default_factory=dict)
    services: Mapping[str, Any] = field(default_factory=dict)
    transient: dict[str, TransientStore] = field(default_factory=dict)

    def portable_context(
        self,
        capability_name: str,
        fact_names: frozenset[str],
        service_names: frozenset[str],
        tool_effect: ToolEffect,
        produced_artifact_profiles: frozenset[str] = frozenset(),
        consumed_artifact_profiles: frozenset[str] = frozenset(),
    ) -> InvocationContext:
        facts: list[FactView] = []
        for name in sorted(fact_names):
            view = self.fact_builders[name](self.tick)
            if not isinstance(view, FactView) or view.name != name:
                raise ValueError(f"fact builder returned invalid view for {name!r}")
            facts.append(view)
        artifact_store = _artifact_store(self.services)
        writer = (
            artifact_store.writer(capability_name, produced_artifact_profiles)
            if artifact_store is not None
            and tool_effect == "artifact_write"
            and "artifact_writer" in service_names
            else None
        )
        reader = (
            artifact_store.reader(consumed_artifact_profiles)
            if artifact_store is not None and "artifact_reader" in service_names
            else None
        )
        return InvocationContext(
            tick_id=self.tick.tick_id,
            triggered_at=self.tick.triggered_at,
            facts=tuple(facts),
            transient=self.transient.setdefault(capability_name, TransientStore()),
            artifact_writer=writer,
            artifact_reader=reader,
            user_messenger=(
                self.services["user_messenger"]
                if tool_effect == "external_side_effect" and "user_messenger" in service_names
                else None
            ),
        )

    def finish_artifacts(
        self,
        invocation: InvocationContext,
        *,
        accept: bool,
    ) -> tuple[StagedArtifact, ...]:
        writer = invocation.artifact_writer
        if writer is None:
            return ()
        if not isinstance(writer, ScopedArtifactWriter):
            raise ArtifactStoreError("artifact writer is not Host-owned")
        if not accept:
            writer.discard_staged()
            return ()
        return writer.take_staged()


@dataclass
class HostRuntime:
    registry: CapabilityRegistry
    config: KindredConfig
    provider_handles: Mapping[str, Any] = field(default_factory=dict)
    fact_view_builders: Mapping[str, FactViewBuilder] = field(default_factory=dict)
    host_services: Mapping[str, Any] = field(default_factory=dict)
    _stack: ExitStack = field(default_factory=ExitStack, repr=False)

    def __post_init__(self) -> None:
        self.provider_handles = MappingProxyType(dict(self.provider_handles))
        self.fact_view_builders = MappingProxyType(dict(self.fact_view_builders))
        self.host_services = MappingProxyType(dict(self.host_services))

    def execution_context(self, tick: HostTickContext) -> HostExecutionContext:
        return HostExecutionContext(tick, self.fact_view_builders, self.host_services)

    def artifact_store(self) -> ArtifactStore | None:
        return _artifact_store(self.host_services)

    def close(self) -> None:
        self._stack.close()


def build_host_runtime(
    *,
    config: KindredConfig,
    internal_bindings: tuple[InternalBinding, ...],
    provider_handles: Mapping[str, Any] | None = None,
    fact_view_builders: Mapping[str, FactViewBuilder] | None = None,
    host_services: Mapping[str, Any] | None = None,
    artifact_profiles: frozenset[str] = frozenset(),
    secret_lookup: Callable[[str, str], str | None] | None = None,
) -> HostRuntime:
    facts, services = fact_view_builders or {}, host_services or {}
    registry = CapabilityRegistry(config=config, declared_capability_names=config.capabilities)
    stack = ExitStack()
    try:
        for binding in internal_bindings:
            registry.register(binding)
        discovery_kwargs = {} if secret_lookup is None else {"secret_lookup": secret_lookup}
        contributions = discover_enabled_contributions(
            config.capabilities,
            internal_capability_names=registry.capability_names(),
            available_host_services=frozenset(services),
            available_fact_views=frozenset(facts),
            available_artifact_profiles=artifact_profiles,
            **discovery_kwargs,
        )
        for loaded in contributions:
            if loaded.contribution.close:
                stack.callback(loaded.contribution.close)
            registry.register(PortableAdapter(loaded.capability_name, loaded.contribution))
        registry.freeze()
    except BaseException:
        stack.close()
        raise
    return HostRuntime(
        registry,
        config,
        provider_handles or {},
        facts,
        services,
        stack,
    )


def _artifact_store(services: Mapping[str, Any]) -> ArtifactStore | None:
    writer = services.get("artifact_writer")
    reader = services.get("artifact_reader")
    if writer is None and reader is None:
        return None
    if not isinstance(writer, ArtifactStore) or reader is not writer:
        raise ArtifactStoreError("artifact services must share one Host ArtifactStore")
    return writer
