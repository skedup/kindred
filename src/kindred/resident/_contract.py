"""Resident 冷启动的严格输入、投影与提交合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from kindred.state._base import StrictBase

PERSONA_PROJECTION_SCHEMA = "workspace_persona_v1"
INSTALL_CONTRACT_VERSION = "open2.v1"
MARKER_SCHEMA_VERSION = 1


class ResidentInitError(RuntimeError):
    """Resident 初始化或提交合同被拒绝。"""


class PersonaTraits(StrictBase):
    openness: int = Field(strict=True, ge=0, le=100)
    agreeableness: int = Field(strict=True, ge=0, le=100)
    conscientiousness: int = Field(strict=True, ge=0, le=100)
    awareness: int = Field(strict=True, ge=0, le=100)
    eros: int = Field(strict=True, ge=0, le=100)


class PersonaProjection(StrictBase):
    soul_excerpt: str = Field(strict=True, min_length=1, max_length=500)
    traits: PersonaTraits

    @field_validator("soul_excerpt")
    @classmethod
    def _trim_excerpt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("soul_excerpt must not be blank")
        return value


class WorldResolution(StrictBase):
    address: str = Field(strict=True, min_length=1, max_length=240)
    city: str = Field(strict=True, min_length=1, max_length=80)
    timezone: str = Field(strict=True, min_length=1, max_length=80)

    @field_validator("address", "city", "timezone")
    @classmethod
    def _trim(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("world value must not be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


@dataclass(frozen=True)
class ResidentInitRequest:
    resident_id: str
    agent_id: str
    workspace: Path
    life_root: Path
    xdg_config_home: Path
    home_address: str
    llm_provider: str
    llm_model: str
    secrets: Mapping[str, str]
    install_now: datetime
    persona_write_consent: bool


@dataclass(frozen=True)
class ResidentInitResult:
    status: str
    install_id: str


__all__ = [
    "INSTALL_CONTRACT_VERSION",
    "MARKER_SCHEMA_VERSION",
    "PERSONA_PROJECTION_SCHEMA",
    "PersonaProjection",
    "PersonaTraits",
    "ResidentInitError",
    "ResidentInitRequest",
    "ResidentInitResult",
    "WorldResolution",
]
