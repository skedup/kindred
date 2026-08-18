"""Hermes control driver used only by the unified Kindred installer."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml

from kindred.config import KindredConfig, install_runtime_secrets, load_kindred_config
from kindred.hermes import HERMES_MOUTH_PLUGIN_DIR
from kindred.hermes.binding import binding_payload, require_hermes_binding
from kindred.hermes.mouth_plugin import plugin_digest
from kindred.hermes.runtime import run_hermes_command
from kindred.hermes.wire import HERMES_BASELINE, HermesRuntimeIdentity, HermesWire
from kindred.mouth_host.config import atomic_write, publish_host
from kindred.mouth_host.model import HermesRuntimeModel
from kindred.resident import PersonaPaths, read_owned_persona_file, require_committed_resident

_PLUGIN_NAME = "kindred-mouth"
_VERSION_RE = re.compile(
    r"^Hermes Agent v(?P<package>\d+\.\d+\.\d+) "
    r"\((?P<tag>\d{4}\.\d{1,2}\.\d{1,2})\)$",
    re.MULTILINE,
)


class HermesInstallError(RuntimeError):
    """Hermes discovery or exact-owned wiring failed."""


@dataclass(frozen=True, slots=True)
class HermesCandidate:
    executable: Path
    home: Path
    identity: HermesRuntimeIdentity = HERMES_BASELINE


_UNVERIFIED_VERSION_WARNING = "当前 Hermes identity 尚未经过 Kindred 验证；将继续执行完整合同检查"


def hermes_version_warning(identity: HermesRuntimeIdentity) -> str | None:
    return _UNVERIFIED_VERSION_WARNING if identity != HERMES_BASELINE else None


def _runtime_identity(executable: Path, home: Path) -> HermesRuntimeIdentity:
    code, output = _run(executable, home, "--version")
    match = _VERSION_RE.search(output.decode("utf-8", errors="replace")) if code == 0 else None
    if match is None:
        raise HermesInstallError("Hermes version identity schema changed")
    return (f"v{match['tag']}", match["package"])


def discover_hermes(executable: str | None = None) -> HermesCandidate | None:
    resolved = executable or shutil.which("hermes")
    if resolved is None:
        return None
    path = Path(resolved).resolve()
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()
    return HermesCandidate(path, home, _runtime_identity(path, home))


def install_hermes(config_path: Path, candidate: HermesCandidate) -> None:
    persona = _prepare_persona(candidate.home)
    from kindred.openclaw import install as shared

    config = shared._ensure_resident(
        persona,
        resident_id="hermes",
        config_path=config_path,
        require_gateway=False,
    )
    install_runtime_secrets(config.resident.secrets_file)
    wire = _select_wire(candidate)
    model = HermesRuntimeModel(kind="hermes", wire=wire, identity=candidate.identity)
    current = config.mouth_host
    if (
        isinstance(current, HermesRuntimeModel)
        and current.wire != model.wire
        and not click.confirm("Hermes session identity changed; explicitly rebind?", default=False)
    ):
        raise HermesInstallError("Hermes session identity changed; rebind not confirmed")
    shared._ensure_relationship(config, persona)
    _install_plugin(candidate)
    publish_host(config_path, model)
    config = load_kindred_config(config_path)
    require_committed_resident(config)
    payload = binding_payload(config)
    atomic_write(
        candidate.home / ".kindred/mouth-binding.json",
        json.dumps(payload, sort_keys=True) + "\n",
    )
    require_hermes_runtime(config)


def require_hermes_runtime(config: KindredConfig) -> HermesRuntimeIdentity:
    model = config.mouth_host
    if not isinstance(model, HermesRuntimeModel):
        raise HermesInstallError("Hermes Mouth host is not configured")
    destination = model.wire.host_home / "plugins" / _PLUGIN_NAME
    if plugin_digest(destination) != plugin_digest(HERMES_MOUTH_PLUGIN_DIR):
        raise HermesInstallError("installed Hermes Plugin contract drifted")
    if not _plugin_enabled(model.wire.host_home):
        raise HermesInstallError("Hermes Plugin is not enabled")
    require_hermes_binding(config)
    identity = _runtime_identity(model.wire.host_executable, model.wire.host_home)
    if identity != model.identity:
        raise HermesInstallError("Hermes runtime identity changed; rerun kindred install")
    return identity


def disconnect_hermes(config: KindredConfig) -> None:
    model = config.mouth_host
    if not isinstance(model, HermesRuntimeModel):
        raise HermesInstallError("Hermes Mouth host is not configured")
    binding = model.wire.host_home / ".kindred/mouth-binding.json"
    destination = model.wire.host_home / "plugins" / _PLUGIN_NAME
    if destination.is_symlink() or (
        destination.exists()
        and plugin_digest(destination) != plugin_digest(HERMES_MOUTH_PLUGIN_DIR)
    ):
        raise HermesInstallError("installed Hermes Plugin ownership drifted")
    if binding.exists() or binding.is_symlink():
        require_hermes_binding(config)
        binding.unlink()
    if not destination.exists():
        return
    code, _ = _run(
        model.wire.host_executable,
        model.wire.host_home,
        "plugins",
        "disable",
        _PLUGIN_NAME,
    )
    if code != 0:
        raise HermesInstallError("Hermes Plugin disable failed")
    shutil.rmtree(destination)


def _prepare_persona(home: Path) -> PersonaPaths:
    persona = PersonaPaths.hermes(home)
    read_owned_persona_file(persona.soul_full, name="SOUL.md")
    persona.identity.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not persona.identity.exists():
        identity = click.prompt("Kindred companion IDENTITY.md content").strip()
        if not identity:
            raise HermesInstallError("Hermes companion identity must not be empty")
        with persona.identity.open("x", encoding="utf-8") as handle:
            handle.write(identity + "\n")
    return persona


def _select_wire(candidate: HermesCandidate) -> HermesWire:
    code, output = _run(
        candidate.executable,
        candidate.home,
        "sessions",
        "export",
        "-",
        "--format",
        "jsonl",
    )
    if code != 0:
        raise HermesInstallError("Hermes session discovery failed")
    rows: list[dict[str, Any]] = []
    try:
        for line in output.splitlines():
            row = json.loads(line)
            if (
                isinstance(row, dict)
                and row.get("chat_type") == "dm"
                and row.get("source") != "cli"
            ):
                rows.append(row)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HermesInstallError("Hermes session export schema changed") from exc
    if not rows:
        raise HermesInstallError("no approved direct Hermes session is available")
    for index, row in enumerate(rows, 1):
        click.echo(f"{index}. {row.get('source')} direct peer")
    selected = click.prompt("Select approved Hermes peer", type=click.IntRange(1, len(rows)))
    row = rows[selected - 1]
    try:
        return HermesWire(
            host_executable=candidate.executable,
            host_home=candidate.home,
            approved_platform=row["source"],
            approved_sender_id=row["user_id"],
            canonical_session_id=row["id"],
            canonical_session_key=row["session_key"],
            outbound_chat_id=row["chat_id"],
            outbound_sender_id=row["user_id"],
            outbound_thread_id=row.get("thread_id"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HermesInstallError("Hermes direct session schema changed") from exc


def _install_plugin(candidate: HermesCandidate) -> None:
    destination = candidate.home / "plugins" / _PLUGIN_NAME
    if destination.exists():
        if plugin_digest(destination) != plugin_digest(HERMES_MOUTH_PLUGIN_DIR):
            raise HermesInstallError("installed Hermes Plugin ownership drifted")
    else:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
            staged = Path(temp) / _PLUGIN_NAME
            shutil.copytree(
                HERMES_MOUTH_PLUGIN_DIR,
                staged,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            os.replace(staged, destination)
    code, _ = _run(
        candidate.executable,
        candidate.home,
        "plugins",
        "enable",
        _PLUGIN_NAME,
        "--no-allow-tool-override",
    )
    if code != 0 or not _plugin_enabled(candidate.home):
        raise HermesInstallError("Hermes Plugin enable failed")


def _plugin_enabled(home: Path) -> bool:
    try:
        raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        plugins = raw.get("plugins", {})
        return _PLUGIN_NAME in plugins.get("enabled", []) and _PLUGIN_NAME not in plugins.get(
            "disabled", []
        )
    except (AttributeError, OSError, TypeError, yaml.YAMLError):
        return False


def _run(executable: Path, home: Path, *args: str) -> tuple[int, bytes]:
    try:
        return run_hermes_command(executable, home, (str(executable), *args))
    except RuntimeError as exc:
        raise HermesInstallError("Hermes CLI is unavailable") from exc


__all__ = [
    "HermesCandidate",
    "HermesInstallError",
    "disconnect_hermes",
    "discover_hermes",
    "hermes_version_warning",
    "install_hermes",
    "require_hermes_runtime",
]
