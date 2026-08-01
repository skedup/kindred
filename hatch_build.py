"""Release build hook: reject missing or stale precompiled Web assets."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if version == "editable":
            return
        static_dir = Path(self.root) / "src/kindred/web/static"
        result = subprocess.run(
            [sys.executable, str(Path(self.root) / "scripts/build_web.py"), "check"]
        )
        if result.returncode != 0:
            raise RuntimeError("precompiled Web assets are missing or stale")
        if self.target_name == "wheel":
            force_include = build_data.get("force_include")
            if not isinstance(force_include, dict):
                raise RuntimeError("wheel build data does not support forced Web assets")
            force_include[str(static_dir)] = "kindred/web/static"
