"""OpenClaw projections for the host-neutral Mouth runtime ports."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from kindred.db.messages import extract_text_summary, protocol_role_to_business
from kindred.mouth_host.runtime import (
    DispatchResult,
    TranscriptBatch,
    TranscriptMessage,
    TranscriptPullError,
)
from kindred.runtime.history_sync import HistorySync
from kindred.runtime.io_bridge import IOBridge

if TYPE_CHECKING:
    from kindred.adapters.openclaw.gateway import GatewayClient
    from kindred.db import KindredDB
    from kindred.openclaw.wire import OpenClawWire

logger = logging.getLogger(__name__)

_CONTRACT = "kindred-heart-outbound-v1"
_ARTIFACT_PROFILE = "kindred.compose.outbound.v1"


class OpenClawTranscriptSource:
    def __init__(self, client: GatewayClient, wire: OpenClawWire, *, limit: int = 30) -> None:
        self._client = client
        self._wire = wire
        self._limit = limit

    def pull(self) -> TranscriptBatch:
        identity = self._wire.transcript_session
        result = self._client.fetch_chat_history(identity, limit=self._limit)
        if "error" in result:
            raise TranscriptPullError("gateway_request_failed")
        if result.get("sessionKey") != identity or result.get("sessionInfoKey") != identity:
            raise TranscriptPullError("canonical_identity_mismatch")
        raw_messages = result.get("messages") or []
        messages = tuple(
            message
            for raw in raw_messages
            if isinstance(raw, dict) and (message := _project_message(raw)) is not None
        )
        return TranscriptBatch(identity=identity, messages=messages)


class OpenClawHistorySync(HistorySync):
    def __init__(self, client: GatewayClient, db: KindredDB, *, wire: OpenClawWire) -> None:
        self._client, self._wire = client, wire
        super().__init__(OpenClawTranscriptSource(client, wire), db)


class OpenClawOutboundChannel:
    def __init__(self, gateway: GatewayClient, wire: OpenClawWire, *, agent_id: str) -> None:
        self._gateway, self._wire = gateway, wire
        self._agent_id = agent_id

    def send(self, text: str, *, artifact_ref: str) -> DispatchResult:
        route = self._wire.outbound_route
        operation_id = _operation_id(artifact_ref, self._wire)
        response = self._gateway.send_direct(
            channel=route.channel,
            account_id=route.account_id,
            to=route.target,
            agent_id=self._agent_id,
            session_key=self._wire.transcript_session,
            message=text,
            idempotency_key=f"kindred-heart-send:{operation_id}",
        )
        if not isinstance(response, dict) or response.get("ok") is not True:
            unknown = isinstance(response, dict) and response.get("side_effect") == "unknown"
            return DispatchResult(status="unknown" if unknown else "failed")

        try:
            context = self._gateway.commit_outbound_context(operation_id, text)
        except Exception:
            context = None
        if not isinstance(context, dict) or context.get("ok") is not True:
            logger.warning("IOBridge.send_to_user: delivered; Mouth context commit failed")
        else:
            logger.info(
                "IOBridge.send_to_user: delivered and Mouth context committed (len=%d)", len(text)
            )
        return DispatchResult(status="accepted")


class OpenClawIOBridge(IOBridge):
    def __init__(self, gateway: GatewayClient, *, wire: OpenClawWire, agent_id: str) -> None:
        self._gateway, self._wire = gateway, wire
        self._agent_id = agent_id
        super().__init__(OpenClawOutboundChannel(gateway, wire, agent_id=agent_id))


def _project_message(raw: dict[str, Any]) -> TranscriptMessage | None:
    role_protocol = str(raw.get("role") or "unknown")
    if role_protocol == "toolResult":
        return None
    openclaw = raw.get("__openclaw") or {}
    msg_id = openclaw.get("id")
    ts_ms = raw.get("timestamp")
    if not msg_id or ts_ms is None:
        return None
    return TranscriptMessage(
        msg_id=str(msg_id),
        seq=openclaw.get("seq"),
        role=protocol_role_to_business(role_protocol),
        text_summary=extract_text_summary(raw.get("content")),
        ts_ms=int(ts_ms),
    )


def _operation_id(artifact_ref: str, wire: OpenClawWire) -> str:
    route = wire.outbound_route
    payload = {
        "account_id": route.account_id,
        "artifact_profile": _ARTIFACT_PROFILE,
        "artifact_ref": artifact_ref,
        "channel": route.channel,
        "contract": _CONTRACT,
        "target": route.target,
        "thread_id": route.thread_id,
        "transcript_session": wire.transcript_session,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
