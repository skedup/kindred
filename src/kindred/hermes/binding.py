"""Strict ownership binding for the retained Hermes Mouth Plugin."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from kindred.config import KindredConfig
from kindred.hermes import HERMES_MOUTH_PLUGIN_DIR
from kindred.hermes.mouth_plugin import load_binding, plugin_digest
from kindred.mouth_host.model import HermesRuntimeModel


class HermesBindingError(RuntimeError):
    pass


def binding_payload(config: KindredConfig) -> dict[str, Any]:
    model, resident = config.mouth_host, config.resident
    if not isinstance(model, HermesRuntimeModel) or not resident.install_id:
        raise HermesBindingError("Resident or Hermes wire is incomplete")
    wire = model.wire
    return {
        "schema_version": 3,
        "install_id": resident.install_id,
        "plugin_digest": plugin_digest(HERMES_MOUTH_PLUGIN_DIR),
        "host_identity": list(model.identity),
        "session_id": wire.canonical_session_id,
        "platform": wire.approved_platform,
        "sender_id": wire.approved_sender_id,
        "bundle_path": str(config.paths.context_bundle.resolve()),
    }


def require_hermes_binding(config: KindredConfig) -> dict[str, Any]:
    model, marker_path = config.mouth_host, config.resident.marker_path
    if not isinstance(model, HermesRuntimeModel) or marker_path is None:
        raise HermesBindingError("Hermes Mouth binding is missing or inconsistent")
    try:
        actual = load_binding(model.wire.host_home)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        valid = (
            actual == binding_payload(config)
            and isinstance(marker, Mapping)
            and marker.get("install_id") == config.resident.install_id
        )
    except (json.JSONDecodeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HermesBindingError("Hermes Mouth binding is missing or inconsistent") from exc
    if not valid:
        raise HermesBindingError("Hermes Mouth binding is missing or inconsistent")
    return actual


__all__ = ["HermesBindingError", "binding_payload", "require_hermes_binding"]
