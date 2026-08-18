"""Byte-for-byte CLI surface contract for the RS3-T3 split."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner, Result

from kindred import cli as cli_module
from kindred.cli import cli
from kindred.config import KindredConfig
from kindred.runtime import platform_service

CONTRACT_PATH = Path(__file__).parents[1] / "fixtures" / "cli_contract.json"
INSTALL_SKILL_PATH = (
    Path(__file__).parents[2] / "src" / "kindred" / "openclaw" / "install_skill" / "SKILL.md"
)
PROG_NAME = "kindred"
TERMINAL_WIDTH = 80


def _load_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _command_tree(
    command: click.Command,
    path: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    context = click.Context(command, info_name=path[-1] if path else "kindred")
    children: list[str] = []
    if isinstance(command, click.Group):
        children = command.list_commands(context)

    nodes: list[dict[str, object]] = [
        {
            "path": " ".join(path),
            "kind": "group" if isinstance(command, click.Group) else "command",
            "children": children,
        }
    ]
    if isinstance(command, click.Group):
        for name in children:
            child = command.get_command(context, name)
            assert child is not None
            nodes.extend(_command_tree(child, (*path, name)))
    return nodes


def _invoke(argv: list[str]) -> Result:
    return CliRunner().invoke(
        cli,
        argv,
        prog_name=PROG_NAME,
        color=False,
        terminal_width=TERMINAL_WIDTH,
    )


def test_cli_contract_fixture_identity_is_frozen() -> None:
    contract = _load_contract()

    assert contract["schema_version"] == 1
    assert contract["baseline"] == {
        "click_version": version("click"),
        "prog_name": PROG_NAME,
        "source_commit": "b72301d3b47e2c177f174e6386449c9fcfc311fd",
        "terminal_width": TERMINAL_WIDTH,
    }


def test_cli_command_tree_matches_frozen_contract() -> None:
    contract = _load_contract()

    assert _command_tree(cli) == contract["command_tree"]


def test_packaged_install_skill_uses_the_single_public_entrypoint() -> None:
    skill = INSTALL_SKILL_PATH.read_text(encoding="utf-8")

    assert "kindred install" in skill
    assert "kindred openclaw install" not in skill
    assert "kindred openclaw uninstall" not in skill


def test_recovery_service_commands_do_not_parse_broken_runtime_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps({"paths": {"life_root": str(tmp_path / "life")}}), encoding="utf-8"
    )
    monkeypatch.setattr(platform_service.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    platform_service.install_services(config, include_web=False)
    config.write_text("openclaw: {}\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        platform_service,
        "_run",
        lambda argv, **_kwargs: calls.append(argv) or (0, "active"),
    )

    assert platform_service.service_status(config)[0] == ("heart", True, True, True)
    assert platform_service.control_services(config, action="stop") == ("heart",)
    platform_service.show_logs("heart", follow=False, lines=5, config_path=config)
    assert calls[-1][0] == "journalctl"


@pytest.mark.parametrize(
    "path, expected",
    [
        pytest.param(path, expected, id=path or "<root>")
        for path, expected in _load_contract()["help"].items()
    ],
)
def test_each_cli_help_matches_frozen_contract(path: str, expected: str) -> None:
    result = _invoke([*path.split(), "--help"])

    assert result.exit_code == 0, result.output
    assert result.output == expected


@pytest.mark.parametrize(
    "case",
    [pytest.param(case, id=case["name"]) for case in _load_contract()["exit_codes"]],
)
def test_cli_exit_code_matrix(case: dict[str, Any]) -> None:
    result = _invoke(case["argv"])

    assert result.exit_code == case["exit_code"], result.output


def test_visual_observer_serve_uses_explicit_config_without_loading_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "observer.yaml"
    config_path.write_text(
        "\n".join(
            [
                "paths:",
                f"  life_root: {tmp_path / 'remote-life'}",
                "web:",
                '  host: "::1"',
                "  port: 9443",
                "resident:",
                f"  secrets_file: {tmp_path / 'must-not-be-read.env'}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        "kindred.config.loader.merge_runtime_secrets",
        lambda *_args, **_kwargs: pytest.fail("observer must not load resident secrets"),
    )

    def require_resident(config: object, *, validate_secrets: bool = True) -> None:
        calls["config"] = config
        calls["validate_secrets"] = validate_secrets

    monkeypatch.setattr(cli_module, "_require_resident_commit", require_resident)
    monkeypatch.setattr(cli_module, "_require_mouth_host_binding", lambda _config: None)
    monkeypatch.setattr("kindred.cli_serve.configure_logging", lambda _level: None)
    monkeypatch.setattr("kindred.web.assets.resolve_web_dist", lambda: tmp_path)
    monkeypatch.setattr(
        "kindred.web.app.create_app",
        lambda **kwargs: kwargs["config"],
    )
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, *, host, port: calls.update(run=(app, host, port)),
    )

    result = _invoke(["serve", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert calls["validate_secrets"] is False
    config = calls["config"]
    assert isinstance(config, KindredConfig)
    assert calls["run"] == (config, "::1", 9443)
    assert config.paths.life_root == tmp_path / "remote-life"
    assert "http://[::1]:9443/" in result.output
