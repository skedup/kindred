#!/usr/bin/env python3
from __future__ import annotations

import argparse
import email
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any


class ReleaseBuildError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _load_inputs(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / "distribution/release-inputs.json").read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError("release inputs are unreadable") from exc
    version = value.get("release_version")
    if value.get("schema_version") != 1 or not isinstance(version, str):
        raise ReleaseBuildError("release inputs have an unsupported schema or version")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ReleaseBuildError("release inputs have an unsupported schema or version")
    root_rows = [row for row in value.get("first_party", []) if row[:1] == ["kindred"]]
    expected_wheel = f"kindred-{version}-py3-none-any.whl"
    if len(root_rows) != 1 or root_rows[0][1:4] != [version, ".", expected_wheel]:
        raise ReleaseBuildError("release version does not match the root wheel")
    _validate_mouth_hosts(value.get("mouth_hosts"), set(value.get("platforms", {})))
    _validate_xhs(value.get("xiaohongshu"), set(value.get("platforms", {})))
    return value


def _validate_mouth_hosts(value: object, platforms: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != {"openclaw", "hermes"}:
        raise ReleaseBuildError("Mouth Host release matrix is invalid")
    expected = {
        "openclaw": ({"version", "build", "protocol", "verification"}, "supported", 2),
        "hermes": ({"release", "package", "verification"}, "experimental", 1),
    }
    for kind, (profile_keys, maturity, count) in expected.items():
        host = value[kind]
        host_platforms = host.get("platforms") if isinstance(host, dict) else None
        profiles = host.get("profiles") if isinstance(host, dict) else None
        if (
            not isinstance(host, dict)
            or set(host) != {"maturity", "platforms", "profiles"}
            or host["maturity"] != maturity
            or not isinstance(host_platforms, list)
            or any(not isinstance(platform, str) for platform in host_platforms)
            or set(host_platforms) != platforms
            or not isinstance(profiles, list)
            or len(profiles) != count
            or any(
                not isinstance(profile, dict)
                or set(profile) != profile_keys
                or profile["verification"] != "verified"
                or not _valid_host_profile(kind, profile)
                for profile in profiles
            )
        ):
            raise ReleaseBuildError("Mouth Host release matrix is invalid")


def _valid_host_profile(kind: str, profile: dict[str, object]) -> bool:
    if kind == "openclaw":
        return (
            all(type(profile[key]) is str and bool(profile[key]) for key in ("version", "build"))
            and type(profile["protocol"]) is int
            and profile["protocol"] > 0
        )
    return all(type(profile[key]) is str and bool(profile[key]) for key in ("release", "package"))


def _validate_xhs(value: object, platforms: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != {
        "source_sha",
        "release_tag",
        "wheel",
        "sidecars",
    }:
        raise ReleaseBuildError("Xiaohongshu release identity is invalid")
    wheel = value["wheel"]
    sidecars = value["sidecars"]
    if not (
        isinstance(value["source_sha"], str)
        and re.fullmatch(r"[0-9a-f]{40}", value["source_sha"])
        and value["release_tag"] == "kindred-xhs-v0.3.3"
        and isinstance(wheel, list)
        and len(wheel) == 6
        and wheel[:3]
        == [
            "kindred-capability-xiaohongshu",
            "0.3.3",
            "kindred_capability_xiaohongshu-0.3.3-py3-none-any.whl",
        ]
        and _valid_frozen_file(wheel[3], wheel[4])
        and wheel[5] == "MIT"
        and isinstance(sidecars, dict)
        and set(sidecars) == platforms
        and all(
            isinstance(row, list)
            and len(row) == 6
            and row[1] == "2.7.2"
            and _valid_frozen_file(row[2], row[3])
            and row[4:] == ["MIT", 1]
            for row in sidecars.values()
        )
    ):
        raise ReleaseBuildError("Xiaohongshu release identity is invalid")


def _valid_frozen_file(size: object, digest: object) -> bool:
    return (
        type(size) is int
        and size > 0
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )


def _verify(path: Path, *, size: int | None = None, digest: str) -> None:
    if (
        not path.is_file()
        or path.is_symlink()
        or (size is not None and path.stat().st_size != size)
        or _sha256(path) != digest
    ):
        raise ReleaseBuildError("frozen input identity mismatch")


def _run(argv: list[str], *, cwd: Path) -> str:
    env = {**os.environ, "PNPM_CONFIG_OFFLINE": "true", "UV_OFFLINE": "true"}
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseBuildError("release build command failed") from exc
    return result.stdout.strip()


def _build_first_party(root: Path, target: Path, inputs: dict[str, Any]) -> list[Path]:
    tools = inputs["build_tools"]
    target.mkdir(parents=True)
    actual = {
        "node": _run(["node", "--version"], cwd=root).removeprefix("v"),
        "pnpm": _run(["pnpm", "--version"], cwd=root),
        "uv": _run(["uv", "--version"], cwd=root).split()[1],
    }
    if any(actual[name] != tools[name] for name in actual):
        raise ReleaseBuildError("release build tool identity mismatch")
    if _sha256(root / "web/pnpm-lock.yaml") != tools["pnpm_lock_sha256"]:
        raise ReleaseBuildError("Web lockfile identity mismatch")
    _run([sys.executable, "scripts/build_web.py", "build"], cwd=root)
    for _name, _version, rel, _filename, _digest in inputs["first_party"]:
        _run(["uv", "build", "--wheel", "--out-dir", str(target), rel], cwd=root)
    wheels = sorted(target.glob("*.whl"))
    expected = {item[3]: item[4] for item in inputs["first_party"]}
    if {wheel.name for wheel in wheels} != expected.keys():
        raise ReleaseBuildError("first-party wheel set is incomplete")
    for wheel in wheels:
        _verify(wheel, digest=expected[wheel.name])
    return wheels


def _wheel_record(path: Path, group: str, licenses: dict[str, str]) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise ValueError
            message = email.message_from_bytes(archive.read(metadata[0]))
        name = str(message["Name"])
        version = str(message["Version"])
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        fallback = "Apache-2.0" if normalized.startswith("kindred") else ""
        license_id = licenses.get(normalized, fallback)
        if not name or not version or not license_id:
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        raise ReleaseBuildError("wheel metadata or license is incomplete") from exc
    return {
        "distribution": name,
        "version": version,
        "filename": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
        "group": group,
        "license": license_id,
    }


def _tar_bundle(source: Path, destination: Path) -> None:
    with destination.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", dereference=True) as archive:
                for path in sorted(source.rglob("*")):
                    info = archive.gettarinfo(path, path.relative_to(source).as_posix())
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ""
                    if info.isfile():
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)


def _validate_sidecar(
    source: Path,
    identity: list[Any],
    platform: str,
    source_sha: str,
) -> None:
    filename, version, size, digest, license_id, service_api_version = identity
    _verify(source, size=size, digest=digest)
    expected_root = filename.removesuffix(".tar.gz")
    try:
        with tarfile.open(source, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                path = Path(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or path.parts[0] != expected_root
                    or not (member.isdir() or member.isfile())
                ):
                    raise ReleaseBuildError("Xiaohongshu sidecar archive is unsafe")
            info = archive.getmember(f"{expected_root}/manifest.json")
            stream = archive.extractfile(info)
            if stream is None:
                raise ReleaseBuildError("Xiaohongshu sidecar manifest is unreadable")
            manifest = json.load(stream)
    except (KeyError, OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError("Xiaohongshu sidecar archive is unreadable") from exc
    expected_target = "macos-arm64" if platform == "macos-arm64" else "ubuntu-24.04-x86_64"
    if (
        manifest.get("version") != version
        or manifest.get("source_sha") != source_sha
        or manifest.get("target") != expected_target
        or str(manifest.get("service_api_version")) != str(service_api_version)
        or license_id != "MIT"
    ):
        raise ReleaseBuildError("Xiaohongshu sidecar identity mismatch")


def _plugin_checksum(root: Path) -> str:
    return _tree_checksum(root / "src/kindred/openclaw/mouth_plugin")


def _tree_checksum(plugin: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(plugin.rglob("*")):
        if path.is_symlink():
            raise ReleaseBuildError("Mouth Plugin tree is invalid")
        if path.is_file():
            digest.update(path.relative_to(plugin).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _hermes_plugin_checksum(root: Path) -> str:
    plugin = root / "src/kindred/hermes/mouth_plugin"
    digest = hashlib.sha256()
    for name in ("__init__.py", "plugin.yaml"):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update((plugin / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _plugin_version(root: Path) -> str:
    try:
        package = json.loads((root / "src/kindred/openclaw/mouth_plugin/package.json").read_text())
        version = package["version"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReleaseBuildError("Mouth Plugin version is unreadable") from exc
    if not isinstance(version, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ReleaseBuildError("Mouth Plugin version is invalid")
    return version


def _hermes_plugin_version(root: Path) -> str:
    try:
        text = (root / "src/kindred/hermes/mouth_plugin/plugin.yaml").read_text()
    except (OSError, UnicodeError) as exc:
        raise ReleaseBuildError("Hermes Mouth Plugin version is unreadable") from exc
    match = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", text, re.MULTILINE)
    if match is None:
        raise ReleaseBuildError("Hermes Mouth Plugin version is invalid")
    return match.group(1)


def _notices(inputs: dict[str, Any], wheels: list[dict[str, Any]]) -> str:
    rows = {(item["distribution"], item["version"], item["license"]) for item in wheels}
    rows.update(tuple(item) for item in inputs["web_runtime"])
    rows.add(("kindred-xhs-sidecar", inputs["xiaohongshu"]["sidecars"]["macos-arm64"][1], "MIT"))
    packages = "".join(
        f"{name} {version} | {license_id}\n" for name, version, license_id in sorted(rows)
    )
    header = """Kindred third-party notices

CPython 3.11.15 | PSF-2.0 | https://www.python.org/
python-build-standalone 20260728 | MPL-2.0 | https://github.com/astral-sh/python-build-standalone/tree/20260728

Dependencies
"""
    return header + packages


def _spdx_package(
    name: str,
    spdx_id: str,
    version: str,
    license_id: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "SPDXID": spdx_id,
        "versionInfo": version,
        "downloadLocation": extra.pop("source", "NOASSERTION"),
        "filesAnalyzed": False,
        "licenseConcluded": license_id,
        "licenseDeclared": license_id,
        **extra,
    }


def _sbom(inputs: dict[str, Any], platforms: dict[str, Any]) -> dict[str, Any]:
    packages = []
    for platform, data in platforms.items():
        for name, version, license_id in (
            ("CPython", data["python"]["version"], "PSF-2.0"),
            ("python-build-standalone", data["python"]["build"], "MPL-2.0"),
        ):
            packages.append(
                _spdx_package(
                    f"{name}-{platform}",
                    f"SPDXRef-{name}-{platform}",
                    version,
                    license_id,
                    source=data["python"]["source"],
                    checksums=[{"algorithm": "SHA256", "checksumValue": data["python"]["sha256"]}],
                )
            )
        packages.extend(
            _spdx_package(
                wheel["distribution"],
                f"SPDXRef-wheel-{platform}-{index}",
                wheel["version"],
                wheel["license"],
                checksums=[{"algorithm": "SHA256", "checksumValue": wheel["sha256"]}],
            )
            for index, wheel in enumerate(data["wheels"])
        )
        sidecar = data["xiaohongshu_sidecar"]
        packages.append(
            _spdx_package(
                f"kindred-xhs-sidecar-{platform}",
                f"SPDXRef-xhs-sidecar-{platform}",
                sidecar["version"],
                sidecar["license"],
                source=f"git+https://github.com/skedup/hi_x_h_5@{inputs['xiaohongshu']['source_sha']}",
                checksums=[{"algorithm": "SHA256", "checksumValue": sidecar["sha256"]}],
            )
        )
    packages.extend(
        _spdx_package(name, f"SPDXRef-web-{index}", version, license_id)
        for index, (name, version, license_id) in enumerate(inputs["web_runtime"])
    )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"kindred-{inputs['release_version']}-offline-bundles",
        "documentNamespace": f"https://kindred.invalid/spdx/{inputs['release_version']}",
        "creationInfo": {
            "created": "1970-01-01T00:00:00Z",
            "creators": ["Tool: Kindred release builder"],
        },
        "packages": packages,
    }


def build_release(root: Path, cache: Path, output: Path) -> dict[str, Any]:
    if (root / ".git").exists() or not output.is_dir() or any(output.iterdir()):
        raise ReleaseBuildError("build requires a clean export and an empty output directory")
    inputs = _load_inputs(root)
    licenses = inputs["licenses"]
    for kind, expected in (("actions", 13), ("activities", 7)):
        assets = root / "src/kindred/life_assets" / kind
        count = sum((path / "manifest.yaml").is_file() for path in assets.iterdir())
        if count != expected:
            raise ReleaseBuildError("public life asset count mismatch")
    with tempfile.TemporaryDirectory(prefix="kindred-release-") as temporary:
        temp = Path(temporary)
        first_party = _build_first_party(root, temp / "first-party", inputs)
        xhs = inputs["xiaohongshu"]
        xhs_wheel_row = xhs["wheel"]
        xhs_wheel = cache / "external" / xhs_wheel_row[2]
        _verify(xhs_wheel, size=xhs_wheel_row[3], digest=xhs_wheel_row[4])
        platform_manifest: dict[str, Any] = {}
        all_wheels: list[dict[str, Any]] = []
        for platform, platform_input in inputs["platforms"].items():
            python_version, build, filename, size, digest, source = platform_input["python"]
            python_archive = cache / "python" / filename
            _verify(python_archive, size=size, digest=digest)
            frozen = inputs["wheels"]["common"] + inputs["wheels"][platform]
            stage = temp / platform
            wheelhouse = stage / "wheelhouse"
            wheelhouse.mkdir(parents=True)
            records: list[dict[str, Any]] = []
            for distribution, version, wheel_name, wheel_hash, group in frozen:
                source_wheel = cache / "wheels" / platform / wheel_name
                _verify(source_wheel, digest=wheel_hash)
                shutil.copy2(source_wheel, wheelhouse / wheel_name)
                record = _wheel_record(source_wheel, group, licenses)
                if (record["distribution"].lower().replace("_", "-"), record["version"]) != (
                    distribution,
                    version,
                ):
                    raise ReleaseBuildError("frozen wheel metadata mismatch")
                records.append(record)
            for wheel in first_party:
                shutil.copy2(wheel, wheelhouse / wheel.name)
                records.append(_wheel_record(wheel, "base", licenses))
            shutil.copy2(xhs_wheel, wheelhouse / xhs_wheel.name)
            xhs_record = _wheel_record(xhs_wheel, "base", licenses)
            if (
                xhs_record["distribution"] != xhs_wheel_row[0]
                or xhs_record["version"] != xhs_wheel_row[1]
                or xhs_record["license"] != xhs_wheel_row[5]
            ):
                raise ReleaseBuildError("Xiaohongshu wheel metadata mismatch")
            records.append(xhs_record)
            sidecar_row = xhs["sidecars"][platform]
            sidecar_source = cache / "external" / sidecar_row[0]
            _validate_sidecar(sidecar_source, sidecar_row, platform, xhs["source_sha"])
            services = stage / "services"
            services.mkdir()
            shutil.copy2(sidecar_source, services / "xhs-mcp-sidecar.tar.gz")
            shutil.copy2(python_archive, stage / "python-runtime.tar.gz")
            bundle_name = f"kindred-v{inputs['release_version']}-{platform}.tar.gz"
            bundle = output / bundle_name
            _tar_bundle(stage, bundle)
            platform_manifest[platform] = {
                **{key: platform_input[key] for key in ("os", "arch", "minimum_os")},
                "python": {
                    "version": python_version,
                    "build": build,
                    "source": source,
                    "license": "PSF-2.0",
                    "distributor_license": "MPL-2.0",
                    "size": size,
                    "sha256": digest,
                },
                "bundle": {
                    "filename": bundle_name,
                    "size": bundle.stat().st_size,
                    "sha256": _sha256(bundle),
                },
                "wheels": records,
                "xiaohongshu_sidecar": {
                    "version": sidecar_row[1],
                    "filename": sidecar_row[0],
                    "size": sidecar_row[2],
                    "sha256": sidecar_row[3],
                    "license": sidecar_row[4],
                    "service_api_version": sidecar_row[5],
                },
            }
            all_wheels.extend(records)
        web = json.loads((root / "src/kindred/web/static/kindred-web-build.json").read_text())
        manifest = {
            "schema_version": 1,
            "release_version": inputs["release_version"],
            "mouth_hosts": inputs["mouth_hosts"],
            "build_tools": inputs["build_tools"],
            "life_assets": {"actions": 15, "activities": 8},
            "xiaohongshu": {
                "source_sha": xhs["source_sha"],
                "release_tag": xhs["release_tag"],
                "wheel": xhs_record,
                "enabled_by_default": True,
                "write_mode": "none",
            },
            "mouth_plugins": {
                "openclaw": {
                    "version": _plugin_version(root),
                    "sha256": _plugin_checksum(root),
                },
                "hermes": {
                    "version": _hermes_plugin_version(root),
                    "sha256": _hermes_plugin_checksum(root),
                },
            },
            "web": {"included": True, "build": web},
            "draw": {"included": True, "enabled_by_default": False},
            "install_skill": {
                "included": True,
                "sha256": _sha256(root / "src/kindred/openclaw/install_skill/SKILL.md"),
            },
            "platforms": platform_manifest,
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        (output / "THIRD_PARTY-NOTICES.txt").write_text(_notices(inputs, all_wheels))
        (output / "SBOM.spdx.json").write_text(
            json.dumps(_sbom(inputs, platform_manifest), indent=2, sort_keys=True) + "\n"
        )
        shutil.copy2(root / "scripts/install.sh", output / "install.sh")
        assets = sorted(path for path in output.iterdir() if path.name != "SHA256SUMS")
        checksums = "".join(f"{_sha256(path)}  {path.name}\n" for path in assets)
        (output / "SHA256SUMS").write_text(checksums)
    _run(
        [sys.executable, str(root / "scripts/public_release.py"), "--repo", str(root), "scan"]
        + ["--trusted-inputs", str(root / "distribution/release-inputs.json"), str(output)],
        cwd=root,
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean-root", type=Path, required=True)
    parser.add_argument("--input-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = build_release(
            args.clean_root.resolve(), args.input_cache.resolve(), args.output.resolve()
        )
    except (OSError, ReleaseBuildError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}))
        return 2
    print(json.dumps({"status": "ok", "platforms": sorted(manifest["platforms"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
