"""Strict internal runtime identities for supported Mouth hosts."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kindred.hermes.wire import HermesRuntimeIdentity, HermesWire
from kindred.openclaw.wire import OpenClawWire


class _RuntimeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )


class OpenClawRuntimeModel(_RuntimeModel):
    kind: Literal["openclaw"]
    wire: OpenClawWire
    agent_id: str = Field(min_length=1)
    workspace: Path

    @model_validator(mode="after")
    def _require_canonical_identity(self) -> OpenClawRuntimeModel:
        if self.agent_id != self.agent_id.strip() or not self.workspace.is_absolute():
            raise ValueError("OpenClaw runtime identity is not canonical")
        return self


class HermesRuntimeModel(_RuntimeModel):
    kind: Literal["hermes"]
    wire: HermesWire
    identity: HermesRuntimeIdentity

    @model_validator(mode="after")
    def _require_canonical_identity(self) -> HermesRuntimeModel:
        if any(
            type(value) is not str or not value or value != value.strip() for value in self.identity
        ):
            raise ValueError("Hermes runtime identity is not canonical")
        return self


HostRuntimeModel = OpenClawRuntimeModel | HermesRuntimeModel


__all__ = ["HermesRuntimeModel", "HostRuntimeModel", "OpenClawRuntimeModel"]
