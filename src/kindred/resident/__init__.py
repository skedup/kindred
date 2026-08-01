"""Resident 冷启动领域入口。"""

from kindred.resident._contract import (
    PersonaProjection,
    PersonaTraits,
    ResidentInitError,
    ResidentInitRequest,
    ResidentInitResult,
    WorldResolution,
)
from kindred.resident._initialize import initialize_resident, require_committed_resident

__all__ = [
    "PersonaProjection",
    "PersonaTraits",
    "ResidentInitError",
    "ResidentInitRequest",
    "ResidentInitResult",
    "WorldResolution",
    "initialize_resident",
    "require_committed_resident",
]
