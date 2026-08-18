"""Resident 冷启动领域入口。"""

from kindred.resident._contract import (
    PersonaPaths,
    PersonaProjection,
    PersonaTraits,
    ResidentInitError,
    ResidentInitRequest,
    ResidentInitResult,
    WorldResolution,
)
from kindred.resident._initialize import (
    initialize_resident,
    read_owned_persona_file,
    require_committed_resident,
)

__all__ = [
    "PersonaProjection",
    "PersonaPaths",
    "PersonaTraits",
    "ResidentInitError",
    "ResidentInitRequest",
    "ResidentInitResult",
    "WorldResolution",
    "initialize_resident",
    "read_owned_persona_file",
    "require_committed_resident",
]
