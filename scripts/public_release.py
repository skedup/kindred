#!/usr/bin/env python3
"""Build and inspect the positive-allowlist public source root."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import struct
import subprocess
import tarfile
import zipfile
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_MAX_DEPTH = 4
_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MAX_MEMBERS = 50_000
_MAX_ARCHIVES = 128
_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
_COMPRESSED_MAGIC = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"Rar!\x1a\x07", b"7z\xbc\xaf'\x1c")
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_CREDENTIAL_REFERENCES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "BAIDU_MAP_AK",
        "BAIDU_MAP_SK",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "KINDRED_GATEWAY_TOKEN",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
    }
)
_CREDENTIAL_PATTERN = re.compile(
    r"""(?x)
    (?:
      (?i:["']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|cookie|password|secret|
      baidu[_-]?map[_-]?(?:ak|sk)|gemini[_-]?api[_-]?key|google[_-]?api[_-]?key|
      anthropic[_-]?api[_-]?key|deepseek[_-]?api[_-]?key|openai[_-]?api[_-]?key|
      xai[_-]?api[_-]?key)["']?)
      \s*[:=]\s*
      (?:"(?P<double>[A-Za-z0-9_./+=~-]{12,})"|'(?P<single>[A-Za-z0-9_./+=~-]{12,})'|
      (?P<bare>[A-Za-z0-9_./+=~${}<>-]{12,}))
      |(?i:\bBearer)\s+(?P<bearer>[A-Za-z0-9._~+/=${}<>-]{12,})
      |(?P<private>BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY)
    )
    """
)
_RULES = {
    "private_platform": re.compile(r"(?i)(xiaohongshu|小红书|\bxhs(?:\b|[_-]))"),
    "internal_domain": re.compile(r"(?i)\b[a-z0-9.-]+\.woa\.com\b"),
    "internal_email": re.compile(
        r"(?i)\b[a-z0-9._%+-]+@(?:[a-z0-9.-]+\.)?(?:woa\.com|tencent\.com)\b"
    ),
    "internal_review": re.compile(
        r"(?i)(?:\bMR\s*!?\s*\d+\b|\bMR-[A-Z0-9][A-Z0-9._-]*|"
        r"\bPR-[A-Z0-9][A-Z0-9._-]*|(?<![A-Za-z0-9])![1-9]\d+\b|merge_requests/\d+)"
    ),
    "private_path": re.compile(
        r"(?:/Users/[^/\s]+|/home/[^/\s]+|/root(?:/|\b)|/private/(?:tmp|var)(?:/|\b)|"
        r"/(?:projects|builds)/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)"
    ),
    "private_network": re.compile(
        r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])"
        r"(?:\.\d{1,3}){2}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2})\b"
    ),
    "credential": _CREDENTIAL_PATTERN,
    "openclaw_identity": re.compile(
        r"(?i)\bagent:[^:\s]+:(?:wecom|telegram|discord|whatsapp):direct:[A-Za-z0-9_-]{6,}"
    ),
}


class PublicReleaseError(RuntimeError):
    def __init__(self, reason_code: str, findings: Sequence[Finding] = ()) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.findings = tuple(findings)


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    rule_id: str
    reason_code: str


@dataclass
class _ScanBudget:
    total_bytes: int = 0
    members: int = 0
    archives: int = 0
    exhausted: bool = False

    def consume(self, size: int, *, member: bool = False, archive: bool = False) -> bool:
        if self.exhausted:
            return False
        self.total_bytes += size
        self.members += int(member)
        self.archives += int(archive)
        self.exhausted = not (
            self.total_bytes <= _MAX_TOTAL_BYTES
            and self.members <= _MAX_MEMBERS
            and self.archives <= _MAX_ARCHIVES
        )
        return not self.exhausted

    def stop(self) -> None:
        self.exhausted = True


@dataclass(frozen=True)
class _Exception:
    rule_ids: frozenset[str]
    sha256: str


@dataclass(frozen=True)
class _GitBlob:
    mode: str
    object_id: str
    size: int


@dataclass(frozen=True)
class _ZipExtraSecurity:
    paths: tuple[str, ...] = ()
    modes: tuple[int, ...] = ()
    fields: tuple[tuple[int, bytes], ...] = ()
    valid: bool = True


def _decode_json(data: bytes, name: str) -> Mapping[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicReleaseError(f"invalid_policy_{name}") from exc
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        raise PublicReleaseError(f"unsupported_policy_{name}")
    return value


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        return _decode_json(path.read_bytes(), path.name)
    except OSError as exc:
        raise PublicReleaseError("policy_read_failed") from exc


def _git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PublicReleaseError("git_read_failed") from exc
    return result.stdout


def _git_commit(root: Path) -> str:
    try:
        commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise PublicReleaseError("git_commit_invalid") from exc
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise PublicReleaseError("git_commit_invalid")
    return commit


def _git_tree(root: Path, commit: str) -> dict[str, _GitBlob]:
    tree: dict[str, _GitBlob] = {}
    for item in _git(root, "ls-tree", "-lrz", "--full-tree", commit).split(b"\0"):
        if not item:
            continue
        try:
            metadata, raw_path = item.split(b"\t", 1)
            mode, kind, object_id, raw_size = metadata.decode("ascii").split()
            rel = raw_path.decode("utf-8")
            size = int(raw_size)
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicReleaseError("git_tree_invalid") from exc
        if kind == "blob":
            tree[rel] = _GitBlob(mode=mode, object_id=object_id, size=size)
    return tree


def _git_blob(root: Path, blob: _GitBlob) -> bytes:
    data = _git(root, "cat-file", "blob", blob.object_id)
    if len(data) != blob.size:
        raise PublicReleaseError("git_blob_size_mismatch")
    return data


def _committed_blob(
    root: Path,
    tree: Mapping[str, _GitBlob],
    path: Path,
) -> tuple[str, _GitBlob]:
    try:
        candidate = path if path.is_absolute() else root / path
        rel = Path(os.path.abspath(candidate)).relative_to(Path(os.path.abspath(root))).as_posix()
    except ValueError as exc:
        raise PublicReleaseError("policy_outside_repository") from exc
    blob = tree.get(rel)
    if blob is None:
        raise PublicReleaseError("policy_not_committed")
    return rel, blob


def _matches(rel: str, paths: Sequence[str], trees: Sequence[str]) -> bool:
    return rel in paths or any(rel.startswith(f"{tree.rstrip('/')}/") for tree in trees)


def select_public_files(
    tracked: Mapping[str, _GitBlob],
    policy: Mapping[str, Any],
) -> tuple[str, ...]:
    paths = tuple(map(str, policy.get("paths", ())))
    trees = tuple(str(item).rstrip("/") for item in policy.get("trees", ()))
    selected = tuple(sorted(rel for rel in tracked if _matches(rel, paths, trees)))
    if any(item not in tracked for item in paths) or any(
        not any(rel.startswith(f"{tree}/") for rel in tracked) for tree in trees
    ):
        raise PublicReleaseError("allowlist_missing_tracked_path")
    if any(tracked[rel].mode not in {"100644", "100755"} for rel in selected):
        raise PublicReleaseError("allowlist_unsafe_git_mode")
    return selected


def validate_provenance(files: Sequence[str], ledger: Mapping[str, Any]) -> None:
    groups = ledger.get("groups")
    if not isinstance(groups, list):
        raise PublicReleaseError("provenance_groups_missing")
    required = ("source", "original_license", "rights_basis", "public_license", "attribution")
    for rel in files:
        matched = [
            group
            for group in groups
            if isinstance(group, Mapping)
            and _matches(
                rel,
                tuple(map(str, group.get("paths", ()))),
                tuple(map(str, group.get("trees", ()))),
            )
        ]
        if len(matched) != 1:
            raise PublicReleaseError("provenance_owner_invalid")
        group = matched[0]
        if (
            group.get("source_status") != "known"
            or group.get("rights_status") != "cleared"
            or group.get("public_decision") != "approved"
            or any(not group.get(key) for key in required)
        ):
            raise PublicReleaseError("provenance_not_cleared")


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and "\\" not in name and ".." not in path.parts


def _safe_tar_link(info: tarfile.TarInfo) -> bool:
    base = posixpath.dirname(info.name) if info.issym() else ""
    return _safe_member(posixpath.normpath(posixpath.join(base, info.linkname)))


def _is_credential_placeholder(value: str) -> bool:
    if re.fullmatch(r"\$(?:[A-Z][A-Z0-9_]*|\{[A-Z][A-Z0-9_]*\})", value):
        return True
    if re.fullmatch(r"\{?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\}?", value):
        return True
    if value in _CREDENTIAL_REFERENCES:
        return True
    normalized = value.lower().replace("-", "_")
    if normalized in {"changeme", "placeholder", "redacted", "your_api_key_here"}:
        return True
    return bool(
        re.fullmatch(
            r"(?:example|sample|dummy|your|replace(?:_with)?)[_a-z0-9]*"
            r"(?:key|token|secret|password|value|here)[_a-z0-9]*",
            normalized,
        )
    )


def _credential_present(text: str) -> bool:
    for match in _CREDENTIAL_PATTERN.finditer(text):
        if match.group("private") is not None:
            return True
        value = next(
            (
                match.group(name)
                for name in ("double", "single", "bare", "bearer")
                if match.group(name)
            ),
            "",
        )
        if (
            match.group("bare") is not None
            and re.fullmatch(r"_[A-Za-z][A-Za-z0-9_]*", value)
            and text[match.end() :].startswith("(")
        ):
            continue
        if not _is_credential_placeholder(value):
            return True
    return False


def _zip_extra_security(extra: bytes, filename: str, flag_bits: int) -> _ZipExtraSecurity:
    paths: list[str] = []
    modes: list[int] = []
    fields: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(extra):
        if len(extra) - offset < 4:
            return _ZipExtraSecurity(valid=False)
        field_id, size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        if size > len(extra) - offset:
            return _ZipExtraSecurity(valid=False)
        payload = extra[offset : offset + size]
        offset += size
        if field_id == 0x7075:
            if len(payload) < 5 or payload[0] != 1:
                return _ZipExtraSecurity(valid=False)
            encoding = "utf-8" if flag_bits & 0x800 else "cp437"
            try:
                raw_name = filename.encode(encoding)
                alternate = payload[5:].decode("utf-8")
            except UnicodeError:
                return _ZipExtraSecurity(valid=False)
            expected_crc = struct.unpack_from("<I", payload, 1)[0]
            if zlib.crc32(raw_name) != expected_crc:
                return _ZipExtraSecurity(valid=False)
            paths.append(alternate)
            fields.append((field_id, payload))
        elif field_id == 0x756E:
            if len(payload) < 10:
                return _ZipExtraSecurity(valid=False)
            expected_crc = struct.unpack_from("<I", payload)[0]
            if zlib.crc32(payload[4:]) != expected_crc:
                return _ZipExtraSecurity(valid=False)
            modes.append(struct.unpack_from("<H", payload, 4)[0])
            fields.append((field_id, payload))
    return _ZipExtraSecurity(paths=tuple(paths), modes=tuple(modes), fields=tuple(fields))


def _zip_local_metadata(data: bytes, info: Any) -> tuple[str, bytes] | None:
    offset = info.header_offset
    if offset < 0 or offset + 30 > len(data) or data[offset : offset + 4] != b"PK\x03\x04":
        return None
    flag_bits, compression, name_size, extra_size = struct.unpack_from("<HH16xHH", data, offset + 6)
    start = offset + 30
    end = start + name_size + extra_size
    if end > len(data) or flag_bits != info.flag_bits or compression != info.compress_type:
        return None
    encoding = "utf-8" if flag_bits & 0x800 else "cp437"
    try:
        filename = data[start : start + name_size].decode(encoding)
    except UnicodeError:
        return None
    return filename, data[start + name_size : end]


def _archive_kind(name: str, data: bytes) -> str | None:
    lower = name.lower()
    if lower.endswith((".zip", ".whl")):
        return "zip"
    if lower.endswith(_TAR_SUFFIXES):
        return "tar"
    known_archive = (
        data.startswith(_ZIP_MAGIC + _COMPRESSED_MAGIC)
        or (len(data) >= 262 and data[257:262] == b"ustar")
        or zipfile.is_zipfile(io.BytesIO(data))
    )
    return "unknown" if known_archive else None


def _exceptions(policy: Mapping[str, Any]) -> dict[str, _Exception]:
    result: dict[str, _Exception] = {}
    for item in policy.get("redaction_exceptions", ()):
        if not isinstance(item, Mapping):
            raise PublicReleaseError("redaction_exception_invalid")
        path = str(item.get("path", ""))
        digest = str(item.get("sha256", ""))
        rules = frozenset(map(str, item.get("rule_ids", ())))
        if (
            not path
            or path in result
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not rules
            or not rules <= _RULES.keys()
        ):
            raise PublicReleaseError("redaction_exception_invalid")
        result[path] = _Exception(rules, digest)
    for digest in policy.get("trusted_archive_sha256", ()):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PublicReleaseError("trusted_archive_invalid")
        result[f"sha256:{digest}"] = _Exception(frozenset(_RULES), digest)
    return result


def _text(data: bytes) -> str:
    parts = [data.decode("utf-8", errors="ignore")]
    if b"\0" in data:
        parts.extend(
            (
                data.decode("utf-16-le", errors="ignore"),
                data.decode("utf-16-be", errors="ignore"),
            )
        )
    return "\n".join(parts)


def _content_findings(data: bytes, label: str, skipped: frozenset[str]) -> list[Finding]:
    text = _text(data)
    return [
        Finding(label, rule_id, "sensitive_content")
        for rule_id, pattern in _RULES.items()
        if rule_id not in skipped
        and (_credential_present(text) if rule_id == "credential" else pattern.search(text))
    ]


def _skipped_rules(
    data: bytes,
    label: str,
    exceptions: Mapping[str, _Exception],
) -> frozenset[str]:
    exception = exceptions.get(label)
    if exception is None:
        return frozenset()
    digest = hashlib.sha256(data).hexdigest()
    return exception.rule_ids if digest == exception.sha256 else frozenset()


def _scan_bytes(
    data: bytes,
    label: str,
    exceptions: Mapping[str, _Exception],
    budget: _ScanBudget,
    *,
    depth: int = 0,
    name_hint: str | None = None,
) -> list[Finding]:
    if budget.exhausted:
        return [Finding(label, "archive_limit", "archive_limit_exceeded")]
    if len(data) > _MAX_FILE_BYTES or depth > _MAX_DEPTH:
        budget.stop()
        return [Finding(label, "archive_limit", "archive_limit_exceeded")]
    if not budget.consume(len(data)):
        return [Finding(label, "archive_limit", "archive_limit_exceeded")]
    kind = _archive_kind(name_hint or label, data)
    if kind == "unknown":
        return [Finding(label, "archive_type", "unknown_archive")]
    if kind:
        if not budget.consume(0, archive=True):
            return [Finding(label, "archive_limit", "archive_limit_exceeded")]
        try:
            trusted = f"sha256:{hashlib.sha256(data).hexdigest()}" in exceptions
            return _scan_archive(data, kind, label, exceptions, budget, depth, trusted=trusted)
        except (OSError, tarfile.TarError, zipfile.BadZipFile, RuntimeError):
            return [Finding(label, "archive_format", "invalid_archive")]
    if label.lower().endswith(".map"):
        return [Finding(label, "source_map", "source_map_not_allowed")]
    return _content_findings(data, label, _skipped_rules(data, label, exceptions))


def _scan_archive(
    data: bytes,
    kind: str,
    label: str,
    exceptions: Mapping[str, _Exception],
    budget: _ScanBudget,
    depth: int,
    *,
    trusted: bool = False,
) -> list[Finding]:
    findings: list[Finding] = []
    skipped = frozenset(_RULES) if trusted else frozenset()
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            findings.extend(_content_findings(archive.comment, f"{label}!metadata", skipped))
            members: Iterable[Any] = archive.infolist()
            for index, info in enumerate(members):
                member = f"{label}!member[{index}]"
                if not budget.consume(0, member=True):
                    findings.append(Finding(member, "archive_limit", "archive_limit_exceeded"))
                    break
                findings.extend(
                    _content_findings(
                        f"{info.filename}\n".encode() + info.comment + info.extra,
                        member,
                        skipped,
                    )
                )
                local = _zip_local_metadata(data, info)
                if local is None:
                    findings.append(Finding(member, "archive_path", "unsafe_archive_member"))
                    continue
                local_name, local_extra = local
                findings.extend(
                    _content_findings(
                        f"{local_name}\n".encode() + local_extra,
                        member,
                        skipped,
                    )
                )
                extra_security = _zip_extra_security(info.extra, info.filename, info.flag_bits)
                local_security = _zip_extra_security(local_extra, local_name, info.flag_bits)
                member_type = stat.S_IFMT(info.external_attr >> 16)
                extra_types = {stat.S_IFMT(mode) for mode in extra_security.modes}
                if (
                    not extra_security.valid
                    or not local_security.valid
                    or local_name != info.filename
                    or local_security != extra_security
                    or not _safe_member(info.filename)
                    or any(not _safe_member(path) for path in extra_security.paths)
                    or member_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or any(value not in {0, stat.S_IFREG, stat.S_IFDIR} for value in extra_types)
                ):
                    findings.append(Finding(member, "archive_path", "unsafe_archive_member"))
                elif (
                    info.file_size > _MAX_FILE_BYTES
                    or budget.total_bytes + info.file_size > _MAX_TOTAL_BYTES
                ):
                    budget.stop()
                    findings.append(Finding(member, "archive_limit", "archive_limit_exceeded"))
                    break
                elif trusted:
                    if not budget.consume(info.file_size):
                        findings.append(Finding(member, "archive_limit", "archive_limit_exceeded"))
                        break
                elif not info.is_dir():
                    with archive.open(info) as stream:
                        payload = stream.read(_MAX_FILE_BYTES + 1)
                    findings.extend(
                        _scan_bytes(
                            payload,
                            member,
                            exceptions,
                            budget,
                            depth=depth + 1,
                            name_hint=info.filename,
                        )
                    )
                    if budget.exhausted:
                        break
        return findings
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        findings.extend(
            _content_findings(
                json.dumps(archive.pax_headers, sort_keys=True).encode(),
                f"{label}!metadata",
                skipped,
            )
        )
        for index, info in enumerate(archive):
            member = f"{label}!member[{index}]"
            if not budget.consume(0, member=True):
                findings.append(Finding(member, "archive_limit", "archive_limit_exceeded"))
                break
            metadata = "\n".join(
                (
                    info.name,
                    info.linkname,
                    info.uname,
                    info.gname,
                    json.dumps(info.pax_headers, sort_keys=True),
                )
            ).encode()
            findings.extend(_content_findings(metadata, member, skipped))
            supported = (
                info.isfile()
                or info.isdir()
                or (trusted and (info.issym() or info.islnk()) and _safe_tar_link(info))
            )
            if not _safe_member(info.name) or not supported:
                findings.append(Finding(member, "archive_path", "unsafe_archive_member"))
            elif info.size > _MAX_FILE_BYTES or budget.total_bytes + info.size > _MAX_TOTAL_BYTES:
                budget.stop()
                findings.append(Finding(member, "archive_limit", "archive_limit_exceeded"))
                break
            elif trusted:
                if not budget.consume(info.size):
                    findings.append(Finding(member, "archive_limit", "archive_limit_exceeded"))
                    break
            elif info.isfile() and (stream := archive.extractfile(info)) is not None:
                payload = stream.read(_MAX_FILE_BYTES + 1)
                findings.extend(
                    _scan_bytes(
                        payload,
                        member,
                        exceptions,
                        budget,
                        depth=depth + 1,
                        name_hint=info.name,
                    )
                )
                if budget.exhausted:
                    break
    return findings


def _scan_snapshots(
    snapshots: Mapping[str, bytes],
    policy: Mapping[str, Any],
) -> tuple[Finding, ...]:
    exceptions = _exceptions(policy)
    budget = _ScanBudget()
    findings: list[Finding] = []
    for rel, data in sorted(snapshots.items()):
        findings.extend(_scan_bytes(data, rel, exceptions, budget))
        if budget.exhausted:
            break
    return tuple(sorted(set(findings)))


def _stat_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_scan_file(
    path: Path,
    label: str,
    expected: os.stat_result,
) -> bytes | Finding:
    if not hasattr(os, "O_NOFOLLOW"):
        raise PublicReleaseError("nofollow_unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise PublicReleaseError("scan_input_missing") from exc
    except OSError as exc:
        raise PublicReleaseError("scan_input_unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PublicReleaseError("scan_input_unsupported")
        if _stat_signature(before) != _stat_signature(expected):
            raise PublicReleaseError("scan_input_changed")
        if before.st_size > _MAX_FILE_BYTES:
            return Finding(label, "archive_limit", "archive_limit_exceeded")
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(data) != before.st_size or _stat_signature(after) != _stat_signature(before):
            raise PublicReleaseError("scan_input_changed")
        return data
    except OSError as exc:
        raise PublicReleaseError("scan_input_unreadable") from exc
    finally:
        os.close(descriptor)


def _size_findings(entries: Iterable[tuple[str, int]]) -> tuple[Finding, ...]:
    total = 0
    findings: list[Finding] = []
    for label, size in entries:
        total += size
        if size > _MAX_FILE_BYTES:
            findings.append(Finding(label, "archive_limit", "archive_limit_exceeded"))
        if total > _MAX_TOTAL_BYTES:
            findings.append(Finding(label, "archive_limit", "archive_limit_exceeded"))
            break
    return tuple(sorted(set(findings)))


def scan_files(root: Path, files: Iterable[str], policy: Mapping[str, Any]) -> tuple[Finding, ...]:
    readable: list[tuple[str, Path, os.stat_result]] = []
    findings: list[Finding] = []
    for rel in sorted(files):
        path = root / rel
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise PublicReleaseError("scan_input_missing") from exc
        except OSError as exc:
            raise PublicReleaseError("scan_input_unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            findings.append(Finding(rel, "filesystem_path", "symlink_not_allowed"))
        elif not stat.S_ISREG(metadata.st_mode):
            raise PublicReleaseError("scan_input_missing")
        else:
            readable.append((rel, path, metadata))
    size_findings = _size_findings((rel, item.st_size) for rel, _path, item in readable)
    if size_findings:
        return tuple(sorted(set(findings).union(size_findings)))
    snapshots: dict[str, bytes] = {}
    for rel, path, metadata in readable:
        value = _read_scan_file(path, rel, metadata)
        if isinstance(value, Finding):
            findings.append(value)
        else:
            snapshots[rel] = value
    findings.extend(_scan_snapshots(snapshots, policy))
    return tuple(sorted(set(findings)))


def scan_path(
    path: Path,
    policy: Mapping[str, Any],
    *,
    label: str | None = None,
) -> tuple[Finding, ...]:
    report_label = label or path.name
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PublicReleaseError("scan_input_missing") from exc
    except OSError as exc:
        raise PublicReleaseError("scan_input_unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        return (Finding(report_label, "filesystem_path", "symlink_not_allowed"),)
    if stat.S_ISREG(metadata.st_mode):
        value = _read_scan_file(path, report_label, metadata)
        if isinstance(value, Finding):
            return (value,)
        return _scan_snapshots({report_label: value}, policy)
    if not stat.S_ISDIR(metadata.st_mode):
        raise PublicReleaseError("scan_input_unsupported")
    if not path.exists():
        raise PublicReleaseError("scan_input_missing")
    files = [
        item.relative_to(path).as_posix()
        for item in path.rglob("*")
        if item.is_symlink() or not item.is_dir()
    ]
    return scan_files(path, files, policy)


def export_clean_root(root: Path, target: Path, policy_path: Path, ledger_path: Path) -> int:
    if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
        raise PublicReleaseError("export_target_not_empty")
    commit = _git_commit(root)
    tree = _git_tree(root, commit)
    policy_rel, policy_blob = _committed_blob(root, tree, policy_path)
    ledger_rel, ledger_blob = _committed_blob(root, tree, ledger_path)
    control = {policy_rel: policy_blob, ledger_rel: ledger_blob}
    control_findings = _size_findings((rel, blob.size) for rel, blob in sorted(control.items()))
    if control_findings:
        raise PublicReleaseError("control_size_limit", control_findings)
    policy = _decode_json(_git_blob(root, policy_blob), policy_path.name)
    ledger = _decode_json(_git_blob(root, ledger_blob), ledger_path.name)
    files = select_public_files(tree, policy)
    validate_provenance(files, ledger)
    candidate_blobs = {**control, **{rel: tree[rel] for rel in files}}
    size_findings = _size_findings(
        (rel, blob.size) for rel, blob in sorted(candidate_blobs.items())
    )
    if size_findings:
        raise PublicReleaseError("source_size_limit", size_findings)
    snapshots = {rel: _git_blob(root, tree[rel]) for rel in files}
    findings = _scan_snapshots(snapshots, policy)
    if findings:
        raise PublicReleaseError("redaction_failed", findings)
    for rel, data in snapshots.items():
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    exported_findings = scan_files(target, files, policy)
    if exported_findings:
        raise PublicReleaseError("export_verification_failed", exported_findings)
    return len(files)


def _failure(exc: PublicReleaseError) -> str:
    value: dict[str, Any] = {"status": "failed", "reason_code": exc.reason_code}
    if exc.findings:
        value["findings"] = [asdict(item) for item in exc.findings]
    return json.dumps(value, sort_keys=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("export").add_argument("target", type=Path)
    scan = commands.add_parser("scan")
    scan.add_argument("paths", nargs="+", type=Path)
    scan.add_argument("--trusted-inputs", type=Path)
    args = parser.parse_args(argv)
    root = args.repo.resolve()
    policy_path = root / "distribution/public-allowlist.json"
    ledger_path = root / "distribution/provenance.json"
    try:
        if args.command == "export":
            count = export_clean_root(root, args.target, policy_path, ledger_path)
            print(json.dumps({"status": "ok", "files": count}, sort_keys=True))
            return 0
        policy = _load_json(policy_path)
        if args.trusted_inputs:
            inputs = _load_json(args.trusted_inputs)
            platform_hashes = {item["python"][4] for item in inputs["platforms"].values()}
            wheel_hashes = {row[3] for rows in inputs["wheels"].values() for row in rows}
            policy["trusted_archive_sha256"] = sorted(platform_hashes | wheel_hashes)
        scanned: list[Finding] = []
        for path in args.paths:
            try:
                label = path.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                label = path.name
            scanned.extend(scan_path(path, policy, label=label))
        findings = tuple(scanned)
        print(json.dumps([asdict(item) for item in sorted(set(findings))], sort_keys=True))
        return 1 if findings else 0
    except PublicReleaseError as exc:
        print(_failure(exc))
        return 2
    except OSError:
        print(json.dumps({"status": "failed", "reason_code": "release_io_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
