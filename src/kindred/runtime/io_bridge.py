"""Heart outbound bridge: direct channel send followed by Mouth context commit."""

from __future__ import annotations

from typing import Any

from kindred.mouth_host.runtime import OutboundChannel

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
        channel: OutboundChannel | Any,
        *,
        wire: Any | None = None,
        agent_id: str | None = None,
    ) -> None:
        if wire is not None:
            # Protected public constructor; production composition injects a channel.
            if agent_id is None:
                raise TypeError("agent_id is required with an OpenClaw wire")
            from kindred.openclaw.runtime import OpenClawOutboundChannel

            channel = OpenClawOutboundChannel(channel, wire, agent_id=agent_id)  # type: ignore[arg-type]
        self._channel = channel

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

        result = self._channel.send(clean, artifact_ref=artifact_ref)
        if result.status != "accepted":
            raise IOBridgeError(
                "send_to_user: Mouth direct send failed",
                unknown_side_effect=result.status == "unknown",
            )


__all__ = ["IOBridge", "IOBridgeError"]
