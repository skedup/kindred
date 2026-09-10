"""Frozen input and offline release builder tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _load_module() -> Any:
    script = Path(__file__).resolve().parents[3] / "scripts/build_offline_release.py"
    spec = importlib.util.spec_from_file_location("build_offline_release", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_install_verifier() -> Any:
    script = Path(__file__).resolve().parents[3] / "scripts/verify_offline_install.py"
    spec = importlib.util.spec_from_file_location("verify_offline_install", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _wheel(path: Path, name: str = "kindred", version: str = "0.1.0") -> Path:
    dist = name.replace("-", "_")
    target = path / f"{dist}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(
            f"{dist}-{version}.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            f"Name: {name}\n"
            f"Version: {version}\n"
            "License-Expression: Apache-2.0\n",
        )
    return target


def _python_archive(path: Path) -> Path:
    runtime = path / "runtime/python/bin"
    runtime.mkdir(parents=True)
    python = runtime / "python3"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    archive = path / "python.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(path / "runtime/python", arcname="python")
    return archive


def _sidecar(path: Path, platform: str, source_sha: str) -> Path:
    target = "macos-arm64" if platform == "macos-arm64" else "ubuntu-24.04-x86_64"
    name = f"kindred-xhs-sidecar-2.7.2-{target}.tar.gz"
    root = path / name.removesuffix(".tar.gz")
    operator = root / "bin/kindred-xhs"
    operator.parent.mkdir(parents=True)
    operator.write_text("#!/bin/sh\n")
    operator.chmod(0o755)
    (root / "LICENSE").write_text("MIT\n")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "version": "2.7.2",
                "source_sha": source_sha,
                "target": target,
                "service_api_version": "1",
            }
        )
    )
    archive = path / name
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(root, arcname=root.name)
    return archive


def test_repository_release_inputs_freeze_two_complete_platforms() -> None:
    root = Path(__file__).resolve().parents[3]
    inputs = json.loads((root / "distribution/release-inputs.json").read_text())

    assert inputs["release_version"] == "0.4.1"
    assert set(inputs["mouth_hosts"]) == {"openclaw", "hermes"}
    assert len(inputs["mouth_hosts"]["openclaw"]["profiles"]) == 2
    assert inputs["mouth_hosts"]["hermes"]["maturity"] == "experimental"
    assert set(inputs["platforms"]) == {"macos-arm64", "ubuntu24-x86_64"}
    assert inputs["build_tools"] == {
        "node": "22.18.0",
        "pnpm": "11.7.0",
        "uv": "0.8.13",
        "hatchling": "1.27.0",
        "pnpm_lock_sha256": "db16c548e256ee09cec71ed3a5131aea0d3c5edd9f7afdc149dc503095cc68be",
    }
    assert len(inputs["first_party"]) == 6
    assert all(item[3].endswith(".whl") and len(item[4]) == 64 for item in inputs["first_party"])
    for platform in inputs["platforms"]:
        wheels = inputs["wheels"]["common"] + inputs["wheels"][platform]
        assert len(wheels) == 60
        assert len({item[0] for item in wheels}) == 60
        assert sum(item[4] == "web" for item in wheels) == 2
        assert all(item[0] in inputs["licenses"] for item in wheels)
        assert all(len(item[3]) == 64 and item[2].endswith(".whl") for item in wheels)
    assert inputs["xiaohongshu"]["source_sha"] == "b019fdbf02ba0e8d4f4fdb4f15b58230a8493f5d"
    assert inputs["xiaohongshu"]["wheel"][1] == "0.3.3"
    assert {row[1] for row in inputs["xiaohongshu"]["sidecars"].values()} == {"2.7.2"}
    assert inputs["memory"] == {
        "mcp_included": True,
        "default_channel": "lexical",
        "vector_included": False,
        "automatic_sync": False,
        "automatic_host_registration": False,
    }


def test_install_closure_verifies_selected_manifest_wheels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _load_install_verifier()
    manifest = {
        "schema_version": 1,
        "platforms": {
            "fixture": {
                "wheels": [
                    {"distribution": "kindred", "version": "0.4.1", "group": "base"},
                    {"distribution": "fastapi", "version": "0.141.1", "group": "web"},
                ]
            }
        },
    }
    monkeypatch.setattr(
        verifier,
        "_installed_distributions",
        lambda: {"kindred": "0.4.1", "fastapi": "0.141.1"},
    )
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    assert verifier.verify_install(manifest, "fixture", no_web=False) == 2


def test_install_closure_rejects_identity_drift_and_fastapi_in_no_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _load_install_verifier()
    manifest = {
        "schema_version": 1,
        "platforms": {
            "fixture": {
                "wheels": [
                    {"distribution": "kindred", "version": "0.4.1", "group": "base"},
                    {"distribution": "fastapi", "version": "0.141.1", "group": "web"},
                ]
            }
        },
    }
    monkeypatch.setattr(verifier.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verifier, "_installed_distributions", lambda: {"kindred": "0.4.0"})
    with pytest.raises(verifier.InstallClosureError, match="identity mismatch"):
        verifier.verify_install(manifest, "fixture", no_web=True)

    monkeypatch.setattr(
        verifier,
        "_installed_distributions",
        lambda: {"kindred": "0.4.1", "fastapi": "0.141.1"},
    )
    with pytest.raises(verifier.InstallClosureError, match="contains FastAPI"):
        verifier.verify_install(manifest, "fixture", no_web=True)


def test_install_closure_rejects_pip_check_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _load_install_verifier()
    manifest = {
        "schema_version": 1,
        "platforms": {
            "fixture": {
                "wheels": [{"distribution": "kindred", "version": "0.4.1", "group": "base"}]
            }
        },
    }
    monkeypatch.setattr(verifier, "_installed_distributions", lambda: {"kindred": "0.4.1"})
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(verifier.InstallClosureError, match="dependency check failed"):
        verifier.verify_install(manifest, "fixture", no_web=False)


def test_release_version_must_match_the_root_wheel(tmp_path: Path) -> None:
    release = _load_module()
    distribution = tmp_path / "distribution"
    distribution.mkdir()
    (distribution / "release-inputs.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_version": "0.2.0",
                "mouth_hosts": {},
                "first_party": [
                    ["kindred", "0.1.0", ".", "kindred-0.1.0-py3-none-any.whl", "0" * 64]
                ],
            }
        )
    )

    with pytest.raises(release.ReleaseBuildError, match="root wheel"):
        release._load_inputs(tmp_path)


def test_mouth_host_matrix_rejects_missing_or_mixed_hosts() -> None:
    release = _load_module()
    platforms = {"macos-arm64", "ubuntu24-x86_64"}

    with pytest.raises(release.ReleaseBuildError, match="Mouth Host"):
        release._validate_mouth_hosts({"openclaw": {}}, platforms)
    with pytest.raises(release.ReleaseBuildError, match="Mouth Host"):
        release._validate_mouth_hosts(
            {
                "openclaw": {
                    "maturity": "supported",
                    "platforms": sorted(platforms),
                    "profiles": [],
                    "hermes": {},
                },
                "hermes": {},
            },
            platforms,
        )


def test_builder_emits_two_dereferenced_bundles_and_release_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _load_module()
    root = tmp_path / "clean"
    cache = tmp_path / "cache"
    output = tmp_path / "output"
    output.mkdir()
    for kind, count in (("actions", 13), ("activities", 7)):
        for index in range(count):
            item = root / "src/kindred/life_assets" / kind / str(index)
            item.mkdir(parents=True)
            (item / "manifest.yaml").write_text("name: fixture\n")
    plugin = root / "src/kindred/openclaw/mouth_plugin"
    plugin.mkdir(parents=True)
    for name in ("binding.js", "index.js", "openclaw.plugin.json", "outbound.js"):
        (plugin / name).write_text("fixture\n")
    (plugin / "package.json").write_text(json.dumps({"version": "0.3.0"}))
    hermes_plugin = root / "src/kindred/hermes/mouth_plugin"
    hermes_plugin.mkdir(parents=True)
    (hermes_plugin / "__init__.py").write_text("# fixture\n")
    (hermes_plugin / "plugin.yaml").write_text("name: kindred-mouth\nversion: 0.1.0\n")
    install_skill = root / "src/kindred/openclaw/install_skill/SKILL.md"
    install_skill.parent.mkdir()
    install_skill.write_text("---\nname: install-kindred\ndescription: fixture\n---\n")
    static = root / "src/kindred/web/static"
    static.mkdir(parents=True)
    (static / "kindred-web-build.json").write_text(
        json.dumps({"schema_version": 1, "node": "22.18.0", "pnpm": "11.7.0"})
    )
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "install.sh").write_text("#!/bin/sh\nexit 0\n")
    first_party_dir = tmp_path / "first-party"
    first_party_dir.mkdir()
    first_party = _wheel(first_party_dir)
    mcp_wheel = _wheel(first_party_dir, "mcp", "2.1.1")
    xhs_wheel = _wheel(first_party_dir, "kindred-capability-xiaohongshu", "0.3.3")
    python = _python_archive(tmp_path)
    digest = hashlib.sha256(python.read_bytes()).hexdigest()
    source_sha = "a" * 40
    sidecars = {
        platform: _sidecar(tmp_path, platform, source_sha)
        for platform in ("macos-arm64", "ubuntu24-x86_64")
    }
    inputs = {
        "schema_version": 1,
        "release_version": "0.1.0",
        "mouth_hosts": {
            "openclaw": {
                "maturity": "supported",
                "platforms": ["macos-arm64", "ubuntu24-x86_64"],
                "profiles": [
                    {"version": "a", "build": "b", "protocol": 4, "verification": "verified"},
                    {"version": "c", "build": "d", "protocol": 4, "verification": "verified"},
                ],
            },
            "hermes": {
                "maturity": "experimental",
                "platforms": ["macos-arm64", "ubuntu24-x86_64"],
                "profiles": [{"release": "v1", "package": "1.0.0", "verification": "verified"}],
            },
        },
        "xiaohongshu": {
            "source_sha": source_sha,
            "release_tag": "kindred-xhs-v0.3.3",
            "wheel": [
                "kindred-capability-xiaohongshu",
                "0.3.3",
                xhs_wheel.name,
                xhs_wheel.stat().st_size,
                hashlib.sha256(xhs_wheel.read_bytes()).hexdigest(),
                "MIT",
            ],
            "sidecars": {
                platform: [
                    sidecar.name,
                    "2.7.2",
                    sidecar.stat().st_size,
                    hashlib.sha256(sidecar.read_bytes()).hexdigest(),
                    "MIT",
                    1,
                ]
                for platform, sidecar in sidecars.items()
            },
        },
        "build_tools": {},
        "memory": {
            "mcp_included": True,
            "default_channel": "lexical",
            "vector_included": False,
            "automatic_sync": False,
            "automatic_host_registration": False,
        },
        "first_party": [
            [
                "kindred",
                "0.1.0",
                ".",
                first_party.name,
                hashlib.sha256(first_party.read_bytes()).hexdigest(),
            ]
        ],
        "platforms": {
            name: {
                "os": "macos" if name.startswith("macos") else "linux",
                "arch": "arm64" if name.startswith("macos") else "x86_64",
                "minimum_os": "fixture",
                "python": [
                    "3.11.15",
                    "fixture",
                    python.name,
                    python.stat().st_size,
                    digest,
                    "https://example.invalid/python",
                ],
            }
            for name in ("macos-arm64", "ubuntu24-x86_64")
        },
        "wheels": {
            "common": [
                [
                    "mcp",
                    "2.1.1",
                    mcp_wheel.name,
                    hashlib.sha256(mcp_wheel.read_bytes()).hexdigest(),
                    "base",
                ]
            ],
            "macos-arm64": [],
            "ubuntu24-x86_64": [],
        },
        "licenses": {"kindred-capability-xiaohongshu": "MIT", "mcp": "MIT"},
        "web_runtime": [],
    }
    distribution = root / "distribution"
    distribution.mkdir()
    (distribution / "release-inputs.json").write_text(json.dumps(inputs))
    (cache / "python").mkdir(parents=True)
    (cache / "python" / python.name).write_bytes(python.read_bytes())
    (cache / "external").mkdir()
    (cache / "external" / xhs_wheel.name).write_bytes(xhs_wheel.read_bytes())
    for platform in inputs["platforms"]:
        wheel_cache = cache / "wheels" / platform
        wheel_cache.mkdir(parents=True)
        (wheel_cache / mcp_wheel.name).write_bytes(mcp_wheel.read_bytes())
    for sidecar in sidecars.values():
        (cache / "external" / sidecar.name).write_bytes(sidecar.read_bytes())
    monkeypatch.setattr(release, "_build_first_party", lambda *_args: [first_party])
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=""),
    )

    manifest = release.build_release(root, cache, output)

    assert set(manifest["platforms"]) == {"macos-arm64", "ubuntu24-x86_64"}
    assert manifest["life_assets"] == {"actions": 15, "activities": 8}
    assert manifest["install_skill"] == {
        "included": True,
        "sha256": hashlib.sha256(install_skill.read_bytes()).hexdigest(),
    }
    assert manifest["draw"] == {"included": True, "enabled_by_default": False}
    assert manifest["memory"] == inputs["memory"]
    assert manifest["mouth_hosts"] == inputs["mouth_hosts"]
    assert manifest["mouth_plugins"]["openclaw"]["version"] == "0.3.0"
    assert manifest["mouth_plugins"]["hermes"]["version"] == "0.1.0"
    for platform in manifest["platforms"]:
        bundle = output / manifest["platforms"][platform]["bundle"]["filename"]
        with tarfile.open(bundle) as archive:
            assert all(not member.issym() and not member.islnk() for member in archive)
            assert "install-metadata.json" not in archive.getnames()
            assert "services/xhs-mcp-sidecar.tar.gz" in archive.getnames()
            assert f"wheelhouse/{xhs_wheel.name}" in archive.getnames()
    sums = (output / "SHA256SUMS").read_text()
    assert "manifest.json" in sums and "SHA256SUMS" not in sums
    sbom = json.loads((output / "SBOM.spdx.json").read_text())
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert all(
        package["filesAnalyzed"] is False
        and package["licenseDeclared"] == package["licenseConcluded"]
        for package in sbom["packages"]
    )
    assert (
        sum(package["name"].startswith("python-build-standalone-") for package in sbom["packages"])
        == 2
    )


def test_builder_requires_clean_root_empty_output_and_exact_asset_count(tmp_path: Path) -> None:
    release = _load_module()
    output = tmp_path / "output"
    output.mkdir()
    (output / "occupied").write_text("x")
    with pytest.raises(release.ReleaseBuildError, match="clean export"):
        release.build_release(tmp_path, tmp_path, output)


def test_first_party_wheel_hash_is_frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = _load_module()
    root = tmp_path / "clean"
    target = tmp_path / "wheels"
    (root / "web").mkdir(parents=True)
    lockfile = root / "web/pnpm-lock.yaml"
    lockfile.write_text("lock")
    wheel_name = "kindred-0.1.0-py3-none-any.whl"

    def fake_run(argv: list[str], *, cwd: Path) -> str:
        assert cwd == root
        if argv[:2] == ["uv", "build"]:
            _wheel(target)
        return {"node": "v22.18.0", "pnpm": "11.7.0", "uv": "uv 0.8.13"}.get(argv[0], "")

    monkeypatch.setattr(release, "_run", fake_run)
    inputs = {
        "build_tools": {
            "node": "22.18.0",
            "pnpm": "11.7.0",
            "uv": "0.8.13",
            "pnpm_lock_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
        },
        "first_party": [["kindred", "0.1.0", ".", wheel_name, "0" * 64]],
        "licenses": {},
    }

    with pytest.raises(release.ReleaseBuildError, match="frozen input identity"):
        release._build_first_party(root, target, inputs)


def test_bundle_archive_is_reproducible(tmp_path: Path) -> None:
    release = _load_module()
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_text("fixture")

    release._tar_bundle(source, tmp_path / "first.tar.gz")
    release._tar_bundle(source, tmp_path / "second.tar.gz")

    assert (tmp_path / "first.tar.gz").read_bytes() == (tmp_path / "second.tar.gz").read_bytes()
