"""Install-time entry point for the stable Action and Activity snapshot."""

from __future__ import annotations

import json

from kindred.capability_host.resources import PackageResourceError, materialize_runtime_assets


def main() -> int:
    try:
        view = materialize_runtime_assets()
    except (OSError, PackageResourceError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "actions_dir": view.actions_dir.name,
                "activities_dir": view.activities_dir.name,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
