from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class ArtifactDescriptor:
    artifact_ref: str
    producer: str
    profile: str


@dataclass(frozen=True)
class SideEffectFact:
    kind: str
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))
