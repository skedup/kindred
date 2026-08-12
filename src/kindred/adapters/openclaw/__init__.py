"""OpenClaw 平台适配层 —— 跟 OpenClaw Gateway 协议对接的边界。

「换一个聊天平台要重写吗？」要 → 属 adapter（平台相关）。本包即 OpenClaw
这一侧的协议实现：JSON-RPC over WebSocket + Gateway v4 device pairing。

模块：
- gateway.py          Gateway WebSocket 客户端（JSON-RPC：chat.history 拉 / chat.send 发）
- device_identity.py  ed25519 设备身份（Gateway v4 self-pairing 握手）

被谁用（这些是「用 adapter 的桥」，属 runtime 编排，不属本层）：
- runtime/io_bridge.py     心主动 push → 调 GatewayClient.send_direct / commit_outbound_context
- runtime/history_sync.py  拉 chat.history 进表 → 调 GatewayClient.fetch_chat_history
- runtime/daemon.py        装配 GatewayClient

未来接 telegram/discord：照 gateway.py 的形状新建 adapters/telegram/ 等。
（多平台 Adapter Protocol 抽象先不做——只 1 个实现时是 YAGNI，等第 2 个平台真来再抽。）
"""

from kindred.adapters.openclaw.device_identity import (
    DeviceIdentity,
    build_device_auth_payload_v3,
)
from kindred.adapters.openclaw.gateway import GatewayClient, GatewayError

__all__ = [
    "DeviceIdentity",
    "GatewayClient",
    "GatewayError",
    "build_device_auth_payload_v3",
]
