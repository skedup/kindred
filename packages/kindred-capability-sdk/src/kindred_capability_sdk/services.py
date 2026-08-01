from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from .artifacts import ArtifactDescriptor, SideEffectFact


class ArtifactWriter(Protocol):
    def stage_bundle(self, profile: str, files: Mapping[str, str | bytes]) -> None: ...


class ArtifactReader(Protocol):
    def describe_committed(self, artifact_ref: str) -> ArtifactDescriptor: ...

    def read_file(
        self,
        artifact_ref: str,
        relative_path: str,
        *,
        size_limit: int,
    ) -> bytes: ...


class UserMessenger(Protocol):
    def send(self, text: str, *, artifact_ref: str) -> SideEffectFact: ...


class SecretResolver(Protocol):
    def get(self, name: str) -> str | None: ...
