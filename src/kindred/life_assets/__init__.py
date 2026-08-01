"""Kindred 随 wheel 发布的第一方生活资产。"""

from pathlib import Path

_PACKAGE_DIR = Path(__file__).parent

ACTIONS_DIR = _PACKAGE_DIR / "actions"
ACTIVITIES_DIR = _PACKAGE_DIR / "activities"

__all__ = ["ACTIONS_DIR", "ACTIVITIES_DIR"]
