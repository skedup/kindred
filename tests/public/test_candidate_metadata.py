"""Public candidate metadata must agree on one upstream and release identity."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import tomllib

from kindred.hermes import HERMES_MOUTH_PLUGIN_DIR
from kindred.hermes.mouth_plugin import plugin_digest
from kindred.hermes.wire import HERMES_BASELINE
from kindred.openclaw import MOUTH_PLUGIN_DIR
from kindred.openclaw.install import OPENCLAW_PROFILES, _plugin_tree_digest

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://github.com/skedup/kindred"
RELEASE = f"{REPOSITORY}/releases/download/v0.4.0"


def _load_release_builder() -> Any:
    script = ROOT / "scripts/build_offline_release.py"
    spec = importlib.util.spec_from_file_location("public_build_offline_release", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_release_scanner() -> Any:
    script = ROOT / "scripts/public_release.py"
    spec = importlib.util.spec_from_file_location("public_release_scanner", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_root_and_capability_metadata_use_the_public_upstream() -> None:
    projects = [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]

    for path in projects:
        project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        expected_version = "0.4.0" if path == ROOT / "pyproject.toml" else "0.1.0"
        assert project["version"] == expected_version
        assert project["urls"]["Repository"] == REPOSITORY
        assert project["urls"]["Issues"] == f"{REPOSITORY}/issues"


def test_public_documents_and_issue_templates_are_in_the_clean_root() -> None:
    policy = json.loads((ROOT / "distribution/public-allowlist.json").read_text())
    paths = set(policy["paths"])

    assert {
        "README.md",
        "README.zh-CN.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/capability_proposal.yml",
        ".github/ISSUE_TEMPLATE/installation_problem.yml",
    } <= paths


def test_readmes_and_bootstrap_use_the_versioned_release() -> None:
    for path in (ROOT / "README.md", ROOT / "README.zh-CN.md"):
        assert f"{RELEASE}/install.sh" in path.read_text(encoding="utf-8")

    bootstrap = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    assert REPOSITORY in bootstrap
    assert "KINDRED_RELEASE_BASE_URL" in bootstrap


def test_release_snapshot_uses_root_version_and_packaged_plugin_identity() -> None:
    builder = _load_release_builder()
    inputs = builder._load_inputs(ROOT)
    root_row = next(row for row in inputs["first_party"] if row[0] == "kindred")
    plugin = json.loads(
        (ROOT / "src/kindred/openclaw/mouth_plugin/package.json").read_text(encoding="utf-8")
    )

    assert inputs["release_version"] == "0.4.0"
    assert root_row[1:4] == ["0.4.0", ".", "kindred-0.4.0-py3-none-any.whl"]
    assert builder._plugin_version(ROOT) == plugin["version"] == "0.4.0"
    assert builder._hermes_plugin_version(ROOT) == "0.1.0"


def test_release_matrix_matches_packaged_host_contracts() -> None:
    inputs = _load_release_builder()._load_inputs(ROOT)
    hosts = inputs["mouth_hosts"]

    assert hosts == {
        "openclaw": {
            "maturity": "supported",
            "platforms": ["macos-arm64", "ubuntu24-x86_64"],
            "profiles": [
                {
                    "version": profile.version,
                    "build": profile.build,
                    "protocol": profile.protocol,
                    "verification": "verified",
                }
                for profile in OPENCLAW_PROFILES
            ],
        },
        "hermes": {
            "maturity": "experimental",
            "platforms": ["macos-arm64", "ubuntu24-x86_64"],
            "profiles": [
                {
                    "release": HERMES_BASELINE[0],
                    "package": HERMES_BASELINE[1],
                    "verification": "verified",
                }
            ],
        },
    }
    assert "source" not in json.dumps(hosts)


def test_mouth_plugin_checksum_covers_outbound_runtime(tmp_path: Path) -> None:
    builder = _load_release_builder()
    source = ROOT / "src/kindred/openclaw/mouth_plugin"
    plugin = tmp_path / "src/kindred/openclaw/mouth_plugin"
    shutil.copytree(source, plugin)
    original = builder._plugin_checksum(tmp_path)

    outbound = plugin / "outbound.js"
    outbound.write_text(outbound.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")

    assert builder._plugin_checksum(tmp_path) != original


def test_release_and_installer_use_the_same_mouth_plugin_identity() -> None:
    builder = _load_release_builder()

    assert builder._plugin_checksum(ROOT) == _plugin_tree_digest(MOUTH_PLUGIN_DIR)
    assert builder._hermes_plugin_checksum(ROOT) == plugin_digest(
        HERMES_MOUTH_PLUGIN_DIR
    ).removeprefix("sha256:")


def test_public_release_scanner_recognizes_xai_credentials() -> None:
    scanner = _load_release_scanner()
    assignment = "XAI_API" + '_KEY="' + "synthetic-secret-value" + '"'

    assert "XAI_API_KEY" in scanner._CREDENTIAL_REFERENCES
    assert scanner._CREDENTIAL_PATTERN.search(assignment) is not None
