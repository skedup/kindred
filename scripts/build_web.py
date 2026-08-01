#!/usr/bin/env python3
"""Build and verify the precompiled Web distribution used by release wheels."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

_MANIFEST = "kindred-web-build.json"
_ASSET_PATTERN = re.compile(r"""(?:src|href)=["'](/assets/[^"']+)["']""")
_SOURCE_FILES = (
    ".node-version",
    "env.d.ts",
    "index.html",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "tsconfig.json",
    "tsconfig.node.json",
    "vite.config.ts",
)


class WebBuildError(RuntimeError):
    pass


def _input_files(web: Path) -> list[Path]:
    files = [web / name for name in _SOURCE_FILES]
    files.extend(sorted(path for path in (web / "src").rglob("*") if path.is_file()))
    if any(not path.is_file() or path.is_symlink() for path in files):
        raise WebBuildError("web build input is missing or unsupported")
    return files


def _input_digest(web: Path) -> str:
    digest = hashlib.sha256()
    for path in _input_files(web):
        digest.update(path.relative_to(web).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _expected_versions(web: Path) -> tuple[str, str]:
    package = json.loads((web / "package.json").read_text(encoding="utf-8"))
    node = package.get("engines", {}).get("node")
    manager = package.get("packageManager", "")
    if not isinstance(node, str) or not manager.startswith("pnpm@"):
        raise WebBuildError("web build versions are not pinned")
    if (web / ".node-version").read_text(encoding="utf-8").strip().removeprefix("v") != node:
        raise WebBuildError("web Node identities disagree")
    return node.removeprefix("v"), manager.removeprefix("pnpm@")


def _dist(root: Path) -> Path:
    return root / "src/kindred/web/static"


def _version(command: str) -> str:
    try:
        result = subprocess.run([command, "--version"], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WebBuildError("required web build tool is unavailable") from exc
    return result.stdout.strip().removeprefix("v")


def verify_dist(root: Path) -> dict[str, Any]:
    web = root / "web"
    dist = _dist(root)
    manifest_path = dist / _MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WebBuildError("web dist manifest is unavailable") from exc
    expected_node, expected_pnpm = _expected_versions(web)
    expected = {
        "schema_version": 1,
        "node": expected_node,
        "pnpm": expected_pnpm,
        "input_sha256": _input_digest(web),
    }
    if manifest != expected:
        raise WebBuildError("web dist is stale or has an incompatible build identity")
    files = [path for path in dist.rglob("*") if path.is_file()]
    index_path = dist / "index.html"
    try:
        references = _ASSET_PATTERN.findall(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise WebBuildError("web dist is incomplete") from exc
    referenced_files = [dist / reference.removeprefix("/") for reference in references]
    if not references or any(
        not path.is_file() or path.stat().st_size == 0 for path in referenced_files
    ):
        raise WebBuildError("web dist is incomplete")
    if any(path.is_symlink() or path.suffix == ".map" for path in files):
        raise WebBuildError("web dist contains unsupported files")
    return {**expected, "files": len(files), "bytes": sum(path.stat().st_size for path in files)}


def build(root: Path) -> dict[str, Any]:
    web = root / "web"
    expected_node, expected_pnpm = _expected_versions(web)
    if _version("node") != expected_node or _version("pnpm") != expected_pnpm:
        raise WebBuildError("web build tool version does not match the pinned identity")
    subprocess.run(["pnpm", "--dir", str(web), "install", "--frozen-lockfile"], check=True)
    subprocess.run(["pnpm", "--dir", str(web), "build"], check=True)
    manifest = {
        "schema_version": 1,
        "node": expected_node,
        "pnpm": expected_pnpm,
        "input_sha256": _input_digest(web),
    }
    (_dist(root) / _MANIFEST).write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    return verify_dist(root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "check"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        result = build(root) if args.command == "build" else verify_dist(root)
    except (OSError, subprocess.CalledProcessError, WebBuildError):
        print(json.dumps({"status": "failed", "reason": "web_build_failed"}))
        return 2
    print(json.dumps({"status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
