"""Single public installer and disconnect-only uninstaller for Mouth hosts."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import click

from kindred.config import KindredConfigError, load_kindred_config
from kindred.hermes.binding import HermesBindingError
from kindred.hermes.install import (
    HermesCandidate,
    HermesInstallError,
    disconnect_hermes,
    discover_hermes,
    hermes_version_warning,
    install_hermes,
)
from kindred.mouth_host.model import HermesRuntimeModel, OpenClawRuntimeModel
from kindred.openclaw import install as openclaw
from kindred.openclaw.binding import OpenClawBindingError
from kindred.openclaw.install import OpenClawInstallError
from kindred.openclaw.install_services import _install_platform_services
from kindred.resident import require_committed_resident
from kindred.runtime import platform_service


class MouthHostInstallError(RuntimeError):
    """Unified host control-plane operation failed."""


@click.command(name="install")
def install() -> None:
    """Create or reconnect one Resident to an existing Mouth host."""
    if not sys.stdin.isatty():
        raise click.ClickException("controlling TTY required; run kindred install in a terminal")
    try:
        kind, candidate = _select_host(_discover_hosts())
        _confirm_data_flows(kind)
        config_path = _config_path()
        if kind == "openclaw":
            openclaw.install_openclaw(config_path)
        else:
            assert isinstance(candidate, HermesCandidate)
            if warning := hermes_version_warning(candidate.identity):
                click.echo(f"WARNING: {warning}")
            install_hermes(config_path, candidate)
        from kindred.openclaw.doctor import require_doctor_ready

        require_doctor_ready(config_path)
        _install_platform_services(config_path)
    except Exception as exc:
        if isinstance(exc, click.ClickException):
            raise
        raise click.ClickException(_safe_failure("install", exc)) from exc
    click.echo(f"Kindred Resident connected to {kind}.")


@click.command(name="uninstall")
def uninstall() -> None:
    """Disconnect exact-owned host wiring while retaining the Resident."""
    config_path = _config_path()
    try:
        config = load_kindred_config(config_path, load_secrets=False)
        require_committed_resident(config, validate_secrets=False)
        platform_service.control_services(config_path, action="stop")
        if isinstance(config.mouth_host, OpenClawRuntimeModel):
            openclaw.disconnect_openclaw(config)
        elif isinstance(config.mouth_host, HermesRuntimeModel):
            disconnect_hermes(config)
        else:
            raise MouthHostInstallError("Mouth host is not configured")
        platform_service.uninstall_services(config_path)
    except Exception as exc:
        raise click.ClickException(_safe_failure("uninstall", exc)) from exc
    click.echo("Kindred disconnected; Persona, Resident, config and life data were retained.")


def _discover_hosts() -> tuple[tuple[str, HermesCandidate | None], ...]:
    candidates: list[tuple[str, HermesCandidate | None]] = []
    if shutil.which("openclaw"):
        openclaw._require_version()
        candidates.append(("openclaw", None))
    if shutil.which("hermes"):
        candidate = discover_hermes()
        if candidate is not None:
            candidates.append(("hermes", candidate))
    if not candidates:
        raise MouthHostInstallError("no runnable Mouth host found")
    return tuple(candidates)


def _select_host(
    candidates: tuple[tuple[str, HermesCandidate | None], ...],
) -> tuple[str, HermesCandidate | None]:
    for index, (kind, _) in enumerate(candidates, 1):
        click.echo(f"{index}. {kind}")
    if len(candidates) == 1:
        if not click.confirm(f"Use {candidates[0][0]} as the Mouth host?"):
            raise MouthHostInstallError("operator cancelled host selection")
        return candidates[0]
    selected = click.prompt("Select Mouth host", type=click.IntRange(1, len(candidates)))
    return candidates[selected - 1]


def _confirm_data_flows(kind: str) -> None:
    host_text = (
        "OpenClaw Gateway handles Mouth history and outbound dispatch"
        if kind == "openclaw"
        else "Hermes stores injected companion context in api_content and handles outbound dispatch"
    )
    click.echo(
        "Functional data flows:\n"
        "- Persona summary and Heart context -> configured LLM provider\n"
        "- home address -> map provider\n"
        f"- {host_text}\n"
        "- enabled Capabilities may call their own providers"
    )
    if not click.confirm("Understand and continue?"):
        raise MouthHostInstallError("operator did not accept functional data flows")


def _config_path() -> Path:
    return openclaw._config_home() / "kindred/config.yaml"


def _safe_failure(operation: str, exc: Exception) -> str:
    safe_errors = (
        HermesBindingError,
        HermesInstallError,
        MouthHostInstallError,
        OpenClawBindingError,
        OpenClawInstallError,
    )
    if isinstance(exc, safe_errors) or (
        isinstance(exc, KindredConfigError)
        and str(exc).startswith("legacy OpenClaw config is unsupported")
    ):
        detail = str(exc)
    else:
        detail = type(exc).__name__
    retained = (
        "retained data was not removed" if operation == "install" else "Resident data was retained"
    )
    return f"Kindred {operation} failed: {detail}; {retained}"


__all__ = ["install", "uninstall"]
