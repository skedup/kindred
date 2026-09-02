"""Discover and compose package-owned Action, Activity, and Artifact resources."""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from kindred.activity.action import load_atomic_action
from kindred.activity.package import list_registered_packages
from kindred.activity.skill import load_activity_skill
from kindred.capability_host.discovery import _check_sdk
from kindred.capability_host.internal import ArtifactProfileRoute
from kindred.life_assets import ACTIONS_DIR, ACTIVITIES_DIR
from kindred_capability_sdk import is_safe_name

ENTRY_POINT_GROUP = "kindred.resources.v1"
MANIFEST_FILE = "kindred-resources.json"
RUNTIME_MANIFEST_FILE = "runtime-assets.json"


class PackageResourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class PackageResources:
    package_name: str
    distribution: str
    version: str
    root: Path
    actions_dir: Path | None
    activities_dir: Path | None
    artifact_routes: tuple[ArtifactProfileRoute, ...]


@dataclass(frozen=True)
class LifeAssetView:
    actions_dir: Path
    activities_dir: Path
    artifact_routes: tuple[ArtifactProfileRoute, ...]


def discover_package_resources() -> tuple[PackageResources, ...]:
    entries = sorted(metadata.entry_points().select(group=ENTRY_POINT_GROUP), key=lambda e: e.name)
    names = [entry.name for entry in entries]
    if len(names) != len(set(names)):
        raise PackageResourceError("duplicate package resource entry point")
    loaded: list[PackageResources] = []
    for entry in entries:
        if not is_safe_name(entry.name):
            raise PackageResourceError("invalid package resource entry point name")
        _check_sdk(entry)
        factory = entry.load()
        if not callable(factory):
            raise PackageResourceError("package resource entry point is not callable")
        root = factory()
        if not isinstance(root, Path):
            raise PackageResourceError("package resource factory must return pathlib.Path")
        distribution, version = _distribution_identity(entry)
        loaded.append(_load_resources(entry.name, distribution, version, root))
    return tuple(loaded)


def runtime_root_from_prefix() -> Path:
    return Path(sys.prefix).resolve().parent


def materialize_runtime_assets(
    runtime_root: Path | None = None,
    *,
    core_actions_dir: Path = ACTIONS_DIR,
    core_activities_dir: Path = ACTIVITIES_DIR,
    resources: tuple[PackageResources, ...] | None = None,
    kindred_version: str | None = None,
) -> LifeAssetView:
    root = (runtime_root or runtime_root_from_prefix()).resolve()
    if not root.is_dir() or root.is_symlink():
        raise PackageResourceError("runtime root is unavailable")
    loaded = discover_package_resources() if resources is None else resources
    version = kindred_version or _installed_kindred_version()
    staging = Path(tempfile.mkdtemp(prefix=".runtime-assets-", dir=root))
    target = root / "runtime-assets"
    backup = root / ".runtime-assets-previous"
    try:
        view = _compose_assets(
            staging,
            loaded,
            core_actions_dir=core_actions_dir,
            core_activities_dir=core_activities_dir,
        )
        sources = _source_records(
            loaded,
            version,
            core_actions_dir=core_actions_dir,
            core_activities_dir=core_activities_dir,
        )
        actions, activities = _validate_composed_assets(view)
        routes = sorted(
            (_route_payload(route) for route in view.artifact_routes),
            key=lambda row: (row["producer_capability"], row["selector_capability"]),
        )
        manifest = {
            "schema_version": 1,
            "kindred_version": version,
            "sources": sources,
            "actions": actions,
            "activities": activities,
            "artifact_routes": routes,
            "tree_sha256": _tree_digest(view),
        }
        (staging / RUNTIME_MANIFEST_FILE).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target.is_symlink() or backup.exists():
            raise PackageResourceError("runtime assets publish target is invalid")
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except BaseException:
            if backup.exists():
                os.replace(backup, target)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return load_runtime_life_assets(
        root,
        core_actions_dir=core_actions_dir,
        core_activities_dir=core_activities_dir,
        resources=loaded,
        kindred_version=version,
    )


