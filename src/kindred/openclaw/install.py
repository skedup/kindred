"""唯一交互入口 ``kindred openclaw install`` 及其原子接线。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import click
import yaml

from kindred.adapters.openclaw.gateway import GatewayClient
from kindred.config import (
    KindredConfig,
    install_runtime_secrets,
    load_kindred_config,
    merge_runtime_secrets,
)
from kindred.config.schema import KindredGatewayConfig
from kindred.openclaw import MOUTH_PLUGIN_DIR
from kindred.openclaw.binding import (
    OpenClawBindingError,
    binding_payload,
    require_openclaw_binding,
)
from kindred.openclaw.wire import OpenClawWire, OpenClawWireError, wire_from_session
from kindred.providers.location_baidu import BaiduLocationProvider
from kindred.relationship._projection import (
    make_google_relationship_projector,
    make_openai_relationship_projector,
)
from kindred.relationship.initialize import RelationshipInitError, initialize_user_relationship
from kindred.relationship.models import RelationshipBootstrap
from kindred.resident import (
    ResidentInitError,
    ResidentInitRequest,
    WorldResolution,
    initialize_resident,
    read_owned_persona_file,
    require_committed_resident,
)
from kindred.resident._projection import make_google_persona_projector
from kindred.runtime import platform_service

OPENCLAW_VERSION = "2026.6.10"
OPENCLAW_BUILD = "aa69b12"
MOUTH_PLUGIN_ID = "kindred-mouth"
MOUTH_PLUGIN_VERSION = "0.3.0"
# Packaged v1 tree; retained only for controlled replacement detection.
_MOUTH_PLUGIN_V1_DIGEST = "002a628ce6f3b0b84a4fd150fea3812ef196a77c1096cf6bdb0ce6b6d5dc7f60"
# Packaged v2 tree; retained only for the controlled v2 -> v3 transition.
_MOUTH_PLUGIN_V2_DIGEST = "be6ec3e16734fa7b0441d060870449da5d1ebb508700846b18ffcc716594f4de"
_OPENCLAW_ENV_KEYS = (
    "HOME",
    "PATH",
    "OPENCLAW_HOME",
    "OPENCLAW_PROFILE",
    "OPENCLAW_STATE_DIR",
    "OPENCLAW_CONFIG_PATH",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "USER",
    "LOGNAME",
    "SHELL",
)
_OPENCLAW_CREDENTIAL_KEYS = frozenset({"OPENCLAW_GATEWAY_TOKEN"})


class OpenClawInstallError(RuntimeError):
    """安装失败；文本不得拼接 OpenClaw 原始输出或私有 wire。"""


@dataclass(frozen=True)
class _Agent:
    agent_id: str
    workspace: Path
    label: str


@dataclass(frozen=True)
class _DiscoveredWire:
    wire: OpenClawWire


def _openclaw_environment(extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    extra = dict(extra_env or {})
    if extra.keys() - _OPENCLAW_CREDENTIAL_KEYS:
        raise OpenClawInstallError("unsupported OpenClaw CLI environment")
    return {
        **{key: os.environ[key] for key in _OPENCLAW_ENV_KEYS if key in os.environ},
        **extra,
    }


def _command(
    argv: Sequence[str],
    *,
    optional_missing: bool = False,
    extra_env: Mapping[str, str] | None = None,
) -> str | None:
    try:
        result = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
            env=_openclaw_environment(extra_env),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OpenClawInstallError("OpenClaw CLI is unavailable") from exc
    if result.returncode:
        if optional_missing and "not found" in result.stderr.lower():
            return None
        raise OpenClawInstallError("OpenClaw CLI command failed")
    return result.stdout


def _json(argv: Sequence[str], *, extra_env: Mapping[str, str] | None = None) -> Any:
    text = _command(argv) if extra_env is None else _command(argv, extra_env=extra_env)
    try:
        return json.loads(text or "")
    except json.JSONDecodeError as exc:
        raise OpenClawInstallError("OpenClaw CLI JSON schema changed") from exc


def _require_version() -> None:
    text = (_command(["openclaw", "--version"]) or "").strip()
    matched = re.fullmatch(r"OpenClaw ([^\s]+) \(([^)]+)\)", text)
    if matched is None or matched.groups() != (OPENCLAW_VERSION, OPENCLAW_BUILD):
        raise OpenClawInstallError("unsupported OpenClaw version or build")


def _agents() -> tuple[_Agent, ...]:
    raw = _json(["openclaw", "agents", "list", "--json"])
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
                _Agent(agent_id.strip(), Path(workspace).expanduser().resolve(True), label)
            )
    except (KeyError, OSError, ValueError) as exc:
        raise OpenClawInstallError("OpenClaw agents list schema changed") from exc
    if not result or any(not item.agent_id for item in result):
        raise OpenClawInstallError("no usable OpenClaw agent")
    return tuple(result)


def _select_agent(candidates: tuple[_Agent, ...]) -> _Agent:
    for index, agent in enumerate(candidates, 1):
        click.echo(f"{index}. {agent.label} ({agent.agent_id})")
    if len(candidates) == 1:
        if not click.confirm("使用这个 OpenClaw agent/workspace？"):
            raise OpenClawInstallError("operator cancelled agent selection")
        return candidates[0]
    index = click.prompt("选择 agent", type=click.IntRange(1, len(candidates)))
    return cast(_Agent, candidates[index - 1])


def _agent_verbose(agent_id: str) -> tuple[int, str | None]:
    raw = _json(["openclaw", "config", "get", "agents.list", "--json"])
    try:
        if not isinstance(raw, list):
            raise ValueError
        matches = [
            (index, row)
            for index, row in enumerate(raw)
            if isinstance(row, Mapping) and row.get("id") == agent_id
        ]
        if len(matches) != 1:
            raise ValueError
        index, row = matches[0]
        value = row.get("verboseDefault")
        if value not in (None, "off", "on", "full"):
            raise ValueError
        return index, cast(str | None, value)
    except (TypeError, ValueError) as exc:
        raise OpenClawInstallError("OpenClaw agent verbosity schema changed") from exc


def _agent_verbose_default(agent_id: str) -> str | None:
    return _agent_verbose(agent_id)[1]


def _configure_agent_verbose_off(agent_id: str) -> None:
    index, value = _agent_verbose(agent_id)
    if value == "off":
        return
    click.echo("将关闭所选 Mouth agent 的工具调用过程展示。")
    _command(
        [
            "openclaw",
            "config",
            "set",
            f"agents.list[{index}].verboseDefault",
            '"off"',
            "--strict-json",
        ]
    )
    if _agent_verbose_default(agent_id) != "off":
        raise OpenClawInstallError("OpenClaw agent verbosity update did not take effect")


def _config_home() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    return Path(root).expanduser() if root else Path.home() / ".config"


def _confirm_data_flows() -> None:
    click.echo(
        "安装与运行会按功能把以下数据发送给你配置的服务：\n"
        "- Persona 摘要与 Heart 处境上下文 -> LLM provider\n"
        "- home address -> 地图 provider\n"
        "- Mouth/history/发送 -> 本机 OpenClaw Gateway\n"
        "- 以后显式启用的 Capability -> 该能力自己的 provider"
    )
    if not click.confirm("理解这些功能性数据流并继续？"):
        raise OpenClawInstallError("operator did not accept functional data flows")


def _secret(name: str, *, optional: bool = False) -> str:
    value = os.environ.get(name, "")
    if value:
        return value
    return cast(
        str,
        click.prompt(
            name,
            hide_input=True,
            default="" if optional else None,
            show_default=False,
        ),
    )


def _ensure_resident(agent: _Agent, config_path: Path) -> KindredConfig:
    if config_path.exists():
        config = load_kindred_config(config_path)
        require_committed_resident(config)
        _require_agent(config, agent)
        return config
    click.echo("将读取 SOUL.md / IDENTITY.md，并允许 Dream 更新该 workspace。")
    if not click.confirm("确认继续 Persona 投影？"):
        raise OpenClawInstallError("Persona consent is required")
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    life_root = (
        Path(click.prompt("Kindred life_root", default=str(data_home / "kindred/life")))
        .expanduser()
        .resolve()
    )
    home_address = click.prompt("ta 的 home address").strip()
    click.echo("当前公开安装适配仅支持 Heart provider=google。")
    model = click.prompt("Heart Gemini model", default="gemini-2.5-flash").strip()
    secrets = {
        "BAIDU_MAP_AK": _secret("BAIDU_MAP_AK"),
        "GEMINI_API_KEY": _secret("GEMINI_API_KEY"),
        "KINDRED_GATEWAY_TOKEN": _secret("KINDRED_GATEWAY_TOKEN"),
    }
    baidu_sk = _secret("BAIDU_MAP_SK", optional=True)
    if baidu_sk:
        secrets["BAIDU_MAP_SK"] = baidu_sk
    projector = make_google_persona_projector(api_key=secrets["GEMINI_API_KEY"], model=model)

    def resolve_world(address: str, ak: str, sk: str | None, now: datetime) -> WorldResolution:
        resolved = BaiduLocationProvider(ak=ak, sk=sk or "").resolve_home(address, at=now)
        return WorldResolution(address=resolved[0], city=resolved[1], timezone=resolved[2])

    initialize_resident(
        ResidentInitRequest(
            resident_id=click.prompt("resident id", default=agent.agent_id).strip(),
            agent_id=agent.agent_id,
            workspace=agent.workspace,
            life_root=life_root,
            xdg_config_home=_config_home(),
            home_address=home_address,
            llm_provider="google",
            llm_model=model,
            secrets=secrets,
            install_now=datetime.now(timezone.utc),
            persona_write_consent=True,
        ),
        project_persona=projector,
        resolve_world=resolve_world,
    )
    config = load_kindred_config(config_path)
    _require_agent(config, agent)
    return config


def _require_agent(config: KindredConfig, agent: _Agent) -> None:
    if config.resident.agent_id != agent.agent_id or config.resident.workspace != agent.workspace:
        raise OpenClawInstallError("OpenClaw agent/workspace does not match Resident")


def _ensure_relationship(config: KindredConfig, agent: _Agent) -> None:
    """Run the internal, one-time Relationship stage after approved-peer selection."""

    workspace = agent.workspace
    expected = (
        workspace / "SOUL.md",
        workspace / "IDENTITY.md",
        workspace / "USER.md",
        workspace / "SOUL_excerpt.md",
    )
    actual = (
        config.paths.soul_full,
        config.paths.identity,
        config.paths.user,
        config.paths.soul_excerpt,
    )
    if actual != expected:
        raise OpenClawInstallError("Relationship Persona authority does not match Resident")
    try:
        read_owned_persona_file(config.paths.soul_full, name="SOUL.md")
        read_owned_persona_file(config.paths.identity, name="IDENTITY.md")
        read_owned_persona_file(config.paths.soul_excerpt, name="SOUL_excerpt.md")
    except ResidentInitError as exc:
        raise OpenClawInstallError("Relationship prerequisite preflight failed") from exc

    def project_user(user_text: str) -> RelationshipBootstrap:
        if config.resident.secrets_file is None:
            raise OpenClawInstallError("Relationship bootstrap requires configured LLM secrets")
        secrets = merge_runtime_secrets(config.resident.secrets_file, env=os.environ)
        if config.llm.provider == "google":
            api_key = secrets.get("GEMINI_API_KEY") or secrets.get("GOOGLE_API_KEY")
            projector = make_google_relationship_projector
            base_url = os.environ.get("GEMINI_BASE_URL")
        elif config.llm.provider == "openai":
            api_key = secrets.get("OPENAI_API_KEY")
            projector = make_openai_relationship_projector
            base_url = os.environ.get("OPENAI_BASE_URL")
        else:
            raise OpenClawInstallError(
                "Relationship bootstrap requires configured Google or OpenAI provider"
            )
        if not api_key:
            raise OpenClawInstallError("Relationship bootstrap credential is missing")
        return projector(api_key=api_key, model=config.llm.model, base_url=base_url)(user_text)

    def confirm_projection(bootstrap: RelationshipBootstrap) -> bool:
        click.echo(
            "Relationship bootstrap: "
            f"role={bootstrap.declared_role}; trust={bootstrap.trust}; "
            f"attachment={bootstrap.attachment}; attraction={bootstrap.attraction}; "
            f"friction={bootstrap.friction}"
        )
        click.echo(f"摘要：{bootstrap.summary}")
        return bool(click.confirm("接受整份 Relationship 初始投影？"))

    result = initialize_user_relationship(
        db_path=config.paths.db,
        user_path=config.paths.user,
        project_user=project_user,
        confirm_identity=lambda: bool(
            click.confirm("确认 USER.md 描述的是刚选择的 approved peer？")
        ),
        confirm_projection=confirm_projection,
    )
    click.echo(f"Relationship initialization: {result.status}")


def _binding_accounts(agent_id: str) -> frozenset[tuple[str, str]]:
    raw = _json(["openclaw", "agents", "bindings", "--agent", agent_id, "--json"])
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
    env = {"OPENCLAW_GATEWAY_TOKEN": gateway.token}
    raw = _json(["openclaw", "devices", "list", "--json"], extra_env=env)
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
    click.echo("Kindred 需要批准一个本机 Gateway device，才能读取已选 direct session。")
    if not click.confirm("批准这个 Kindred device？"):
        raise OpenClawInstallError("operator cancelled Kindred device approval")
    _json(
        ["openclaw", "devices", "approve", request_id, "--json"],
        extra_env=env,
    )


def _discover_wire(gateway: GatewayClient, agent_id: str) -> _DiscoveredWire:
    scope = _json(["openclaw", "config", "get", "session.dmScope", "--json"])
    accounts = _binding_accounts(agent_id)
    bindings = _json(["openclaw", "config", "get", "bindings", "--json"])
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
        _approve_pending_device(gateway)
        response = gateway.list_sessions(agent_id=agent_id)
    rows = response.get("sessions")
    if scope != "per-channel-peer" or response.get("ok") is not True or not isinstance(rows, list):
        raise OpenClawInstallError("OpenClaw session isolation is unavailable")
    candidates: list[tuple[Mapping[str, Any], OpenClawWire]] = []
    for row in rows:
        try:
            wire = wire_from_session(row, dm_scope=scope)
        except OpenClawWireError:
            continue
        if (wire.approved_peer.provider, wire.approved_peer.account_id) in accounts:
            candidates.append((row, wire))
    if not candidates:
        raise OpenClawInstallError("no approved direct OpenClaw session is available")
    for index, (_, wire) in enumerate(candidates, 1):
        target = wire.approved_peer.target
        masked = f"{target[:2]}…{target[-2:]}" if len(target) > 4 else "****"
        click.echo(f"{index}. {wire.approved_peer.provider} direct peer {masked}")
    selected = click.prompt("选择唯一 approved peer", type=click.IntRange(1, len(candidates)))
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
    if binding_state not in {"v1", "v2", "v3"}:
        return
    identity_changed = config.openclaw != discovered.wire
    if identity_changed and not click.confirm(
        "已绑定的 Mouth 会话身份发生变化，确认显式重新绑定？",
        default=False,
    ):
        raise OpenClawInstallError("OpenClaw session identity changed; rebind not confirmed")


def _inspect_plugin() -> Mapping[str, Any] | None:
    argv = ["openclaw", "plugins", "inspect", MOUTH_PLUGIN_ID, "--runtime", "--json"]
    report_text = _command(argv, optional_missing=True)
    if report_text is None:
        return None
    try:
        report = json.loads(report_text)
    except json.JSONDecodeError as exc:
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted") from exc
    if not isinstance(report, Mapping):
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted")
    return report


def _require_plugin_report(
    report: Mapping[str, Any] | None,
    *,
    require_prompt_injection: bool,
) -> None:
    if _plugin_generation(report, require_prompt_injection=require_prompt_injection) != "v3":
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted")


def _plugin_generation(
    report: Mapping[str, Any] | None, *, require_prompt_injection: bool = False
) -> str:
    if report is None:
        return "absent"
    try:
        plugin, hooks = report["plugin"], report["typedHooks"]
        installed = Path(plugin["source"]).parent
        version = plugin["version"]
        valid_header = (
            plugin["id"] == MOUTH_PLUGIN_ID
            and plugin["status"] == "loaded"
            and plugin["origin"] == "global"
            and Path(report["install"]["installPath"]).resolve() == installed.resolve()
            and report["install"]["source"] == "path"
            and any(item.get("name") == "before_prompt_build" for item in hooks)
        )
        digest = _plugin_tree_digest(installed)
        if version == MOUTH_PLUGIN_VERSION:
            valid = digest == _plugin_tree_digest(MOUTH_PLUGIN_DIR) and (
                not require_prompt_injection or report["policy"]["allowPromptInjection"] is True
            )
            generation = "v3"
        elif version == "0.2.0":
            valid = digest == _MOUTH_PLUGIN_V2_DIGEST
            generation = "v2"
        elif version == "0.1.0":
            valid = digest == _MOUTH_PLUGIN_V1_DIGEST
            generation = "v1"
        else:
            valid, generation = False, "unknown"
    except (KeyError, OSError, TypeError) as exc:
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted") from exc
    if not valid_header or not valid:
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted")
    return generation


def _plugin_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise OpenClawInstallError("installed OpenClaw plugin contract drifted")
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _binding_generation(config: KindredConfig, *, home: Path | None = None) -> str:
    root = home or Path.home()
    path = root / ".config/kindred/openclaw-binding.json"
    if not path.exists() and not path.is_symlink():
        return "absent"
    try:
        require_openclaw_binding(config, home=root)
        return "v3"
    except OpenClawBindingError:
        pass
    try:
        info = path.lstat()
        resident = config.resident
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or resident.marker_path is None
        ):
            raise ValueError
        actual = json.loads(path.read_text(encoding="utf-8"))
        session_id = actual["session_id"]
        expected = _v2_binding_payload(config, session_id=session_id)
        marker = json.loads(resident.marker_path.read_text(encoding="utf-8"))
        if actual == expected and marker.get("install_id") == resident.install_id:
            return "v2"
    except (KeyError, OSError, TypeError, ValueError):
        pass
    try:
        info = path.lstat()
        resident = config.resident
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or resident.marker_path is None
        ):
            raise ValueError
        expected = _v1_binding_payload(config)
        actual = json.loads(path.read_text(encoding="utf-8"))
        marker = json.loads(resident.marker_path.read_text(encoding="utf-8"))
        if actual == expected and marker.get("install_id") == resident.install_id:
            return "v1"
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return "unknown"


def _v1_binding_payload(config: KindredConfig) -> dict[str, Any]:
    wire, resident = config.openclaw, config.resident
    if wire is None or resident.workspace is None or resident.marker_path is None:
        raise OpenClawInstallError("legacy OpenClaw binding identity is incomplete")

    def digest(value: str) -> str:
        return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"

    return {
        "schema_version": 1,
        "install_id": resident.install_id,
        "agent_id": resident.agent_id,
        "workspace_digest": digest(str(resident.workspace)),
        "transcript_session_digest": digest(wire.transcript_session),
        "peer_scope": {
            "message_provider": wire.approved_peer.provider,
            "channel_id_digest": digest(wire.approved_peer.target),
        },
        "bundle_path": str(config.paths.context_bundle.resolve()),
        "resident_marker_path": str(resident.marker_path.resolve()),
    }


def _v2_binding_payload(config: KindredConfig, *, session_id: str) -> dict[str, Any]:
    if (
        not isinstance(session_id, str)
        or not session_id.strip()
        or session_id != session_id.strip()
        or len(session_id) > 512
    ):
        raise OpenClawInstallError("legacy OpenClaw binding identity is incomplete")
    payload = binding_payload(config)
    payload["schema_version"] = 2
    payload["session_id"] = session_id
    return payload


def _plugin(config: KindredConfig | None = None) -> None:
    report = _inspect_plugin()
    plugin_state = _plugin_generation(report)
    binding_state = _binding_generation(config) if config is not None else "absent"
    incompatible = (plugin_state == "v1" and binding_state != "v1") or (
        plugin_state == "v2" and binding_state not in {"v1", "v2"}
    )
    if binding_state == "unknown" or incompatible:
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted")
    if plugin_state == "v3" and report is not None:
        try:
            prompt_policy = report["policy"].get("allowPromptInjection")
            if prompt_policy is True:
                _require_plugin_report(report, require_prompt_injection=True)
                return
            if prompt_policy not in (None, False):
                raise TypeError
        except (AttributeError, KeyError, TypeError) as exc:
            raise OpenClawInstallError("installed OpenClaw plugin contract drifted") from exc
    if plugin_state in {"v1", "v2"}:
        _command(["openclaw", "plugins", "uninstall", MOUTH_PLUGIN_ID, "--force"])
        if _inspect_plugin() is not None:
            raise OpenClawInstallError("Kindred Mouth Plugin uninstall did not take effect")
        report = None
    if report is None:
        _command(["openclaw", "plugins", "install", str(MOUTH_PLUGIN_DIR)])
        report = _inspect_plugin()
    _require_plugin_report(report, require_prompt_injection=False)
    _command(["openclaw", "plugins", "doctor"])
    _command(
        [
            "openclaw",
            "config",
            "set",
            f"plugins.entries.{MOUTH_PLUGIN_ID}.hooks.allowPromptInjection",
            "true",
            "--strict-json",
        ]
    )
    report = _inspect_plugin()
    _require_plugin_report(report, require_prompt_injection=True)


def require_openclaw_runtime(config: KindredConfig, *, home: Path | None = None) -> None:
    """离线确认固定 OpenClaw、Plugin v3 与 private binding v3 一致。"""
    if config.openclaw is None:
        return
    _require_version()
    require_openclaw_binding(config, home=home)
    _require_plugin_report(_inspect_plugin(), require_prompt_injection=True)


def _uninstall_plugin() -> None:
    report = _inspect_plugin()
    if report is None:
        return
    if _plugin_generation(report) not in {"v2", "v3"}:
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted")
    _command(["openclaw", "plugins", "uninstall", MOUTH_PLUGIN_ID, "--force"])
    if _inspect_plugin() is not None:
        raise OpenClawInstallError("Kindred Mouth Plugin uninstall did not take effect")


def _publish_wire(config_path: Path, wire: OpenClawWire, gateway_port: int) -> None:
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError
        raw["openclaw"] = wire.model_dump(mode="json")
        daemon = raw.setdefault("daemon", {})
        gateway = raw.setdefault("gateway", {})
        if not isinstance(daemon, dict) or not isinstance(gateway, dict):
            raise ValueError
        daemon["session_key"] = wire.transcript_session
        gateway["port"] = gateway_port
        text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise OpenClawInstallError("Kindred config is unavailable") from exc
    _atomic_write(config_path, text)


def _atomic_write(path: Path, text: str) -> None:
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


@click.group(name="openclaw")
def openclaw_cli() -> None:
    """OpenClaw 原生安装与接线。"""


@openclaw_cli.command(name="install")
def openclaw_install() -> None:
    """交互式创建/复用 Resident，并接通唯一 Mouth peer。"""
    if not sys.stdin.isatty():
        raise click.ClickException("需要 controlling TTY；请在终端运行 kindred openclaw install")
    try:
        _confirm_data_flows()
        _require_version()
        agent = _select_agent(_agents())
        config_path = _config_home() / "kindred/config.yaml"
        config = _ensure_resident(agent, config_path)
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
        binding_state = _binding_generation(config)
        _confirm_rebind(config, discovered, binding_state=binding_state)
        _ensure_relationship(config, agent)
        _configure_agent_verbose_off(agent.agent_id)
        _plugin(config)
        _publish_wire(config_path, discovered.wire, gateway_port)
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
        from kindred.openclaw.doctor import DoctorError, require_doctor_ready

        try:
            require_doctor_ready(config_path, online=True)
        except DoctorError as exc:
            raise OpenClawInstallError(str(exc)) from exc
        try:
            web_available = bool(__import__("fastapi") and __import__("uvicorn"))
        except ImportError:
            web_available = False
        if not web_available:
            click.echo("Web 依赖未安装；需要时安装 kindred[web]。")
        include_web = web_available and click.confirm("安装 Kindred Web 后台服务？", default=False)
        platform_service.install_services(config_path, include_web=include_web)
        if click.confirm("现在启动 Kindred？", default=False):
            platform_service.control_services(config_path, action="start")
    except (
        OpenClawBindingError,
        OpenClawInstallError,
        RelationshipInitError,
        platform_service.PlatformServiceError,
        ResidentInitError,
    ) as exc:
        raise click.ClickException(f"OpenClaw 安装失败：{exc}") from exc
    except Exception as exc:
        raise click.ClickException("OpenClaw 安装失败；请检查隔离安装日志") from exc
    click.echo("Kindred Resident 与 OpenClaw Mouth 已接线。")


@openclaw_cli.command(name="uninstall")
def openclaw_uninstall() -> None:
    """停止并断开 Kindred，保留 Resident 与全部生活数据。"""
    config_path = _config_home() / "kindred/config.yaml"
    binding_path = Path.home() / ".config/kindred/openclaw-binding.json"
    try:
        config = load_kindred_config(config_path, load_secrets=False)
        require_committed_resident(config)
        platform_service.control_services(config_path, action="stop")
        _require_version()
        if binding_path.exists() or binding_path.is_symlink():
            if config.openclaw is None:
                raise OpenClawBindingError("OpenClaw Mouth binding ownership is unavailable")
            if _binding_generation(config) not in {"v2", "v3"}:
                raise OpenClawBindingError("OpenClaw Mouth binding is missing or inconsistent")
        _uninstall_plugin()
        if binding_path.exists() or binding_path.is_symlink():
            binding_path.unlink()
        platform_service.uninstall_services(config_path)
    except (
        OpenClawBindingError,
        OpenClawInstallError,
        platform_service.PlatformServiceError,
        ResidentInitError,
    ) as exc:
        raise click.ClickException(f"OpenClaw 卸载失败：{exc}") from exc
    except Exception as exc:
        raise click.ClickException("OpenClaw 卸载失败；已保留 Resident 与生活数据") from exc
    click.echo("Kindred 已停止并与 OpenClaw 断开；Resident 与生活数据均已保留。")


__all__ = ["OpenClawInstallError", "openclaw_cli", "require_openclaw_runtime"]
