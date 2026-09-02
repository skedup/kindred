"""Filesystem layout for telemetry artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TelemetryLayout:
    db_path: Path

    @classmethod
    def from_life_root(cls, life_root: Path) -> TelemetryLayout:
        return cls(db_path=life_root / "data" / "kindred-telemetry.db")