def load_runtime_life_assets(
    runtime_root: Path | None = None,
    *,
    core_actions_dir: Path = ACTIONS_DIR,
    core_activities_dir: Path = ACTIVITIES_DIR,
    resources: tuple[PackageResources, ...] | None = None,
    kindred_version: str | None = None,
) -> LifeAssetView:
    root = (runtime_root or runtime_root_from_prefix()).resolve() / "runtime-assets"
    if not root.is_dir() or root.is_symlink():
        raise PackageResourceError("stable runtime assets are unavailable; rerun kindred install")
    _require_safe_tree(root)
    try:
        raw: Any = json.loads((root / RUNTIME_MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageResourceError("stable runtime assets manifest is unavailable") from exc
    required = {
        "schema_version",
        "kindred_version",
        "sources",
        "actions",
        "activities",
        "artifact_routes",
        "tree_sha256",
    }
    if not isinstance(raw, dict) or set(raw) != required or raw["schema_version"] != 1:
        raise PackageResourceError("stable runtime assets manifest shape is invalid")
    loaded = discover_package_resources() if resources is None else resources
    version = kindred_version or _installed_kindred_version()
    expected_sources = _source_records(
        loaded,
        version,
        core_actions_dir=core_actions_dir,
        core_activities_dir=core_activities_dir,
    )
    if raw["kindred_version"] != version or raw["sources"] != expected_sources:
        raise PackageResourceError("stable runtime assets do not match installed wheels")
    route_rows = raw["artifact_routes"]
    if not isinstance(route_rows, list):
        raise PackageResourceError("stable runtime assets manifest shape is invalid")
    routes = tuple(_parse_route(item) for item in route_rows)
    view = LifeAssetView(root / "actions", root / "activities", routes)
    actions, activities = _validate_composed_assets(view)
    if raw["actions"] != actions or raw["activities"] != activities:
        raise PackageResourceError("stable runtime assets package set is invalid")
    expected_digest = _tree_digest(view)
    if raw["tree_sha256"] != expected_digest:
        raise PackageResourceError("stable runtime assets digest mismatch")
    return view


def _distribution_identity(entry: Any) -> tuple[str, str]:
    dist = getattr(entry, "dist", None)
    if dist is None:
        raise PackageResourceError("package resource distribution identity is unavailable")
    try:
        name = str(dist.metadata["Name"])
        version = str(dist.version)
    except (AttributeError, KeyError, TypeError) as exc:
        raise PackageResourceError("package resource distribution identity is unavailable") from exc
    if not name or not version:
        raise PackageResourceError("package resource distribution identity is unavailable")
    return name, version


def _installed_kindred_version() -> str:
    try:
        return metadata.version("kindred")
    except metadata.PackageNotFoundError as exc:
        raise PackageResourceError("Kindred distribution identity is unavailable") from exc


def _load_resources(
    package_name: str,
    distribution: str,
    version: str,
    root: Path,
) -> PackageResources:
    root = root.resolve()
    manifest_path = root / MANIFEST_FILE
    try:
        raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageResourceError("package resource manifest is unavailable") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "artifact_routes"}:
        raise PackageResourceError("package resource manifest shape is invalid")
    if raw["schema_version"] != 1 or not isinstance(raw["artifact_routes"], list):
        raise PackageResourceError("package resource manifest version is invalid")
    routes = tuple(_parse_route(item) for item in raw["artifact_routes"])
    actions = _package_root(root, "actions", "Action")
    activities = _package_root(root, "activities", "Activity")
    return PackageResources(
        package_name,
        distribution,
        version,
        root,
        actions,
        activities,
        routes,
    )


def _package_root(root: Path, name: str, kind: str) -> Path | None:
    path = root / name
    if not path.exists():
        return None
    if not path.is_dir():
        raise PackageResourceError(f"{kind} resource root is invalid")
    _require_safe_tree(path)
    child_dirs = {child.name for child in path.iterdir() if child.is_dir()}
    registered = set(list_registered_packages(packages_dir=path))
    if child_dirs != registered:
        raise PackageResourceError(f"{kind} resource package is incomplete")
    return path


def _parse_route(raw: Any) -> ArtifactProfileRoute:
    required = {"producer_capability", "selector_capability", "profile"}
    optional = {"member_paths", "source_ref_kinds"}
    if not isinstance(raw, dict) or not required <= set(raw) or set(raw) - required - optional:
        raise PackageResourceError("artifact profile route shape is invalid")
    producer = raw["producer_capability"]
    selector = raw["selector_capability"]
    profile = raw["profile"]
    members = raw.get("member_paths", {})
    kinds = raw.get("source_ref_kinds", [])
    if not is_safe_name(producer) or not is_safe_name(selector):
        raise PackageResourceError("artifact profile route capability name is invalid")
    if not isinstance(profile, str) or not profile or profile.strip() != profile:
        raise PackageResourceError("artifact profile route profile is invalid")
    if not isinstance(members, dict) or any(
        not is_safe_name(key) or not _safe_member_path(value) for key, value in members.items()
    ):
        raise PackageResourceError("artifact profile route member path is invalid")
    if not isinstance(kinds, list) or any(not is_safe_name(kind) for kind in kinds):
        raise PackageResourceError("artifact profile route source ref kind is invalid")
    return ArtifactProfileRoute(
        producer,
        selector,
        profile.strip(),
        dict(members),
        frozenset(kinds),
    )


def _safe_member_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == value


def _copy_packages(roots: tuple[Path, ...], target: Path, kind: str) -> None:
    target.mkdir()
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            raise PackageResourceError(f"{kind} resource root is unavailable")
        _require_safe_tree(root)
        for name in list_registered_packages(packages_dir=root):
            if name in seen:
                raise PackageResourceError(f"duplicate {kind} package: {name}")
            seen.add(name)
            shutil.copytree(root / name, target / name)


def _compose_assets(
    root: Path,
    resources: tuple[PackageResources, ...],
    *,
    core_actions_dir: Path,
    core_activities_dir: Path,
) -> LifeAssetView:
    routes = tuple(route for item in resources for route in item.artifact_routes)
    selectors = [(route.producer_capability, route.selector_capability) for route in routes]
    if len(selectors) != len(set(selectors)):
        raise PackageResourceError("duplicate artifact profile route selector")
    actions_dir, activities_dir = root / "actions", root / "activities"
    _copy_packages(
        (core_actions_dir,)
        + tuple(item.actions_dir for item in resources if item.actions_dir is not None),
        actions_dir,
        "Action",
    )
    _copy_packages(
        (core_activities_dir,)
        + tuple(item.activities_dir for item in resources if item.activities_dir is not None),
        activities_dir,
        "Activity",
    )
    view = LifeAssetView(actions_dir, activities_dir, routes)
    _validate_composed_assets(view)
    return view


def _validate_composed_assets(view: LifeAssetView) -> tuple[list[str], list[str]]:
    actions = sorted(list_registered_packages(packages_dir=view.actions_dir))
    activities = sorted(list_registered_packages(packages_dir=view.activities_dir))
    if not actions or not activities:
        raise PackageResourceError("stable runtime assets cannot be empty")
    action_names = frozenset(actions)
    for name in actions:
        load_atomic_action(name, actions_dir=view.actions_dir)
    for name in activities:
        skill = load_activity_skill(
            name,
            activities_dir=view.activities_dir,
            actions_dir=view.actions_dir,
        )
        if {use.action for use in skill.uses} - action_names:
            raise PackageResourceError(f"Activity {name!r} references missing Action(s)")
    return actions, activities


def _source_records(
    resources: tuple[PackageResources, ...],
    kindred_version: str,
    *,
    core_actions_dir: Path,
    core_activities_dir: Path,
) -> list[dict[str, str]]:
    rows = [
        {
            "distribution": "kindred",
            "version": kindred_version,
            "resources_sha256": _resource_digest(
                (("actions", core_actions_dir), ("activities", core_activities_dir))
            ),
        }
    ]
    rows.extend(
        {
            "distribution": item.distribution,
            "version": item.version,
            "resources_sha256": _resource_digest(
                tuple(
                    (name, path)
                    for name, path in (
                        ("actions", item.actions_dir),
                        ("activities", item.activities_dir),
                        (MANIFEST_FILE, item.root / MANIFEST_FILE),
                    )
                    if path is not None
                )
            ),
        }
        for item in resources
    )
    rows.sort(key=lambda row: (row["distribution"], row["version"]))
    if len({row["distribution"] for row in rows}) != len(rows):
        raise PackageResourceError("duplicate package resource distribution")
    return rows


def _resource_digest(roots: tuple[tuple[str, Path], ...]) -> str:
    digest = hashlib.sha256()
    for prefix, root in roots:
        if root.is_symlink() or not root.exists():
            raise PackageResourceError("package resource tree is unavailable")
        paths = (root,) if root.is_file() else tuple(sorted(root.rglob("*")))
        for path in paths:
            if path.is_symlink():
                raise PackageResourceError("package resource tree contains symlink")
            if path.is_file():
                relative = path.name if root.is_file() else path.relative_to(root).as_posix()
                digest.update(f"{prefix}/{relative}".encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
    return digest.hexdigest()


def _route_payload(route: ArtifactProfileRoute) -> dict[str, Any]:
    return {
        "producer_capability": route.producer_capability,
        "selector_capability": route.selector_capability,
        "profile": route.profile,
        "member_paths": dict(sorted(route.member_paths.items())),
        "source_ref_kinds": sorted(route.source_ref_kinds),
    }


def _tree_digest(view: LifeAssetView) -> str:
    digest = hashlib.sha256()
    for prefix, root in (("actions", view.actions_dir), ("activities", view.activities_dir)):
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise PackageResourceError("stable runtime assets contain symlink")
            if path.is_file():
                digest.update(f"{prefix}/{path.relative_to(root).as_posix()}".encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
    routes = [_route_payload(route) for route in view.artifact_routes]
    routes.sort(key=lambda row: (row["producer_capability"], row["selector_capability"]))
    digest.update(json.dumps(routes, separators=(",", ":"), sort_keys=True).encode())
    return digest.hexdigest()


def _require_safe_tree(root: Path) -> None:
    if root.is_symlink():
        raise PackageResourceError("resource tree contains symlink")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PackageResourceError("resource tree contains symlink")


__all__ = [
    "ENTRY_POINT_GROUP",
    "LifeAssetView",
    "PackageResourceError",
    "PackageResources",
    "discover_package_resources",
    "load_runtime_life_assets",
    "materialize_runtime_assets",
    "runtime_root_from_prefix",
]
