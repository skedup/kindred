"""OPEN4 structural doctor and side-effect-free online preflight."""

from __future__ import annotations

import ipaddress
import os
import socket
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from kindred.activity.action import list_registered_actions, load_atomic_action
from kindred.activity.skill import list_registered_activities, load_activity_skill
from kindred.adapters.openclaw.device_identity import DeviceIdentity
from kindred.adapters.openclaw.gateway import GatewayClient
from kindred.capability_host.discovery import discover_enabled_contributions
from kindred.capability_host.facts import ACTIVITY_CURRENT_FACT, ARTIFACT_EXPLICIT_REFS_FACT
from kindred.config import KindredConfig, load_kindred_config, read_secrets_file
from kindred.db import KindredDB
from kindred.inventory.facts import INVENTORY_CHOICE_CONTEXT_FACT
from kindred.life_assets import ACTIONS_DIR, ACTIVITIES_DIR
from kindred.llm.client import ToolCapableLlmClient
from kindred.llm.factory import build_llm_client
from kindred.openclaw.binding import require_openclaw_binding
from kindred.openclaw.install import (
    _agent_verbose_default,
    _agents,
    _binding_accounts,
    _command,
    _json,
    require_openclaw_runtime,
)
from kindred.openclaw.wire import APPROVED_DM_SCOPE, OpenClawWireError, wire_from_session
from kindred.relationship.preflight import require_user_relationship
from kindred.resident import read_owned_persona_file, require_committed_resident
from kindred.runtime import platform_service
from kindred_capability_sdk import ToolCall, ToolDef, ToolResult

Status = Literal["ok", "warning", "failed"]
SCHEMA_VERSION = 1
_REQUIRED_CAPABILITIES = frozenset({"compose", "inventory", "send"})
_FACTS = frozenset(
    {ACTIVITY_CURRENT_FACT, ARTIFACT_EXPLICIT_REFS_FACT, INVENTORY_CHOICE_CONTEXT_FACT}
)
_SERVICES = frozenset({"artifact_writer", "artifact_reader", "user_messenger"})


class DoctorError(RuntimeError):
    """The shared preflight did not establish runtime readiness."""


class _Fail(RuntimeError):
    def __init__(self, hint: str, *, retryable: bool = False) -> None:
        super().__init__(hint)
        self.hint, self.retryable = hint, retryable


@dataclass(frozen=True)
class DoctorReport:
    online: bool
    checks: tuple[dict[str, object], ...]
    schema_version: int = SCHEMA_VERSION

    @property
    def exit_code(self) -> int:
        failed = tuple(check for check in self.checks if check["status"] == "failed")
        if not failed:
            return 0
        return 1 if any(not check["retryable"] for check in failed) else 2

    def as_dict(self) -> dict[str, object]:
        status: Status = (
            "failed"
            if self.exit_code
            else ("warning" if any(check["status"] == "warning" for check in self.checks) else "ok")
        )
        return {
            "schema_version": self.schema_version,
            "online": self.online,
            "status": status,
            "retryable": self.exit_code == 2,
            "checks": list(self.checks),
        }


@dataclass
class _Context:
    config_path: Path
    home: Path
    config: KindredConfig | None = None
    secrets: dict[str, str] = field(default_factory=dict)


def run_doctor(
    config_path: Path | None = None,
    *,
    online: bool = False,
    home: Path | None = None,
) -> DoctorReport:
    context = _Context(config_path or platform_service.service_config_path(), home or Path.home())
    specs: tuple[tuple[str, Callable[[], tuple[Status, str] | str]], ...] = (
        ("config.runtime", lambda: _config(context)),
        ("resident.runtime", lambda: _resident_runtime(context)),
        ("openclaw.local", lambda: _openclaw_local(context)),
        ("life_assets", _life_assets),
        ("capabilities", lambda: _capabilities(context)),
        ("llm.config", lambda: _llm_config(context)),
        ("services", lambda: _services(context)),
    )
    checks = [_check(check_id, function) for check_id, function in specs]
    if online and not any(check["status"] == "failed" for check in checks):
        checks.extend(
            (
                _check("online.gateway", lambda: _online_gateway(context)),
                _check("online.llm", lambda: _online_llm(context)),
            )
        )
    return DoctorReport(online=online, checks=tuple(checks))


def require_doctor_ready(config_path: Path, *, online: bool = True) -> DoctorReport:
    report = run_doctor(config_path, online=online)
    if report.exit_code:
        kind = "retryable" if report.exit_code == 2 else "permanent"
        raise DoctorError(f"Kindred preflight failed ({kind}); run `kindred doctor --online`")
    return report


