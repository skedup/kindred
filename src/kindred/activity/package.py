"""Activity/action package filesystem contract.

M4.6 splits package files into:

- ``manifest.yaml``: machine contract read by Python loaders.
- ``SKILL.md``: skill narrative read by the heart prompt.

Legacy YAML frontmatter in ``SKILL.md`` is intentionally not supported at
runtime.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from kindred.state._types import SAFE_NAME_PATTERN

MANIFEST_FILE = "manifest.yaml"
SKILL_FILE = "SKILL.md"
_LOG = logging.getLogger(__name__)


class ActivityPackageError(RuntimeError):
    """Package loading error for manifest/body filesystem issues."""


class ActivityPackageParts(NamedTuple):
    """Parsed package files plus their source paths."""

    manifest: dict[str, Any]
    skill_body: str
    manifest_path: Path
    skill_path: Path


def is_valid_package_name(name: str) -> bool:
    """Return whether a package directory name is safe and registered-form."""

    return bool(SAFE_NAME_PATTERN.match(name))


def list_registered_packages(*, packages_dir: Path) -> list[str]:
    """List package dirs that have both ``manifest.yaml`` and ``SKILL.md``."""

    base = Path(packages_dir)
    if not base.is_dir():
        return []
    names: list[str] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        if not is_valid_package_name(child.name):
            continue
        manifest_path = child / MANIFEST_FILE
        skill_path = child / SKILL_FILE
        if manifest_path.is_file() and skill_path.is_file():
            names.append(child.name)
        elif skill_path.is_file() and not manifest_path.is_file():
            _LOG.warning(
                "%s 看起来仍是旧 SKILL.md frontmatter activity/action package；"
                "已跳过注册，请改为 manifest.yaml + SKILL.md 目录格式。",
                child,
            )
    return sorted(names)


def load_package_parts(*, packages_dir: Path, name: str, package_kind: str) -> ActivityPackageParts:
    """Read one package and parse its manifest.

    ``package_kind`` is used only for human-facing errors ("activity"/"action").
    It deliberately stays free-form to avoid an enum just for diagnostics.
    """

    if not is_valid_package_name(name):
        raise ActivityPackageError(
            f"非法 {package_kind} 名 {name!r}（须匹配 ^[a-z][a-z0-9_]*$，拒 / 与 ..）"
        )
    base = Path(packages_dir).resolve()
    package_dir = base / name
    manifest_path = package_dir / MANIFEST_FILE
    skill_path = package_dir / SKILL_FILE
    try:
        package_dir.resolve().relative_to(base)
        manifest_path.resolve().relative_to(base)
        skill_path.resolve().relative_to(base)
    except ValueError as exc:
        raise ActivityPackageError(f"{package_kind} 路径越界：{name!r}") from exc

    if not manifest_path.is_file():
        raise ActivityPackageError(f"找不到 {package_kind} manifest：{manifest_path}")
    if not skill_path.is_file():
        raise ActivityPackageError(f"找不到 {package_kind} SKILL：{skill_path}")

    skill_body = skill_path.read_text(encoding="utf-8").strip()
    if skill_body.startswith("---"):
        raise ActivityPackageError(
            f"{skill_path}: SKILL.md 不再支持 YAML frontmatter；机器字段必须放入 manifest.yaml"
        )

    try:
        loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ActivityPackageError(f"{manifest_path}: YAML 解析失败：{exc}") from exc
    if not isinstance(loaded, dict):
        raise ActivityPackageError(f"{manifest_path}: manifest 不是 YAML 映射")
    return ActivityPackageParts(
        manifest=loaded,
        skill_body=skill_body,
        manifest_path=manifest_path,
        skill_path=skill_path,
    )
