"""The thin shell bootstrap only installs verified local release bytes."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

VERSION = "0.3.1"


def _command(path: Path, name: str, body: str) -> None:
    target = path / name
    target.write_text("#!/bin/sh\n" + body)
    target.chmod(0o755)


def _bundle(server: Path, platform: str, *, traversal: bool = False) -> None:
    stage = server / "stage"
    runtime = server / "runtime"
    python = runtime / "python/bin/python3"
    python.parent.mkdir(parents=True)
    python.write_text(
        """#!/bin/sh
if [ "$1" = -m ] && [ "$2" = venv ]; then
  rm -rf "$3"
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
  printf '#!/bin/sh\\nexit 0\\n' >"$3/bin/kindred"
  chmod +x "$3/bin/python" "$3/bin/kindred"
  exit 0
fi
case "$1" in
*/install.py)
  [ "$2" != verify ] || [ "${KINDRED_FAIL_VERIFY:-0}" != 1 ] || exit 2
  [ "$2" != install ] || {
  printf '%s' "${KINDRED_NO_WEB:-0}" >"$HOME/install-mode"
  }
  exit 0 ;;
esac
exit 2
"""
    )
    python.chmod(0o755)
    stage.mkdir()
    with tarfile.open(stage / "python-runtime.tar.gz", "w:gz") as archive:
        archive.add(runtime / "python", arcname="python")
    (stage / "wheelhouse").mkdir()
    name = f"kindred-v{VERSION}-{platform}.tar.gz"
    with tarfile.open(server / name, "w:gz") as archive:
        for item in stage.iterdir():
            archive.add(item, arcname=item.name)
        if traversal:
            member = tarfile.TarInfo("../escape")
            member.size = 0
            archive.addfile(member)
    bundle = server / name
    manifest = {
        "schema_version": 1,
        "release_version": VERSION,
        "platforms": {
            platform: {
                "bundle": {
                    "filename": name,
                    "size": bundle.stat().st_size,
                    "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                },
                "wheels": [],
            }
        },
    }
    (server / "manifest.json").write_text(json.dumps(manifest))
    assets = (server / "manifest.json", bundle)
    (server / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in assets
        )
    )


def _run(
    tmp_path: Path,
    platform: str,
    *,
    no_web: bool = False,
    traversal: bool = False,
    installed_version: str | None = None,
    fail_verify: bool = False,
) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    server = tmp_path / "server"
    commands = tmp_path / "commands"
    home.mkdir(parents=True, exist_ok=True)
    server.mkdir(exist_ok=True)
    commands.mkdir(exist_ok=True)
    if not (server / "manifest.json").exists():
        _bundle(server, platform, traversal=traversal)
    if platform == "macos-arm64":
        _command(commands, "uname", '[ "$1" = -s ] && echo Darwin || echo arm64\n')
        _command(commands, "sw_vers", "echo 14.6.1\n")
    else:
        _command(commands, "uname", '[ "$1" = -s ] && echo Linux || echo x86_64\n')
    release_file = tmp_path / "os-release"
    release_file.write_text("ID=ubuntu\nVERSION_ID=24.04\n")
    if installed_version:
        marker = home / ".local/share/kindred/runtime/existing/.kindred-release-version"
        marker.parent.mkdir(parents=True)
        marker.write_text(installed_version + "\n")
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{commands}:/usr/bin:/bin:/usr/sbin:/sbin",
        "KINDRED_RELEASE_BASE_URL": server.as_uri(),
        "KINDRED_OS_RELEASE_FILE": str(release_file),
        "KINDRED_NO_WEB": "1" if no_web else "0",
        "KINDRED_FAIL_VERIFY": "1" if fail_verify else "0",
    }
    return subprocess.run(
        ["sh", str(root / "scripts/install.sh")],
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
    )


@pytest.mark.parametrize("platform", ["macos-arm64", "ubuntu24-x86_64"])
@pytest.mark.parametrize("no_web", [False, True])
def test_fake_offline_install_selects_platform_and_web_mode(
    tmp_path: Path, platform: str, no_web: bool
) -> None:
    result = _run(tmp_path, platform, no_web=no_web)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home/install-mode").read_text() == ("1" if no_web else "0")
    marker = tmp_path / f"home/.local/share/kindred/runtime/{VERSION}/.kindred-release-version"
    assert marker.read_text().strip() == VERSION
    assert "Continue in a terminal" in result.stdout


def test_same_version_repairs_but_different_version_refuses_before_download(tmp_path: Path) -> None:
    same = tmp_path / "same"
    assert _run(same, "macos-arm64").returncode == 0
    kindred = same / f"home/.local/share/kindred/runtime/{VERSION}/venv/bin/kindred"
    kindred.write_text("healthy\n")
    failed = _run(same, "macos-arm64", fail_verify=True)
    assert failed.returncode == 2
    assert kindred.read_text() == "healthy\n"
    assert _run(same, "macos-arm64").returncode == 0
    assert kindred.read_text() != "healthy\n"
    different = _run(tmp_path / "different", "macos-arm64", installed_version="9.9.9")
    assert different.returncode == 2
    assert "different Kindred version" in different.stderr
    unexpected = tmp_path / "different" / "home" / f".local/share/kindred/runtime/{VERSION}"
    assert not unexpected.exists()


def test_bootstrap_defaults_to_the_versioned_github_release() -> None:
    source = (Path(__file__).resolve().parents[2] / "scripts/install.sh").read_text()

    assert (
        "BASE_URL=${KINDRED_RELEASE_BASE_URL:-"
        "https://github.com/skedup/kindred/releases/download/v$VERSION}"
    ) in source


def test_bootstrap_uses_public_manifest_as_the_only_install_authority() -> None:
    source = (Path(__file__).resolve().parents[2] / "scripts/install.sh").read_text()

    assert "install-metadata" not in source
    assert 'selected = manifest["platforms"][platform]' in source


def test_bootstrap_is_host_neutral_and_defers_discovery_to_python_installer() -> None:
    source = (Path(__file__).resolve().parents[2] / "scripts/install.sh").read_text()

    assert "openclaw --version" not in source
    assert "hermes --version" not in source
    assert 'exec "$BIN_DIR/kindred" install' in source


def test_hash_mismatch_and_archive_traversal_fail_closed(tmp_path: Path) -> None:
    traversal = _run(tmp_path / "traversal", "macos-arm64", traversal=True)
    assert traversal.returncode == 2
    assert "unsafe path" in traversal.stderr
    assert not (tmp_path / "traversal/escape").exists()

    server_case = tmp_path / "hash"
    home = server_case / "home"
    server = server_case / "server"
    commands = server_case / "commands"
    home.mkdir(parents=True)
    server.mkdir()
    commands.mkdir()
    _bundle(server, "macos-arm64")
    bundle = server / f"kindred-v{VERSION}-macos-arm64.tar.gz"
    bundle.write_bytes(bundle.read_bytes() + b"changed")
    _command(commands, "uname", '[ "$1" = -s ] && echo Darwin || echo arm64\n')
    _command(commands, "sw_vers", "echo 14.6.1\n")
    result = subprocess.run(
        ["sh", str(Path(__file__).resolve().parents[2] / "scripts/install.sh")],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{commands}:/usr/bin:/bin:/usr/sbin:/sbin",
            "KINDRED_RELEASE_BASE_URL": server.as_uri(),
        },
    )
    assert result.returncode == 2
    assert "checksum mismatch" in result.stderr