def _check(check_id: str, function: Callable[[], tuple[Status, str] | str]) -> dict[str, object]:
    try:
        result = function()
        status, hint = result if isinstance(result, tuple) else ("ok", result)
        retryable = False
    except _Fail as exc:
        status, retryable, hint = "failed", exc.retryable, exc.hint
    except Exception:
        status, retryable, hint = "failed", False, "检查失败；请核对本地安装合同"
    return {"check_id": check_id, "status": status, "retryable": retryable, "hint": hint}


def _cfg(context: _Context) -> KindredConfig:
    if context.config is None:
        raise _Fail("配置未通过，无法继续此项检查")
    return context.config


def _config(context: _Context) -> str:
    try:
        info = context.config_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise ValueError
        config = load_kindred_config(context.config_path)
        if config.resident.secrets_file is None:
            raise ValueError
        secrets = read_secrets_file(config.resident.secrets_file)
        host = config.gateway.host
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError
        if not config.gateway.token:
            raise ValueError
    except Exception as exc:
        raise _Fail("配置、secrets 或 loopback Gateway 合同无效") from exc
    context.config, context.secrets = config, secrets
    return "配置、凭据结构与 loopback Gateway 合同正常"


def _resident_runtime(context: _Context) -> tuple[Status, str]:
    config = _cfg(context)
    try:
        require_committed_resident(config)
        workspace = config.resident.workspace
        if workspace is None:
            raise ValueError
        if (
            config.paths.soul_full != workspace / "SOUL.md"
            or config.paths.identity != workspace / "IDENTITY.md"
            or config.paths.user != workspace / "USER.md"
            or config.paths.soul_excerpt != workspace / "SOUL_excerpt.md"
        ):
            raise ValueError
        read_owned_persona_file(config.paths.soul_full, name="SOUL.md")
        read_owned_persona_file(config.paths.identity, name="IDENTITY.md")
        read_owned_persona_file(config.paths.user, name="USER.md", optional=True)
        read_owned_persona_file(config.paths.soul_excerpt, name="SOUL_excerpt.md")
        if any(
            not path.is_file() or not os.access(path, os.R_OK | os.W_OK)
            for path in (config.paths.character_card, config.paths.db)
        ):
            raise ValueError
        if any(
            not path.is_dir() or not os.access(path, os.W_OK)
            for path in (config.paths.life_root, config.paths.run_dir)
        ):
            raise ValueError
        with KindredDB.open_readonly(config.paths.db) as db:
            db.count_inventory_items()
            require_user_relationship(db)
    except Exception as exc:
        raise _Fail("Resident、Persona、DB、Catalog、Relationship 或运行目录不完整") from exc
    bundle = config.paths.context_bundle
    if not bundle.exists():
        return "warning", "context bundle 尚未由首个 tick 生成"
    if not bundle.is_file() or not os.access(bundle, os.R_OK):
        raise _Fail("context bundle 类型或读取边界无效")
    return "ok", "Resident、Persona、DB、Catalog、Relationship 与 bundle 边界正常"


def _openclaw_local(context: _Context) -> tuple[Status, str]:
    config = _cfg(context)
    try:
        require_openclaw_runtime(config, home=context.home)
        agents = _agents()
    except Exception as exc:
        raise _Fail("OpenClaw Plugin/binding 不一致；请重新运行 kindred openclaw install") from exc
    if not any(
        agent.agent_id == config.resident.agent_id and agent.workspace == config.resident.workspace
        for agent in agents
    ):
        raise _Fail("OpenClaw agent/workspace 与 Resident 不一致")
    scope = _json(("openclaw", "config", "get", "session.dmScope", "--json"))
    if scope != APPROVED_DM_SCOPE:
        raise _Fail("OpenClaw dmScope 必须为 per-channel-peer")
    wire = config.openclaw
    if wire is None:
        raise _Fail("OpenClaw transcript/peer/route wire 缺失")
    peer = wire.approved_peer
    if (peer.provider, peer.account_id) not in _binding_accounts(config.resident.agent_id):
        raise _Fail("OpenClaw binding account 与 approved peer 不一致")
    _gateway(config)
    _check_plugin()
    if _agent_verbose_default(config.resident.agent_id) != "off":
        return "warning", "Mouth agent 未显式关闭工具调用过程展示；请重跑 install"
    return "ok", "CLI、wire、binding、dmScope 与 packaged Mouth Plugin 一致"


def _check_plugin() -> None:
    try:
        _command(["openclaw", "plugins", "doctor"])
    except Exception as exc:
        raise _Fail("OpenClaw Plugin doctor 未通过") from exc


