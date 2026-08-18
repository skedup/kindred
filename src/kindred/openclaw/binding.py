"""Mouth Plugin 摘要 binding 的构造与运行期 fail-closed 校验。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kindred.config import KindredConfig
from kindred.mouth_host.model import OpenClawRuntimeModel


class OpenClawBindingError(RuntimeError):
    """Binding 缺失、漂移或无法与 Resident/wire 对账。"""


def binding_payload(config: KindredConfig) -> dict[str, Any]:
    """从私有 wire 生成稳定的 Plugin 授权 binding。"""
    resident, model = config.resident, config.mouth_host
    if (
        not isinstance(model, OpenClawRuntimeModel)
        or not resident.install_id
        or resident.marker_path is None
    ):
        raise OpenClawBindingError("Resident or OpenClaw wire is incomplete")
    wire = model.wire
    return {
        "schema_version": 3,
        "install_id": resident.install_id,
        "agent_id": model.agent_id,
        "workspace_digest": _digest(str(model.workspace)),
        "session_key": wire.transcript_session,
        "peer_scope": {
            "message_provider": wire.approved_peer.provider,
            "channel_id_digest": _digest(wire.approved_peer.target),
        },
        "bundle_path": str(config.paths.context_bundle.resolve()),
        "resident_marker_path": str(resident.marker_path.resolve()),
    }


def require_openclaw_binding(
    config: KindredConfig, *, home: Path | None = None
) -> dict[str, Any] | None:
    """Config 已含 wire 时，固定 binding 与 Resident marker 必须 exact match。"""
    if not isinstance(config.mouth_host, OpenClawRuntimeModel):
        return None
    path = (home or Path.home()) / ".config/kindred/openclaw-binding.json"
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise ValueError
        actual = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(actual, Mapping):
            raise ValueError
        expected = binding_payload(config)
        marker = json.loads(Path(expected["resident_marker_path"]).read_text(encoding="utf-8"))
        valid = (
            actual == expected
            and isinstance(marker, Mapping)
            and marker.get("install_id") == expected["install_id"]
        )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise OpenClawBindingError("OpenClaw Mouth binding is missing or inconsistent") from exc
    if not valid:
        raise OpenClawBindingError("OpenClaw Mouth binding is missing or inconsistent")
    return dict(actual)


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


__all__ = ["OpenClawBindingError", "binding_payload", "require_openclaw_binding"]
