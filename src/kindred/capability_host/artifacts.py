"""Host-owned directory artifacts for Portable capabilities."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from kindred_capability_sdk import ArtifactDescriptor

_REF_RE = re.compile(r"^artifact:([0-9a-f]{32})$")
_MANIFEST = "artifact.json"
_MAX_FILES = 24
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 24 * 1024 * 1024


class ArtifactStoreError(RuntimeError):
    """Artifact request failed without exposing content or private paths."""


@dataclass(frozen=True)
class StagedArtifact:
    """Host 私有的暂存句柄，不穿越 Portable SPI。"""

    stage_ref: str
    profile: str


@dataclass(frozen=True)
class CommittedArtifactMember:
    path: str
    bytes: int
    media_type: str
    available: bool


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._staging = root / ".staging"
        self._pending: dict[str, tuple[Path, ArtifactDescriptor]] = {}

    def writer(self, producer: str, profiles: frozenset[str]) -> ScopedArtifactWriter:
        return ScopedArtifactWriter(self, producer, profiles)

    def reader(self, profiles: frozenset[str]) -> ScopedArtifactReader:
        return ScopedArtifactReader(self, profiles)

    def commit(self, staged_artifact: StagedArtifact) -> ArtifactDescriptor:
        pending = self._pending.pop(staged_artifact.stage_ref, None)
        if pending is None or pending[1].profile != staged_artifact.profile:
            raise ArtifactStoreError("unknown staged artifact")
        staged, descriptor = pending
        try:
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(staged, self._bundle_dir(descriptor.artifact_ref))
        except OSError as exc:
            self._pending[staged_artifact.stage_ref] = pending
            raise ArtifactStoreError("artifact commit failed") from exc
        return descriptor

    def discard(self, staged_artifact: StagedArtifact) -> None:
        if (pending := self._pending.pop(staged_artifact.stage_ref, None)) is not None:
            shutil.rmtree(pending[0], ignore_errors=True)

    def describe(self, artifact_ref: str, profiles: frozenset[str]) -> ArtifactDescriptor:
        manifest = self._read_manifest(artifact_ref)
        profile = manifest.get("profile")
        producer = manifest.get("producer")
        if not isinstance(profile, str) or not isinstance(producer, str):
            raise ArtifactStoreError("artifact manifest is invalid")
        if profile not in profiles:
            raise ArtifactStoreError("artifact profile is not authorized")
        return ArtifactDescriptor(artifact_ref, producer, profile)

    def read(
        self,
        artifact_ref: str,
        relative_path: str,
        *,
        profiles: frozenset[str],
        size_limit: int,
    ) -> bytes:
        if not isinstance(size_limit, int) or isinstance(size_limit, bool) or size_limit <= 0:
            raise ArtifactStoreError("artifact read size limit is invalid")
        manifest = self._read_manifest(artifact_ref)
        profile = manifest.get("profile")
        if not isinstance(profile, str):
            raise ArtifactStoreError("artifact manifest is invalid")
        if profile not in profiles:
            raise ArtifactStoreError("artifact profile is not authorized")
        normalized = _relative_path(relative_path)
        members = manifest.get("members")
        if not isinstance(members, list) or normalized not in {
            item.get("path") for item in members if isinstance(item, dict)
        }:
            raise ArtifactStoreError("artifact member is not available")
        bundle = self._bundle_dir(artifact_ref)
        path = bundle
        try:
            for part in PurePosixPath(normalized).parts:
                path /= part
                if path.is_symlink():
                    raise ArtifactStoreError("artifact member is not a regular file")
            if not path.is_file():
                raise ArtifactStoreError("artifact member is not a regular file")
            if path.stat().st_size > size_limit:
                raise ArtifactStoreError("artifact member exceeds read limit")
            return path.read_bytes()
        except OSError as exc:
            raise ArtifactStoreError("artifact member read failed") from exc

    def inspect_committed(
        self,
        descriptor: ArtifactDescriptor,
    ) -> tuple[CommittedArtifactMember, ...]:
        manifest = self._read_descriptor_manifest(descriptor)
        raw_members = manifest.get("members")
        if not isinstance(raw_members, list) or not raw_members:
            raise ArtifactStoreError("artifact manifest is invalid")
        bundle = self._bundle_dir(descriptor.artifact_ref)
        seen: set[str] = set()
        members: list[CommittedArtifactMember] = []
        for raw in raw_members:
            if not isinstance(raw, dict):
                raise ArtifactStoreError("artifact manifest is invalid")
            path = _relative_path(raw.get("path"))
            size = raw.get("bytes")
            media_type = raw.get("media_type")
            if (
                path == _MANIFEST
                or path in seen
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(media_type, str)
                or not media_type
            ):
                raise ArtifactStoreError("artifact manifest is invalid")
            seen.add(path)
            members.append(
                CommittedArtifactMember(
                    path=path,
                    bytes=size,
                    media_type=media_type,
                    available=_member_matches(bundle, path, size),
                )
            )
        return tuple(sorted(members, key=lambda member: member.path))

    def read_committed(
        self,
        descriptor: ArtifactDescriptor,
        relative_path: str,
        *,
        size_limit: int,
    ) -> bytes:
        if not isinstance(size_limit, int) or isinstance(size_limit, bool) or size_limit <= 0:
            raise ArtifactStoreError("artifact read size limit is invalid")
        normalized = _relative_path(relative_path)
        members = {item.path: item for item in self.inspect_committed(descriptor)}
        member = members.get(normalized)
        if member is None or not member.available:
            raise ArtifactStoreError("artifact member is not available")
        if member.bytes > size_limit:
            raise ArtifactStoreError("artifact member exceeds read limit")
        path = self._bundle_dir(descriptor.artifact_ref) / PurePosixPath(normalized)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ArtifactStoreError("artifact member read failed") from exc
        if len(content) != member.bytes or len(content) > size_limit:
            raise ArtifactStoreError("artifact member changed during read")
        return content

    def _stage(
        self,
        producer: str,
        profile: str,
        files: Mapping[str, str | bytes],
    ) -> StagedArtifact:
        if profile.strip() != profile or not profile:
            raise ArtifactStoreError("artifact profile is invalid")
        if not isinstance(files, Mapping) or not files or len(files) > _MAX_FILES:
            raise ArtifactStoreError("artifact file count is invalid")

        normalized: dict[str, bytes] = {}
        total = 0
        for raw_path, raw_content in files.items():
            path = _relative_path(raw_path)
            if path == _MANIFEST:
                raise ArtifactStoreError("artifact manifest is reserved")
            if path in normalized:
                raise ArtifactStoreError("artifact contains duplicate paths")
            content = raw_content.encode() if isinstance(raw_content, str) else raw_content
            if not isinstance(content, bytes):
                raise ArtifactStoreError("artifact content must be text or bytes")
            if len(content) > _MAX_FILE_BYTES:
                raise ArtifactStoreError("artifact member is too large")
            total += len(content)
            if total > _MAX_TOTAL_BYTES:
                raise ArtifactStoreError("artifact bundle is too large")
            normalized[path] = content

        nonce = secrets.token_hex(16)
        artifact_ref = f"artifact:{nonce}"
        stage_ref = f"stage:{secrets.token_hex(16)}"
        descriptor = ArtifactDescriptor(artifact_ref, producer, profile)
        staged = self._staging / nonce
        try:
            staged.mkdir(parents=True, mode=0o700)
            members: list[dict[str, Any]] = []
            for relative, content in sorted(normalized.items()):
                destination = staged / PurePosixPath(relative)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _write_exclusive(destination, content)
                media_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
                members.append({"path": relative, "bytes": len(content), "media_type": media_type})
            _write_exclusive(
                staged / _MANIFEST,
                json.dumps(
                    {
                        "artifact_ref": artifact_ref,
                        "producer": producer,
                        "profile": profile,
                        "members": members,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode(),
            )
        except ArtifactStoreError:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(staged, ignore_errors=True)
            raise ArtifactStoreError("artifact staging failed") from exc
        self._pending[stage_ref] = (staged, descriptor)
        return StagedArtifact(stage_ref, profile)

    def _read_manifest(self, artifact_ref: str) -> dict[str, Any]:
        bundle = self._bundle_dir(artifact_ref)
        if bundle.is_symlink():
            raise ArtifactStoreError("artifact manifest is invalid")
        path = bundle / _MANIFEST
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactStoreError("artifact is not committed or manifest is invalid") from exc
        if not isinstance(raw, dict) or raw.get("artifact_ref") != artifact_ref:
            raise ArtifactStoreError("artifact manifest is invalid")
        return raw

    def _read_descriptor_manifest(self, descriptor: ArtifactDescriptor) -> dict[str, Any]:
        manifest = self._read_manifest(descriptor.artifact_ref)
        if (
            manifest.get("producer") != descriptor.producer
            or manifest.get("profile") != descriptor.profile
        ):
            raise ArtifactStoreError("artifact descriptor does not match manifest")
        return manifest

    def _bundle_dir(self, artifact_ref: str) -> Path:
        match = _REF_RE.fullmatch(artifact_ref) if isinstance(artifact_ref, str) else None
        if match is None:
            raise ArtifactStoreError("artifact ref is invalid")
        return self._root / match.group(1)


class ScopedArtifactWriter:
    def __init__(self, store: ArtifactStore, producer: str, profiles: frozenset[str]) -> None:
        self._store = store
        self._producer = producer
        self._profiles = profiles
        self._staged: StagedArtifact | None = None

    def stage_bundle(self, profile: str, files: Mapping[str, str | bytes]) -> None:
        if profile not in self._profiles:
            raise ArtifactStoreError("artifact profile is not authorized")
        if self._staged is not None:
            raise ArtifactStoreError("only one artifact bundle may be staged per invocation")
        self._staged = self._store._stage(self._producer, profile, files)

    def take_staged(self) -> tuple[StagedArtifact, ...]:
        if self._staged is None:
            return ()
        staged = self._staged
        self._staged = None
        return (staged,)

    def discard_staged(self) -> None:
        if self._staged is not None:
            self._store.discard(self._staged)
            self._staged = None


class ScopedArtifactReader:
    def __init__(self, store: ArtifactStore, profiles: frozenset[str]) -> None:
        self._store = store
        self._profiles = profiles

    def describe_committed(self, artifact_ref: str) -> ArtifactDescriptor:
        return self._store.describe(artifact_ref, self._profiles)

    def read_file(self, artifact_ref: str, relative_path: str, *, size_limit: int) -> bytes:
        return self._store.read(
            artifact_ref,
            relative_path,
            profiles=self._profiles,
            size_limit=size_limit,
        )


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ArtifactStoreError("artifact path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactStoreError("artifact path is invalid")
    if path.parts[0] == "assets" and len(path.parts) != 2:
        raise ArtifactStoreError("assets only accepts direct files")
    return path.as_posix()


def _write_exclusive(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)


def _member_matches(bundle: Path, relative_path: str, expected_size: int) -> bool:
    parts = PurePosixPath(relative_path).parts
    path = bundle.joinpath(*parts)
    try:
        linked = any(
            bundle.joinpath(*parts[:index]).is_symlink() for index in range(1, len(parts) + 1)
        )
        return not linked and path.is_file() and path.stat().st_size == expected_size
    except OSError:
        return False


__all__ = [
    "ArtifactStore",
    "ArtifactStoreError",
    "CommittedArtifactMember",
    "ScopedArtifactReader",
    "ScopedArtifactWriter",
    "StagedArtifact",
]
