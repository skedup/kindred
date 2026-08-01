"""DEPRECATED / MOVED —— 兼容 shim，re-export ``kindred.adapters.openclaw.device_identity``。

本模块在 refactor(adapters) 中迁到 ``kindred.adapters.openclaw.device_identity``
（Gateway v4 ed25519 device pairing 属 OpenClaw 平台协议层，归 adapters/）。

旧路径 ``kindred.runtime.device_identity`` 保留为薄 re-export，避免外部脚本 /
运维探针 / 未入仓部署代码 import 旧路径时硬断。**新代码请直接 import 新路径。**

后续可在确认无外部依赖后移除本 shim。
"""

from __future__ import annotations

from kindred.adapters.openclaw.device_identity import (
    DeviceIdentity,
    build_device_auth_payload_v3,
)

__all__ = ["DeviceIdentity", "build_device_auth_payload_v3"]
