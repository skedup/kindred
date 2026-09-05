#!/usr/bin/env python3
import argparse
import importlib.metadata as metadata
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


class InstallClosureError(RuntimeError):
    pass


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _installed_distributions() -> dict[str, str]:
    return {
        _canonical_name(dist.metadata["Name"]): dist.version
        for dist in metadata.distributions()
        if dist.metadata["Name"]
    }


def verify_install(manifest: dict[str, Any], platform: str, *, no_web: bool) -> int:
    try:
        wheels = manifest["platforms"][platform]["wheels"]
    except (KeyError, TypeError) as exc:
        raise InstallClosureError("release manifest is invalid") from exc
    if manifest.get("schema_version") != 1 or not isinstance(wheels, list):
        raise InstallClosureError("release manifest is invalid")
    expected = {
        _canonical_name(row["distribution"]): row["version"]
        for row in wheels
        if not no_web or row["group"] != "web"
    }
    installed = _installed_distributions()
    if any(installed.get(name) != version for name, version in expected.items()):
        raise InstallClosureError("installed distribution identity mismatch")
    if no_web and "fastapi" in installed:
        raise InstallClosureError("no-Web install contains FastAPI")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "check"], capture_output=True, check=False
    )
    if result.returncode != 0:
        raise InstallClosureError("pip dependency check failed")
    return len(expected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--no-web", action="store_true")
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        count = verify_install(manifest, args.platform, no_web=args.no_web)
    except (OSError, UnicodeError, json.JSONDecodeError, InstallClosureError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}))
        return 2
    print(json.dumps({"status": "ok", "installed_distributions": count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
