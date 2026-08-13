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


def test_repository_release_inputs_freeze_two_complete_platforms() -> None:
    root = Path(__file__).resolve().parents[3]
    inputs = json.loads((root / "distribution/release-inputs.json").read_text())

    assert inputs["release_version"] == "0.2.1"
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
        assert len(wheels) == 46
        assert len({item[0] for item in wheels}) == 46
        assert sum(item[4] == "web" for item in wheels) == 4
        assert all(item[0] in inputs["licenses"] for item in wheels)
        assert all(len(item[3]) == 64 and item[2].endswith(".whl") for item in wheels)


def test_release_version_must_match_the_root_wheel(tmp_path: Path) -> None:
    release = _load_module()
    distribution = tmp_path / "distribution"
    distribution.mkdir()
    (distribution / "release-inputs.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_version": "0.2.0",
                "first_party": [
                    ["kindred", "0.1.0", ".", "kindred-0.1.0-py3-none-any.whl", "0" * 64]
                ],
            }
        )
    )

    with pytest.raises(release.ReleaseBuildError, match="root wheel"):
        release._load_inputs(tmp_path)


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
    python = _python_archive(tmp_path)
    digest = hashlib.sha256(python.read_bytes()).hexdigest()
    inputs = {
        "schema_version": 1,
        "release_version": "0.1.0",
        "openclaw": {"version": "2026.6.10", "build": "aa69b12", "protocol": 4},
        "build_tools": {},
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
        "wheels": {"common": [], "macos-arm64": [], "ubuntu24-x86_64": []},
        "licenses": {},
        "web_runtime": [],
    }
    distribution = root / "distribution"
    distribution.mkdir()
    (distribution / "release-inputs.json").write_text(json.dumps(inputs))
    (cache / "python").mkdir(parents=True)
    (cache / "python" / python.name).write_bytes(python.read_bytes())
    monkeypatch.setattr(release, "_build_first_party", lambda *_args: [first_party])
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=""),
    )

    manifest = release.build_release(root, cache, output)

    assert set(manifest["platforms"]) == {"macos-arm64", "ubuntu24-x86_64"}
    assert manifest["life_assets"] == {"actions": 13, "activities": 7}
    assert manifest["install_skill"] == {
        "included": True,
        "sha256": hashlib.sha256(install_skill.read_bytes()).hexdigest(),
    }
    assert manifest["draw"] == {"included": True, "enabled_by_default": False}
    assert manifest["mouth_plugin"]["version"] == "0.3.0"
    for platform in manifest["platforms"]:
        bundle = output / manifest["platforms"][platform]["bundle"]["filename"]
        with tarfile.open(bundle) as archive:
            assert all(not member.issym() and not member.islnk() for member in archive)
            assert "install-metadata.json" not in archive.getnames()
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
