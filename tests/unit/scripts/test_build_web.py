"""Narrow checks for the fixed-input Web release build."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_module() -> Any:
    script = Path(__file__).resolve().parents[3] / "scripts" / "build_web.py"
    spec = importlib.util.spec_from_file_location("build_web", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _web_source(tmp_path: Path) -> Path:
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    values = {
        ".node-version": "22.18.0\n",
        "env.d.ts": "",
        "index.html": "<div id='app'></div>",
        "package.json": json.dumps(
            {"engines": {"node": "22.18.0"}, "packageManager": "pnpm@11.7.0"}
        ),
        "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
        "pnpm-workspace.yaml": "packages: []\n",
        "tsconfig.json": "{}",
        "tsconfig.node.json": "{}",
        "vite.config.ts": "export default {}",
    }
    for name, value in values.items():
        (web / name).write_text(value, encoding="utf-8")
    (web / "src/main.ts").write_text("export {}", encoding="utf-8")
    return web


def _write_dist(module: Any, root: Path) -> None:
    dist = root / "src/kindred/web/static"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<script src='/assets/app.js'></script>", encoding="utf-8")
    (dist / "assets/app.js").write_text("export {}", encoding="utf-8")
    node, pnpm = module._expected_versions(root / "web")
    (dist / "kindred-web-build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "node": node,
                "pnpm": pnpm,
                "input_sha256": module._input_digest(root / "web"),
            }
        ),
        encoding="utf-8",
    )


def test_verify_dist_detects_stale_source_and_source_maps(tmp_path: Path) -> None:
    module = _load_module()
    _web_source(tmp_path)
    _write_dist(module, tmp_path)
    assert module.verify_dist(tmp_path)["files"] == 3

    (tmp_path / "web/src/main.ts").write_text("export const changed = true", encoding="utf-8")
    with pytest.raises(module.WebBuildError, match="stale"):
        module.verify_dist(tmp_path)

    _write_dist(module, tmp_path)
    (tmp_path / "src/kindred/web/static/assets/app.js.map").write_text("{}", encoding="utf-8")
    with pytest.raises(module.WebBuildError, match="unsupported"):
        module.verify_dist(tmp_path)


def test_verify_dist_rejects_missing_or_empty_referenced_asset(tmp_path: Path) -> None:
    module = _load_module()
    _web_source(tmp_path)
    _write_dist(module, tmp_path)
    asset = tmp_path / "src/kindred/web/static/assets/app.js"

    asset.unlink()
    with pytest.raises(module.WebBuildError, match="incomplete"):
        module.verify_dist(tmp_path)

    asset.write_text("", encoding="utf-8")
    with pytest.raises(module.WebBuildError, match="incomplete"):
        module.verify_dist(tmp_path)


def test_build_uses_exact_tools_and_frozen_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    _web_source(tmp_path)
    calls: list[list[str]] = []
    versions = iter(("22.18.0", "11.7.0"))
    monkeypatch.setattr(module, "_version", lambda _command: next(versions))

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[-1] == "build":
            dist = tmp_path / "src/kindred/web/static"
            (dist / "assets").mkdir(parents=True)
            (dist / "index.html").write_text(
                "<script src='/assets/app.js'></script>", encoding="utf-8"
            )
            (dist / "assets/app.js").write_text("export {}", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.build(tmp_path)

    assert calls[0][-2:] == ["install", "--frozen-lockfile"]
    assert calls[1][-1] == "build"
    assert result["node"] == "22.18.0"
    assert result["pnpm"] == "11.7.0"
