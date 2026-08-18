"""Internal, explicit composition for the two retained Mouth hosts."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kindred.hermes import HERMES_MOUTH_PLUGIN_DIR
from kindred.hermes.mouth_plugin import validate_runtime_resources
from kindred.hermes.runtime import CommandRunner, HermesOutboundChannel, HermesTranscriptSource
from kindred.mouth_host.model import HermesRuntimeModel, HostRuntimeModel, OpenClawRuntimeModel
from kindred.mouth_host.runtime import (
    OutboundChannel,
    TranscriptPullError,
    TranscriptSource,
)
from kindred.openclaw.runtime import OpenClawOutboundChannel, OpenClawTranscriptSource
from kindred.resident import read_owned_persona_file
from kindred.resident._contract import PersonaPaths

if TYPE_CHECKING:
    from kindred.adapters.openclaw.gateway import GatewayClient


HostCompositionError = RuntimeError


@dataclass(frozen=True, slots=True)
class HostPreparation:
    model: HostRuntimeModel
    transcript: TranscriptSource
    persona: PersonaPaths
    outbound: OutboundChannel

    def checks(self, *, online: bool = False) -> tuple[dict[str, object], ...]:
        structural = _check(
            "mouth_host.structural", lambda: _structural_ready(self.model, self.persona)
        )
        if not online or structural["status"] == "failed":
            return (structural,)
        return (structural, _check("mouth_host.transcript", self.transcript.pull))


def prepare_host(
    model: HostRuntimeModel,
    *,
    openclaw_gateway: GatewayClient | None = None,
    hermes_runner: CommandRunner | None = None,
) -> HostPreparation:
    if isinstance(model, OpenClawRuntimeModel):
        if openclaw_gateway is None or hermes_runner is not None:
            raise HostCompositionError("OpenClaw transport is unavailable")
        return HostPreparation(
            model,
            OpenClawTranscriptSource(openclaw_gateway, model.wire),
            PersonaPaths.openclaw(model.workspace),
            OpenClawOutboundChannel(openclaw_gateway, model.wire, agent_id=model.agent_id),
        )
    elif isinstance(model, HermesRuntimeModel):
        if openclaw_gateway is not None:
            raise HostCompositionError("Hermes transport is invalid")
        return HostPreparation(
            model,
            HermesTranscriptSource(model.wire, runner=hermes_runner),
            PersonaPaths.hermes(model.wire.host_home),
            HermesOutboundChannel(model.wire, runner=hermes_runner),
        )
    else:
        raise HostCompositionError("unknown Mouth host kind")


def _structural_ready(model: HostRuntimeModel, persona: PersonaPaths) -> None:
    if isinstance(model, OpenClawRuntimeModel):
        if (
            model.agent_id != model.agent_id.strip()
            or not model.workspace.is_absolute()
            or persona != PersonaPaths.openclaw(model.workspace)
        ):
            raise HostCompositionError("OpenClaw runtime identity mismatch")
    else:
        wire, binding = model.wire, validate_runtime_resources(model.wire.host_home)
        expected = (wire.canonical_session_id, wire.approved_platform, wire.approved_sender_id)
        if (
            tuple(binding[key] for key in ("session_id", "platform", "sender_id")) != expected
            or not (HERMES_MOUTH_PLUGIN_DIR / "plugin.yaml").is_file()
            or not (wire.host_home / "config.yaml").is_file()
            or not wire.host_executable.is_file()
            or not os.access(wire.host_executable, os.X_OK)
            or persona != PersonaPaths.hermes(wire.host_home)
        ):
            raise HostCompositionError("Hermes runtime identity mismatch")
    read_owned_persona_file(persona.soul_full, name="SOUL.md")
    read_owned_persona_file(persona.identity, name="IDENTITY.md")
    read_owned_persona_file(persona.user, name="USER.md", optional=True)
    read_owned_persona_file(persona.soul_excerpt, name="SOUL_excerpt.md")


def _check(check_id: str, function: Callable[[], object]) -> dict[str, object]:
    try:
        function()
        status, retryable, hint = "ok", False, "Mouth host check passed"
    except Exception as exc:
        retryable = isinstance(exc, TranscriptPullError) and exc.reason in {
            "gateway_request_failed",
            "poll_throttled",
            "timeout",
        }
        status, hint = "failed", "Mouth host check failed; verify the local host contract"
    return {"check_id": check_id, "status": status, "retryable": retryable, "hint": hint}
