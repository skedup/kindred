"""Synthetic smoke checks retained in the clean public root."""

from __future__ import annotations

import dataclasses
import os
import tarfile
import zipfile
from email.parser import Parser
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from kindred.activity import list_registered_actions, list_registered_activities
from kindred.config import DEFAULT_CONFIG
from kindred.llm import build_llm_client
from kindred.llm.anthropic_client import AnthropicLlmClient
from kindred.llm.claude_code_client import ClaudeCodeLlmClient
from kindred.llm.deepseek_client import DeepSeekLlmClient
from kindred.llm.gemini_client import GeminiLlmClient
from kindred.llm.openai_client import OpenAILlmClient
from kindred.llm.real_client import parse_llm_json, text_fingerprint

_PUBLIC_DISTRIBUTIONS = {
    "kindred",
    "kindred-capability-sdk",
    "kindred-capability-compose",
    "kindred-capability-draw",
    "kindred-capability-inventory",
    "kindred-capability-send",
}


def test_public_life_assets_are_complete() -> None:
    assert len(list_registered_actions()) == 13
    assert len(list_registered_activities()) == 7


def test_portable_capability_entry_points_remain_loadable() -> None:
    entries = metadata.entry_points().select(group="kindred.capability.v1")
    by_name = {entry.name: entry for entry in entries}

    assert {"compose", "inventory", "send"} <= by_name.keys()
    assert all(callable(by_name[name].load()) for name in ("compose", "inventory", "send"))


def test_portable_capability_packages_carry_the_root_license() -> None:
    root = Path(__file__).resolve().parents[2]
    expected = (root / "LICENSE").read_bytes()

    for package in (
        "kindred-capability-sdk",
        "kindred-capability-compose",
        "kindred-capability-draw",
        "kindred-capability-inventory",
        "kindred-capability-send",
    ):
        assert (root / "packages" / package / "LICENSE").read_bytes() == expected
        metadata_text = (root / "packages" / package / "pyproject.toml").read_text(encoding="utf-8")
        assert 'license = "Apache-2.0"' in metadata_text
        assert 'license-files = ["LICENSE"]' in metadata_text


def test_installed_root_distribution_declares_its_license_file() -> None:
    distribution = metadata.distribution("kindred")

    assert distribution.metadata.get_all("License-File") == ["LICENSE"]
    assert any(str(path).endswith("licenses/LICENSE") for path in distribution.files or ())


def test_built_public_distributions_carry_apache_license() -> None:
    artifact_dir_value = os.environ.get("KINDRED_PUBLIC_ARTIFACT_DIR")
    if artifact_dir_value is None:
        pytest.skip("set KINDRED_PUBLIC_ARTIFACT_DIR when validating clean build artifacts")
    artifact_dir = Path(artifact_dir_value)
    wheels: dict[str, Path] = {}
    sdists: dict[str, Path] = {}

    for wheel in artifact_dir.glob("*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            assert len(metadata_names) == 1
            package_metadata = Parser().parsestr(archive.read(metadata_names[0]).decode())
            name = package_metadata["Name"]
            assert name not in wheels
            assert package_metadata["License-Expression"] == "Apache-2.0"
            assert package_metadata.get_all("License-File") == ["LICENSE"]
            assert any(path.endswith(".dist-info/licenses/LICENSE") for path in archive.namelist())
            wheels[name] = wheel

    for sdist in artifact_dir.glob("*.tar.gz"):
        with tarfile.open(sdist) as archive:
            members = archive.getmembers()
            metadata_members = [member for member in members if member.name.endswith("/PKG-INFO")]
            assert len(metadata_members) == 1
            stream = archive.extractfile(metadata_members[0])
            assert stream is not None
            package_metadata = Parser().parsestr(stream.read().decode())
            name = package_metadata["Name"]
            assert name not in sdists
            assert package_metadata["License-Expression"] == "Apache-2.0"
            assert package_metadata.get_all("License-File") == ["LICENSE"]
            assert any(
                len(Path(member.name).parts) == 2 and Path(member.name).name == "LICENSE"
                for member in members
            )
            sdists[name] = sdist

    assert wheels.keys() == _PUBLIC_DISTRIBUTIONS
    assert sdists.keys() == _PUBLIC_DISTRIBUTIONS


def test_built_root_distribution_contains_precompiled_web_without_source_maps() -> None:
    artifact_dir_value = os.environ.get("KINDRED_PUBLIC_ARTIFACT_DIR")
    if artifact_dir_value is None:
        pytest.skip("set KINDRED_PUBLIC_ARTIFACT_DIR when validating clean build artifacts")
    artifact_dir = Path(artifact_dir_value)
    root_wheel = next(artifact_dir.glob("kindred-[0-9]*.whl"))
    root_sdist = next(artifact_dir.glob("kindred-[0-9]*.tar.gz"))

    with zipfile.ZipFile(root_wheel) as archive:
        names = set(archive.namelist())
        assert "kindred/web/static/index.html" in names
        assert "kindred/web/static/kindred-web-build.json" in names
        assert any(name.startswith("kindred/web/static/assets/") for name in names)
        assert not any(name.endswith(".map") for name in names)

    with tarfile.open(root_sdist) as archive:
        names = {member.name for member in archive.getmembers()}
        assert any(name.endswith("/src/kindred/web/static/index.html") for name in names)
        assert not any(name.endswith(".map") for name in names)


@pytest.mark.parametrize(
    ("provider", "client_type"),
    [
        ("claude_code", ClaudeCodeLlmClient),
        ("anthropic", AnthropicLlmClient),
        ("google", GeminiLlmClient),
        ("deepseek", DeepSeekLlmClient),
        ("openai", OpenAILlmClient),
    ],
)
def test_all_public_llm_providers_route_to_their_client(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    client_type: type[Any],
) -> None:
    sentinel = object()
    monkeypatch.setattr(client_type, "from_config", lambda _config: sentinel)
    config = dataclasses.replace(
        DEFAULT_CONFIG,
        llm=dataclasses.replace(DEFAULT_CONFIG.llm, provider=provider),
    )

    assert build_llm_client(config) is sentinel


def test_public_llm_parser_and_fingerprint_contract() -> None:
    private_text = "synthetic response body"

    assert parse_llm_json('prefix\n```json\n{"ok": true}\n```\n') == {"ok": True}
    fingerprint = text_fingerprint(private_text)
    assert fingerprint.startswith(f"bytes={len(private_text.encode())} sha256=")
    assert private_text not in fingerprint
