"""Agent discovery, approved-peer binding, and private wire publication."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kindred.adapters.openclaw.gateway import GatewayClient
from kindred.config import KindredConfig
from kindred.mouth_host.config import MouthHostConfigError, atomic_write, publish_host
from kindred.mouth_host.model import OpenClawRuntimeModel
from kindred.openclaw.install_plugin import OpenClawInstallError
from kindred.openclaw.wire import OpenClawWire, OpenClawWireError


@dataclass(frozen=True)
class _Agent:
    agent_id: str
    workspace: Path
    label: str


@dataclass(frozen=True)
class _DiscoveredWire:
    wire: OpenClawWire


def _ops() -> Any:
    """Resolve the stable facade lazily so its patch seams stay authoritative."""
    from kindred.openclaw import install

    return install


def _agents() -> tuple[_Agent, ...]:
    ops = _ops()
    raw = ops._json(["openclaw", "agents", "list", "--json"])
    if not isinstance(raw, list):
        raise OpenClawInstallError("OpenClaw agents list schema changed")
    result: list[_Agent] = []
    try:
        for row in raw:
            if not isinstance(row, Mapping):
                raise ValueError
            agent_id, workspace = row["id"], row["workspace"]
            if not isinstance(agent_id, str) or not isinstance(workspace, str):
                raise ValueError
            label = row.get("identityName") or row.get("name") or agent_id
            if not isinstance(label, str):
                raise ValueError
            result.append(
                _Agent(
                    agent_id.strip(),
                    ops.Path(workspace).expanduser().resolve(True),
                    label,
                )
            )
    except (KeyError, OSError, ValueError) as exc:
        raise OpenClawInstallError("OpenClaw agents list schema changed") from exc
    if not result or any(not item.agent_id for item in result):
        raise OpenClawInstallError("no usable OpenClaw agent")
    return tuple(result)


def _select_agent(candidates: tuple[_Agent, ...]) -> _Agent:
    ops = _ops()
    for index, agent in enumerate(candidates, 1):
        ops.click.echo(f"{index}. {agent.label} ({agent.agent_id})")
    if len(candidates) == 1:
        if not ops.click.confirm("使用这个 OpenClaw agent/workspace？"):
            raise OpenClawInstallError("operator cancelled agent selection")
        return candidates[0]
    index = ops.click.prompt("选择 agent", type=ops.click.IntRange(1, len(candidates)))
    return cast(_Agent, candidates[index - 1])


def _binding_accounts(agent_id: str) -> frozenset[tuple[str, str]]:
    ops = _ops()
    raw = ops._json(["openclaw", "agents", "bindings", "--agent", agent_id, "--json"])
    accounts: set[tuple[str, str]] = set()
    try:
        if not isinstance(raw, list):
            raise ValueError
        for row in raw:
            match = row["match"]
            if row["agentId"] != agent_id or not isinstance(match, Mapping):
                raise ValueError
            channel, account = match["channel"], match["accountId"]
            if not isinstance(channel, str) or not isinstance(account, str):
                raise ValueError
            accounts.add((channel, account))
    except (KeyError, TypeError, ValueError) as exc:
        raise OpenClawInstallError("OpenClaw agent bindings schema changed") from exc
    if len(accounts) != 1:
        raise OpenClawInstallError("V1 requires one dedicated OpenClaw channel account")
    return frozenset(accounts)


def _approve_pending_device(gateway: GatewayClient) -> None:
    ops = _ops()
    env = {"OPENCLAW_GATEWAY_TOKEN": gateway.token}
    raw = ops._json(["openclaw", "devices", "list", "--json"], extra_env=env)
    try:
        pending = raw["pending"]
        if not isinstance(pending, list):
            raise ValueError
        matches = [
            row
            for row in pending
            if isinstance(row, Mapping)
            and row.get("deviceId") == gateway.identity.device_id
            and row.get("publicKey") == gateway.identity.public_key_b64url
            and row.get("clientId") == "gateway-client"
            and row.get("clientMode") == "backend"
            and row.get("role") == "operator"
            and row.get("scopes") == ["operator.admin"]
            and row.get("isRepair") is False
        ]
        request_id = matches[0]["requestId"] if len(matches) == 1 else None
        if not isinstance(request_id, str) or not request_id:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise OpenClawInstallError("Kindred Gateway device approval is unavailable") from exc
    ops.click.echo("Kindred 需要批准一个本机 Gateway device，才能读取已选 direct session。")
    if not ops.click.confirm("批准这个 Kindred device？"):
        raise OpenClawInstallError("operator cancelled Kindred device approval")
    ops._json(
        ["openclaw", "devices", "approve", request_id, "--json"],
        extra_env=env,
    )


def _discover_wire(gateway: GatewayClient, agent_id: str) -> _DiscoveredWire:
    ops = _ops()
    scope = ops._json(["openclaw", "config", "get", "session.dmScope", "--json"])
    accounts = ops._binding_accounts(agent_id)
    bindings = ops._json(["openclaw", "config", "get", "bindings", "--json"])
    if not isinstance(bindings, list):
        raise OpenClawInstallError("OpenClaw bindings schema changed")
    for row in bindings:
        if not isinstance(row, Mapping) or row.get("agentId") != agent_id:
            continue
        match, session = row.get("match"), row.get("session", {})
        if not isinstance(match, Mapping) or not isinstance(session, Mapping):
            raise OpenClawInstallError("OpenClaw bindings schema changed")
        account = (match.get("channel"), match.get("accountId"))
        if account in accounts and session.get("dmScope", scope) != "per-channel-peer":
            raise OpenClawInstallError("OpenClaw binding dmScope is not isolated")
    response = gateway.list_sessions(agent_id=agent_id)
    error = response.get("error")
    if isinstance(error, str) and "pairing required" in error.lower():
        ops._approve_pending_device(gateway)
        response = gateway.list_sessions(agent_id=agent_id)
    rows = response.get("sessions")
    if scope != "per-channel-peer" or response.get("ok") is not True or not isinstance(rows, list):
        raise OpenClawInstallError("OpenClaw session isolation is unavailable")
    candidates: list[tuple[Mapping[str, Any], OpenClawWire]] = []
    for row in rows:
        try:
            wire = ops.wire_from_session(row, dm_scope=scope)
        except OpenClawWireError:
            continue
        if (wire.approved_peer.provider, wire.approved_peer.account_id) in accounts:
            candidates.append((row, wire))
    if not candidates:
        raise OpenClawInstallError("no approved direct OpenClaw session is available")
    for index, (_, wire) in enumerate(candidates, 1):
        target = wire.approved_peer.target
        masked = f"{target[:2]}…{target[-2:]}" if len(target) > 4 else "****"
        ops.click.echo(f"{index}. {wire.approved_peer.provider} direct peer {masked}")
    selected = ops.click.prompt(
        "选择唯一 approved peer",
        type=ops.click.IntRange(1, len(candidates)),
    )
    wire = candidates[selected - 1][1]
    history = gateway.fetch_chat_history(wire.transcript_session, limit=1)
    session_id = history.get("sessionId")
    if (
        history.get("ok") is not True
        or history.get("sessionKey") != wire.transcript_session
        or history.get("sessionInfoKey") != wire.transcript_session
        or not isinstance(session_id, str)
        or not session_id.strip()
        or session_id != session_id.strip()
        or len(session_id) > 512
    ):
        raise OpenClawInstallError("OpenClaw history identity validation failed")
    return _DiscoveredWire(cast(OpenClawWire, wire))


def _confirm_rebind(
    config: KindredConfig,
    discovered: _DiscoveredWire,
    *,
    binding_state: str,
) -> None:
    ops = _ops()
    if binding_state != "v3":
        return
    current = config.mouth_host
    identity_changed = (
        not isinstance(current, OpenClawRuntimeModel) or current.wire != discovered.wire
    )
    if identity_changed and not ops.click.confirm(
        "已绑定的 Mouth 会话身份发生变化，确认显式重新绑定？",
        default=False,
    ):
        raise OpenClawInstallError("OpenClaw session identity changed; rebind not confirmed")


def _binding_generation(
    config: KindredConfig,
    *,
    home: Path | None = None,
) -> str:
    ops = _ops()
    root = home or ops.Path.home()
    path = root / ".config/kindred/openclaw-binding.json"
    if not path.exists() and not path.is_symlink():
        return "absent"
    try:
        ops.require_openclaw_binding(config, home=root)
        return "v3"
    except ops.OpenClawBindingError:
        pass
    return "unknown"


def _publish_wire(
    config_path: Path,
    wire: OpenClawWire,
    gateway_port: int,
    *,
    agent: _Agent,
) -> None:
    try:
        publish_host(
            config_path,
            OpenClawRuntimeModel(
                kind="openclaw",
                wire=wire,
                agent_id=agent.agent_id,
                workspace=agent.workspace,
            ),
            gateway_port=gateway_port,
        )
    except MouthHostConfigError as exc:
        raise OpenClawInstallError("Kindred config is unavailable") from exc


def _atomic_write(path: Path, text: str) -> None:
    atomic_write(path, text)
