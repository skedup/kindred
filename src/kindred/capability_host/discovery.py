"""Discover explicitly enabled Portable capability entry points."""

from __future__ import annotations

import importlib.metadata as metadata
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from packaging.requirements import Requirement
from packaging.version import Version

from kindred.config import KindredCapabilityConfig
from kindred_capability_sdk import (
    CapabilityContribution,
    SecretResolver,
    is_safe_name,
)

ENTRY_POINT_GROUP = "kindred.capability.v1"
SDK_DISTRIBUTION = "kindred-capability-sdk"


class CapabilityDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedContribution:
    capability_name: str
    contribution: CapabilityContribution


class NamespacedSecretResolver(SecretResolver):
    def __init__(self, capability_name: str, lookup: Callable[[str, str], str | None]) -> None:
        self._namespace, self._lookup = capability_name, lookup

    def get(self, name: str) -> str | None:
        if not is_safe_name(name):
            raise ValueError(f"invalid secret name: {name!r}")
        return self._lookup(self._namespace, name)


def _environment_secret(namespace: str, name: str) -> str | None:
    return os.environ.get(f"KINDRED_CAPABILITY_{namespace}_{name}".upper())


def discover_enabled_contributions(
    configs: Mapping[str, KindredCapabilityConfig],
    *,
    internal_capability_names: frozenset[str],
    available_host_services: frozenset[str],
    available_fact_views: frozenset[str],
    available_artifact_profiles: frozenset[str],
    secret_lookup: Callable[[str, str], str | None] = _environment_secret,
) -> tuple[LoadedContribution, ...]:
    enabled = sorted(name for name, value in configs.items() if value.enabled)
    if not enabled:
        return ()
    entry_points = tuple(metadata.entry_points().select(group=ENTRY_POINT_GROUP))
    loaded: list[LoadedContribution] = []
    for name in enabled:
        if not is_safe_name(name) or name in internal_capability_names:
            raise CapabilityDiscoveryError(f"invalid or conflicting capability: {name!r}")
        matches = [entry for entry in entry_points if entry.name == name]
        if len(matches) != 1:
            raise CapabilityDiscoveryError(
                f"enabled capability {name!r} requires one {ENTRY_POINT_GROUP} entry point"
            )
        entry = matches[0]
        _check_sdk(entry)
        factory = entry.load()
        if not callable(factory):
            raise CapabilityDiscoveryError(f"capability {name!r} factory is not callable")
        contribution = factory(
            settings=configs[name].settings,
            secrets=NamespacedSecretResolver(name, secret_lookup),
        )
        if not isinstance(contribution, CapabilityContribution):
            raise CapabilityDiscoveryError(f"capability {name!r} returned an invalid contribution")
        missing = (
            contribution.required_host_services - available_host_services,
            contribution.required_fact_views - available_fact_views,
        )
        if any(missing):
            if contribution.close:
                contribution.close()
            raise CapabilityDiscoveryError(
                f"capability {name!r} has missing grants: "
                f"services={sorted(missing[0])}, views={sorted(missing[1])}"
            )
        loaded.append(LoadedContribution(name, contribution))
    produced_profiles = frozenset(
        profile for item in loaded for profile in item.contribution.produced_artifact_profiles
    )
    available_profiles = available_artifact_profiles | produced_profiles
    for item in loaded:
        missing_profiles = item.contribution.consumed_artifact_profiles - available_profiles
        if not missing_profiles:
            continue
        for opened in reversed(loaded):
            if opened.contribution.close:
                opened.contribution.close()
        raise CapabilityDiscoveryError(
            f"capability {item.capability_name!r} has missing grants: "
            f"profiles={sorted(missing_profiles)}"
        )
    return tuple(loaded)


def _check_sdk(entry: metadata.EntryPoint) -> None:
    if entry.dist is None:
        raise CapabilityDiscoveryError(f"entry point {entry.name!r} has no distribution")
    requirements = [Requirement(raw) for raw in entry.dist.requires or ()]
    matching = [
        requirement
        for requirement in requirements
        if requirement.name.lower().replace("_", "-") == SDK_DISTRIBUTION
    ]
    if len(matching) != 1:
        raise CapabilityDiscoveryError(
            f"distribution for {entry.name!r} must require {SDK_DISTRIBUTION}"
        )
    if (
        matching[0].specifier
        and Version(metadata.version(SDK_DISTRIBUTION)) not in matching[0].specifier
    ):
        raise CapabilityDiscoveryError(f"capability {entry.name!r} requires an incompatible SDK")
