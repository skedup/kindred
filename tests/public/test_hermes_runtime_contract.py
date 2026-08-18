"""Public synthetic contract for the experimental Hermes runtime ports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from kindred.config import KindredConfigError, load_kindred_config
from kindred.hermes import mouth_plugin
from kindred.hermes.install import hermes_version_warning
from kindred.hermes.mouth_plugin import plugin_digest
from kindred.hermes.runtime import CommandRunner, HermesOutboundChannel, HermesTranscriptSource
from kindred.hermes.wire import HERMES_BASELINE, HermesWire
from kindred.mouth_host import install as host_installer
from kindred.mouth_host.composition import HermesRuntimeModel, prepare_host
from kindred.mouth_host.config import publish_host


def _binding(bundle: Path) -> dict[str, object]:
    return {
        "schema_version": 3,
        "install_id": "synthetic-install",
        "plugin_digest": plugin_digest(Path(mouth_plugin.__file__).parent),
        "host_identity": list(HERMES_BASELINE),
        "session_id": "session",
        "platform": "synthetic",
        "sender_id": "peer",
        "bundle_path": str(bundle),
    }


def _runtime(
    tmp_path: Path, runner: CommandRunner
) -> tuple[HermesTranscriptSource, HermesOutboundChannel]:
    home = tmp_path / "host"
    home.mkdir()
    executable = tmp_path / "hermes"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    (home / "state.db").touch()
    (home / "config.yaml").touch()
    wire = HermesWire(
        host_executable=executable,
        host_home=home,
        approved_platform="synthetic",
        approved_sender_id="peer",
        canonical_session_id="session",
        canonical_session_key="key",
        outbound_chat_id="chat",
        outbound_sender_id="peer",
    )
    return HermesTranscriptSource(wire, runner=runner), HermesOutboundChannel(wire, runner=runner)


def test_unified_installer_discovers_hermes_only_and_writes_nothing_without_a_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = host_installer.HermesCandidate(tmp_path / "hermes", tmp_path / "host")
    monkeypatch.setattr(
        host_installer.shutil,
        "which",
        lambda name: str(candidate.executable) if name == "hermes" else None,
    )
    monkeypatch.setattr(host_installer, "discover_hermes", lambda: candidate)

    assert host_installer._discover_hosts() == (("hermes", candidate),)

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setattr(host_installer.shutil, "which", lambda _name: None)
    with pytest.raises(host_installer.MouthHostInstallError, match="no runnable Mouth host"):
        host_installer._discover_hosts()

    assert list(empty_home.iterdir()) == []


def test_hermes_verified_and_future_identities_keep_warning_boundary() -> None:
    assert hermes_version_warning(HERMES_BASELINE) is None
    warning = hermes_version_warning(("v2027.1.1", "0.30.0"))
    assert warning is not None and "完整合同检查" in warning


def test_hermes_runtime_uses_official_export_and_send_shapes(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], bytes | None]] = []

    def run(argv: tuple[str, ...], stdin: bytes | None) -> tuple[int, bytes]:
        calls.append((argv, stdin))
        if argv[1] == "sessions":
            return 0, json.dumps(
                {
                    "id": "session",
                    "source": "synthetic",
                    "user_id": "peer",
                    "session_key": "key",
                    "chat_id": "chat",
                    "chat_type": "dm",
                    "thread_id": None,
                    "messages": [{"id": 1, "role": "user", "content": "hello", "timestamp": 1}],
                }
            ).encode()
        return 0, b'{"success":true,"mirrored":true}'

    source, channel = _runtime(tmp_path, run)
    batch = source.pull()
    result = channel.send("hello", artifact_ref="artifact:test")

    assert [(message.msg_id, message.role) for message in batch.messages] == [("1", "partner")]
    assert result.status == "accepted"
    assert calls[0][0][1:] == (
        "sessions",
        "export",
        "-",
        "--session-id",
        "session",
        "--format",
        "jsonl",
    )
    assert calls[1][0][1:] == ("send", "--to", "synthetic:chat", "--json")


def test_hermes_plugin_scopes_bundle_and_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "host"
    persona = home / ".kindred/persona"
    persona.mkdir(parents=True)
    for name in ("IDENTITY.md", "USER.md", "SOUL_excerpt.md"):
        (persona / name).write_text(f"synthetic-{name}", encoding="utf-8")
    bundle = tmp_path / "bundle.md"
    bundle.write_text("synthetic-bundle", encoding="utf-8")
    (home / ".kindred/mouth-binding.json").write_text(
        json.dumps(_binding(bundle)),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    mouth_plugin._last_published.clear()

    event = {
        "turn_id": "turn-1",
        "task_id": "task-1",
        "session_id": "session",
        "platform": "synthetic",
        "sender_id": "peer",
        "parent_session_id": "",
    }
    first = mouth_plugin._on_pre_llm_call(**event)
    second = mouth_plugin._on_pre_llm_call(**{**event, "turn_id": "turn-2"})
    other = mouth_plugin._on_pre_llm_call(**{**event, "sender_id": "peer-b"})

    assert first is not None and first["context"].count("KINDRED CONTEXT BUNDLE") == 2
    assert first["context"].count("KINDRED COMPANION SNAPSHOT") == 2
    assert second is not None and "KINDRED COMPANION SNAPSHOT" not in second["context"]
    assert other is None

    bundle.write_text(" \n", encoding="utf-8")
    assert mouth_plugin._on_pre_llm_call(**event) is None


def test_internal_hermes_composition_requires_same_row_identity(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    home = tmp_path / "host"
    persona = home / ".kindred/persona"
    persona.mkdir(parents=True)
    (home / "SOUL.md").write_text("soul", encoding="utf-8")
    for name in ("IDENTITY.md", "SOUL_excerpt.md"):
        (persona / name).write_text(name, encoding="utf-8")
    (home / "MEMORY.md").mkdir()
    (home / "memories").mkdir()
    (home / "memories/USER.md").mkdir()
    executable = tmp_path / "hermes"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    (home / "state.db").touch()
    (home / "config.yaml").touch()
    bundle = tmp_path / "bundle.md"
    bundle.write_text("bundle", encoding="utf-8")
    (home / ".kindred/mouth-binding.json").write_text(
        json.dumps(_binding(bundle)),
        encoding="utf-8",
    )
    wire = HermesWire(
        host_executable=executable,
        host_home=home,
        approved_platform="synthetic",
        approved_sender_id="peer",
        canonical_session_id="session",
        canonical_session_key="key",
        outbound_chat_id="chat",
        outbound_sender_id="peer",
    )

    def runner(argv: tuple[str, ...], _stdin: bytes | None) -> tuple[int, bytes]:
        calls.append(argv)
        return 0, json.dumps(
            {
                "id": "session",
                "source": "synthetic",
                "user_id": "peer",
                "session_key": "key",
                "chat_id": "chat",
                "chat_type": "dm",
                "thread_id": None,
                "messages": [],
            }
        ).encode()

    prepared = prepare_host(
        HermesRuntimeModel(kind="hermes", wire=wire, identity=HERMES_BASELINE),
        hermes_runner=runner,
    )
    assert [check["status"] for check in prepared.checks(online=True)] == ["ok", "ok"]

    assert prepared.persona.soul_full == home / "SOUL.md"
    assert prepared.persona.identity == persona / "IDENTITY.md"
    assert len(calls) == 1 and calls[0][1] == "sessions"


def test_tagged_hermes_config_round_trip_and_legacy_rejection(tmp_path: Path) -> None:
    candidate = tmp_path / "hermes"
    home = tmp_path / "host"
    model = HermesRuntimeModel(
        kind="hermes",
        wire=HermesWire(
            host_executable=candidate,
            host_home=home,
            approved_platform="synthetic",
            approved_sender_id="peer",
            canonical_session_id="session",
            canonical_session_key="key",
            outbound_chat_id="chat",
            outbound_sender_id="peer",
        ),
        identity=("v2026.9.1", "0.21.0"),
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}\n", encoding="utf-8")

    publish_host(config_path, model)
    loaded = load_kindred_config(config_path, env={})

    assert loaded.mouth_host == model
    assert loaded.daemon.session_key == "session"

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["mouth_host"]["identity"] = ["v2026.8.13", "f80f453a", "0.20.1"]
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(KindredConfigError):
        load_kindred_config(config_path, env={})

    config_path.write_text("openclaw: {}\n", encoding="utf-8")
    with pytest.raises(KindredConfigError, match="rerun kindred install"):
        load_kindred_config(config_path, env={})
