"""Stable OpenClaw driver behind the unified ``kindred install`` command."""

from __future__ import annotations

import json
import os  # noqa: F401 - compatibility seam for existing installer tests/importers
import subprocess  # noqa: F401 - compatibility seam for existing installer tests/importers
import sys  # noqa: F401 - platform seam used by isolated installer tests
import tempfile
from pathlib import Path

import click

from kindred.adapters.openclaw.gateway import GatewayClient
from kindred.config import KindredConfig, install_runtime_secrets, load_kindred_config
from kindred.config.schema import KindredGatewayConfig
from kindred.openclaw import MOUTH_PLUGIN_DIR  # noqa: F401 - compatibility re-export
from kindred.openclaw import install_binding as _install_binding
from kindred.openclaw import install_plugin as _install_plugin
from kindred.openclaw import install_services as _install_services
from kindred.openclaw.binding import (
    OpenClawBindingError,
    binding_payload,
    require_openclaw_binding,
)
from kindred.openclaw.install_binding import _Agent, _DiscoveredWire  # noqa: F401
from kindred.openclaw.install_plugin import OpenClawInstallError
from kindred.openclaw.wire import wire_from_session  # noqa: F401
from kindred.providers.location_baidu import BaiduLocationProvider  # noqa: F401
from kindred.relationship._projection import (
    make_google_relationship_projector,  # noqa: F401
    make_openai_relationship_projector,  # noqa: F401
    make_xai_relationship_projector,  # noqa: F401
)
from kindred.relationship.initialize import RelationshipInitError  # noqa: F401
from kindred.relationship.models import RelationshipBootstrap  # noqa: F401
from kindred.resident import ResidentInitError, require_committed_resident  # noqa: F401
from kindred.resident._contract import PersonaPaths
from kindred.resident._projection import make_google_persona_projector  # noqa: F401
from kindred.runtime import platform_service  # noqa: F401

OPENCLAW_VERSION = _install_plugin.OPENCLAW_VERSION
OPENCLAW_BUILD = _install_plugin.OPENCLAW_BUILD
OPENCLAW_PROTOCOL = _install_plugin.OPENCLAW_PROTOCOL
OPENCLAW_PROFILES = _install_plugin.OPENCLAW_PROFILES
MOUTH_PLUGIN_ID = _install_plugin.MOUTH_PLUGIN_ID
MOUTH_PLUGIN_VERSION = _install_plugin.MOUTH_PLUGIN_VERSION
_MOUTH_PLUGIN_V1_DIGEST = _install_plugin._MOUTH_PLUGIN_V1_DIGEST
_MOUTH_PLUGIN_V2_DIGEST = _install_plugin._MOUTH_PLUGIN_V2_DIGEST
_MOUTH_PLUGIN_V3_DIGEST = _install_plugin._MOUTH_PLUGIN_V3_DIGEST
_OPENCLAW_ENV_KEYS = _install_plugin._OPENCLAW_ENV_KEYS
_OPENCLAW_CREDENTIAL_KEYS = _install_plugin._OPENCLAW_CREDENTIAL_KEYS
_openclaw_environment = _install_plugin._openclaw_environment
_command = _install_plugin._command
_json = _install_plugin._json
_require_version = _install_plugin._require_version
_version_warning = _install_plugin._version_warning
_agents = _install_binding._agents
_select_agent = _install_binding._select_agent
_agent_verbose = _install_plugin._agent_verbose
_agent_verbose_default = _install_plugin._agent_verbose_default
_configure_agent_verbose_off = _install_plugin._configure_agent_verbose_off
_config_home = _install_services._config_home
_secret = _install_services._secret
_ensure_resident = _install_services._ensure_resident
_require_agent = _install_services._require_agent
_require_persona = _install_services._require_persona
_ensure_relationship = _install_services._ensure_relationship
_install_platform_services = _install_services._install_platform_services
_binding_accounts = _install_binding._binding_accounts
_approve_pending_device = _install_binding._approve_pending_device
_discover_wire = _install_binding._discover_wire
_confirm_rebind = _install_binding._confirm_rebind
_inspect_plugin = _install_plugin._inspect_plugin
_require_plugin_report = _install_plugin._require_plugin_report
_plugin_generation = _install_plugin._plugin_generation
_plugin_tree_digest = _install_plugin._plugin_tree_digest
_binding_generation = _install_binding._binding_generation
_plugin = _install_plugin._plugin
require_openclaw_runtime = _install_plugin.require_openclaw_runtime
_uninstall_plugin = _install_plugin._uninstall_plugin
_publish_wire = _install_binding._publish_wire
_atomic_write = _install_binding._atomic_write


def install_openclaw(config_path: Path) -> None:
    """Run the retained OpenClaw stages behind the unified installer."""
    warning = _version_warning(_require_version())
    if warning:
        click.echo(f"WARNING: {warning}")
    agent = _select_agent(_agents())
    persona = PersonaPaths.openclaw(agent.workspace)
    config = _ensure_resident(
        persona,
        resident_id=agent.agent_id,
        config_path=config_path,
        require_gateway=True,
    )
    install_runtime_secrets(config.resident.secrets_file)
    config = load_kindred_config(config_path)
    gateway_port = int(_json(["openclaw", "config", "get", "gateway.port", "--json"]))
    gateway = GatewayClient.from_config(
        KindredGatewayConfig(
            host=config.gateway.host,
            port=gateway_port,
            token=config.gateway.token,
        ),
        identity_path=config.paths.life_root / ".device-identity.json",
    )
    discovered = _discover_wire(gateway, agent.agent_id)
    _confirm_rebind(config, discovered, binding_state=_binding_generation(config))
    _ensure_relationship(config, persona)
    _configure_agent_verbose_off(agent.agent_id)
    _plugin(config)
    _publish_wire(config_path, discovered.wire, gateway_port, agent=agent)
    config = load_kindred_config(config_path)
    require_committed_resident(config)
    binding = binding_payload(config)
    with tempfile.TemporaryDirectory(prefix="kindred-open3b-") as temp:
        staged = Path(temp) / ".config/kindred/openclaw-binding.json"
        _atomic_write(staged, json.dumps(binding, sort_keys=True) + "\n")
        require_openclaw_binding(config, home=Path(temp))
    _atomic_write(
        Path.home() / ".config/kindred/openclaw-binding.json",
        json.dumps(binding, sort_keys=True) + "\n",
    )
    require_openclaw_runtime(config)


def disconnect_openclaw(config: KindredConfig) -> None:
    """Remove exact-owned OpenClaw Plugin and binding only."""
    binding_path = Path.home() / ".config/kindred/openclaw-binding.json"
    if binding_path.exists() or binding_path.is_symlink():
        if _binding_generation(config) != "v3":
            raise OpenClawBindingError("OpenClaw Mouth binding is missing or inconsistent")
        binding_path.unlink()
    _uninstall_plugin()


__all__ = [
    "OpenClawInstallError",
    "disconnect_openclaw",
    "install_openclaw",
    "require_openclaw_runtime",
]