def _life_assets() -> str:
    actions, activities = list_registered_actions(), list_registered_activities()
    expected_actions = sorted(path.name for path in ACTIONS_DIR.iterdir() if path.is_dir())
    expected_activities = sorted(path.name for path in ACTIVITIES_DIR.iterdir() if path.is_dir())
    if (
        not actions
        or not activities
        or actions != expected_actions
        or activities != expected_activities
    ):
        raise _Fail("Activity/Action wheel 资产缺失或不完整")
    for name in actions:
        load_atomic_action(name)
    for name in activities:
        load_activity_skill(name)
    return "Activity/Action wheel 资产可完整严格加载"


def _capabilities(context: _Context) -> tuple[Status, str]:
    configs = _cfg(context).capabilities
    required = {
        name: value
        for name, value in configs.items()
        if name in _REQUIRED_CAPABILITIES and value.enabled
    }
    _discover(required)
    optional = {
        name: value
        for name, value in configs.items()
        if name not in _REQUIRED_CAPABILITIES and name != "location" and value.enabled
    }
    try:
        _discover({**required, **optional})
    except Exception:
        return "warning", "可选 Capability package 或 grant 当前不可用"
    return "ok", "enabled Capability package 与 Host grants 可装配"


def _discover(configs: Mapping[str, Any]) -> None:
    loaded = discover_enabled_contributions(
        configs,
        internal_capability_names=frozenset({"location"}),
        available_host_services=_SERVICES,
        available_fact_views=_FACTS,
        available_artifact_profiles=frozenset(),
    )
    for item in reversed(loaded):
        if item.contribution.close:
            item.contribution.close()


def _llm_config(context: _Context) -> str:
    config = _cfg(context)
    required = {
        "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "anthropic": ("ANTHROPIC_API_KEY",),
        "deepseek": ("DEEPSEEK_API_KEY",),
        "openai": ("OPENAI_API_KEY",),
    }.get(config.llm.provider, ())
    if required and not any(context.secrets.get(name) or os.environ.get(name) for name in required):
        raise _Fail("Heart LLM credential 缺失")
    with _secret_env(context.secrets):
        client = build_llm_client(config)
        try:
            if not isinstance(client, ToolCapableLlmClient):
                raise _Fail("Heart LLM 不支持当前工具环合同")
        finally:
            client.close()
    return "Heart LLM provider、model、credential 与工具环合同齐全"


def _services(context: _Context) -> tuple[Status, str]:
    try:
        status = platform_service.service_status(context.config_path)
    except Exception as exc:
        raise _Fail("Heart/Web service 模板或运行目录漂移") from exc
    web = next(row for row in status if row[0] == "web")
    try:
        __import__("fastapi")
        __import__("uvicorn")
        web_extra = True
    except ImportError:
        web_extra = False
    if web[1] and not web_extra:
        raise _Fail("Web service 已安装但 kindred[web] 缺失")
    if _port_open(8787) and not web[2]:
        raise _Fail("Web 端口已被非托管进程占用")
    facts = ", ".join(
        f"{name}={'active' if active else 'inactive'}" for name, _, _, active in status
    )
    return (
        ("ok", f"Heart/Web 模板与端口正常；{facts}")
        if web_extra
        else ("warning", f"Web extra 未安装；{facts}")
    )


