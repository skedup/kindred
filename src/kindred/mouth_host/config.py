"""Atomic serialization for the selected Mouth host."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from kindred.mouth_host.model import HostRuntimeModel, OpenClawRuntimeModel


class MouthHostConfigError(RuntimeError):
    pass


def model_payload(model: HostRuntimeModel) -> dict[str, object]:
    return model.model_dump(mode="json")


def publish_host(
    config_path: Path,
    model: HostRuntimeModel,
    *,
    gateway_port: int | None = None,
) -> None:
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError
        daemon, gateway = raw.setdefault("daemon", {}), raw.setdefault("gateway", {})
        if not isinstance(daemon, dict) or not isinstance(gateway, dict):
            raise ValueError
        raw["mouth_host"] = model_payload(model)
        daemon["session_key"] = (
            model.wire.transcript_session
            if isinstance(model, OpenClawRuntimeModel)
            else model.wire.canonical_session_id
        )
        if gateway_port is not None:
            gateway["port"] = gateway_port
        text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise MouthHostConfigError("Kindred config is unavailable") from exc
    atomic_write(config_path, text)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
        staged = Path(file.name)
    try:
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


__all__ = ["MouthHostConfigError", "atomic_write", "model_payload", "publish_host"]
