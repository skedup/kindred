"""Public contract for exact OpenClaw outbound and Mouth context wiring."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from kindred import cli as cli_module
from kindred.adapters.openclaw.device_identity import DeviceIdentity
from kindred.adapters.openclaw.gateway import GatewayClient, _RetryableError
from kindred.cli import cli
from kindred.config import load_kindred_config
from kindred.db import KindredDB
from kindred.openclaw import MOUTH_PLUGIN_DIR, OpenClawWire
from kindred.openclaw.binding import binding_payload, require_openclaw_binding
from kindred.openclaw.install import _publish_wire
from kindred.relationship.models import RelationshipProfile
from kindred.resident import (
    PersonaProjection,
    PersonaTraits,
    ResidentInitRequest,
    WorldResolution,
    initialize_resident,
)
from kindred.runtime.daemon import HeartDaemon
from kindred.runtime.history_sync import HistorySync
from kindred.runtime.io_bridge import IOBridge, IOBridgeError

_ARTIFACT = "artifact:00000000000000000000000000000000"
_SESSION = "agent:resident:synthetic:direct:peer"


@dataclass
class _Gateway:
    direct_response: object = field(default_factory=lambda: {"ok": True, "payload": {}})
    context_response: object = field(
        default_factory=lambda: {"ok": True, "payload": {"status": "committed"}}
    )
    direct_calls: list[dict[str, Any]] = field(default_factory=list)
    context_calls: list[tuple[str, str]] = field(default_factory=list)

    def send_direct(self, **kwargs: Any) -> object:
        self.direct_calls.append(kwargs)
        return self.direct_response

    def commit_outbound_context(self, operation_id: str, text: str) -> object:
        self.context_calls.append((operation_id, text))
        return self.context_response


def _wire(*, target: str = "synthetic-peer") -> OpenClawWire:
    return OpenClawWire.model_validate(
        {
            "transcript_session": _SESSION,
            "approved_peer": {
                "provider": "synthetic",
                "account_id": "synthetic-account",
                "target": target,
            },
            "outbound_route": {
                "channel": "synthetic",
                "account_id": "synthetic-account",
                "target": target,
                "thread_id": "synthetic-thread",
            },
        }
    )


def test_direct_send_uses_exact_wire_then_commits_hidden_context() -> None:
    gateway = _Gateway()

    IOBridge(gateway, wire=_wire(), agent_id="resident").send_to_user(  # type: ignore[arg-type]
        "  synthetic text  ",
        artifact_ref=_ARTIFACT,
    )

    assert gateway.direct_calls == [
        {
            "channel": "synthetic",
            "account_id": "synthetic-account",
            "to": "synthetic-peer",
            "agent_id": "resident",
            "session_key": _SESSION,
            "message": "synthetic text",
            "idempotency_key": f"kindred-heart-send:{gateway.context_calls[0][0]}",
        }
    ]
    assert gateway.context_calls == [(gateway.context_calls[0][0], "synthetic text")]
    assert len(gateway.context_calls[0][0]) == 64


def test_failed_direct_send_never_commits_context() -> None:
    gateway = _Gateway(direct_response={"error": "unknown", "side_effect": "unknown"})

    with pytest.raises(IOBridgeError) as raised:
        IOBridge(gateway, wire=_wire(), agent_id="resident").send_to_user(  # type: ignore[arg-type]
            "synthetic text",
            artifact_ref=_ARTIFACT,
        )

    assert raised.value.unknown_side_effect is True
    assert gateway.context_calls == []


def test_gateway_client_uses_exact_rpc_and_classifies_post_attempt_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayClient(
        "127.0.0.1",
        18789,
        "synthetic-token",
        identity=DeviceIdentity.generate(),
        max_retries=1,
    )
    socket = object()
    captured: list[tuple[str, dict[str, Any]]] = []

    @contextmanager
    def connected():
        yield socket

    def accepted(ws: object, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert ws is socket
        captured.append((method, params))
        return {
            "ok": True,
            "payload": {"runId": "run", "messageId": "message", "channel": "synthetic"},
        }

    monkeypatch.setattr(client, "_connected_ws", connected)
    monkeypatch.setattr(client, "_rpc_call", accepted)
    result = client.send_direct(
        channel="synthetic",
        account_id="account",
        to="peer",
        agent_id="resident",
        session_key=_SESSION,
        message=" committed text ",
        idempotency_key="kindred-heart-send:" + "a" * 64,
    )

    assert result["ok"] is True
    assert captured == [
        (
            "send",
            {
                "channel": "synthetic",
                "accountId": "account",
                "to": "peer",
                "agentId": "resident",
                "sessionKey": _SESSION,
                "message": "committed text",
                "idempotencyKey": "kindred-heart-send:" + "a" * 64,
            },
        )
    ]

    def uncertain(_ws: object, _method: str, _params: dict[str, Any]) -> dict[str, Any]:
        raise _RetryableError("synthetic transport failure")

    monkeypatch.setattr(client, "_rpc_call", uncertain)
    assert client.send_direct(
        channel="synthetic",
        account_id="account",
        to="peer",
        agent_id="resident",
        session_key=_SESSION,
        message="committed text",
        idempotency_key="kindred-heart-send:" + "a" * 64,
    ) == {"error": "send outcome is unknown", "side_effect": "unknown"}


def test_history_sync_rejects_canonical_drift_before_message_upsert(tmp_path: Path) -> None:
    message = {
        "role": "user",
        "content": "synthetic message",
        "timestamp": 1,
        "__openclaw": {"id": "message-1", "seq": 1},
    }

    class Client:
        def __init__(self, session_info_key: str) -> None:
            self.session_info_key = session_info_key

        def fetch_chat_history(self, session_key: str, limit: int = 30) -> dict[str, Any]:
            assert session_key == _SESSION
            assert limit == 30
            return {
                "ok": True,
                "messages": [message],
                "sessionKey": _SESSION,
                "sessionInfoKey": self.session_info_key,
            }

    with KindredDB.open(tmp_path / "kindred.db") as db:
        assert HistorySync(Client("other-session"), db, wire=_wire()).sync_once() == 0  # type: ignore[arg-type]
        assert db.get_recent_messages(session_key=_SESSION, limit=10) == []
        assert HistorySync(Client(_SESSION), db, wire=_wire()).sync_once() == 1  # type: ignore[arg-type]
        assert [row.msg_id for row in db.get_recent_messages(session_key=_SESSION, limit=10)] == [
            "message-1"
        ]


def test_binding_v3_and_wire_publication_share_the_canonical_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kindred.cli import _require_openclaw_runtime as require_cli_runtime
    from kindred.openclaw import install as installer

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "resident.json"
    marker.write_text(json.dumps({"install_id": "install-a"}), encoding="utf-8")
    bundle = tmp_path / "context-bundle.md"
    bundle.write_text("synthetic bundle", encoding="utf-8")
    config = load_kindred_config(env={})
    config = replace(
        config,
        paths=replace(config.paths, context_bundle=bundle),
        daemon=replace(config.daemon, session_key=_SESSION),
        openclaw=_wire(),
        resident=replace(
            config.resident,
            install_id="install-a",
            resident_id="resident",
            agent_id="resident",
            workspace=workspace,
            marker_path=marker,
        ),
    )
    payload = binding_payload(config)
    assert payload["schema_version"] == 3
    assert payload["session_key"] == _SESSION
    assert "session_id" not in payload

    binding = home / ".config/kindred/openclaw-binding.json"
    binding.parent.mkdir(parents=True)
    binding.write_text(json.dumps(payload), encoding="utf-8")
    binding.chmod(0o600)
    assert require_openclaw_binding(config, home=home) == payload
    runtime_checks: list[object] = []
    monkeypatch.setattr(
        installer, "require_openclaw_runtime", lambda value: runtime_checks.append(value)
    )
    require_cli_runtime(config)
    assert runtime_checks == [config]

    config_path = tmp_path / "config.yaml"
    config_path.write_text("daemon:\n  session_key: old\ngateway:\n  port: 1\n", encoding="utf-8")
    _publish_wire(config_path, _wire(), 18789)
    published = config_path.read_text(encoding="utf-8")
    assert published.count(_SESSION) == 2
    assert "port: 18789" in published


def test_launchagent_run_uses_local_identity_without_openclaw_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, xdg, workspace = tmp_path / "home", tmp_path / "xdg", tmp_path / "workspace"
    workspace.mkdir()
    for name in ("SOUL.md", "IDENTITY.md", "USER.md"):
        (workspace / name).write_text(f"synthetic {name}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    for key, value in {
        "BAIDU_MAP_AK": "example-map-key",
        "GEMINI_API_KEY": "example-llm-key",
        "KINDRED_GATEWAY_TOKEN": "example-gateway-token",
    }.items():
        monkeypatch.setenv(key, value)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    initialize_resident(
        ResidentInitRequest(
            resident_id="resident",
            agent_id="resident",
            workspace=workspace.resolve(),
            life_root=(tmp_path / "life").resolve(),
            xdg_config_home=xdg.resolve(),
            home_address="synthetic address",
            llm_provider="google",
            llm_model="gemini-test",
            secrets={
                "BAIDU_MAP_AK": "example-map-key",
                "GEMINI_API_KEY": "example-llm-key",
                "KINDRED_GATEWAY_TOKEN": "example-gateway-token",
            },
            install_now=datetime(2026, 8, 13, tzinfo=timezone.utc),
            persona_write_consent=True,
        ),
        project_persona=lambda *_: PersonaProjection(
            soul_excerpt="synthetic excerpt",
            traits=PersonaTraits(
                openness=50,
                agreeableness=50,
                conscientiousness=50,
                awareness=50,
                eros=50,
            ),
        ),
        resolve_world=lambda *_: WorldResolution(
            address="synthetic resolved address",
            city="Synthetic City",
            timezone="Asia/Shanghai",
        ),
    )
    config_path = xdg / "kindred/config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["openclaw"] = _wire().model_dump(mode="json")
    raw.setdefault("daemon", {})["session_key"] = _SESSION
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_kindred_config(config_path)
    with KindredDB.open(config.paths.db) as db, db.transaction():
        db.create_relationship(
            RelationshipProfile(
                subject_key="user",
                declared_role="unlabeled",
                trust=0,
                attachment=0,
                attraction=0,
                friction=0,
            )
        )
    binding = home / ".config/kindred/openclaw-binding.json"
    binding.parent.mkdir(parents=True)
    binding.write_text(json.dumps(binding_payload(config)), encoding="utf-8")
    binding.chmod(0o600)
    events: list[str] = []
    monkeypatch.setattr(cli_module, "_configure_observability", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        "kindred.runtime.daemon.run_daemon",
        lambda loaded, *_args, **_kwargs: events.append(loaded.openclaw.transcript_session) or 0,
    )

    result = CliRunner().invoke(cli, ["run", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert events == [_SESSION]


def test_installer_orders_relationship_wire_doctor_and_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kindred.openclaw import install as installer

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    life_root = tmp_path / "life"
    workspace.mkdir()
    life_root.mkdir()
    marker = life_root / "resident.json"
    marker.write_text(json.dumps({"install_id": "install-a"}), encoding="utf-8")
    config = load_kindred_config(env={})
    config = replace(
        config,
        paths=replace(config.paths, life_root=life_root),
        daemon=replace(config.daemon, session_key=_SESSION),
        openclaw=_wire(),
        resident=replace(
            config.resident,
            install_id="install-a",
            resident_id="resident",
            agent_id="resident",
            workspace=workspace,
            marker_path=marker,
        ),
    )
    agent = installer._Agent("resident", workspace, "Resident")
    discovered = installer._DiscoveredWire(_wire())
    events: list[str] = []

    def mark(name: str, value: object = None) -> object:
        events.append(name)
        return value

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(installer, "_confirm_data_flows", lambda: mark("data_flows"))
    monkeypatch.setattr(installer, "_require_version", lambda: mark("version"))
    monkeypatch.setattr(installer, "_agents", lambda: [agent])
    monkeypatch.setattr(installer, "_select_agent", lambda _agents: agent)
    monkeypatch.setattr(installer, "_ensure_resident", lambda *_args: config)
    monkeypatch.setattr(installer, "install_runtime_secrets", lambda *_args: None)
    monkeypatch.setattr(installer, "load_kindred_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(installer, "_json", lambda *_args, **_kwargs: 18789)
    monkeypatch.setattr(
        installer.GatewayClient,
        "from_config",
        classmethod(lambda _cls, *_args, **_kwargs: object()),
    )
    monkeypatch.setattr(installer, "_discover_wire", lambda *_args: discovered)
    monkeypatch.setattr(installer, "_binding_generation", lambda _config: "none")
    monkeypatch.setattr(installer, "_confirm_rebind", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "_ensure_relationship", lambda *_args: mark("relationship"))
    monkeypatch.setattr(installer, "_configure_agent_verbose_off", lambda *_args: None)
    monkeypatch.setattr(installer, "_plugin", lambda *_args: mark("plugin"))
    monkeypatch.setattr(installer, "_publish_wire", lambda *_args: mark("wire"))
    monkeypatch.setattr(installer, "require_committed_resident", lambda *_args: None)
    monkeypatch.setattr(installer, "binding_payload", lambda _config: {"schema_version": 3})
    monkeypatch.setattr(installer, "require_openclaw_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "require_openclaw_runtime", lambda *_args: mark("runtime"))
    monkeypatch.setattr(
        "kindred.openclaw.doctor.require_doctor_ready",
        lambda *_args, **_kwargs: mark("doctor"),
    )
    monkeypatch.setattr(
        installer.platform_service,
        "install_services",
        lambda *_args, **_kwargs: mark("service"),
    )
    monkeypatch.setattr(installer.click, "confirm", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(installer.click, "echo", lambda *_args, **_kwargs: None)

    assert installer.openclaw_install.callback is not None
    installer.openclaw_install.callback()

    assert events == [
        "data_flows",
        "version",
        "relationship",
        "plugin",
        "wire",
        "runtime",
        "doctor",
        "service",
    ]
    assert (home / ".config/kindred/openclaw-binding.json").is_file()


def test_daemon_builds_history_and_outbound_from_the_same_verified_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_kindred_config(env={})
    config = replace(
        config,
        paths=replace(config.paths, life_root=tmp_path),
        openclaw=_wire(),
        resident=replace(config.resident, agent_id="resident"),
    )
    gateway = object()
    monkeypatch.setattr(
        GatewayClient,
        "from_config",
        classmethod(lambda _cls, *_args, **_kwargs: gateway),
    )
    daemon = HeartDaemon(config, None)  # type: ignore[arg-type]

    with KindredDB.open(tmp_path / "kindred.db") as db:
        history = daemon._build_history_sync(db)
        outbound = daemon._build_io_bridge()

    assert history is not None and history._client is gateway
    assert history._wire is config.openclaw
    assert outbound is not None and outbound._gateway is gateway
    assert outbound._wire is config.openclaw
    assert outbound._agent_id == "resident"


def test_packaged_mouth_plugin_executes_exact_entry_and_outbound_contract(
    tmp_path: Path,
) -> None:
    package = json.loads((MOUTH_PLUGIN_DIR / "package.json").read_text(encoding="utf-8"))

    assert package["version"] == "0.3.0"
    assert package["openclaw"]["compat"]["pluginApi"] == "2026.6.10"
    for relative in (
        "binding.js",
        "index.js",
        "outbound.js",
        "openclaw.plugin.json",
        "test/binding.test.js",
        "test/outbound.test.js",
    ):
        assert (Path(MOUTH_PLUGIN_DIR) / relative).is_file()
    node = shutil.which("node")
    assert node is not None, "packaged Mouth contract requires Node in the release gate"
    result = subprocess.run(
        [node, "--test", "test/binding.test.js", "test/outbound.test.js"],
        cwd=MOUTH_PLUGIN_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "fail 0" in result.stdout

    plugin = tmp_path / "plugin"
    shutil.copytree(MOUTH_PLUGIN_DIR, plugin)
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundle = tmp_path / "context-bundle.md"
    bundle.write_text("synthetic mouth context\n", encoding="utf-8")
    marker = tmp_path / "resident.json"
    marker.write_text(json.dumps({"install_id": "install-a"}), encoding="utf-8")
    binding = home / ".config/kindred/openclaw-binding.json"
    binding.parent.mkdir(parents=True)
    binding.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "install_id": "install-a",
                "agent_id": "resident",
                "workspace_digest": "sha256:" + hashlib.sha256(str(workspace).encode()).hexdigest(),
                "session_key": _SESSION,
                "peer_scope": {
                    "message_provider": "synthetic",
                    "channel_id_digest": "sha256:"
                    + hashlib.sha256(b"synthetic-channel").hexdigest(),
                },
                "bundle_path": str(bundle),
                "resident_marker_path": str(marker),
            }
        ),
        encoding="utf-8",
    )
    binding.chmod(0o600)
    sdk = tmp_path / "node_modules/openclaw"
    sdk.mkdir(parents=True)
    (sdk / "package.json").write_text(
        json.dumps(
            {
                "name": "openclaw",
                "type": "module",
                "exports": {
                    "./plugin-sdk/plugin-entry": "./plugin-entry.js",
                    "./plugin-sdk/session-store-runtime": "./session-store.js",
                    "./plugin-sdk/session-transcript-runtime": "./session-transcript.js",
                },
            }
        ),
        encoding="utf-8",
    )
    (sdk / "plugin-entry.js").write_text(
        "export const definePluginEntry = (entry) => entry;\n", encoding="utf-8"
    )
    (sdk / "session-store.js").write_text(
        "export const getSessionEntry = () => ({ sessionId: 'generation-a' });\n",
        encoding="utf-8",
    )
    (sdk / "session-transcript.js").write_text(
        """export const withSessionTranscriptWriteLock = async (_identity, callback) =>
  callback({
    appendMessage: async ({ message }) => ({ appended: true, message, messageId: 'message-a' }),
    publishUpdate: async () => undefined,
    readEvents: async () => [{
      type: 'message',
      message: { role: 'assistant', api: 'synthetic', provider: 'synthetic', model: 'model' },
    }],
  });
