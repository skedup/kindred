"""Heart outbound bridge: direct channel send followed by Mouth context commit."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kindred.adapters.openclaw.gateway import GatewayClient
    from kindred.openclaw import OpenClawWire

logger = logging.getLogger(__name__)

_CONTRACT = "kindred-heart-outbound-v1"
_ARTIFACT_PROFILE = "kindred.compose.outbound.v1"
_MAX_OUTBOUND_UTF8_BYTES = 20_000


class IOBridgeError(Exception):
    """IOBridge 出向失败（ws 发送失败）。

    调用方（act 节点胶水）catch 后应**降级不崩 tick**——push 失败是运行时问题，
    不该让整个 graph tick 炸（与 act 节点「failure 不 raise」哲学一致）。
    """

    def __init__(self, message: str, *, unknown_side_effect: bool = False) -> None:
        super().__init__(message)
        self.unknown_side_effect = unknown_side_effect


class IOBridge:
    """心的出向桥：原文直发 approved peer，再提交嘴的隐藏上下文。"""

    def __init__(
        self,
        gateway: GatewayClient,
        *,
        wire: OpenClawWire,
        agent_id: str,
    ) -> None:
        self._gateway = gateway
        self._wire = wire
        self._agent_id = agent_id

    # ── 出向：心主动联系 user ──────────────────────────────────

    def send_to_user(self, text: str, *, artifact_ref: str) -> None:
        """直发 committed 正文；投递成功后 best-effort 提交 Mouth context。"""
        clean = (text or "").strip()
        if not clean:
            raise IOBridgeError("send_to_user: text 为空，拒绝发空消息")
        if len(clean.encode("utf-8")) > _MAX_OUTBOUND_UTF8_BYTES:
            raise IOBridgeError("send_to_user: text 超出安全长度")
        if not isinstance(artifact_ref, str) or not artifact_ref.strip():
            raise IOBridgeError("send_to_user: artifact_ref 无效")

        route = self._wire.outbound_route
        operation_id = _operation_id(
            artifact_ref=artifact_ref,
            transcript_session=self._wire.transcript_session,
            channel=route.channel,
            target=route.target,
            account_id=route.account_id,
            thread_id=route.thread_id,
        )
        response = self._gateway.send_direct(
            channel=route.channel,
            account_id=route.account_id,
            to=route.target,
            agent_id=self._agent_id,
            session_key=self._wire.transcript_session,
            message=clean,
            idempotency_key=f"kindred-heart-send:{operation_id}",
        )
        if not isinstance(response, dict) or response.get("ok") is not True:
            unknown = isinstance(response, dict) and response.get("side_effect") == "unknown"
            raise IOBridgeError(
                "send_to_user: Gateway direct send failed",
                unknown_side_effect=unknown,
            )

        try:
            context = self._gateway.commit_outbound_context(operation_id, clean)
        except Exception:
            logger.warning("IOBridge.send_to_user: delivered; Mouth context commit failed")
            return
        if not isinstance(context, dict) or context.get("ok") is not True:
            logger.warning("IOBridge.send_to_user: delivered; Mouth context commit failed")
        else:
            logger.info(
                "IOBridge.send_to_user: delivered and Mouth context committed (len=%d)",
                len(clean),
            )


def _operation_id(
    *,
    artifact_ref: str,
    transcript_session: str,
    channel: str,
    target: str,
    account_id: str,
    thread_id: str | None,
) -> str:
    payload = {
        "account_id": account_id,
        "artifact_profile": _ARTIFACT_PROFILE,
        "artifact_ref": artifact_ref,
        "channel": channel,
        "contract": _CONTRACT,
        "target": target,
        "thread_id": thread_id,
        "transcript_session": transcript_session,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


__all__ = ["IOBridge", "IOBridgeError"]
