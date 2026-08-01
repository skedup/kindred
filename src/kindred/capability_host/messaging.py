"""Host-owned user message delivery service."""

from __future__ import annotations

from dataclasses import dataclass

from kindred.runtime.io_bridge import IOBridge, IOBridgeError
from kindred_capability_sdk import SideEffectFact

SEND_DELIVERY_FACT_KIND = "send_to_user_delivery"


@dataclass(frozen=True)
class HostUserMessenger:
    """把 Portable send 的正文交给既有 IOBridge，并返回安全投递事实。"""

    bridge: IOBridge | None

    def send(self, text: str, *, artifact_ref: str) -> SideEffectFact:
        if not isinstance(artifact_ref, str) or not artifact_ref.strip():
            raise ValueError("artifact_ref must be a non-empty string")
        if self.bridge is None:
            return SideEffectFact(
                SEND_DELIVERY_FACT_KIND,
                {
                    "artifact_ref": artifact_ref,
                    "delivered": False,
                    "error_type": "ProviderUnavailable",
                },
            )
        try:
            self.bridge.send_to_user(text)
        except IOBridgeError as exc:
            if exc.unknown_side_effect:
                raise
            return SideEffectFact(
                SEND_DELIVERY_FACT_KIND,
                {
                    "artifact_ref": artifact_ref,
                    "delivered": False,
                    "error_type": "DeliveryFailed",
                },
            )
        return SideEffectFact(
            SEND_DELIVERY_FACT_KIND,
            {
                "artifact_ref": artifact_ref,
                "delivered": True,
                "text_len": len(text),
            },
        )
