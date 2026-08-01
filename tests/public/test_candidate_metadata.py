"""Public candidate metadata must agree on one upstream and release identity."""

from __future__ import annotations

import json
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://github.com/skedup/kindred"
RELEASE = f"{REPOSITORY}/releases/download/v0.1.0"


def test_root_and_capability_metadata_use_the_public_upstream() -> None:
    projects = [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]

    for path in projects:
        project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        assert project["version"] == "0.1.0"
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
