"""Strict session, peer, and outbound-route contract for OpenClaw."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

APPROVED_DM_SCOPE = "per-channel-peer"


class OpenClawWireError(ValueError):
    """The OpenClaw session cannot prove the configured peer boundary."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator("*", mode="before", check_fields=False)
    @classmethod
    def _reject_blank_or_padded_text(cls, value: object) -> object:
        if isinstance(value, str) and (not value.strip() or value != value.strip()):
            raise ValueError("text field must be non-empty and unpadded")
        return value


class ApprovedPeer(_StrictModel):
    """The only direct peer Kindred is allowed to treat as its user."""

    provider: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    target: str = Field(min_length=1)


class OutboundRoute(_StrictModel):
    """The explicit delivery route for the approved peer."""

    channel: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    target: str = Field(min_length=1)
    thread_id: str | None = None


class OpenClawWire(_StrictModel):
    """One canonical transcript and its same-row peer/delivery projections."""

    transcript_session: str = Field(min_length=1)
    approved_peer: ApprovedPeer
    outbound_route: OutboundRoute

    @model_validator(mode="after")
    def _same_peer(self) -> OpenClawWire:
        peer, route = self.approved_peer, self.outbound_route
        if (
            peer.provider != route.channel
            or peer.account_id != route.account_id
            or peer.target != route.target
        ):
            raise ValueError("outbound_route does not match approved_peer")
        return self


def wire_from_session(session: Mapping[str, Any], *, dm_scope: str) -> OpenClawWire:
    """Project one verified direct session into the runtime wire contract."""
    if dm_scope != APPROVED_DM_SCOPE:
        raise OpenClawWireError("OpenClaw dmScope is not per-channel-peer")
    if session.get("kind") != "direct" or session.get("chatType") != "direct":
        raise OpenClawWireError("OpenClaw session is not direct")
    origin = _mapping(session.get("origin"), "origin")
    delivery = _mapping(session.get("deliveryContext"), "deliveryContext")
    try:
        return OpenClawWire.model_validate(
            {
                "transcript_session": session["key"],
                "approved_peer": {
                    "provider": origin["provider"],
                    "account_id": origin["accountId"],
                    "target": origin["to"],
                },
                "outbound_route": {
                    "channel": delivery["channel"],
                    "account_id": delivery["accountId"],
                    "target": delivery["to"],
                    "thread_id": delivery.get("threadId"),
                },
            }
        )
    except (KeyError, TypeError, ValueError):
        raise OpenClawWireError("OpenClaw session wire is incomplete or inconsistent") from None


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OpenClawWireError(f"OpenClaw session {field} is missing")
    return value


__all__ = [
    "APPROVED_DM_SCOPE",
    "ApprovedPeer",
    "OpenClawWire",
    "OpenClawWireError",
    "OutboundRoute",
    "wire_from_session",
]
