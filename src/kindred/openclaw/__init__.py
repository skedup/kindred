"""OpenClaw-specific runtime wiring owned by Kindred."""

from pathlib import Path

from kindred.openclaw.wire import (
    APPROVED_DM_SCOPE,
    ApprovedPeer,
    OpenClawWire,
    OpenClawWireError,
    OutboundRoute,
    wire_from_session,
)

MOUTH_PLUGIN_DIR = Path(__file__).parent / "mouth_plugin"

__all__ = [
    "APPROVED_DM_SCOPE",
    "ApprovedPeer",
    "MOUTH_PLUGIN_DIR",
    "OpenClawWire",
    "OpenClawWireError",
    "OutboundRoute",
    "wire_from_session",
]