def _online_gateway(context: _Context) -> tuple[Status, str]:
    config = _cfg(context)
    wire = config.openclaw
    assert wire is not None
    gateway = _gateway(config)
    sessions = gateway.list_sessions(agent_id=config.resident.agent_id)
    if "error" in sessions:
        raise _gateway_fail(sessions["error"])
    rows = sessions.get("sessions")
    if not isinstance(rows, list):
        raise _Fail("Gateway session 与 approved wire 不一致")
    matches = [row for row in rows if row.get("key") == wire.transcript_session]
    try:
        if len(matches) != 1:
            rotated = False
            for row in rows:
                try:
                    candidate = wire_from_session(row, dm_scope=APPROVED_DM_SCOPE)
                except OpenClawWireError:
                    continue
                if (
                    candidate.approved_peer == wire.approved_peer
                    and candidate.outbound_route == wire.outbound_route
                ):
                    rotated = True
                    break
            if rotated:
                raise _Fail("approved peer 的 transcript 已轮换；请重跑 install")
            raise ValueError
        projected = wire_from_session(matches[0], dm_scope=APPROVED_DM_SCOPE)
        if projected != wire:
            raise ValueError
        same_route = []
        for row in rows:
            try:
                candidate = wire_from_session(row, dm_scope=APPROVED_DM_SCOPE)
            except OpenClawWireError:
                continue
            if (
                candidate.approved_peer == wire.approved_peer
                and candidate.outbound_route == wire.outbound_route
            ):
                same_route.append(row)
        if len(same_route) > 1:
            timestamps = [row.get("updatedAt") for row in same_route]
            if any(not isinstance(value, int) or isinstance(value, bool) for value in timestamps):
                raise ValueError
            latest_at = max(timestamps)
            latest = [row for row in same_route if row.get("updatedAt") == latest_at]
            if len(latest) != 1:
                raise ValueError
            if latest[0].get("key") != wire.transcript_session:
                raise _Fail("approved peer 的 transcript 已轮换；请重跑 install")
        verbose = matches[0].get("verboseLevel")
        if verbose not in (None, "off", "on", "full"):
            raise ValueError
    except _Fail:
        raise
    except (TypeError, ValueError) as exc:
        raise _Fail("Gateway session 与 approved wire 不一致") from exc
    history = gateway.fetch_chat_history(wire.transcript_session, limit=1)
    if "error" in history:
        raise _gateway_fail(history["error"])
    binding = require_openclaw_binding(config, home=context.home)
    session_id = history.get("sessionId")
    if (
        binding is None
        or history.get("ok") is not True
        or history.get("sessionKey") != wire.transcript_session
        or history.get("sessionInfoKey") != wire.transcript_session
        or not isinstance(session_id, str)
        or not session_id.strip()
        or session_id != session_id.strip()
        or len(session_id) > 512
    ):
        raise _Fail("Gateway history 与 Mouth 稳定会话身份不一致；请重跑 install")
    history.pop("messages", None)
    if verbose in ("on", "full"):
        return "warning", "approved session 显式开启工具调用展示；请在会话执行 /verbose off"
    return "ok", "Gateway auth、session 分页与当前 transcript 世代正常"


def _online_llm(context: _Context) -> str:
    calls: list[str] = []
    tool = ToolDef(
        "doctor_noop",
        "A local no-op used only for Kindred doctor.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        "read_only",
    )

    def handler(call: ToolCall) -> ToolResult:
        calls.append(call.name)
        return ToolResult.ok(call, {"ok": True})

    with _secret_env(context.secrets):
        client = build_llm_client(_cfg(context))
        try:
            structured = client.complete(
                "Return a synthetic pass verdict with empty blocked and warnings arrays.",
                role="dream.gate",
            )
            if (
                structured.get("verdict") != "pass"
                or structured.get("blocked") != []
                or structured.get("warnings") != []
            ):
                raise ValueError
            if not isinstance(client, ToolCapableLlmClient):
                raise ValueError
            result = client.complete_with_tools(
                "Call doctor_noop exactly once, then return a synthetic pass gate verdict.",
                role="dream.gate",
                tools=(tool,),
                handler=handler,
                max_rounds=2,
            )
            if calls != [tool.name] or any(
                event.call.name != tool.name for event in result.tool_events
            ):
                raise ValueError
        except Exception as exc:
            text = f"{type(exc).__name__} {exc}".lower()
            raise _Fail("Heart LLM online 检查失败", retryable="timeout" in text) from exc
        finally:
            client.close()
    return "Heart LLM 结构化输出与本地 no-op tool calling 正常"


def _gateway(config: KindredConfig) -> GatewayClient:
    path = config.paths.life_root / ".device-identity.json"
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise ValueError
        identity = DeviceIdentity._load(path)
    except Exception as exc:
        raise _Fail("Gateway device identity 缺失、损坏或权限过宽") from exc
    return GatewayClient(
        config.gateway.host,
        config.gateway.port,
        config.gateway.token,
        identity=identity,
        max_retries=1,
    )


def _gateway_fail(value: object) -> _Fail:
    text = value if isinstance(value, str) else ""
    lower = text.lower()
    permanent = any(
        word in lower
        for word in (
            "unauthorized",
            "forbidden",
            "auth",
            "token",
            "identity",
            "protocol",
            "challenge",
            "nonce",
            "schema",
            "mismatch",
            "request failed",
        )
    )
    retryable = not permanent and any(
        word in lower
        for word in (
            "timeout",
            "timed out",
            "socket",
            "connection refused",
            "connection reset",
            "connection closed",
            "disconnect",
            "broken pipe",
            "unreachable",
            "temporarily unavailable",
            "failed after",
        )
    )
    return _Fail("Gateway online 检查失败", retryable=retryable)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.05):
            return True
    except OSError:
        return False


@contextmanager
def _secret_env(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if key not in os.environ:
                os.environ[key] = value
        yield
    finally:
        for key, original in previous.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


__all__ = [
    "DoctorError",
    "DoctorReport",
    "require_doctor_ready",
    "run_doctor",
]