""",
        encoding="utf-8",
    )
    entry_probe = """
import plugin from './plugin/index.js';
const registered = { hooks: new Map(), methods: new Map() };
const api = {
  on(name, callback) { registered.hooks.set(name, callback); },
  registerGatewayMethod(name, handler, options) {
    registered.methods.set(name, { handler, scope: options?.scope });
  },
};
plugin.register(api);
if (plugin.id !== 'kindred-mouth') process.exit(2);
const hook = registered.hooks.get('before_prompt_build');
if (!hook || registered.hooks.size !== 1) process.exit(3);
const injected = hook({}, {
  agentId: 'resident',
  workspaceDir: process.env.KINDRED_TEST_WORKSPACE,
  sessionKey: 'agent:resident:synthetic:direct:peer',
  messageProvider: 'synthetic',
  channelId: 'synthetic-channel',
});
if (injected?.prependContext !== 'synthetic mouth context') process.exit(4);
const rpc = registered.methods.get('kindred.mouth.commitOutbound');
if (!rpc || rpc.scope !== 'operator.admin' || registered.methods.size !== 1) process.exit(5);
let response;
await rpc.handler({
  params: { operation_id: 'a'.repeat(64), text: 'synthetic outbound' },
  respond: (...args) => { response = args; },
});
if (response?.[0] !== true || response?.[1]?.status !== 'committed') process.exit(6);
"""
    subprocess.run(
        [node, "--input-type=module", "--eval", entry_probe],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(home), "KINDRED_TEST_WORKSPACE": str(workspace)},
        check=True,
        capture_output=True,
        text=True,
    )
